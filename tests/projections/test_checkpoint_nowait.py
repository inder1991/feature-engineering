"""Composition audit finding [9] — the human confirm surfaces' synchronous drain must NOT block on
the 'overlay' ``projection_checkpoints`` row when a concurrent ingest holds it (that lock is held to
ingest commit, across the multi-minute D4/Pass-B LLM stages). ``try_lock_checkpoint_nowait`` takes the
row ``FOR UPDATE NOWAIT`` and reports contention (False) instead of blocking, so the confirm defers to
its fail-closed projection-lag path.

The 'overlay' checkpoint row is pre-seeded committed by migration 0507, so a SECOND session can lock
it directly to simulate the in-flight ingest's held row lock."""
from __future__ import annotations

import psycopg

from featuregen.projections.runner import try_lock_checkpoint_nowait


def test_try_lock_checkpoint_nowait_reports_contention_without_blocking(conn, _dsn):
    holder = psycopg.connect(_dsn)
    try:
        # A concurrent holder locks the committed 'overlay' checkpoint row and keeps its tx OPEN —
        # exactly what an in-flight ingest's in-tx _drain_projection does (holds the row to commit).
        holder.execute(
            "SELECT 1 FROM projection_checkpoints WHERE projection_name = 'overlay' FOR UPDATE")
        # CONTENDED: the NOWAIT probe returns False and NEVER blocks (the test finishing proves it —
        # the pre-fix plain FOR UPDATE would hang here until the holder commits).
        assert try_lock_checkpoint_nowait(conn, "overlay") is False
        # The caller's transaction is left USABLE (the FOR UPDATE NOWAIT ran in its own savepoint,
        # cleanly rolled back) — a follow-on query still succeeds, no InFailedSqlTransaction.
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        holder.rollback()   # release the held lock
        holder.close()
    # FREE again once the holder is gone: the probe acquires the lock (True).
    assert try_lock_checkpoint_nowait(conn, "overlay") is True
    conn.rollback()         # release before teardown so nothing lingers
