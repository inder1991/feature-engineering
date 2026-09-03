-- src/featuregen/db/migrations/1144_generation_authorization_rule_target.sql
-- A generation may be authorized for a RULE-BASED label (1142), not only a catalog column.
--
-- THE GAP THIS CLOSES. The target registry landed complete and DISCONNECTED: a person could author
-- and register a derived label and nothing in the platform could then be trained against it.
-- `target_consumer` was created by 1142 and no code has ever written a row to it, so §9's first
-- question — "this column is being retired, which labels break and who is training on them?" — had
-- no answer at all.
--
-- WHY BESIDE `target_ref` RATHER THAN REPLACING IT. A bare column target fits neither rule shape:
-- `state_change` needs from/to values and `event_window` needs a second table. "Superseding"
-- `target_ref` would therefore mean inventing a third passthrough shape purely to migrate existing
-- rows, and moving the signed-reading path and the confirm gate with it. The two columns sit side
-- by side and exactly one may be set.
--
-- THE IDENTITY IS DELIBERATELY UNTOUCHED. `generation_authorization.revision_id` is content-
-- addressed over `identity_payload`, and `verification_attempt` (1080) is keyed on it. The new key
-- is emitted only when a rule target is actually present, so every authorization already recorded
-- keeps the id it has and its verifications keep pointing at it. Two tests pin the pre-change
-- hashes.

ALTER TABLE generation_authorization
    ADD COLUMN IF NOT EXISTS target_definition_id text NULL
        REFERENCES target_definition(definition_id);

-- One statement, not two that can disagree. Restated for two KINDS of target: an exploration build
-- has neither, a prediction has exactly one. Allowing both would authorize one generation to
-- predict two different things.
ALTER TABLE generation_authorization
    DROP CONSTRAINT IF EXISTS generation_authorization_target_matches_mode;
ALTER TABLE generation_authorization
    ADD CONSTRAINT generation_authorization_target_matches_mode CHECK (
        (target_mode = 'exploration')
            = (target_ref IS NULL AND target_definition_id IS NULL)
        AND NOT (target_ref IS NOT NULL AND target_definition_id IS NOT NULL)
    );

CREATE INDEX IF NOT EXISTS generation_authorization_target_definition_idx
    ON generation_authorization (target_definition_id)
    WHERE target_definition_id IS NOT NULL;

-- The intake side. `contract_intent.target_ref` (0965) persists the prediction target server-side
-- so draft/confirm never trust a client-supplied one; a rule-based label needs the same treatment
-- and the same mutual exclusion.
ALTER TABLE contract_intent
    ADD COLUMN IF NOT EXISTS target_definition_id text NULL
        REFERENCES target_definition(definition_id);

ALTER TABLE contract_intent
    DROP CONSTRAINT IF EXISTS contract_intent_one_kind_of_target;
ALTER TABLE contract_intent
    ADD CONSTRAINT contract_intent_one_kind_of_target CHECK (
        NOT (target_ref IS NOT NULL AND target_definition_id IS NOT NULL)
    );
