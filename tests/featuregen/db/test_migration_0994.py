from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest

# First-release hardening #3: the durable ingestion_run manifest. 0994 creates the run read-model
# plus its append-only status history, and PostgreSQL enforces the manifest's invariants — closed
# origin/status vocabularies, non-negative counts, and "a terminal status carries completed_at" —
# so an application bug can never persist a malformed audit row. Migrations are applied once per
# session by the root `_dsn` fixture; the `conn` fixture rolls each test's writes back.

_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


def _run(conn, run_id: str = "ingrun_T1", *, origin_type: str = "upload",
         status: str = "in_progress", completed_at: datetime | None = None,
         row_count: int | None = None, quarantined_count: int | None = None) -> None:
    conn.execute(
        "INSERT INTO ingestion_run (id, origin_type, catalog_source, actor_subject, status, "
        "started_at, completed_at, heartbeat_at, row_count, quarantined_count) "
        "VALUES (%s, %s, 'deposits', 'user:tester', %s, %s, %s, %s, %s, %s)",
        (run_id, origin_type, status, _NOW, completed_at, _NOW, row_count, quarantined_count))


def _rejected(conn, insert, /, *args, **kwargs) -> None:
    """The insert must fail a CHECK; savepointed so one test can probe several violations.

    The no-op execute first OPENS the test's implicit outer transaction: ``conn.transaction()`` on
    an idle psycopg connection would otherwise be a top-level transaction that COMMITS on a
    no-raise exit, leaking rows past the fixture's teardown rollback (0993 pattern)."""
    conn.execute("SELECT 1")
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        insert(conn, *args, **kwargs)


# ── ingestion_run ─────────────────────────────────────────────────────────────────────────────────


def test_terminal_status_requires_completed_at(conn) -> None:
    for status in ("ingested", "held", "rejected", "failed", "abandoned"):
        _rejected(conn, _run, f"ingrun_{status}", status=status, completed_at=None)


def test_in_progress_needs_no_completed_at(conn) -> None:
    _run(conn, "ingrun_open", status="in_progress", completed_at=None)


def test_terminal_status_with_completed_at_accepted(conn) -> None:
    _run(conn, "ingrun_done", status="ingested", completed_at=_NOW,
         row_count=9, quarantined_count=0)


def test_negative_counts_rejected(conn) -> None:
    _rejected(conn, _run, "ingrun_r", status="ingested", completed_at=_NOW, row_count=-1)
    _rejected(conn, _run, "ingrun_q", status="ingested", completed_at=_NOW, quarantined_count=-1)


def test_bad_status_rejected(conn) -> None:
    _rejected(conn, _run, "ingrun_s1", status="done", completed_at=_NOW)
    _rejected(conn, _run, "ingrun_s2", status="cancelled", completed_at=_NOW)   # reserved, not open


def test_bad_origin_type_rejected(conn) -> None:
    _rejected(conn, _run, "ingrun_o1", origin_type="api")
    _rejected(conn, _run, "ingrun_o2", origin_type="")


# ── ingestion_run_status_event ────────────────────────────────────────────────────────────────────


def test_status_event_references_its_run(conn) -> None:
    _run(conn, "ingrun_ev")
    conn.execute(
        "INSERT INTO ingestion_run_status_event (ingestion_run_id, status, at) "
        "VALUES ('ingrun_ev', 'in_progress', %s)", (_NOW,))
    conn.execute("SELECT 1")
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        conn.execute(
            "INSERT INTO ingestion_run_status_event (ingestion_run_id, status, at) "
            "VALUES ('ingrun_missing', 'in_progress', %s)", (_NOW,))


def test_status_event_id_is_generated(conn) -> None:
    """Append-only history: ids are identity-generated — a writer cannot supply (or collide) one."""
    _run(conn, "ingrun_gen")
    conn.execute("SELECT 1")
    with pytest.raises(psycopg.errors.GeneratedAlways), conn.transaction():
        conn.execute(
            "INSERT INTO ingestion_run_status_event (id, ingestion_run_id, status, at) "
            "VALUES (1, 'ingrun_gen', 'in_progress', %s)", (_NOW,))
