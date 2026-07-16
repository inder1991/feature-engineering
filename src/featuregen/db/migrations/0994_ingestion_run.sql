-- src/featuregen/db/migrations/0994_ingestion_run.sql
-- First-release hardening #3: the durable ingestion-run manifest. Every ingestion attempt — upload
-- or (later) connector; ingested, held, rejected, parse-failed, or crashed — leaves a queryable
-- record of WHO ingested WHAT, WHEN, under WHAT settings, with WHAT outcome. A READ-MODEL, not
-- event-sourced: the run row is opened in_progress on an independent committing connection (so it
-- survives the request transaction rolling back) and terminalized either atomically with the
-- ingest transaction (ingested/held/rejected) or durably on its own connection (failure paths).
--   file_sha256 is NULLABLE: a rejected oversized/unreadable file is never fully hashed. The
--   checksum supports CORRELATION, not byte-level reproducibility (the file is not retained).
--   effective_config is the ALLOWLISTED, schema-versioned flag snapshot pinned at run start
--   (never secrets — see overlay/upload/ingestion_run.py::effective_config_snapshot).
--   pre/post_source_fingerprint correlate the source's graph state around the run (algo versioned
--   by fingerprint_algo_version; 'gn-v1' = sorted graph_node rows — correlation, not drift).
--   status: 'cancelled' is RESERVED (design #3) and deliberately NOT in the CHECK yet; 'abandoned'
--   is written only by the reconciliation sweep (follow-up) when an in_progress lease expires.
--   A TERMINAL status must carry completed_at — an application bug cannot record a finished run
--   with no finish time. heartbeat_at + (status, heartbeat_at) index serve the future sweep;
--   (catalog_source, started_at DESC) serves per-source listing.
-- Retention (design #3): first release keeps everything; the retention seam is documented, not built.
CREATE TABLE IF NOT EXISTS ingestion_run (
    id                       text        PRIMARY KEY,
    origin_type              text        NOT NULL CHECK (origin_type IN ('upload', 'connector')),
    catalog_source           text        NOT NULL,
    filename                 text        NULL,
    file_sha256              text        NULL,
    actor_subject            text        NOT NULL,
    actor_role_claims        text[]      NOT NULL DEFAULT '{}',
    authorization_decision   text        NULL,
    pre_source_fingerprint   text        NULL,
    post_source_fingerprint  text        NULL,
    fingerprint_algo_version text        NULL,
    effective_config         jsonb       NOT NULL DEFAULT '{}',
    row_count                int         NULL CHECK (row_count IS NULL OR row_count >= 0),
    quarantined_count        int         NULL CHECK (quarantined_count IS NULL
                                                     OR quarantined_count >= 0),
    status                   text        NOT NULL CHECK (status IN
        ('in_progress', 'ingested', 'held', 'rejected', 'failed', 'abandoned')),
    started_at               timestamptz NOT NULL,
    completed_at             timestamptz NULL,
    heartbeat_at             timestamptz NOT NULL,
    redacted_failure_code    text        NULL,
    CONSTRAINT ingestion_run_terminal_completed_check
        CHECK (status = 'in_progress' OR completed_at IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS ingestion_run_sweep_idx
    ON ingestion_run (status, heartbeat_at);
CREATE INDEX IF NOT EXISTS ingestion_run_source_idx
    ON ingestion_run (catalog_source, started_at DESC);

-- Append-only status history: one row per transition, never an in-place status that loses a
-- concurrent update. `status` is deliberately unconstrained beyond NOT NULL — the history may
-- record vocabulary the run table has not admitted yet (e.g. the reserved 'cancelled').
CREATE TABLE IF NOT EXISTS ingestion_run_status_event (
    id               bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ingestion_run_id text        NOT NULL REFERENCES ingestion_run(id),
    status           text        NOT NULL,
    at               timestamptz NOT NULL,
    reason_code      text        NULL
);
CREATE INDEX IF NOT EXISTS ingestion_run_status_event_run_idx
    ON ingestion_run_status_event (ingestion_run_id, at);
