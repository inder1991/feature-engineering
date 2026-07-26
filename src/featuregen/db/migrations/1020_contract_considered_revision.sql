-- Delivery A: immutable considered-set revisions.
--
-- `contract_considered` remains the mutable latest pointer used by regenerate/broaden workflows.
-- Authority-bearing recipe role bindings live only in this append-only revision, keyed by the exact
-- generation run shown to the user. The full canonical envelope is content-hashed by the application;
-- relational lineage columns are duplicated for indexed lookup and verified on every authority read.
CREATE TABLE IF NOT EXISTS contract_considered_revision (
    considered_revision_id          text        PRIMARY KEY,
    intent_id                       text        NOT NULL REFERENCES contract_intent (intent_id),
    generation_run_id               text        NOT NULL,
    metadata_snapshot_id            text        NULL,
    metadata_snapshot_content_hash  text        NULL,
    considered_json                 jsonb       NOT NULL,
    considered_content_hash         text        NOT NULL,
    canonicalization_version        text        NOT NULL,
    created_at                      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (intent_id, generation_run_id)
);

CREATE INDEX IF NOT EXISTS contract_considered_revision_run_idx
    ON contract_considered_revision (generation_run_id);

ALTER TABLE contract_considered
    ADD COLUMN IF NOT EXISTS considered_revision_id text NULL;
ALTER TABLE contract_considered
    ADD COLUMN IF NOT EXISTS considered_content_hash text NULL;

ALTER TABLE contract_gate1_choice
    ADD COLUMN IF NOT EXISTS considered_revision_id text NULL;
ALTER TABLE contract_gate1_choice
    ADD COLUMN IF NOT EXISTS considered_content_hash text NULL;

CREATE OR REPLACE FUNCTION contract_considered_revision_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'contract_considered_revision records are write-once: % not allowed on %',
        TG_OP, COALESCE(OLD.considered_revision_id, NEW.considered_revision_id);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER contract_considered_revision_no_mutation
    BEFORE UPDATE OR DELETE ON contract_considered_revision
    FOR EACH ROW EXECUTE FUNCTION contract_considered_revision_write_once();

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'featuregen_app') THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON contract_considered_revision FROM featuregen_app;
    END IF;
END $$;
