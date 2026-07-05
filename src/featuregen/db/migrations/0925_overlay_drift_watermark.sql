-- src/featuregen/db/migrations/0925_overlay_drift_watermark.sql
-- SP-1.5 Task 4: drift-scan watermark. The "last successful catalog-drift scan completed at" per
-- catalog_source, written ATOMICALLY at the end of a drift run (same transaction as the snapshot
-- advance + dependent-staling), so a crash mid-run leaves NO watermark advance and the drift is
-- simply re-detected next run (never laundered). Read by Task 5's read-time drift-freshness guard:
-- resolve_fact fails closed when `now - last_completed_at > drift_freshness_sla` (or no watermark).
-- Idempotent. (A chunked, resumable overlay_drift_run for very large catalogs is a deferred scale
-- follow-up — the atomic single-transaction run is crash-safe without it.)
CREATE TABLE IF NOT EXISTS overlay_drift_watermark (
    catalog_source    text        PRIMARY KEY,
    last_completed_at timestamptz NOT NULL,
    last_run_id       text        NOT NULL
);
