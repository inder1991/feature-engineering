-- src/featuregen/db/migrations/1106_action_decision_revision.sql
-- THE DECISION, RECORDED — so the worker's second check has something to compare against.
--
-- WHY IT MUST PERSIST. The gate is checked twice: at request time, so a person gets an actionable
-- answer, and in the worker immediately before the act, which catches a queue bypass and catches the
-- state moving in between. An earlier design returned an evidence hash and stored nothing — so the
-- worker's "second check" had nothing to compare against and could only ask the question again. Two
-- independent answers to one question is not a drift check; it is a coin flipped twice.
--
-- ▲ AND DRIFT IS A REFUSAL, NOT A RE-DECISION. A worker that finds moved evidence and simply
-- re-evaluates will usually get "allowed" again and proceed — silently executing under an answer no
-- human was ever shown. The comparison is what makes that impossible; the row is what makes the
-- comparison possible.

CREATE TABLE IF NOT EXISTS action_decision_revision (
    decision_id              text PRIMARY KEY CHECK (btrim(decision_id) <> ''),

    action                   text NOT NULL CHECK (action IN (
                                 'AUTHOR_FORMULA', 'GENERATE_PREVIEW',
                                 'EXECUTE_SANDBOX', 'PUBLISH_SANDBOX',
                                 'MATERIALIZE_PRODUCTION', 'PUBLISH_PRODUCTION')),
    resource_identity_hash   text NOT NULL CHECK (btrim(resource_identity_hash) <> ''),

    -- ▲ NOT NULL. A decision with no authorization is a decision nobody was entitled to ask for,
    -- and an act carrying only such a decision is a queue bypass with paperwork.
    authorization_id         text NOT NULL,

    -- One verdict PER MEMBER, with the all-must-pass fold recorded rather than recomputed later.
    per_member_verdicts_json jsonb NOT NULL,
    allowed                  boolean NOT NULL,

    blockers_json            jsonb NOT NULL DEFAULT '[]'::jsonb,
    -- ▲ WARNINGS ARE PART OF THE RECORD. A warning computed and dropped is worse than no warning:
    -- it teaches the platform to believe it warned.
    warnings_json            jsonb NOT NULL DEFAULT '[]'::jsonb,

    policy_version           text NOT NULL CHECK (btrim(policy_version) <> ''),

    -- The revision pins this answer was computed from, and their hash. The worker recomputes the
    -- hash and COMPARES; it does not re-derive a fresh verdict and proceed on whatever it gets.
    evidence_pins_json       jsonb NOT NULL,
    evidence_hash            text NOT NULL CHECK (btrim(evidence_hash) <> ''),

    decided_at               timestamptz NOT NULL DEFAULT now(),

    -- ▲ THE COMPOSITE BINDING. A plain `authorization_id` reference would let a preview decision
    -- cite an authorization issued for production, or for another resource, and every key would
    -- still pass. Migration 1095 solved exactly this shape for generation by keying on the
    -- RELATIONSHIP rather than the id, and the replacement must be at least as strong.
    CONSTRAINT action_decision_revision_matches_its_authorization
        FOREIGN KEY (action, resource_identity_hash, authorization_id)
        REFERENCES action_authorization_revision (action, resource_identity_hash, authorization_id)
);

-- ▲ THE KEY AN ATTEMPT NEEDS to reference a decision AND its authorization together. Without it an
-- attempt could cite decision D — issued under authorization A — while itself naming authorization
-- B. That is the same defect one level further down, and it was in the first draft of the section
-- that exists to prevent it.
CREATE UNIQUE INDEX IF NOT EXISTS action_decision_revision_act_key
    ON action_decision_revision (action, resource_identity_hash, decision_id, authorization_id);

CREATE INDEX IF NOT EXISTS action_decision_revision_by_resource
    ON action_decision_revision (resource_identity_hash, decided_at DESC);

-- ▲ APPEND-ONLY. A decision that can be edited after the act is a record of what somebody wishes
-- had been decided.
CREATE OR REPLACE FUNCTION action_decision_revision_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'action_decision_revision is append-only: a decision records an answer that was '
                    'given, and one that can be rewritten was not an answer';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS action_decision_revision_no_change ON action_decision_revision;
CREATE TRIGGER action_decision_revision_no_change
    BEFORE UPDATE OR DELETE ON action_decision_revision
    FOR EACH ROW EXECUTE FUNCTION action_decision_revision_write_once();
