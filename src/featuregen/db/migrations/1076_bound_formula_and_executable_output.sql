-- src/featuregen/db/migrations/1076_bound_formula_and_executable_output.sql
-- S5 (docs/superpowers/plans/2026-08-15-integration-consolidation.md): the executable output policy,
-- the bound formula revision, and the compilations that produced it.
--
-- THE ACCEPTANCE IS THE SCHEMA'S SHAPE. "A compiler version bump leaves the bound-formula hash
-- unchanged" is why `compiler_version` is NOT a column of `bound_formula_revision`: identity is
-- formula + bound inputs + environment + executable output, and the toolchain that performed the
-- compilation is provenance. Putting it in the revision would have meant a version bump minting a
-- second revision for a computation that did not change, invalidating every downstream pin.
--
-- SO WHERE DOES IT GO. `bound_formula_compilation` — one row per compile, many per revision. That
-- keeps BOTH facts: the same bound formula recompiled under a new compiler is ONE revision, and the
-- record that a second compiler produced it is not lost. Exactly the split 1074 made for inventory
-- observations, where several observations legitimately share one content identity.
--
-- DECLARED IS NOT EXECUTABLE, and the column names say so. `currency_code` holds a three-letter code
-- — what the number IS — and `conversion_policy_ref` separately holds the policy that produced it.
-- `FormulaOutputPolicyV2.currency` holds `'converted:<ref>'`, a DECLARATION, and lifting that string
-- into `currency_code` would make the answer to "what currency is this column in" a policy
-- reference. The CHECK below refuses that spelling outright rather than trusting the writer.
--
-- INTENT MISMATCHES ARE RECORDED, not just raised. A refusal that leaves no trace is a refusal
-- nobody can review, and "the author expected AED and the governed facts produced USD" is precisely
-- what a reviewer needs to see. `compared_fields` is stored WITH the mismatch because S5's whole
-- acceptance is about which fields were eligible for comparison — a mismatch report that does not
-- say what was in scope cannot be checked against the rule.
--
-- NOT APPLIED. This file is written, not run.

-- ── the executable output policy ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS executable_output_policy (
    output_hash          text PRIMARY KEY CHECK (btrim(output_hash) <> ''),

    physical_type        text NOT NULL CHECK (btrim(physical_type) <> ''),
    unit                 text NOT NULL,

    -- A CODE, never a declaration. Empty for a non-monetary column; three upper-case letters
    -- otherwise. The type refuses `'converted:...'` in memory and this refuses it in the database,
    -- because the two are the same rule and one of them being absent is how the other gets removed.
    currency_code        text NOT NULL CHECK (
                             currency_code = '' OR currency_code ~ '^[A-Z]{3}$'),

    -- The policy that PRODUCED that code, when one did. Separate from the code because "this column
    -- is in AED" and "this column was converted to AED by policy P" are different facts, and a
    -- consumer reconciling a total needs both.
    conversion_policy_ref text NOT NULL,

    output_additivity    text NOT NULL CHECK (
                             output_additivity IN ('additive', 'non_additive', 'semi_additive')),
    nullable             boolean NOT NULL,
    physical_type_policy text NOT NULL CHECK (btrim(physical_type_policy) <> ''),
    recorded_at          timestamptz NOT NULL DEFAULT now(),

    -- A conversion that produced no currency code converted to nothing nameable.
    CONSTRAINT executable_output_policy_conversion_names_a_currency
        CHECK (conversion_policy_ref = '' OR currency_code <> ''),
    -- The unit says it is money and nothing says which money.
    CONSTRAINT executable_output_policy_monetary_has_a_currency
        CHECK (unit <> 'monetary' OR currency_code <> '')
);

