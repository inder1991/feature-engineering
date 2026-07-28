-- src/featuregen/db/migrations/1035_column_summary.sql
-- An AI-written, per-column summary — the answer to a source whose description column is filled by
-- BUCKET rather than by column.
--
-- CIB ships 111 columns with 47 distinct descriptions: 80% of rows repeat another row's sentence,
-- and one sentence covers 12 columns. `data_domain` is "Customer" for all 111. So the only field
-- that distinguishes one column from another is `term_name`, and two genuinely different attributes
-- (new-to-bank NOW vs new-to-bank NINE MONTHS AGO) are described identically.
--
-- The existing definition drafter cannot help: `_definition_targets` only targets columns with NO
-- declared definition ("a drafted one only ever fills a blank"), and these all have one. The
-- boilerplate OCCUPIES the field, so the system reads the question as answered and the one mechanism
-- that could have written something better never runs.
--
-- This is a SEPARATE field, deliberately, not a replacement:
--   * `definition` keeps whatever the source declared, at SOURCE authority. FTR's descriptions are
--     genuinely curated bank knowledge; overwriting them with a model's paraphrase would lose the
--     specifics AND downgrade a source-attested fact to an llm-proposed one.
--   * `ai_summary` is always llm/proposed, never load-bearing, human-correctable through the same
--     field-decision machinery as every other field.
--
-- It is indexed into search_doc because DISCOVERY is half the point: today a search for "new to
-- bank" finds nothing, even though two columns are precisely about it.
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS ai_summary text NULL;

-- Cache, mirroring enrichment_definition (0952) and carrying the cache_version dimension 0977 added
-- to its siblings from the start — a prompt/vocabulary change must invalidate cleanly rather than
-- serve stale summaries. Keyed on the content hash of the metadata the summary was written FROM, so
-- a re-upload of unchanged columns re-uses them and pays the provider once.
CREATE TABLE IF NOT EXISTS enrichment_summary (
    content_hash  text        NOT NULL,
    cache_version text        NOT NULL DEFAULT 'v1',
    summary       text        NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (content_hash, cache_version)
);
