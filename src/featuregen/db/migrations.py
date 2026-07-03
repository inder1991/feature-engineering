from __future__ import annotations

import hashlib
from pathlib import Path

from featuregen.contracts.db import DbConn
from featuregen.runtime.ddl import RUNTIME_CORE_DDL
from featuregen.state_machine.ddl import STATE_MACHINE_DDL

# Phase 05+ migrations are authored as .sql files under db/migrations/ and applied in
# lexical order AFTER the core Python DDL above. Their 05xx_ prefix sorts them after the
# core tables (events, documents, runtime, ...) they reference.
_SQL_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

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

# run_workflow_state — SAMPLE state-bearing projection WITH a degraded flag (§3.6).
# Fail-closed: an unappliable event marks the aggregate degraded and blocks its commands.
# Phase-01-owned; DDL verbatim from the shared contract (overview § "Database schema").
RUN_WORKFLOW_STATE = """
CREATE TABLE IF NOT EXISTS run_workflow_state (
    run_id              text        PRIMARY KEY,
    request_id          text        NOT NULL,
    feature_id          text        NULL,
    current_state       text        NOT NULL,
    table_version       integer     NOT NULL,
    cost_units          numeric(18,4) NOT NULL DEFAULT 0,
    candidates_explored integer     NOT NULL DEFAULT 0,
    degraded            boolean     NOT NULL DEFAULT false,
    degraded_reason     text        NULL,
    degraded_event_id   text        NULL REFERENCES events(event_id),
    last_applied_seq    bigint      NOT NULL DEFAULT 0,
    updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS run_workflow_state_degraded_idx
    ON run_workflow_state (degraded) WHERE degraded = true;
CREATE INDEX IF NOT EXISTS run_workflow_state_state_idx
    ON run_workflow_state (current_state);
"""

# =========================================================================
# Phase 02 — documents DAG: documents (write-once), stage_primary, blob_index,
# document_type_registry. DDL verbatim from the shared contract (overview §
# "Database schema"); the write-once trigger is owned by this phase.
# =========================================================================

# documents — immutable staged document DAG (§3.4). Write-once; no UPDATE.
DOCUMENTS = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id              text        PRIMARY KEY,
    global_seq          bigint      NOT NULL DEFAULT nextval('global_seq_seq'),
    request_id          text        NULL,
    feature_id          text        NULL,
    run_id              text        NULL,
    stage               text        NOT NULL,
    schema_version      integer     NOT NULL,
    branch_role         text        NOT NULL CHECK (branch_role IN ('candidate','primary','rejected','repair')),
    derived_from        text[]      NOT NULL DEFAULT '{}',
    supersedes          text[]      NOT NULL DEFAULT '{}',
    body_ref            text        NULL,
    content_hash        text        NOT NULL,
    body_classification text        NOT NULL CHECK (body_classification IN ('pii-erasable','governance-retained')),
    actor               jsonb       NOT NULL,
    provenance          jsonb       NOT NULL,
    reject_reason       text        NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT documents_stage_enum CHECK (stage IN (
        'DRAFT_CONTRACT','ASSUMPTION_LEDGER','CONFIRMED_CONTRACT','MAPPED_CONTRACT',
        'FEATURE_PLAN','CANDIDATE_SQL','VALIDATION_REPORT','SANDBOX_RESULT','DQ_REPORT',
        'EVALUATION_REPORT','RISK_ASSESSMENT','EXPLAINABILITY','MONITORING_SPEC','APPROVAL_RECORD'
    )),
    CONSTRAINT documents_reject_reason_present CHECK (
        branch_role <> 'rejected' OR reject_reason IS NOT NULL
    )
);
CREATE INDEX IF NOT EXISTS documents_run_stage_idx ON documents (run_id, stage);
CREATE INDEX IF NOT EXISTS documents_global_idx    ON documents (global_seq);

-- Write-once enforcement (no UPDATE/DELETE) — installed as a row trigger by Phase 02.
CREATE OR REPLACE FUNCTION documents_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'documents are write-once: % not allowed on doc_id=%',
        TG_OP, COALESCE(OLD.doc_id, NEW.doc_id);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER documents_no_mutation
    BEFORE UPDATE OR DELETE ON documents
    FOR EACH ROW EXECUTE FUNCTION documents_write_once();
