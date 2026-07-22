-- src/featuregen/db/migrations/1006_catalog_metadata_snapshot.sql
-- Delivery C0 (immutable feature-generation metadata snapshot) Task 1: the tables only (the
-- builder + wiring land in later C0 tasks). C0 gives feature generation a REPRODUCIBLE, drift-aware
-- read of committed catalog state: a feature-generation workflow enters REPEATABLE READ, reads the
-- catalog through the authority-aware adapter, and persists the EXACT values/refs/decision-ids/
-- fact-ids/read-scope/versions it consumed as an immutable, hashed snapshot.
--   catalog_metadata_snapshot (header) and catalog_metadata_snapshot_item (rows) are IMMUTABLE
--   feature-generation REPLAY artifacts: a regulator can prove EXACTLY what catalog state a feature
--   contract was authored against. CLASSIFICATION: governance-retained (per the governance-retained
--   store, mirroring llm_call 0510); the write-once triggers make them physically immutable.
--   RUNTIME OWNER: the feature-generation workflow ONLY — ingestion NEVER creates a snapshot.
--   feature_generation_run is the durable generation-run manifest, created FIRST in the feature tx
--   so the snapshot header can FK it; it is NOT write-once (a run manifest may accrete context).
-- All statements idempotent: CREATE ... IF NOT EXISTS / CREATE OR REPLACE.

-- 1) The durable generation-run manifest — created FIRST in the feature-generation transaction so
--    the snapshot header can reference it. Minimal + forward-compatible; intent_id is NULLABLE with
--    no CHECK because a run may exist before a Gate-#1 choice is recorded against it.
CREATE TABLE IF NOT EXISTS feature_generation_run (
    generation_run_id text        PRIMARY KEY,
    intent_id         text        NULL,                    -- the Gate-#1 intent this run serves, if any
    actor             jsonb       NOT NULL,                -- identity_to_jsonb(...) of the authoring actor
    flags             jsonb       NOT NULL DEFAULT '{}',   -- feature-context flag + config captured
    created_at        timestamptz NOT NULL DEFAULT now()
);

-- 2) The immutable snapshot HEADER (write-once, governance-retained). content_hash seals the exact
--    catalog state consumed; read_scope_hash + isolation_level + projection_watermarks + the *_version
--    columns pin the read the feature contract was authored against.
CREATE TABLE IF NOT EXISTS catalog_metadata_snapshot (
    snapshot_id           text        PRIMARY KEY,
    generation_run_id     text        NOT NULL REFERENCES feature_generation_run(generation_run_id),
    read_scope_hash       text        NOT NULL,
    isolation_level       text        NOT NULL,               -- e.g. 'repeatable read'
    projection_watermarks jsonb       NOT NULL DEFAULT '{}',  -- per-projection replay watermarks
    policy_version        text        NULL,
    registry_version      text        NULL,
    config_version        text        NULL,
    content_hash          text        NOT NULL,               -- seals the exact snapshotted catalog state
    item_count            int         NOT NULL DEFAULT 0,     -- MF-1: the sealed item-SET size, stamped at
    --   build (a cheap cross-check for the future reload path). On the write-once header, so it is itself
    --   immutable once committed.
    created_at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS catalog_metadata_snapshot_run_idx
    ON catalog_metadata_snapshot (generation_run_id);

CREATE OR REPLACE FUNCTION catalog_metadata_snapshot_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'catalog_metadata_snapshot records are write-once: % not allowed on snapshot_id=%',
        TG_OP, COALESCE(OLD.snapshot_id, NEW.snapshot_id);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER catalog_metadata_snapshot_no_mutation
    BEFORE UPDATE OR DELETE ON catalog_metadata_snapshot
    FOR EACH ROW EXECUTE FUNCTION catalog_metadata_snapshot_write_once();

-- 3) The immutable snapshot ITEMS (write-once, governance-retained): one row per catalog value/ref
--    the feature-generation read consumed, with its authority attribution and the decision/fact
--    provenance ids. UNIQUE (snapshot_id, item_hash): an item appears at most once per snapshot.
CREATE TABLE IF NOT EXISTS catalog_metadata_snapshot_item (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- MF-1: the FK is DEFERRABLE INITIALLY DEFERRED so the builder can INSERT ALL ITEMS FIRST and the
    -- header LAST within one transaction (the header write is what SEALS the set — see the seal trigger
    -- below). The child-before-parent order is validated at COMMIT, so a truly orphan item (no header
    -- ever) still fails closed.
    snapshot_id       text   NOT NULL REFERENCES catalog_metadata_snapshot(snapshot_id)
                             DEFERRABLE INITIALLY DEFERRED,
    catalog_source    text   NOT NULL,
    graph_ref         text   NOT NULL,
    logical_ref       text   NULL,
    physical_ref      text   NULL,
    item_kind         text   NOT NULL,                     -- e.g. 'field' | 'table_fact'
    field_or_fact_type text  NOT NULL,
    value_json        jsonb  NOT NULL DEFAULT '{}',        -- the exact value consumed
    authority_json    jsonb  NOT NULL DEFAULT '{}',        -- the authority attribution consumed
    decision_event_id text   NULL,                         -- the governing decision, if any
    fact_key          text   NULL,
    fact_event_id     text   NULL,                         -- the governing fact event, if any
    item_hash         text   NOT NULL,
    CONSTRAINT catalog_metadata_snapshot_item_hash_unique UNIQUE (snapshot_id, item_hash)
);
CREATE INDEX IF NOT EXISTS catalog_metadata_snapshot_item_snapshot_idx
    ON catalog_metadata_snapshot_item (snapshot_id);

CREATE OR REPLACE FUNCTION catalog_metadata_snapshot_item_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'catalog_metadata_snapshot_item records are write-once: % not allowed on id=%',
        TG_OP, COALESCE(OLD.id, NEW.id);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER catalog_metadata_snapshot_item_no_mutation
    BEFORE UPDATE OR DELETE ON catalog_metadata_snapshot_item
    FOR EACH ROW EXECUTE FUNCTION catalog_metadata_snapshot_item_write_once();

-- 4) MF-1 — HARD SEAL of the item SET (not just each row). The write-once trigger above only blocks
--    UPDATE/DELETE; without this a NEW item_hash row could be INSERTed into an already-committed snapshot
--    forever, silently desyncing the header's content_hash (which nothing reverifies). This BEFORE INSERT
--    trigger seals the set the moment the header commits: an item INSERT is REFUSED once a header row
--    exists for its snapshot_id. During the build the header is not present yet (the builder inserts ALL
--    ITEMS FIRST, then the header LAST under the DEFERRABLE FK above), so items are allowed; the header
--    write is what seals. The immutable replay artifact is thus immutable at the SET level, not just the
--    row level.
CREATE OR REPLACE FUNCTION catalog_metadata_snapshot_item_seal() RETURNS trigger AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM catalog_metadata_snapshot WHERE snapshot_id = NEW.snapshot_id) THEN
        RAISE EXCEPTION 'catalog_metadata_snapshot_item set is sealed: a header already exists for '
            'snapshot_id=%, no new items may be inserted', NEW.snapshot_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER catalog_metadata_snapshot_item_seal_on_insert
    BEFORE INSERT ON catalog_metadata_snapshot_item
    FOR EACH ROW EXECUTE FUNCTION catalog_metadata_snapshot_item_seal();
