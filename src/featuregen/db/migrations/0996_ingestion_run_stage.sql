-- src/featuregen/db/migrations/0996_ingestion_run_stage.sql
-- First-release hardening #22: per-stage ingestion status. One row per (run, stage, attempt) —
-- a CHILD table of the 0994 ingestion_run manifest, NOT a mutable JSON blob on the run (which
-- would lose concurrent updates; review #19). Stages are buffered in memory during ingest and
-- flushed alongside the run's terminalize, so a stage row always describes a run whose terminal
-- state committed with it — and the flush never touches any ingestion response body.
--   state is the design-#22 taxonomy, closed by CHECK so an application bug cannot invent a
--   vocabulary: disabled (feature flag off), not_applicable (e.g. glossary stages on a technical
--   upload), skipped_no_client (no LLM provider configured), not_run, running, waiting, retrying,
--   succeeded, partial (the stage caught PER-ITEM failures internally — an outer success is not
--   evidence all items succeeded), failed, deferred (e.g. brake-held), lagged (projection-lag
--   skip; re-runs on the next caught-up ingest), cancelled, audit_degraded.
--   attempt makes retries append-only: (ingestion_run_id, stage, attempt) is UNIQUE, so a
--   concurrent or repeated stage write adds attempt N+1 instead of clobbering attempt N.
--   reason_code is a short machine code (e.g. 'projection_lag'); detail is a small JSONB of
--   counts (asserted/quarantined/unresolved) — never row data, never secrets.
CREATE TABLE IF NOT EXISTS ingestion_run_stage (
    id               bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ingestion_run_id text        NOT NULL REFERENCES ingestion_run(id),
    stage            text        NOT NULL,
    attempt          int         NOT NULL DEFAULT 1,
    state            text        NOT NULL CHECK (state IN
        ('disabled', 'not_applicable', 'skipped_no_client', 'not_run', 'running', 'waiting',
         'retrying', 'succeeded', 'partial', 'failed', 'deferred', 'lagged', 'cancelled',
         'audit_degraded')),
    started_at       timestamptz NULL,
    completed_at     timestamptz NULL,
    reason_code      text        NULL,
    detail           jsonb       NULL,
    CONSTRAINT ingestion_run_stage_attempt_unique UNIQUE (ingestion_run_id, stage, attempt)
);
CREATE INDEX IF NOT EXISTS ingestion_run_stage_run_idx
    ON ingestion_run_stage (ingestion_run_id);
