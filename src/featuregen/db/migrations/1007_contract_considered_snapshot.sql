-- src/featuregen/db/migrations/1007_contract_considered_snapshot.sql
-- Delivery C0 Task 5 — reference the immutable feature-generation metadata snapshot (migration 1006)
-- from the considered set. When a considered set is built on the feature-generation connection
-- (REPEATABLE READ, C0-T2), the builder mints a generation run, snapshots the in-scope catalog state
-- (catalog_metadata_snapshot / _item, C0-T3), and records the lineage HERE so /contract/draft and
-- /contract/confirm reload the SERVER snapshot the set was authored against — never a client-supplied
-- id. ADDITIVE + NULLABLE: pre-C0 rows and any non-feature-gen (READ COMMITTED) caller that takes no
-- snapshot simply leave these NULL (no CHECK). Plain text refs (NOT foreign keys) — mirrors how the
-- codebase keeps historical graph/run refs as strings, and avoids any insert-ordering hazard against
-- feature_generation_run / catalog_metadata_snapshot (whose own FKs already chain run -> snapshot).
-- Idempotent (ADD COLUMN IF NOT EXISTS).
ALTER TABLE contract_considered ADD COLUMN IF NOT EXISTS generation_run_id     text;
ALTER TABLE contract_considered ADD COLUMN IF NOT EXISTS snapshot_id           text;
ALTER TABLE contract_considered ADD COLUMN IF NOT EXISTS snapshot_content_hash text;
