-- src/featuregen/db/migrations/1063_semantic_option_decision.sql
-- Remediation A1b (reservation 1063 per the 2026-08-13 remediation plan): the MINIMAL immutable
-- option-decision record — the frozen layer of the two-layer activation policy (A1).
--
-- One row per SERVED semantic option in a considered revision: the facts the user's card was
-- built from, frozen at generation. Activation (A2) assembles FrozenOptionFactsV1 from THIS row
-- plus the revision — never from FeatureIdea (which carries no review validity, no readiness,
-- no computation identity: validated finding 3) and never from live catalog reads (finding 1's
-- other half, CurrentActivationStateV1, is the caller's separate re-read).
--
-- Shape decisions:
--  * APPEND-ONLY (the 1034/1060/1062 guard idiom): the record is what the user SAW; a mutable
--    row could be edited into something they never approved.
--  * Keyed (considered_revision_id, option_id) UNIQUE — the exact card in the exact revision.
--    LIFE-03's wrong-row risk dies here: option detail and activation join by THIS key, plus
--    the exact observation_id link, never "newest row for the definition".
--  * Phase D's Task D1 ENRICHES this record (full verdicts, losing shortlist, plan envelope,
--    decision manifest) — additive columns/JSONB, same table, no second store.
CREATE TABLE IF NOT EXISTS semantic_option_decision (
    decision_id                     text        PRIMARY KEY,
    considered_revision_id          text        NOT NULL,
    option_id                       text        NOT NULL,
    generation_run_id               text        NOT NULL,
    source_definition_id            text        NOT NULL,
    generation_source               text        NOT NULL,   -- recipe | llm_intent | user_defined
    computation_kind                text        NOT NULL,
    planning_request_hash           text        NOT NULL,
    parameter_values                jsonb       NOT NULL DEFAULT '[]'::jsonb,
    binding_state                   text        NOT NULL,
    confirmation_required_roles     jsonb       NOT NULL DEFAULT '[]'::jsonb,
    readiness                       text        NOT NULL,
    review_current                  boolean     NOT NULL DEFAULT false,
    recipe_revision_hash            text        NOT NULL DEFAULT '',
    validation_status               text        NOT NULL DEFAULT 'NEEDS_EXTERNAL_VALIDATION',
    outstanding_requirement_codes   jsonb       NOT NULL DEFAULT '[]'::jsonb,
    has_reviewed_formula_expectation boolean    NOT NULL DEFAULT false,
    formula_expectation_revision    text        NOT NULL DEFAULT '',
    plan_envelope_present           boolean     NOT NULL DEFAULT false,
    dataset_story                   jsonb       NOT NULL DEFAULT '{}'::jsonb,
    policy_revision_pins            jsonb       NOT NULL DEFAULT '{}'::jsonb,
    metadata_snapshot_id            text,
    observation_id                  text,
    context_hash                    text        NOT NULL DEFAULT '',
    recorded_at                     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT semantic_option_decision_option_uq UNIQUE (considered_revision_id, option_id),
    CONSTRAINT semantic_option_decision_binding_chk CHECK (
        binding_state IN ('bound', 'ambiguous', 'missing', 'blocked'))
);

CREATE INDEX IF NOT EXISTS semantic_option_decision_run_idx
    ON semantic_option_decision (generation_run_id, recorded_at);

CREATE OR REPLACE FUNCTION semantic_option_decision_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'semantic_option_decision is append-only: %% is not allowed. The record is what the '
        'user saw at generation — a mutable row could be edited into something they never '
        'approved.';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER semantic_option_decision_no_mutation
    BEFORE UPDATE OR DELETE ON semantic_option_decision
    FOR EACH ROW EXECUTE FUNCTION semantic_option_decision_append_only();
CREATE OR REPLACE TRIGGER semantic_option_decision_no_truncate
    BEFORE TRUNCATE ON semantic_option_decision
    FOR EACH STATEMENT EXECUTE FUNCTION semantic_option_decision_append_only();
