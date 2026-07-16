-- src/featuregen/db/migrations/0995_integration_import_run_link.sql
-- First-release hardening #3, connector tier: connector imports get an ingestion_run too, and the
-- dependency is REVERSED per the design (review #2) — the run is opened BEFORE the pull, so
-- integration_import points at the run, never the run at the import. A failed pull/ingest therefore
-- still has its run even though no integration_import row was ever written.
--   Nullable: additive on an audit table (rows written before this column predate the manifest).
--   No FK, deliberately: integration_import carries every reference as plain text so the history
--   can never be erased by deleting its referents (same rule as sync_id / integration_id).
ALTER TABLE integration_import ADD COLUMN IF NOT EXISTS ingestion_run_id text;
