-- src/featuregen/db/migrations/0950_enrichment_concept.sql
-- LLM enrichment cache: a column's classified concept keyed by a content-hash of its identity +
-- declared metadata (table|column|type|definition). A cache hit is reused with NO LLM call, which
-- is what makes re-ingest cheap and replay-safe. Advisory (concept only degrades search, never a
-- fact). Full ENRICHMENT_APPLIED event-sourcing + llm_call audit trace are later increments.
CREATE TABLE IF NOT EXISTS enrichment_concept (
    content_hash text        PRIMARY KEY,
    concept      text        NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);
