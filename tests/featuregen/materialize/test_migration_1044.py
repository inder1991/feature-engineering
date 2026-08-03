"""Migration 1044 — the run-event stream is ORDERED, and terminal means terminal.

Migration 1034 made ``materialization_run_event`` append-only, but "append" was still too
permissive in two ways the status fold cannot survive:

* a NON-terminal event could be appended after a terminal one — only the four terminal kinds
  carry the one-terminal partial-unique index, so ``RUN_PREPARED`` after ``PUBLISHED`` inserts
  cleanly and ``fold_run_status`` raises forever;
* ``seq`` is caller-supplied and was unvalidated beyond the ``(run_id, seq)`` PK — one
  out-of-order INSERT permanently bricks ``run_status()`` on a table whose 1034 triggers block
  every repair path (UPDATE, DELETE and TRUNCATE all refuse).

So 1044 refuses the WRITE, not merely the read: a BEFORE INSERT trigger raises when the run
already holds a terminal event, or when ``NEW.seq`` does not extend the run's max seq.

The fixture re-applies 1034's SQL and then 1044's over the session-migrated database — both files
are idempotent, and the re-application is itself part of what these tests prove — so the file is
explicit about the schema it measures rather than inheriting it silently.
"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

import featuregen.db.migrations as _migrations

MIGRATION_NAME = "1044_run_event_ordering"

GEN = "gen-1044"
RUN = "run-1044"
NOW = "2026-07-31T10:00:00+00:00"


def _migration_sql(name: str) -> str:
    return (Path(_migrations.__file__).resolve().parent / "migrations"
            / f"{name}.sql").read_text(encoding="utf-8")


@pytest.fixture
def conn(conn):
    """The repo-root ``conn`` (a real PG connection, writes rolled back on teardown) with the two
    migrations this file measures re-applied — proving 1044 is re-runnable over an already-migrated
    database — and the generation every event FKs to seeded (dependency order, as in 1034's
    seeders: a guard proved against a table that cannot even take a valid row proves nothing)."""
    conn.execute(_migration_sql("1034_materialization_control_plane"))
    conn.execute(_migration_sql(MIGRATION_NAME))
    conn.execute(
        "INSERT INTO materialization_generation (generation_id, logical_group_name, "
        "materialization_contract_hash, group_plan_hash, generated_project_hash, created_at) "
        "VALUES (%s, 'cif_daily', 'ct-hash', 'gp-hash', 'proj-hash', %s)",
        (GEN, NOW))
    return conn


def _append(conn, *, seq: int, kind: str, run_id: str = RUN) -> None:
    """The same INSERT ``append_run_event`` issues (same table, same column list — mirrored from
    control_plane.py), so what is refused here is refused for the production writer too."""
    conn.execute(
        "INSERT INTO materialization_run_event (run_id, seq, generation_id, event_kind, "
        "occurred_at, detail) VALUES (%s, %s, %s, %s, %s, %s)",
        (run_id, seq, GEN, kind, NOW, ""))


def _count(conn, run_id: str = RUN) -> int:
    return conn.execute("SELECT count(*) FROM materialization_run_event WHERE run_id = %s",
                        (run_id,)).fetchone()[0]


def test_the_migration_is_applied_by_apply_migrations(conn) -> None:
    """The session fixture ran ``apply_migrations``; the ledger row proves the file was picked up
    under the number it claims (filename-stem keying), rather than the trigger existing only
    because this file's fixture executed the SQL by hand."""
    assert conn.execute("SELECT 1 FROM schema_migrations WHERE name = %s",
                        (MIGRATION_NAME,)).fetchone() is not None


def test_an_event_after_a_terminal_one_is_refused_by_the_database(conn) -> None:
    """The bricking write: only the four terminal kinds carry the one-terminal partial index, so
    before 1044 a NON-terminal event after PUBLISHED inserted cleanly — and the fold raised forever
    on a table with no repair path. The count proves refusal means "never landed", not
    insert-then-error."""
    _append(conn, seq=1, kind="RUN_PREPARED")
    _append(conn, seq=2, kind="PUBLISHED")
    with pytest.raises(psycopg.errors.RaiseException, match="terminal"), conn.transaction():
        _append(conn, seq=3, kind="COMPUTATION_COMPLETED")
    assert _count(conn) == 2


def test_a_seq_that_does_not_extend_the_run_is_refused(conn) -> None:
    """The FIRST event may land at any seq — a run's stream needn't start at 0, and this test only
    reaches its refusal because the seq-5 opener is accepted. What is refused is a later event that
    fails to EXTEND the run's max."""
    _append(conn, seq=5, kind="RUN_PREPARED")
    with pytest.raises(psycopg.errors.RaiseException, match="does not extend"), conn.transaction():
        _append(conn, seq=4, kind="RUN_SUBMITTED")
    assert _count(conn) == 1


def test_the_ordinary_ascending_stream_still_inserts(conn) -> None:
    """The must-survive control, mirroring 1034's INSERT test: a guard that refused the ordinary
    ascending stream would make the refusal tests above pass for the wrong reason."""
    for seq, kind in enumerate(("RUN_PREPARED", "RUN_SUBMITTED", "COMPUTATION_COMPLETED",
                                "GATES_PASSED", "PUBLISHED")):
        _append(conn, seq=seq, kind=kind)
    assert _count(conn) == 5
