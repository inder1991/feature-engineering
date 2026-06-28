from __future__ import annotations

from datetime import datetime, timezone

from sp0.contracts import NewTimer
from sp0.runtime.timers import build_escalation_ladder, schedule_timer

UTC = timezone.utc


def _count(conn, sql, *args):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchone()[0]


def test_schedule_timer_inserts_row(conn):
    t = NewTimer(kind="sla", fire_at=datetime(2026, 7, 1, tzinfo=UTC),
                 idempotency_key="k1", task_id="task_1")
    tid = schedule_timer(conn, "run", "run_1", t)
    assert tid
    assert _count(conn, "SELECT count(*) FROM timers WHERE idempotency_key='k1'") == 1
    with conn.cursor() as cur:
        cur.execute("SELECT status, aggregate, aggregate_id, task_id FROM timers WHERE timer_id=%s", (tid,))
        assert cur.fetchone() == ("scheduled", "run", "run_1", "task_1")


def test_schedule_timer_idempotent(conn):
    t = NewTimer(kind="reminder", fire_at=datetime(2026, 7, 1, tzinfo=UTC), idempotency_key="dup")
    a = schedule_timer(conn, "run", "run_1", t)
    b = schedule_timer(conn, "run", "run_1", t)
    assert a == b
    assert _count(conn, "SELECT count(*) FROM timers WHERE idempotency_key='dup'") == 1


def test_build_escalation_ladder(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO business_calendars (calendar_name) VALUES ('ops')")
    opened = datetime(2026, 6, 26, 9, 0, tzinfo=UTC)  # Friday
    ladder = build_escalation_ladder(
        conn, aggregate="run", aggregate_id="run_1", task_id="task_9", task_version=3,
        opened_at=opened, sla="2d", reminder="1d", escalation="1d", business_calendar="ops",
    )
    kinds = [t.kind for t in ladder]
    # fire-time order: the reminder fires BEFORE the SLA deadline; §5.5 lists the
    # conceptual ladder as SLA -> reminder -> escalation -> auto-park (see Task 2 intro).
    assert kinds == ["reminder", "sla", "escalation", "auto_park"]
    assert all(t.cas_task_version == 3 for t in ladder)
    assert all(t.idempotency_key.startswith("ladder:task_9:v3:") for t in ladder)
    fire_times = [t.fire_at for t in ladder]
    assert fire_times == sorted(fire_times)  # monotonically increasing rungs
