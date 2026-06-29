-- src/featuregen/db/migrations/0504_overlay_events.sql
-- SP-1 Phase 1 (design §2.1): additive, backward-compatible extension of SP-0's `events`
-- table to host the `overlay_fact` aggregate. Idempotent: re-running is a clean no-op.

-- 1. Widen the aggregate CHECK to admit 'overlay_fact'. The original inline CHECK on the
--    column is auto-named events_aggregate_check.
ALTER TABLE events DROP CONSTRAINT IF EXISTS events_aggregate_check;
ALTER TABLE events ADD CONSTRAINT events_aggregate_check
    CHECK (aggregate IN ('request','feature','run','overlay_fact'));

-- 2. Typed mirror column for overlay facts (aggregate_id == overlay_fact_id == fact_key).
ALTER TABLE events ADD COLUMN IF NOT EXISTS overlay_fact_id text;

-- 3. Recreate the id-consistency invariant with an explicit overlay branch: for overlay
--    facts the canonical key is overlay_fact_id and the run/feature/request mirrors are NULL.
ALTER TABLE events DROP CONSTRAINT IF EXISTS events_aggregate_id_consistent;
ALTER TABLE events ADD CONSTRAINT events_aggregate_id_consistent CHECK (
    (aggregate = 'request' AND aggregate_id = request_id) OR
    (aggregate = 'feature' AND aggregate_id = feature_id) OR
    (aggregate = 'run'     AND aggregate_id = run_id)     OR
    (aggregate = 'overlay_fact' AND aggregate_id = overlay_fact_id
        AND request_id IS NULL AND feature_id IS NULL AND run_id IS NULL)
);

-- 4. Partial index for per-fact lookups (mirrors events_run_idx / events_feature_idx).
CREATE INDEX IF NOT EXISTS events_overlay_fact_idx
    ON events (overlay_fact_id) WHERE overlay_fact_id IS NOT NULL;
