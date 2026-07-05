-- src/featuregen/db/migrations/0955_quarantine_row.sql
-- Review queue: the per-source quarantine of rows that couldn't ingest (raw row + reason). Replaced
-- wholesale on each successful ingest of the source (quarantine is re-evaluated every upload, not
-- sticky). Sensitive cell values would be redacted before display; the raw here is schema metadata.
CREATE TABLE IF NOT EXISTS quarantine_row (
    catalog_source text        NOT NULL,
    row_index      integer     NOT NULL,
    raw            jsonb       NOT NULL,
    reason         text        NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (catalog_source, row_index)
);
