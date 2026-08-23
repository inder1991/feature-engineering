-- src/featuregen/db/migrations/1118_compiler_certification_programme.sql
-- §12 — the deterministic (compiler) certification programme's storage: a SIBLING programme,
-- never nullable columns on the LLM one. Verified: 1097's contract requires author/critic
-- provider hashes NOT NULL, and a compiler run has neither — a sibling contract table, not that
-- row with fabricated hashes. Numbered 1118: the run-spine peer holds 1115–1117 (§17's rule:
-- re-verify the ledger at write time; revision four's "1113/1114 here" collided with step 7).
--
-- ▲ A CASE IS ONE GOVERNED REVISION (§12.2): approved IR + frozen dataset + expected rows +
-- tolerances + runtime profile under ONE immutable hash reviewers approve TOGETHER — separate
-- approvals would double §21's arithmetic and permit an approved IR against a dataset nobody
-- approved: a certification of arithmetic against unknown inputs.

CREATE TABLE IF NOT EXISTS recipe_compiler_evaluation_contract (
    contract_hash               text PRIMARY KEY CHECK (btrim(contract_hash) <> ''),
    compiler_programme_version  integer NOT NULL,
    grammar_version             integer NOT NULL,
    producer_version            integer NOT NULL,
    canonicalization_version    integer NOT NULL,
    corpus_version              integer NOT NULL,
    corpus_content_hash         text NOT NULL CHECK (btrim(corpus_content_hash) <> ''),
    expectation_registry_hash   text NOT NULL CHECK (btrim(expectation_registry_hash) <> ''),
    -- NO provider hashes, structurally: the whole claim of this programme is that the
    -- deterministic lane spends nothing and answers to no provider.
    recorded_at                 timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS recipe_compiler_eval_case (
    case_revision_hash      text PRIMARY KEY CHECK (btrim(case_revision_hash) <> ''),
    expectation_ref         text NOT NULL CHECK (btrim(expectation_ref) <> ''),
    blueprint_revision      text NOT NULL CHECK (btrim(blueprint_revision) <> ''),
    blueprint_hash          text NOT NULL CHECK (btrim(blueprint_hash) <> ''),
    approved_ir_json        jsonb NOT NULL,
    approved_ir_hash        text NOT NULL CHECK (btrim(approved_ir_hash) <> ''),
    -- Content hash of the REVIEWED test data — synthetic by default; a masked extract is a
    -- governance exception carrying read-scope and data-use checks (§12.2).
    dataset_pin             text NOT NULL CHECK (btrim(dataset_pin) <> ''),
    expected_rows_json      jsonb NOT NULL,
    expected_rows_hash      text NOT NULL CHECK (btrim(expected_rows_hash) <> ''),
    -- ▲ CONSTRAINED EMPTY (R16): the grammar cannot express an approximate operation, so a
    -- tolerance names a property nothing can have. The column survives so a future grammar
    -- version changes a CHECK, not a schema shape.
    declared_tolerances_json jsonb NOT NULL DEFAULT '[]'::jsonb
        CONSTRAINT compiler_case_tolerances_empty CHECK (
            jsonb_array_length(declared_tolerances_json) = 0),
    -- §10.2's stack: renderer, runtimes, timezone, ANSI mode, decimal implementation — inside
    -- the ONE approved revision, because a certificate that does not name them certifies a run
    -- nobody can reproduce.
    runtime_profile_json    jsonb NOT NULL,
    approved_by             jsonb NOT NULL,
    approved_at             timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION recipe_compiler_eval_case_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'recipe_compiler_eval_case is append-only: reviewers approved ONE combined '
                    'revision, and a case that can be edited afterwards was not the one approved';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS recipe_compiler_eval_case_no_change ON recipe_compiler_eval_case;
CREATE TRIGGER recipe_compiler_eval_case_no_change
    BEFORE UPDATE OR DELETE ON recipe_compiler_eval_case
    FOR EACH ROW EXECUTE FUNCTION recipe_compiler_eval_case_write_once();

CREATE TABLE IF NOT EXISTS recipe_compiler_eval_attempt (
    attempt_id              text PRIMARY KEY CHECK (btrim(attempt_id) <> ''),
    case_revision_hash      text NOT NULL REFERENCES recipe_compiler_eval_case(case_revision_hash),
    contract_hash           text NOT NULL
        REFERENCES recipe_compiler_evaluation_contract(contract_hash),
    ir_comparison           text NOT NULL CHECK (ir_comparison IN (
        'MATCHED', 'DIFFERED', 'INPUT_IDENTITY_MOVED')),
    value_comparison        text NOT NULL CHECK (value_comparison IN (
        'MATCHED', 'DIFFERED', 'UNMEASURED')),
    first_difference_path   text,
    outcome                 text NOT NULL CHECK (outcome IN (
        'PASSED', 'FAILED_IR', 'IR_INPUT_IDENTITY_MOVED', 'FAILED_VALUES', 'UNMEASURED',
        'FAILED_DISPATCH_PRESENT')),
    unmeasured_reason       text,
    -- ▲ An ASSERTION, not a column that happens to be zero: a compiler attempt that recorded a
    -- provider dispatch is a failed attempt regardless of its comparisons.
    provider_dispatch_count integer NOT NULL DEFAULT 0,
    evaluated_at            timestamptz NOT NULL DEFAULT now(),

    -- The honest ladder, enforced: a PASS requires BOTH comparisons matched and zero dispatches;
    -- an UNMEASURED outcome names why.
    CONSTRAINT compiler_attempt_pass_is_both CHECK (
        outcome <> 'PASSED' OR (ir_comparison = 'MATCHED' AND value_comparison = 'MATCHED'
                                AND provider_dispatch_count = 0)),
    CONSTRAINT compiler_attempt_unmeasured_names_why CHECK (
        (outcome = 'UNMEASURED') = (unmeasured_reason IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS recipe_compiler_eval_attempt_by_case
    ON recipe_compiler_eval_attempt (case_revision_hash, evaluated_at);
