-- src/featuregen/db/migrations/0973_connector_config.sql
-- OpenMetadata connector v1 (spec 2026-07-09): configured connections + the import audit trail.
--
-- connector_config: one row per configured connection. The bot token is NEVER stored: token_env
-- names the environment variable holding it (featuregen.privacy.kms exposes no envelope
-- seal/unseal API to reuse, so the env-reference deployment option from the spec is used).
CREATE TABLE IF NOT EXISTS connector_config (
    connector_id  text        PRIMARY KEY,
    name          text        NOT NULL UNIQUE,
    kind          text        NOT NULL DEFAULT 'openmetadata' CHECK (kind IN ('openmetadata')),
    base_url      text        NOT NULL,
    target_source text        NOT NULL,
    tag_map       jsonb       NOT NULL DEFAULT '{}'::jsonb,   -- OM tagFQN -> sensitivity ('' = ignore)
    filters       jsonb       NOT NULL DEFAULT '{}'::jsonb,   -- service|database|schema -> fnmatch pattern
    table_naming  text        NOT NULL DEFAULT 'table' CHECK (table_naming IN ('table', 'schema_table')),
    token_env     text        NOT NULL,                       -- env var REFERENCE, never the token
    created_by    text        NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- connector_import: one row per approved import (audit). connector_id/name are plain text on
-- purpose (no FK): deleting a connector configuration must never erase its import history.
-- approved_by is the human who clicked Approve (the ingest actor); vehicle names the connector.
CREATE TABLE IF NOT EXISTS connector_import (
    import_id      text        PRIMARY KEY,
    connector_id   text        NOT NULL,
    connector_name text        NOT NULL,
    target_source  text        NOT NULL,
    snapshot_hash  text        NOT NULL,
    approved_by    text        NOT NULL,
    vehicle        text        NOT NULL DEFAULT 'openmetadata-connector',
    result         jsonb       NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS connector_import_connector_idx
    ON connector_import (connector_id, created_at);
