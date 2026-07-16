from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest

# First-release hardening #22: per-stage ingestion status. 0996 creates ingestion_run_stage — one
# row per (run, stage, attempt) with a typed state — as a CHILD of the 0994 ingestion_run manifest.
# PostgreSQL enforces the invariants: the closed 14-state vocabulary, the FK to a real run, and the
# (run, stage, attempt) uniqueness that makes concurrent stage writes append attempts instead of
# clobbering each other. Migrations are applied once per session by the root `_dsn` fixture; the
# `conn` fixture rolls each test's writes back.

_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)

_STATES = (
    "disabled", "not_applicable", "skipped_no_client", "not_run", "running", "waiting",
    "retrying", "succeeded", "partial", "failed", "deferred", "lagged", "cancelled",
    "audit_degraded")


def _run(conn, run_id: str = "ingrun_S1") -> str:
    conn.execute(
        "INSERT INTO ingestion_run (id, origin_type, catalog_source, actor_subject, status, "
        "started_at, heartbeat_at) VALUES (%s, 'upload', 'deposits', 'user:tester', "
        "'in_progress', %s, %s)", (run_id, _NOW, _NOW))
    return run_id


def _stage(conn, run_id: str, stage: str = "validation", *, attempt: int = 1,
           state: str = "succeeded") -> None:
    conn.execute(
        "INSERT INTO ingestion_run_stage (ingestion_run_id, stage, attempt, state, completed_at) "
        "VALUES (%s, %s, %s, %s, %s)", (run_id, stage, attempt, state, _NOW))


def test_every_taxonomy_state_accepted(conn) -> None:
    run_id = _run(conn)
    for i, state in enumerate(_STATES):
        _stage(conn, run_id, f"stage_{i}", state=state)


def test_unknown_state_rejected_by_check(conn) -> None:
    run_id = _run(conn)
    conn.execute("SELECT 1")   # open the outer tx before the savepoint (0993/0994 pattern)
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        _stage(conn, run_id, state="held")   # a RUN status, not a stage state
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        _stage(conn, run_id, state="")


def test_stage_requires_a_real_run(conn) -> None:
    conn.execute("SELECT 1")
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        _stage(conn, "ingrun_MISSING")


def test_run_stage_attempt_unique(conn) -> None:
    run_id = _run(conn)
    _stage(conn, run_id, "drift", attempt=1)
    _stage(conn, run_id, "drift", attempt=2)          # a retry appends, never clobbers
    _stage(conn, run_id, "validation", attempt=1)     # another stage, same run: fine
    conn.execute("SELECT 1")
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        _stage(conn, run_id, "drift", attempt=2)


def test_stage_id_is_generated(conn) -> None:
    run_id = _run(conn)
    conn.execute("SELECT 1")
    with pytest.raises(psycopg.errors.GeneratedAlways), conn.transaction():
        conn.execute(
            "INSERT INTO ingestion_run_stage (id, ingestion_run_id, stage, attempt, state) "
            "VALUES (1, %s, 'validation', 1, 'succeeded')", (run_id,))
