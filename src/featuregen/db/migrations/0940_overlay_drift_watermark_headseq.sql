-- src/featuregen/db/migrations/0940_overlay_drift_watermark_headseq.sql
-- SP-1.5 review #2 (projection-lag fail-open): the drift watermark now records the global_seq at
-- scan completion (head_seq). detect_catalog_changes appends OVERLAY_FACT_STALED events AND advances
-- the watermark in one transaction, but the overlay projection applies those STALEs in a SEPARATE,
-- lagging stage — so between the drift commit and the projection catching up, a just-drifted fact is
-- still VERIFIED in overlay_fact_state and its watermark is fresh, and reads would SERVE it. Recording
-- head_seq lets resolve_fact fail closed until the overlay projection checkpoint has caught up to the
-- drift (checkpoint_seq >= head_seq), closing the window. Idempotent; default 0 (pre-existing rows
-- are treated as "already caught up" until the next scan stamps a real head_seq).
ALTER TABLE overlay_drift_watermark ADD COLUMN IF NOT EXISTS head_seq bigint NOT NULL DEFAULT 0;
