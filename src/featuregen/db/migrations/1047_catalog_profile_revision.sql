-- src/featuregen/db/migrations/1047_catalog_profile_revision.sql
-- Release-A profile Task 2 (reservation: verified-interfaces doc D7 — 1047 belongs to this task).
--
-- 1) Catalog narrative: genuinely NEW state (correction 2 — no object-level evidence key or
--    current-pointer source exists for a catalog's own prose). Immutable revisions + a CAS current
--    pointer (`pointer_version`, the bridge_store pattern). The revision id is DETERMINISTIC from
--    canonical content ('cpr_' + materialize_hash of values + the D2 producer/strength/lifecycle
--    axes — the CHECK-pinned 'pbr_' precedent, migration 1036), so a byte-identical re-authoring
--    resolves to the SAME revision id and can never churn a dataset_profile_hash (D12.10), while
--    provenance (producer_ref / ingestion_run_id / created_at) stays outside identity.
--    Text/list bounds are enforced in code BEFORE persistence (catalog_profiles.py, the
--    field_correction._MAX_LEN precedent); the CHECKs here are the belt-and-braces backstop.
--
-- 2) Table-node compat projections for the two NEW operational profile classifications
--    (authority_role / temporal_storage_model): DISPLAY columns + their *_decision_id links,
--    following the 0984/1031 projected-field pattern — rebuildable, never authoritative
--    (operational reads go through the decision log / is_feature_eligible, never the flat column).
--    These live here because migration 1047 is the ONLY number this task owns (D7).

CREATE TABLE IF NOT EXISTS catalog_profile_revision (
    revision_id       text        PRIMARY KEY
        CHECK (revision_id ~ '^cpr_[0-9a-f]{64}$'),
    catalog_source    text        NOT NULL,
    display_name      text        NULL CHECK (display_name IS NULL OR char_length(display_name) <= 200),
    description       text        NULL CHECK (description IS NULL OR char_length(description) <= 4000),
    business_context  text        NULL CHECK (business_context IS NULL OR char_length(business_context) <= 4000),
    -- jsonb array of bounded domain strings; shape re-validated in code before every insert.
    business_domains  jsonb       NOT NULL DEFAULT '[]'
        CHECK (jsonb_typeof(business_domains) = 'array'),
    producer          text        NOT NULL,
    strength          text        NOT NULL,
    lifecycle         text        NOT NULL,
    -- Persistence PROVENANCE, deliberately outside content identity (§6.2).
    producer_ref      text        NOT NULL,
    ingestion_run_id  text        NULL,
    content_hash      text        NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS catalog_profile_revision_source_idx
    ON catalog_profile_revision(catalog_source);

CREATE TABLE IF NOT EXISTS catalog_profile_current (
    catalog_source    text        PRIMARY KEY,
    revision_id       text        NOT NULL REFERENCES catalog_profile_revision(revision_id),
    pointer_version   integer     NOT NULL CHECK (pointer_version >= 1),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

-- Table-node display projections (rebuildable; the resolver + the Task-0.6 unconditional
-- table_display_reprojection stage re-derive them from evidence on every ingest).
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS authority_role                     text NULL;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS temporal_storage_model             text NULL;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS authority_role_decision_id         text NULL;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS temporal_storage_model_decision_id text NULL;

ALTER TABLE graph_node DROP CONSTRAINT IF EXISTS graph_node_authority_role_check;
ALTER TABLE graph_node ADD CONSTRAINT graph_node_authority_role_check
    CHECK (authority_role IS NULL OR authority_role IN
           ('system_of_record', 'mastered_view', 'authoritative_replica',
            'non_authoritative_replica', 'derived', 'external_reference', 'unknown'));

ALTER TABLE graph_node DROP CONSTRAINT IF EXISTS graph_node_temporal_storage_model_check;
ALTER TABLE graph_node ADD CONSTRAINT graph_node_temporal_storage_model_check
    CHECK (temporal_storage_model IS NULL OR temporal_storage_model IN
           ('current_only', 'scd1', 'scd2', 'snapshot', 'event_log', 'unknown'));