-- ── the bound formula revision ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bound_formula_revision (
    revision_id             text PRIMARY KEY CHECK (btrim(revision_id) <> ''),

    -- Identity, and nothing else. The same formula bound to different inputs, or to the same inputs
    -- in a different environment, is a different bound revision — those are the facts that decide
    -- what the computation reads. NO compiler version: see the header.
    formula_content_hash    text NOT NULL CHECK (btrim(formula_content_hash) <> ''),
    bound_input_set_hash    text NOT NULL CHECK (btrim(bound_input_set_hash) <> ''),
    environment_id          text NOT NULL CHECK (btrim(environment_id) <> ''),
    executable_output_hash  text NOT NULL REFERENCES executable_output_policy(output_hash),

    recorded_at             timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS bound_formula_revision_by_formula
    ON bound_formula_revision (formula_content_hash);
CREATE INDEX IF NOT EXISTS bound_formula_revision_by_environment
    ON bound_formula_revision (environment_id);

-- One row per COMPILE. Several per revision is the ordinary case and is the whole point.
CREATE TABLE IF NOT EXISTS bound_formula_compilation (
    compilation_id   text PRIMARY KEY CHECK (btrim(compilation_id) <> ''),
    revision_id      text NOT NULL REFERENCES bound_formula_revision(revision_id),
    compiler_version text NOT NULL CHECK (btrim(compiler_version) <> ''),
    compiled_at      text NOT NULL CHECK (btrim(compiled_at) <> ''),
    recorded_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS bound_formula_compilation_by_revision
    ON bound_formula_compilation (revision_id);

-- ── intent mismatches, kept ──────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS output_intent_mismatch (
    mismatch_id          text PRIMARY KEY CHECK (btrim(mismatch_id) <> ''),
    formula_content_hash text NOT NULL CHECK (btrim(formula_content_hash) <> ''),
    environment_id       text NOT NULL CHECK (btrim(environment_id) <> ''),

    code                 text NOT NULL CHECK (btrim(code) <> ''),
    field                text NOT NULL CHECK (btrim(field) <> ''),
    intended             text NOT NULL,
    resolved             text NOT NULL,
    detail               text NOT NULL,

    -- WHICH FIELDS WERE IN SCOPE. S5's acceptance is a claim about this set, so a stored mismatch
    -- that did not carry it could not be checked against the rule afterwards — and the rule is
    -- exactly the thing most likely to be quietly widened later.
    compared_fields      jsonb NOT NULL,
    recorded_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS output_intent_mismatch_by_formula
    ON output_intent_mismatch (formula_content_hash);

-- ── append-only ──────────────────────────────────────────────────────────────────────────────────
-- Everything here is either a content-addressed artifact or a record that something happened.
-- Neither kind has an in-place edit that means anything: a rewritten output policy would change
-- what a sealed revision claims to produce, and a rewritten mismatch would rewrite the history of a
-- disagreement someone is meant to review.
CREATE OR REPLACE FUNCTION s5_bound_formula_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS executable_output_policy_no_update ON executable_output_policy;
CREATE TRIGGER executable_output_policy_no_update
    BEFORE UPDATE OR DELETE ON executable_output_policy
    FOR EACH ROW EXECUTE FUNCTION s5_bound_formula_write_once();

DROP TRIGGER IF EXISTS bound_formula_revision_no_update ON bound_formula_revision;
CREATE TRIGGER bound_formula_revision_no_update
    BEFORE UPDATE OR DELETE ON bound_formula_revision
    FOR EACH ROW EXECUTE FUNCTION s5_bound_formula_write_once();

DROP TRIGGER IF EXISTS bound_formula_compilation_no_update ON bound_formula_compilation;
CREATE TRIGGER bound_formula_compilation_no_update
    BEFORE UPDATE OR DELETE ON bound_formula_compilation
    FOR EACH ROW EXECUTE FUNCTION s5_bound_formula_write_once();

DROP TRIGGER IF EXISTS output_intent_mismatch_no_update ON output_intent_mismatch;
CREATE TRIGGER output_intent_mismatch_no_update
    BEFORE UPDATE OR DELETE ON output_intent_mismatch
    FOR EACH ROW EXECUTE FUNCTION s5_bound_formula_write_once();
