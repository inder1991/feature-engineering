-- src/featuregen/db/migrations/0508_feature_contract_events.sql
-- SP-2 Phase 1 (design §2, §2.1 #1): additive, backward-compatible extension of SP-0's `events`
-- table to host SP-2's own `feature_contract` aggregate — the Feature Contract lifecycle, FOLDED
-- from its stream (fold_feature_contract_state, §4.6/§11), never a projection. Same recipe as
-- SP-1's 0504_overlay_events.sql. Idempotent: re-running is a clean no-op. Adds an allowed
-- aggregate value; rewrites no existing row.

-- 1. Widen the aggregate CHECK to admit 'feature_contract'. This DROP/ADD PRESERVES the
--    'overlay_fact' value SP-1's 0504 added (0508 sorts after 0504, so it rebuilds on top of it).
ALTER TABLE events DROP CONSTRAINT IF EXISTS events_aggregate_check;
ALTER TABLE events ADD CONSTRAINT events_aggregate_check
    CHECK (aggregate IN ('request','feature','run','overlay_fact','feature_contract'));

-- 2. Typed mirror column for the feature_contract aggregate (aggregate_id == feature_contract_id).
ALTER TABLE events ADD COLUMN IF NOT EXISTS feature_contract_id text;

-- 3. Rebuild the id-consistency invariant with an explicit feature_contract branch. The contract
--    lifecycle is per-run (one contract per run, aggregate_id == feature_contract_id == run_id), so
--    a feature_contract event ALWAYS carries its run_id mirror (NON-NULL — the correlation key the
--    get_contract read model reads, §13) and MAY carry request_id; feature_id is ALWAYS NULL (a
--    contract precedes any feature) — X3. The request/feature/run/overlay_fact branches are
--    preserved verbatim from SP-0 + SP-1's 0504.
ALTER TABLE events DROP CONSTRAINT IF EXISTS events_aggregate_id_consistent;
ALTER TABLE events ADD CONSTRAINT events_aggregate_id_consistent CHECK (
    (aggregate = 'request' AND aggregate_id = request_id) OR
    (aggregate = 'feature' AND aggregate_id = feature_id) OR
    (aggregate = 'run'     AND aggregate_id = run_id)     OR
    (aggregate = 'overlay_fact' AND aggregate_id = overlay_fact_id
        AND request_id IS NULL AND feature_id IS NULL AND run_id IS NULL) OR
    (aggregate = 'feature_contract' AND aggregate_id = feature_contract_id
        AND run_id IS NOT NULL AND feature_id IS NULL)
);

-- 4. Partial index for per-contract lookups (mirrors events_overlay_fact_idx).
CREATE INDEX IF NOT EXISTS events_feature_contract_idx
    ON events (feature_contract_id) WHERE feature_contract_id IS NOT NULL;
