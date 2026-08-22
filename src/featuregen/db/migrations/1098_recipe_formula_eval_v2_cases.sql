-- src/featuregen/db/migrations/1098_recipe_formula_eval_v2_cases.sql
-- The V2/V3 evaluation lane's own cases and attempts.
--
-- WHY NOT REUSE 1029's `recipe_formula_eval_case`. It is V1-shaped in one load-bearing way:
-- `recipe_id` is NOT NULL. A V2 clean case is keyed by an EXPECTATION REF — 295 of the 317 registry
-- recipes declare a ref that is not their own name — and a V2 adversarial case has no recipe at all,
-- because "this proposal is malformed" is a fact about the language and not about any recipe.
-- Writing a fixture name into a column called `recipe_id` would be precisely the kind of
-- mislabelling this program has spent its time undoing, so the V2 lane names its subject honestly:
-- what KIND of subject, and which one.
--
-- THE RUN TABLE IS SHARED, deliberately. `recipe_formula_eval_run` carries corpus, versions,
-- provider, model, controls, budgets and window — all of which mean the same thing in both lanes —
-- and 1097 gave it the `evaluation_contract_hash` that says which lane a row belongs to. Two run
-- tables would have made "which runs exist" a question with two answers.
--
-- BOTH TABLES ARE WRITE-ONCE, like their V1 counterparts. An evaluation whose cases or attempts
-- could be edited after the fact is not evidence; it is a claim.

CREATE TABLE IF NOT EXISTS recipe_formula_eval_case_v2 (
    eval_run_id      text NOT NULL REFERENCES recipe_formula_eval_run (eval_run_id),
    case_id          text NOT NULL,

    case_kind        text NOT NULL CHECK (case_kind IN ('clean', 'adversarial')),

    -- WHAT THIS CASE IS ABOUT, named by kind so a reader never has to guess which namespace the
    -- ref belongs to. 'expectation_ref' for a clean case; 'gold_fixture' for an adversarial one.
    subject_kind     text NOT NULL CHECK (subject_kind IN ('expectation_ref', 'gold_fixture')),
    subject_ref      text NOT NULL CHECK (btrim(subject_ref) <> ''),

    -- The reviewed bytes this case was frozen against — the proposal's canonical sha256 for a clean
    -- case, the fixture file's for an adversarial one (an invalid proposal has no canonical form).
    fixture_name     text NOT NULL CHECK (btrim(fixture_name) <> ''),
    fixture_pin      text NOT NULL CHECK (length(fixture_pin) = 64),

    expected_json    jsonb NOT NULL,
    expected_hash    text NOT NULL CHECK (length(expected_hash) = 64),

    created_at       timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (eval_run_id, case_id),

    -- A clean case names an expectation; an adversarial one names a fixture. Enforced here rather
    -- than only in code, because the pairing is what makes `subject_ref` unambiguous.
    CONSTRAINT recipe_formula_eval_case_v2_subject_matches_kind CHECK (
        (case_kind = 'clean'       AND subject_kind = 'expectation_ref')
     OR (case_kind = 'adversarial' AND subject_kind = 'gold_fixture')
    )
);

CREATE TABLE IF NOT EXISTS recipe_formula_eval_attempt_v2 (
    attempt_id            text PRIMARY KEY,
    eval_run_id           text NOT NULL,
    case_id               text NOT NULL,
    repeat_index          integer NOT NULL CHECK (repeat_index >= 0),

    authoring_run_id      text NULL REFERENCES formula_authoring_run (authoring_run_id),
    author_dispatch_refs  jsonb NOT NULL CHECK (jsonb_typeof(author_dispatch_refs) = 'array'),
    critic_dispatch_refs  jsonb NOT NULL CHECK (jsonb_typeof(critic_dispatch_refs) = 'array'),
    llm_call_refs         jsonb NOT NULL CHECK (jsonb_typeof(llm_call_refs) = 'array'),

    disposition           text NOT NULL CHECK (btrim(disposition) <> ''),

    -- ▲ RECORDED PER ATTEMPT, not derived at read time. Whether a run qualified as V3 evidence
    -- depends on the trace replaying and on every author call having been REQUESTED under the v3
    -- contract, and both are facts about the moment the attempt ran. Deriving it later would let a
    -- since-repaired trace make a historical attempt look better than it was.
    v3_evidence           boolean NOT NULL,
    v3_evidence_problems  jsonb NOT NULL CHECK (jsonb_typeof(v3_evidence_problems) = 'array'),

    outcome_json          jsonb NOT NULL,
    outcome_hash          text NOT NULL CHECK (length(outcome_hash) = 64),

    input_tokens          bigint NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens         bigint NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    cost_amount           numeric NOT NULL DEFAULT 0 CHECK (cost_amount >= 0),

    created_at            timestamptz NOT NULL DEFAULT now(),

    UNIQUE (eval_run_id, case_id, repeat_index),
    FOREIGN KEY (eval_run_id, case_id)
        REFERENCES recipe_formula_eval_case_v2 (eval_run_id, case_id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX IF NOT EXISTS recipe_formula_eval_attempt_v2_by_run
    ON recipe_formula_eval_attempt_v2 (eval_run_id, case_id);

-- ── write-once, both tables ─────────────────────────────────────────────────────────────────────
-- 1029 already defines `recipe_formula_eval_write_once()` and it says exactly the right thing, so
-- it is reused rather than duplicated under a new name: one function, one message, one meaning.
DROP TRIGGER IF EXISTS recipe_formula_eval_case_v2_write_once ON recipe_formula_eval_case_v2;
CREATE TRIGGER recipe_formula_eval_case_v2_write_once
    BEFORE UPDATE OR DELETE ON recipe_formula_eval_case_v2
    FOR EACH ROW EXECUTE FUNCTION recipe_formula_eval_write_once();

DROP TRIGGER IF EXISTS recipe_formula_eval_attempt_v2_write_once ON recipe_formula_eval_attempt_v2;
CREATE TRIGGER recipe_formula_eval_attempt_v2_write_once
    BEFORE UPDATE OR DELETE ON recipe_formula_eval_attempt_v2
    FOR EACH ROW EXECUTE FUNCTION recipe_formula_eval_write_once();
