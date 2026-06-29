from __future__ import annotations

from datetime import UTC, datetime, timedelta

from featuregen.contracts import NewTimer
from featuregen.runtime.timers import poll_due_timers, schedule_timer

UTC = UTC
NOW = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)


def _schedule(conn, key, fire_at, kind="sla"):
    return schedule_timer(
        conn, "run", "run_1", NewTimer(kind=kind, fire_at=fire_at, idempotency_key=key)
    )


def _status(conn, tid):
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM timers WHERE timer_id=%s", (tid,))
        return cur.fetchone()[0]


def test_due_timer_claimed(conn):
    tid = _schedule(conn, "due", NOW - timedelta(minutes=1))
    claimed = poll_due_timers(conn, owner="poller-a", lease_seconds=60, batch=10, now=NOW)
    assert tid in claimed
    assert _status(conn, tid) == "leased"


def test_future_timer_not_claimed(conn):
    tid = _schedule(conn, "future", NOW + timedelta(hours=1))
    assert poll_due_timers(conn, owner="poller-a", lease_seconds=60, batch=10, now=NOW) == []
    assert _status(conn, tid) == "scheduled"


def test_overdue_timer_claimed_on_recovery(conn):
    tid = _schedule(conn, "overdue", NOW - timedelta(days=30))
    claimed = poll_due_timers(conn, owner="poller-a", lease_seconds=60, batch=10, now=NOW)
    assert tid in claimed


def test_expired_lease_reclaimed(conn):
    tid = _schedule(conn, "stale-lease", NOW - timedelta(minutes=5))
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE timers SET status='leased', lease_owner='dead', lease_expires_at=%s "
            "WHERE timer_id=%s",
            (NOW - timedelta(minutes=1), tid),
        )
    claimed = poll_due_timers(conn, owner="poller-b", lease_seconds=60, batch=10, now=NOW)
    assert tid in claimed
    with conn.cursor() as cur:
        cur.execute("SELECT lease_owner FROM timers WHERE timer_id=%s", (tid,))
        assert cur.fetchone()[0] == "poller-b"
