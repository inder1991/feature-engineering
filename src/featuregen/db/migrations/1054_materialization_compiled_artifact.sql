-- src/featuregen/db/migrations/1054_materialization_compiled_artifact.sql
-- Phase G §3.6 — what a run INTENDED TO READ survives the run.
--
-- THE HOLE THIS CLOSES. A compile used to leave only HASHES behind: `group_plan_hash`,
-- `materialization_contract_hash` and `generated_project_hash` in `materialization_generation`, and
-- the same three in `GENERATED.lock` on disk. The bodies those hashes name had no writer at all.
-- Two consequences: a human holding a `group_plan_hash` could not answer "which features, which
-- columns, which spine?" without re-deriving the whole compilation from the catalog as it stood at
-- the time; and §3.3's reconciler — which resolves a mid-chain failure by READING EVIDENCE rather
-- than by resuming — had no compile-side evidence to read. This table is that evidence.
--
-- TWO KINDS OF RECORD, AND WHY THIS ONE IS THE FIRST.
--   * `materialization_request` (1053) is a MUTABLE COORDINATION record — who asked, is it still
--     being worked, has its lease expired. It is UPDATEd on every acceptance, renewal and terminal
--     link, and therefore deliberately carries NO append-only guard: a row that could not be updated
--     could not carry a lease at all.
--   * `materialization_compiled_artifact` describes WHAT WAS COMPILED. It is written once, inside
--     the same transaction as the generation row it hangs off, and there is no later moment at which
--     changing it could be right — a rewritten plan body would mean §9's gates are auditable against
--     a packing list nobody packed. So it takes 1034's guards, all three of them, and it takes them
--     from 1034's OWN function rather than a private copy (see below).
--
-- KEYED BY GENERATION, AND ONLY BY GENERATION. One generation compiles one plan under one contract
-- (`group_plan_revision` makes the same statement about the packing list, for the same reason), so
-- `generation_id` is the PRIMARY KEY and a second artifact row is a second answer to a question that
-- has one. The FK is real: an artifact naming a generation that does not exist would describe a
-- compilation the plane never recorded.
--
-- WHAT IS DELIBERATELY NOT HERE: the PREPARED PARAMETERS. Plan §3.6 names them alongside the plan
-- and the contract, and they do not belong on this table:
--   * they are RUN-scoped, not generation-scoped. `prepare_run` (runprep.py:831) takes `run_id` and
--     `business_dt` and returns parameters carrying both, plus the resolved input snapshots and the
--     `sandbox_execution_hash`. Two runs of one generation — a re-run, a second business date —
--     produce two different parameter sets, and a generation-keyed row can hold only one of them;
--   * a NULLABLE column left for a later writer could never be filled, because this table refuses
--     UPDATE. A column nobody can ever write is not a reservation, it is a permanent NULL.
-- G-2 owns `prepare_run` and will need a RUN-keyed table of its own; nothing here is in its way, and
-- nothing here has to be un-said first.
--
-- NUMBERING. 1054 was free across every ref when this landed
-- (`git log --all --diff-filter=A -- 'src/featuregen/db/migrations/1054*'` is empty). 1053 is Task
-- 1's `materialization_request`; 1055 stays RESERVED and UNUSED for G-3's active-revision pointer
-- (§3.5) and is not taken here.
--
-- Idempotent / re-runnable in the repo style (`apply_migrations` ledgers by filename, and the test
-- suite re-executes this exact SQL against a POPULATED table): CREATE TABLE IF NOT EXISTS +
-- CREATE OR REPLACE TRIGGER (PostgreSQL 14+, 1034's style) + a role-guarded REVOKE. Nothing here
-- drops, alters or deletes, so re-application cannot destroy a record.

CREATE TABLE IF NOT EXISTS materialization_compiled_artifact (
    generation_id            text PRIMARY KEY
                                 REFERENCES materialization_generation(generation_id),
    -- The §9 packing list, as the ONE canonicalization scheme sees it: the body stored is exactly
    -- the payload `materialize_hash` consumed, so it re-derives to the digest beside it. Opaque in
    -- CONTENT — reading a plan here would make this table a second definition of one — but pinned in
    -- SHAPE: an array or a bare scalar is a writer that stored a fragment, and a fragment hashes to
    -- a value nothing in the plane holds.
    group_plan               jsonb NOT NULL CHECK (jsonb_typeof(group_plan) = 'object'),
    -- Re-derived by the writer from the body above, never accepted from a caller. It duplicates
    -- `materialization_generation.group_plan_hash` ON PURPOSE: that column is what other records
    -- point at, and this one is what makes THIS row self-describing — a reader can check the body
    -- against the hash without joining, and the two are written in one transaction so they cannot
    -- disagree.
    group_plan_hash          text NOT NULL CHECK (btrim(group_plan_hash) <> ''),
    materialization_contract jsonb NOT NULL
                                 CHECK (jsonb_typeof(materialization_contract) = 'object'),
    contract_hash            text NOT NULL CHECK (btrim(contract_hash) <> ''),
    recorded_at              timestamptz NOT NULL DEFAULT now()
);

-- No secondary index: the only question this table answers is "what did generation X compile?", and
-- that is the primary key. An index on a hash would invite "which generation produced this plan?",
-- which `materialization_generation` already answers and which must have exactly one home.

-- ── the append-only guards ───────────────────────────────────────────────────────────────────────
-- 1034's function, NOT a copy of it. The rule it states ("the materialization control plane is
-- append-only … a record that can be rewritten proves nothing") is one rule; a private copy here
-- would be a second place for it to drift and a second message for an operator to meet. 1054 sorts
-- after 1034 in the lexical apply order, so the function exists by the time this runs — and a test
-- asserts the trigger on this table and the trigger on `materialization_generation` execute the very
-- same OID, so a copy introduced later fails rather than passing quietly.
CREATE OR REPLACE TRIGGER materialization_compiled_artifact_no_mutation
    BEFORE UPDATE OR DELETE ON materialization_compiled_artifact
    FOR EACH ROW EXECUTE FUNCTION materialization_control_plane_append_only();

-- A FOR EACH ROW trigger does NOT fire on TRUNCATE (1034's header records the measurement on this
-- repo's server). This is the only guard that does. Nothing references this table, so a bare
-- TRUNCATE is not short-circuited by the FK error 1034 warns about and genuinely reaches it.
CREATE OR REPLACE TRIGGER materialization_compiled_artifact_no_truncate
    BEFORE TRUNCATE ON materialization_compiled_artifact
    FOR EACH STATEMENT EXECUTE FUNCTION materialization_control_plane_append_only();

-- Defence in depth for the non-superuser production role, guarded by a role-exists check so it is a
-- clean no-op in the superuser test cluster where the role is absent (1034's pattern).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'featuregen_app') THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON materialization_compiled_artifact FROM featuregen_app;
    END IF;
END $$;
