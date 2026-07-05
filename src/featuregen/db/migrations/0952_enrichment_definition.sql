-- src/featuregen/db/migrations/0952_enrichment_definition.sql
-- LLM definition-drafting cache: a drafted business description for a column that had NO declared
-- definition, keyed by the same content-hash as the concept cache. A declared definition is NEVER
-- overwritten (R3) — only blank ones are drafted. Cache-first (no re-LLM on unchanged columns).
CREATE TABLE IF NOT EXISTS enrichment_definition (
    content_hash text        PRIMARY KEY,
    definition   text        NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);
