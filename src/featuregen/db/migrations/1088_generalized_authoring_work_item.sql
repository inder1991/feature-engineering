-- src/featuregen/db/migrations/1088_generalized_authoring_work_item.sql
-- S2 (docs/superpowers/plans/2026-08-15-integration-consolidation.md): the GENERALIZED authoring
-- work item — a new table plus a compatibility reader, NOT an in-place backfill.
--
-- WHY NOT A BACKFILL, IN ONE LINE. `recipe_formula_shadow_work_item` (1023:120) is write-once by
-- trigger AND has UPDATE/DELETE revoked from the app role. Migration 1068 already hit this and
-- recorded the answer in its own header — "Backfilling would be impossible in any case: 1023's
-- write-once triggers refuse UPDATE" — so it added nullable columns rather than rewriting rows.
-- Generalizing the SHAPE cannot be done that way, because the legacy table's NOT NULL columns are
-- recipe-specific (`recipe_id`, `recipe_candidate_key`, `recipe_expectation_json`). A free-form or
-- user-defined authoring run has none of them and could not produce a legal legacy row.
--
-- SO: a new table for the general shape, and a READER that unions both. The legacy rows keep their
-- exact meaning and their write-once guarantee; nothing is migrated, rewritten or reinterpreted.
-- `authoring_work_item_compat` in `overlay/upload/authoring_work_item_store.py` is that reader, and
-- it labels every row with the `origin` it came from so a caller can never mistake a legacy
-- recipe-shaped item for a generalized one.
--
-- WHAT IS ACTUALLY GENERAL HERE. `origin` is the axis: `recipe` items are authored from a reviewed
-- blueprint, `llm_intent` and `user_definition` are not. The expectation is stored as opaque JSON
-- with its hash beside it because the three origins do not share a schema — and inventing a union
-- schema would mean every origin carrying the other two's empty fields.
--
-- MIGRATION NUMBER. 1073 in the plan carries three deliverables under one filename, and
-- `apply_migrations` ledgers by stem AND byte checksum. Each takes its own: 1084 (typed planning
-- request), 1087 (feature definition), 1088 (this).
--
-- NOT APPLIED. This file is written, not run.

CREATE TABLE IF NOT EXISTS authoring_work_item (
    work_item_id            text PRIMARY KEY CHECK (btrim(work_item_id) <> ''),
    idempotency_key         text NOT NULL UNIQUE CHECK (btrim(idempotency_key) <> ''),

    -- THE generalizing axis. Closed, because a work item whose origin nothing recognises cannot be
    -- authored by any path — and an open vocabulary would let one arrive and sit unprocessed.
    origin                  text NOT NULL
                                 CHECK (origin IN ('recipe', 'llm_intent', 'user_definition')),

    intent_id               text NOT NULL CHECK (btrim(intent_id) <> ''),
    considered_revision_id  text NOT NULL CHECK (btrim(considered_revision_id) <> ''),
    option_id               text NOT NULL CHECK (btrim(option_id) <> ''),

    -- Opaque per origin, with its hash beside it. The three origins do not share a schema, and a
    -- union schema would make every origin carry the other two's empty fields.
    expectation_json        jsonb NOT NULL,
    expectation_hash        text  NOT NULL CHECK (btrim(expectation_hash) <> ''),

    -- Present exactly when the origin authors from a REVIEWED blueprint. A recipe item without one
    -- cannot take the deterministic path, and a non-recipe item claiming one would be asserting a
    -- review that never happened — the CHECK below makes both unrepresentable.
    reviewed_blueprint_revision text,

    binding_plan_hash       text NOT NULL CHECK (btrim(binding_plan_hash) <> ''),
    frozen_configuration_hash text NOT NULL CHECK (btrim(frozen_configuration_hash) <> ''),
    created_at              timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT authoring_work_item_blueprint_matches_origin CHECK (
        (origin = 'recipe' AND reviewed_blueprint_revision IS NOT NULL)
        OR (origin <> 'recipe' AND reviewed_blueprint_revision IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS authoring_work_item_by_option
    ON authoring_work_item (considered_revision_id, option_id);

CREATE INDEX IF NOT EXISTS authoring_work_item_by_origin
    ON authoring_work_item (origin);

-- Write-once, matching the legacy table's guarantee rather than weakening it. A work item is what
-- an authoring run replays FROM; one that can be edited after the run makes the replay a record of
-- something that never happened.
CREATE OR REPLACE FUNCTION authoring_work_item_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'authoring_work_item is write-once (%)', OLD.work_item_id;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS authoring_work_item_no_update ON authoring_work_item;
CREATE TRIGGER authoring_work_item_no_update
    BEFORE UPDATE OR DELETE ON authoring_work_item
    FOR EACH ROW EXECUTE FUNCTION authoring_work_item_write_once();

-- The same role-guarded revoke 1023 and 1055 apply, so the app role cannot bypass the trigger.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'featuregen_app') THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON authoring_work_item FROM featuregen_app;
    END IF;
END $$;
