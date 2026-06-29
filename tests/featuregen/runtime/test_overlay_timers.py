from __future__ import annotations

from datetime import UTC, datetime

from featuregen.contracts import NewTimer
from featuregen.db.migrations import apply_migrations
from featuregen.runtime.timers import schedule_timer


def test_schedule_overlay_expiry_timer_succeeds(conn):
    timer_id = schedule_timer(
        conn,
        "overlay_fact",
        "a1b2c3",
        NewTimer(
            kind="overlay_expiry",
            fire_at=datetime(2026, 12, 1, tzinfo=UTC),
            idempotency_key="a1b2c3:expiry",
        ),
    )
    assert timer_id.startswith("tmr_")
    row = conn.execute(
        "SELECT kind, aggregate, aggregate_id, status FROM timers WHERE timer_id=%s",
        (timer_id,),
    ).fetchone()
    assert row == ("overlay_expiry", "overlay_fact", "a1b2c3", "scheduled")


def test_overlay_timers_migration_is_idempotent(conn):
    apply_migrations(conn)
    apply_migrations(conn)
    chk = conn.execute(
        "SELECT 1 FROM pg_constraint WHERE conname='timers_kind_check'"
    ).fetchone()
    assert chk is not None
