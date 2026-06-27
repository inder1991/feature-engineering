from __future__ import annotations

from sp0.contracts.db import DbConn

GLOBAL_SEQ = """
CREATE SEQUENCE IF NOT EXISTS global_seq_seq AS bigint
    INCREMENT BY 1 START WITH 1 NO CYCLE CACHE 1;
"""

EVENTS = """
CREATE TABLE IF NOT EXISTS events (
    event_id        text        PRIMARY KEY,
    global_seq      bigint      NOT NULL DEFAULT nextval('global_seq_seq'),
    aggregate       text        NOT NULL CHECK (aggregate IN ('request','feature','run')),
    aggregate_id    text        NOT NULL,
    stream_version  integer     NOT NULL CHECK (stream_version > 0),
    request_id      text        NULL,
    feature_id      text        NULL,
    run_id          text        NULL,
    type            text        NOT NULL,
    schema_version  integer     NOT NULL,
    table_version   integer     NOT NULL,
    actor           jsonb       NOT NULL,
    payload         jsonb       NOT NULL,
    provenance      jsonb       NOT NULL,
    caused_by       text        NULL REFERENCES events(event_id),
    occurred_at     timestamptz NOT NULL,
    recorded_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT events_optimistic_concurrency UNIQUE (aggregate, aggregate_id, stream_version),
    CONSTRAINT events_global_seq_unique       UNIQUE (global_seq),
    CONSTRAINT events_aggregate_id_consistent CHECK (
        (aggregate = 'request' AND aggregate_id = request_id) OR
        (aggregate = 'feature' AND aggregate_id = feature_id) OR
        (aggregate = 'run'     AND aggregate_id = run_id)
    )
);
CREATE INDEX IF NOT EXISTS events_stream_idx   ON events (aggregate, aggregate_id, stream_version);
CREATE INDEX IF NOT EXISTS events_global_idx   ON events (global_seq);
CREATE INDEX IF NOT EXISTS events_run_idx      ON events (run_id)     WHERE run_id     IS NOT NULL;
CREATE INDEX IF NOT EXISTS events_feature_idx  ON events (feature_id) WHERE feature_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS events_request_idx  ON events (request_id) WHERE request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS events_type_idx     ON events (type);
"""

EVENT_TYPE_REGISTRY = """
CREATE TABLE IF NOT EXISTS event_type_registry (
    type_name      text        NOT NULL,
    schema_version integer     NOT NULL,
    json_schema    jsonb       NOT NULL,
    owner          text        NOT NULL,
    status         text        NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active','deprecated','withdrawn')),
    registered_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (type_name, schema_version)
);
"""

REGISTRY_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS registry_snapshots (
    snapshot_id   text        PRIMARY KEY,
    registry      text        NOT NULL CHECK (registry IN ('events','docs')),
    captured_at   timestamptz NOT NULL DEFAULT now(),
    contents      jsonb       NOT NULL
);
"""

PROJECTION_CHECKPOINTS = """
CREATE TABLE IF NOT EXISTS projection_checkpoints (
    projection_name text        PRIMARY KEY,
    checkpoint_seq  bigint      NOT NULL DEFAULT 0,
    head_seq        bigint      NOT NULL DEFAULT 0,
    is_analytics    boolean     NOT NULL DEFAULT false,
    updated_at      timestamptz NOT NULL DEFAULT now()
);
"""

# Phase-01-owned supporting table: atomic read-switch alias for parallel
# projection migration (§3.6). Not in the shared core DDL; internal to Phase 01.
PROJECTION_ACTIVE_ALIAS = """
CREATE TABLE IF NOT EXISTS projection_active_alias (
    alias            text        PRIMARY KEY,
    projection_name  text        NOT NULL,
    switched_seq     bigint      NOT NULL DEFAULT 0,
    switched_at      timestamptz NOT NULL DEFAULT now()
);
"""

# Phase-01-owned generic degraded ledger (§3.6). run_projection records the affected
# aggregate here (from ProjectionApplyError.aggregate/aggregate_id/reason) when a
# fail-closed projection cannot apply a poison event, realizing the shared run_projection
# docstring's "mark the affected aggregate degraded and stop advancing it" without
# depending on run_workflow_state (owned by a later phase).
PROJECTION_DEGRADED = """
CREATE TABLE IF NOT EXISTS projection_degraded (
    projection_name text        NOT NULL,
    aggregate       text        NOT NULL,
    aggregate_id    text        NOT NULL,
    reason          text        NOT NULL,
    poison_event_id text        NULL REFERENCES events(event_id),
    poison_seq      bigint      NOT NULL,
    degraded_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (projection_name, aggregate, aggregate_id)
);
"""

MIGRATIONS: list[tuple[str, str]] = [
    ("0001_global_seq", GLOBAL_SEQ),
    ("0002_events", EVENTS),
    ("0003_event_type_registry", EVENT_TYPE_REGISTRY),
    ("0004_registry_snapshots", REGISTRY_SNAPSHOTS),
    ("0005_projection_checkpoints", PROJECTION_CHECKPOINTS),
    ("0006_projection_active_alias", PROJECTION_ACTIVE_ALIAS),
    ("0007_projection_degraded", PROJECTION_DEGRADED),
]


def apply_migrations(conn: DbConn) -> None:
    """Create all Phase 01 DDL objects (idempotent)."""
    with conn.cursor() as cur:
        for _name, sql in MIGRATIONS:
            cur.execute(sql)
    conn.commit()
