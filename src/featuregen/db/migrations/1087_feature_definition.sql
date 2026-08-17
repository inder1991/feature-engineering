-- src/featuregen/db/migrations/1087_feature_definition.sql
-- S2 (docs/superpowers/plans/2026-08-15-integration-consolidation.md): `FeatureDefinitionV1`,
-- created or resolved at AUTHORING and linked to the selection that asked for it.
--
-- WHY IT IS NOT PART OF THE SELECTION. C-B2 froze `FeatureSelectionRevisionV1` so that it is
-- constructible with NO definition, and its test asserts no field name contains "definition". That
-- is the point: a person chooses a feature before the system has decided what to call it, and
-- requiring a definition at selection time would invert the order in which those two things
-- actually happen. The link lives here, append-only, and is created when authoring resolves a name.
--
-- CREATED OR RESOLVED. Two selections that author the same feature share ONE definition — the
-- definition is identified by its content, so "create if absent" and "resolve if present" are the
-- same operation and the caller does not have to know which happened.
--
-- MIGRATION NUMBER. The plan reserves 1073 for S2 and lists THREE deliverables against that one
-- filename. `apply_migrations` ledgers by filename stem AND byte checksum (db/migrations.py:310-318)
-- and raises on drift, so whichever deliverable writes 1073 first owns it and the others cannot
-- edit the file once it has been applied anywhere. Per the product owner's direction each
-- deliverable takes its own filename: the typed planning request is 1084, this is 1087, and the
-- generalized work item is 1088.
--
-- NOT APPLIED. This file is written, not run.

CREATE TABLE IF NOT EXISTS feature_definition (
    definition_id  text PRIMARY KEY CHECK (btrim(definition_id) <> ''),

    -- The published column name, already folded through `hive_identifier` by the writer. Stored
    -- folded rather than raw because this is the name a table actually carries; keeping the raw
    -- spelling here would invite a second normalization at read time.
    feature_name   text NOT NULL CHECK (feature_name ~ '^[a-z][a-z0-9_]{0,127}$'),

    -- What the definition IS, content-addressed. Two selections that author the same feature
    -- resolve to one row through this.
    content_hash   text NOT NULL UNIQUE CHECK (btrim(content_hash) <> ''),

    entity         text NOT NULL CHECK (btrim(entity) <> ''),
    grain_keys     jsonb NOT NULL,
    recorded_at    timestamptz NOT NULL DEFAULT now()
);

-- One feature NAME per entity. Two definitions sharing a name at one grain would publish to one
-- column, which is the collision `hive_identifier`'s own docstring exists to prevent.
CREATE UNIQUE INDEX IF NOT EXISTS feature_definition_name_per_entity
    ON feature_definition (entity, feature_name);

-- ── the append-only link ─────────────────────────────────────────────────────────────────────────
-- A selection resolves to a definition ONCE. Re-authoring the same selection to a different
-- definition would mean the feature a person chose became a different feature without anybody
-- recording the change.
CREATE TABLE IF NOT EXISTS feature_selection_definition_link (
    selection_revision_id text PRIMARY KEY
                              REFERENCES feature_selection_revision(revision_id),
    definition_id         text NOT NULL REFERENCES feature_definition(definition_id),
    linked_at             timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS feature_selection_definition_link_by_definition
    ON feature_selection_definition_link (definition_id);

CREATE OR REPLACE FUNCTION feature_definition_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS feature_definition_no_update ON feature_definition;
CREATE TRIGGER feature_definition_no_update
    BEFORE UPDATE OR DELETE ON feature_definition
    FOR EACH ROW EXECUTE FUNCTION feature_definition_write_once();

DROP TRIGGER IF EXISTS feature_selection_definition_link_no_update
    ON feature_selection_definition_link;
CREATE TRIGGER feature_selection_definition_link_no_update
    BEFORE UPDATE OR DELETE ON feature_selection_definition_link
    FOR EACH ROW EXECUTE FUNCTION feature_definition_write_once();
