-- src/featuregen/db/migrations/0977_enrichment_cache_versioning.sql
-- Spec C6: the enrichment caches keyed on content_hash alone served stale values after the
-- vocabulary / prompt / schema changed. Add a cache_version dimension so a version bump invalidates
-- cleanly. Existing rows are stamped 'legacy' (a distinct version), so the first ingest under the
-- current fingerprint simply recomputes them. Backward-compatible; all idempotent.
ALTER TABLE enrichment_concept    ADD COLUMN IF NOT EXISTS cache_version text NOT NULL DEFAULT 'legacy';
ALTER TABLE enrichment_definition ADD COLUMN IF NOT EXISTS cache_version text NOT NULL DEFAULT 'legacy';
ALTER TABLE enrichment_domain     ADD COLUMN IF NOT EXISTS cache_version text NOT NULL DEFAULT 'legacy';

ALTER TABLE enrichment_concept    DROP CONSTRAINT IF EXISTS enrichment_concept_pkey;
ALTER TABLE enrichment_definition DROP CONSTRAINT IF EXISTS enrichment_definition_pkey;
ALTER TABLE enrichment_domain     DROP CONSTRAINT IF EXISTS enrichment_domain_pkey;

ALTER TABLE enrichment_concept    ADD PRIMARY KEY (content_hash, cache_version);
ALTER TABLE enrichment_definition ADD PRIMARY KEY (content_hash, cache_version);
ALTER TABLE enrichment_domain     ADD PRIMARY KEY (content_hash, cache_version);
