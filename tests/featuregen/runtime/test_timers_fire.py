from __future__ import annotations

from datetime import datetime, timedelta, timezone

from featuregen.contracts import NewTimer
from featuregen.runtime.timers import (
    cancel_timers_for_task,
    fire_timer,
    poll_due_timers,
    schedule_timer,
)

UTC = timezone.utc
NOW = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)


def _sched(conn, key, *, kind="escalation", task_id=None, cas=None):
    return schedule_timer(conn, "run", "run_1",
                          NewTimer(kind=kind, fire_at=NOW - timedelta(minutes=1),
                                   idempotency_key=key, task_id=task_id, cas_task_version=cas))


def _queue_count(conn, message_id):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM queue WHERE message_id=%s", (message_id,))
        return cur.fetchone()[0]


def _status(conn, tid):
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM timers WHERE timer_id=%s", (tid,))
        return cur.fetchone()[0]


def test_fire_enqueues_once_and_is_idempotent(conn):
    tid = _sched(conn, "k-fire")
    poll_due_timers(conn, owner="p", lease_seconds=60, batch=10, now=NOW)
    out1 = fire_timer(conn, tid, now=NOW)
    assert out1.fired is True
    assert _status(conn, tid) == "fired"
    assert _queue_count(conn, "k-fire") == 1
    # re-fire (e.g. overdue duplicate) -> one effect, no second queue row
    out2 = fire_timer(conn, tid, now=NOW)
    assert out2.fired is False and out2.suppressed_reason == "already_fired"
    assert _queue_count(conn, "k-fire") == 1


def test_cas_match_fires(conn):
    tid = _sched(conn, "k-match", task_id="task_1", cas=1)
    poll_due_timers(conn, owner="p", lease_seconds=60, batch=10, now=NOW)
    out = fire_timer(conn, tid, now=NOW, resolve_task_version=lambda c, t: 1)
    assert out.fired is True
    assert _queue_count(conn, "k-match") == 1


def test_cas_mismatch_suppressed(conn):
    tid = _sched(conn, "k-mismatch", task_id="task_1", cas=1)
    poll_due_timers(conn, owner="p", lease_seconds=60, batch=10, now=NOW)
    out = fire_timer(conn, tid, now=NOW, resolve_task_version=lambda c, t: 2)
    assert out.fired is False and out.suppressed_reason == "cas_mismatch"
    assert _status(conn, tid) == "cancelled"
    assert _queue_count(conn, "k-mismatch") == 0


def test_answered_task_suppressed(conn):
    tid = _sched(conn, "k-answered", task_id="task_1", cas=1)
    poll_due_timers(conn, owner="p", lease_seconds=60, batch=10, now=NOW)
    out = fire_timer(conn, tid, now=NOW, resolve_task_version=lambda c, t: None)
    assert out.fired is False and out.suppressed_reason == "task_closed"
    assert _queue_count(conn, "k-answered") == 0


def test_cancel_on_answer_voids_unfired_rungs(conn):
    a = _sched(conn, "lad-a", kind="reminder", task_id="task_7", cas=1)
    b = _sched(conn, "lad-b", kind="escalation", task_id="task_7", cas=1)
    n = cancel_timers_for_task(conn, "task_7")
    assert n == 2
    assert _status(conn, a) == "cancelled" and _status(conn, b) == "cancelled"
    # a late fire on a cancelled timer is refused
    out = fire_timer(conn, b, now=NOW, resolve_task_version=lambda c, t: 1)
    assert out.fired is False and out.suppressed_reason == "task_closed"


def test_auto_park_rung_uses_canonical_handler(conn):
    # The ladder's auto_park rung must enqueue the SAME handler the cost breaker uses
    # ('runtime.auto_park', Task 10), NOT 'timer.auto_park' (§5.6 mirrors the §5.5 ladder),
    # so downstream registers ONE park handler for both ladder + cost-ceiling parking.
    tid = _sched(conn, "k-park", kind="auto_park")
    poll_due_timers(conn, owner="p", lease_seconds=60, batch=10, now=NOW)
    assert fire_timer(conn, tid, now=NOW).fired is True
    with conn.cursor() as cur:
        cur.execute("SELECT handler FROM queue WHERE message_id='k-park'")
        assert cur.fetchone()[0] == "runtime.auto_park"
