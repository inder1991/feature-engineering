-- src/featuregen/db/migrations/0974_integration_two_tier.sql
-- Supersede the flat connector_config (v1, migration 0973 — UNSHIPPED to main, only ran in the
-- demo DB) with the TWO-TIER model, grounded in OpenMetadata's own data model.
--
-- OpenMetadata's hierarchy is DatabaseService -> Database -> Schema -> Table -> Column, and a bot
-- JWT token authenticates to the WHOLE instance (it sees every DatabaseService). So the connection
-- (URL + token) is generic and belongs once per instance; the per-source binding is a sync that
-- maps one DatabaseService to one FeatureGen catalog source.
--
--   INTEGRATION  = one OpenMetadata instance (URL + sealed token ref + default tag map). Generic;
--                  sees all services. RBAC-managed. One row per instance.
--   SYNC         = one DatabaseService (optionally narrowed by database/schema) -> one catalog
--                  source, with a tag-map override + table naming. Many per integration.
--
-- The flat v1 tables are dropped: connector_config/connector_import never shipped to main, so there
-- is no production data to preserve. The import audit trail is recreated as integration_import.
DROP TABLE IF EXISTS connector_import;
DROP TABLE IF EXISTS connector_config;

-- Tier 1: one OpenMetadata instance. The bot token is NEVER stored — token_env names the
-- environment variable holding it (featuregen.privacy.kms exposes no envelope seal/unseal API to
-- reuse, so the env-reference deployment option is used). tag_map is the instance-wide DEFAULT
-- OM tagFQN -> sensitivity map; a sync may override individual tags.
CREATE TABLE IF NOT EXISTS integration (
    integration_id text        PRIMARY KEY,                    -- intg_<ulid>
    name           text        NOT NULL UNIQUE,
    base_url       text        NOT NULL,
    token_env      text        NOT NULL,                       -- env var REFERENCE, never the token
    tag_map        jsonb       NOT NULL DEFAULT '{}'::jsonb,   -- default OM tagFQN -> sensitivity ('' = ignore)
    created_by     text        NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now()
);

-- Tier 2: one DatabaseService (optionally narrowed to a database/schema) -> one catalog source.
-- ON DELETE CASCADE: removing an integration removes its syncs (documented — rotate/retire an
-- instance in one place). tag_map_override is NULL to inherit the integration's map wholesale, or a
-- partial map that WINS per tag over the integration default. The UNIQUE (integration_id,
-- service_name) enforces one sync per service within an instance (the default binding).
CREATE TABLE IF NOT EXISTS integration_sync (
    sync_id          text        PRIMARY KEY,                  -- sync_<ulid>
    integration_id   text        NOT NULL REFERENCES integration (integration_id) ON DELETE CASCADE,
    service_name     text        NOT NULL,                     -- OM DatabaseService name
    database_filter  text,                                     -- optional narrow (fnmatch on database)
    schema_filter    text,                                     -- optional narrow (fnmatch on schema)
    target_source    text        NOT NULL,                     -- FeatureGen catalog source
    tag_map_override jsonb,                                    -- NULL = inherit integration.tag_map
    table_naming     text        NOT NULL DEFAULT 'table' CHECK (table_naming IN ('table', 'schema_table')),
    created_by       text        NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    last_import_at   timestamptz,
    UNIQUE (integration_id, service_name)
);
CREATE INDEX IF NOT EXISTS integration_sync_integration_idx
    ON integration_sync (integration_id);

-- Import audit: one row per approved import. sync_id/integration_id are plain text on purpose (no
-- FK): deleting a sync or integration must never erase the history of what it imported. approved_by
-- is the human who clicked Approve (the ingest actor); vehicle names the connector.
CREATE TABLE IF NOT EXISTS integration_import (
    import_id      text        PRIMARY KEY,                    -- omimp_<ulid>
    sync_id        text        NOT NULL,
    integration_id text        NOT NULL,
    target_source  text        NOT NULL,
    snapshot_hash  text        NOT NULL,
    approved_by    text        NOT NULL,
    vehicle        text        NOT NULL DEFAULT 'openmetadata-connector',
    result         jsonb       NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS integration_import_sync_idx
    ON integration_import (sync_id, created_at);