"""

# stage_primary — projection of PRIMARY_SELECTED (§3.4). Fail-closed.
# Enforces "one live primary per (run_id, stage)"; current = highest global_seq.
STAGE_PRIMARY = """
CREATE TABLE IF NOT EXISTS stage_primary (
    run_id        text        NOT NULL,
    stage         text        NOT NULL,
    doc_id        text        NOT NULL REFERENCES documents(doc_id),
    selected_seq  bigint      NOT NULL,
    selected_at   timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS one_live_primary_per_run_stage ON stage_primary (run_id, stage);
"""

# blob_index (documents-index) — object-store index for mark-and-sweep blob GC (§5.1).
# Schema owned here; GC mechanism built by Phase 05.
BLOB_INDEX = """
CREATE TABLE IF NOT EXISTS blob_index (
    blob_id        text        PRIMARY KEY,
    object_key     text        NOT NULL,
    content_hash   text        NOT NULL,
    classification text        NOT NULL CHECK (classification IN ('pii-erasable','governance-retained')),
    kms_key_id     text        NULL,
    referenced     boolean     NOT NULL DEFAULT false,
    status         text        NOT NULL DEFAULT 'live'
                       CHECK (status IN ('live','orphan','quarantined','swept','shredded')),
    size_bytes     bigint      NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    swept_at       timestamptz NULL
);
CREATE INDEX IF NOT EXISTS blob_index_gc_idx ON blob_index (status) WHERE status IN ('orphan','quarantined');
"""

# document_type_registry — versioned document/artifact schemas + upcasters (§3.7).
DOCUMENT_TYPE_REGISTRY = """
CREATE TABLE IF NOT EXISTS document_type_registry (LIKE event_type_registry INCLUDING ALL);
"""

MIGRATIONS: list[tuple[str, str]] = [
    ("0001_global_seq", GLOBAL_SEQ),
    ("0002_events", EVENTS),
    ("0003_event_type_registry", EVENT_TYPE_REGISTRY),
    ("0004_registry_snapshots", REGISTRY_SNAPSHOTS),
    ("0005_projection_checkpoints", PROJECTION_CHECKPOINTS),
    ("0006_projection_active_alias", PROJECTION_ACTIVE_ALIAS),
    ("0007_projection_degraded", PROJECTION_DEGRADED),
    ("0012_run_workflow_state", RUN_WORKFLOW_STATE),
    ("0008_documents", DOCUMENTS),
    ("0009_stage_primary", STAGE_PRIMARY),
    ("0010_blob_index", BLOB_INDEX),
    ("0011_document_type_registry", DOCUMENT_TYPE_REGISTRY),
    ("0030_state_machine", STATE_MACHINE_DDL),  # <-- Phase 03 (added)
    ("0040_runtime_core", RUNTIME_CORE_DDL),  # <-- Phase 04 (added)
]


# Applied-migration ledger (review MAJOR #8). Bootstrapped FIRST — before any ledger read —
# so apply_migrations can record what it ran (name + SHA-256 of the source SQL + applied_at),
# skip already-applied unchanged migrations instead of blindly re-executing their DDL, and
# raise on drift when an already-applied migration's source SQL has since changed.
SCHEMA_MIGRATIONS = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    name       text        PRIMARY KEY,
    checksum   text        NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);
"""


def _sql_file_migrations() -> list[tuple[str, str]]:
    """Load db/migrations/*.sql in lexical order (Phase 05+ file-based migrations)."""
    if not _SQL_MIGRATIONS_DIR.is_dir():
        return []
    return [
        (path.stem, path.read_text(encoding="utf-8"))
        for path in sorted(_SQL_MIGRATIONS_DIR.glob("*.sql"))
    ]


def apply_migrations(conn: DbConn) -> None:
    """Apply all migrations once, ledgered (idempotent): core Python DDL then file-based 05xx_ SQL.

    Each migration is recorded in ``schema_migrations`` with a SHA-256 of its source SQL. Per
    migration, in order:
      * a ledger row with the SAME checksum → SKIP (do not re-execute the DDL);
      * a ledger row with a DIFFERENT checksum → raise ``RuntimeError`` (drift: an already-applied
        migration's source SQL changed — silently skipping it would leave the schema wrong);
      * no ledger row → execute the SQL and INSERT the ledger row.

    On a FRESH database every migration runs exactly once and the ledger is populated; on a
    re-run against an already-migrated DB every migration SKIPs, so this stays safely re-runnable.
    All of it happens inside one committing transaction (as before).
    """
    with conn.cursor() as cur:
        cur.execute(SCHEMA_MIGRATIONS)
        for name, sql in [*MIGRATIONS, *_sql_file_migrations()]:
            checksum = hashlib.sha256(sql.encode()).hexdigest()
            cur.execute("SELECT checksum FROM schema_migrations WHERE name = %s", (name,))
            row = cur.fetchone()
            if row is not None:
                if row[0] == checksum:
                    continue  # already applied, unchanged — skip
                raise RuntimeError(
                    f"migration {name} checksum drift: recorded {row[0]} != source {checksum}"
                )
            cur.execute(sql)
            cur.execute(
                "INSERT INTO schema_migrations (name, checksum) VALUES (%s, %s)",
                (name, checksum),
            )
    conn.commit()
