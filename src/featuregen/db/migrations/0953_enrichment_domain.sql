-- src/featuregen/db/migrations/0953_enrichment_domain.sql
-- LLM domain classification: a table's business domain (e.g. Deposits, Payments), classified per
-- table from its name + column names, keyed by a table content-hash. Written onto every node of the
-- table (table + its columns) and folded into search_doc. Advisory; cache-first.
CREATE TABLE IF NOT EXISTS enrichment_domain (
    content_hash text        PRIMARY KEY,
    domain       text        NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS domain text NULL;
