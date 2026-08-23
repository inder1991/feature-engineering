-- src/featuregen/db/migrations/1116_run_lineage_considered_fks.sql
-- The considered-revision bridge, enforced (run-spine spec §9). Both columns were bare
-- `text NOT NULL CHECK` (1072:87, 1090:50) — a draft or selection could name a considered revision
-- that does not exist, and the run projection would silently drop it. Live-measured 2026-08-23:
-- 0 orphans in both tables (7 draft rows, 0 selection rows), so plain ADD CONSTRAINT validates.
-- Idempotent via the guarded DO block (ADD CONSTRAINT has no IF NOT EXISTS).
-- NOT APPLIED. This file is written, not run.
DO $$ BEGIN
    ALTER TABLE formula_draft
        ADD CONSTRAINT formula_draft_considered_fk
        FOREIGN KEY (considered_revision_id)
        REFERENCES contract_considered_revision (considered_revision_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE feature_selection_revision
        ADD CONSTRAINT feature_selection_revision_considered_fk
        FOREIGN KEY (considered_revision_id)
        REFERENCES contract_considered_revision (considered_revision_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
