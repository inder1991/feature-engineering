-- src/featuregen/db/migrations/1001_dispatch_flag_provenance.sql
-- Phase 3C.1 run provenance: record the scoped-applicability + ranking flag state on each shadow
-- dispatch (compile + telemetry were already recorded). NULLABLE by design — existing rows and any
-- run whose route did not record them carry NULL = "unprovable", which the 3C.1 window selector
-- treats as a fail-closed exclusion. New rows write actual booleans. WORM: dispatch stays append-only
-- (write-once); this migration only ADDS columns and never relaxes the 0999 revoke posture
-- (0999_planner_shadow_store.sql revokes UPDATE/DELETE/TRUNCATE on planner_shadow_dispatch).
ALTER TABLE planner_shadow_dispatch ADD COLUMN IF NOT EXISTS scoped_applicability_flag boolean NULL;
ALTER TABLE planner_shadow_dispatch ADD COLUMN IF NOT EXISTS ranking_flag             boolean NULL;
