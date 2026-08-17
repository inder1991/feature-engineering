-- src/featuregen/db/migrations/1079_execution_proof_and_capability.sql
-- S8 (docs/superpowers/plans/2026-08-15-integration-consolidation.md): the development gold
-- EXECUTION PROOF, the mutations it survived, and the two SEPARATELY RECORDED capability facts.
--
-- "SUPPORTED" IS TWO FACTS, NEVER ONE WORD (invariant 11, §0.4). `renderer_dispatchable` is a
-- property of THIS BUILD — the renderer either has a branch for the operator or it does not, and
-- `engine_capability.py` derives it from the renderer's own self-description. `execution_proved` is
-- a property of a RUN — generated code executed against reviewed gold. They are stored in two
-- columns because collapsing them into one "supported" flag loses which half is missing, and the
-- two halves have completely different remedies: write a renderer branch, or run the proof.
--
-- CHANGED BYTES INVALIDATE A PROOF WITH NO VERSION BUMP. `generated_project_hash` is part of the
-- proof's own content hash (C-D5), so a proof is looked up BY the bytes it was taken over. Nothing
-- here lets a proof be "still valid" for different bytes — that is what a version field would
-- permit, and a version bump is a claim someone remembered to make.
--
-- THE MUTATION SET IS RECORDED PER MUTATION, not only as a version integer. The integer says WHICH
-- set ran; these rows say WHAT it caught. A proof that recorded only the version could not answer
-- "did the missing-status-filter mutation actually fail?" after the fact — and a mutation silently
-- passing is precisely how a gold proof becomes ceremonial.
--
-- NO S9 CHECK-SET COLUMN, deliberately. Sandbox verification does not exist at S8 and is a
-- separate concept (§0.4: "never interchangeable"). A nullable column for it would get filled in
-- eventually, by someone who assumed it meant the same thing.
--
-- NOT APPLIED. This file is written, not run.

CREATE TABLE IF NOT EXISTS operator_execution_proof (
    proof_hash              text PRIMARY KEY CHECK (btrim(proof_hash) <> ''),

    -- Every pin the proof is ABOUT. A proof with a missing pin is a proof about an unknown
    -- configuration, which is not a proof — the type refuses construction, and so does this.
    signature               text NOT NULL CHECK (btrim(signature) <> ''),
    signature_version       integer NOT NULL CHECK (signature_version >= 1),
    compiler_version        text NOT NULL CHECK (btrim(compiler_version) <> ''),
    renderer_version        text NOT NULL CHECK (btrim(renderer_version) <> ''),
    physical_type_policy    text NOT NULL CHECK (btrim(physical_type_policy) <> ''),
    topology_version        integer NOT NULL CHECK (topology_version >= 1),
    gold_corpus_hash        text NOT NULL CHECK (btrim(gold_corpus_hash) <> ''),

    -- THE BYTES. Part of `proof_hash`, so different bytes are a different proof rather than the
    -- same proof under new circumstances.
    generated_project_hash  text NOT NULL CHECK (btrim(generated_project_hash) <> ''),

    mutation_set_version    integer NOT NULL CHECK (mutation_set_version >= 1),

    -- All eight runtimes. The artifact runs on all of them, and an unpinned one is a runtime the
    -- proof does not cover.
    engine_versions         jsonb NOT NULL CHECK (jsonb_array_length(engine_versions) = 8),

    recorded_at             timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS operator_execution_proof_by_project
    ON operator_execution_proof (generated_project_hash);

-- WHAT THE MUTATION SET CAUGHT, one row per mutation. `detected` is NOT NULL and there is no
-- "not run" value: a mutation that was not run is an absent row, and an absent row is what the
-- completeness check below counts. Recording "not run" as a value would let a proof carry a
-- mutation nobody exercised and still look complete.
CREATE TABLE IF NOT EXISTS operator_proof_mutation (
    proof_hash    text NOT NULL REFERENCES operator_execution_proof(proof_hash),
    mutation_name text NOT NULL CHECK (btrim(mutation_name) <> ''),
    detected      boolean NOT NULL,
    detail        text NOT NULL,

    PRIMARY KEY (proof_hash, mutation_name)
);

CREATE INDEX IF NOT EXISTS operator_proof_mutation_undetected
    ON operator_proof_mutation (proof_hash) WHERE NOT detected;

-- ── the two capability facts, side by side and never merged ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS engine_operator_capability (
    engine_id             text NOT NULL CHECK (btrim(engine_id) <> ''),
    operator_kind         text NOT NULL CHECK (btrim(operator_kind) <> ''),

    -- Fact one: a property of THIS BUILD. Derived from the renderer, never typed out.
    renderer_dispatchable boolean NOT NULL,

    -- Fact two: a property of a RUN. NULL means "no proof has been taken", which is different from
    -- `false` ("a proof ran and the operator did not execute correctly") — and a caller that cannot
    -- tell those apart would report an unproved operator as a broken one.
    execution_proof_hash  text REFERENCES operator_execution_proof(proof_hash),

    recorded_at           timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (engine_id, operator_kind)
);

-- ── append-only where it must be ─────────────────────────────────────────────────────────────────
-- A proof and its mutation results are what a capability claim rests on. The capability row is the
-- one mutable thing here: a renderer branch added in a later build legitimately changes
-- `renderer_dispatchable`, and a later proof legitimately moves `execution_proof_hash` — but only
-- ever to a proof that exists, which the foreign key enforces.
CREATE OR REPLACE FUNCTION s8_proof_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS operator_execution_proof_no_update ON operator_execution_proof;
CREATE TRIGGER operator_execution_proof_no_update
    BEFORE UPDATE OR DELETE ON operator_execution_proof
    FOR EACH ROW EXECUTE FUNCTION s8_proof_write_once();

DROP TRIGGER IF EXISTS operator_proof_mutation_no_update ON operator_proof_mutation;
CREATE TRIGGER operator_proof_mutation_no_update
    BEFORE UPDATE OR DELETE ON operator_proof_mutation
    FOR EACH ROW EXECUTE FUNCTION s8_proof_write_once();
