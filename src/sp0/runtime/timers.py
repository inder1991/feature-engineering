from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from psycopg.types.json import Jsonb
from ulid import ULID  # python-ulid (declared by Phase 01); ULID-style id minting

from sp0.contracts import DbConn, NewTimer
from sp0.runtime.business_calendar import resolve_business_deadline


def schedule_timer(conn: DbConn, aggregate: str, aggregate_id: str, timer: NewTimer) -> str:
    """Insert one durable timer (status='scheduled'). The PK is a freshly minted ULID-style
    'tmr_…' id (overview id convention); idempotency is enforced by the UNIQUE
    idempotency_key, so a duplicate schedule creates no second row and returns the EXISTING
    timer_id. Used by the §5.1 atomic step (Phase 04) and by the poller for re-arming."""
    timer_id = f"tmr_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO timers (timer_id, idempotency_key, aggregate, aggregate_id, task_id,
                                kind, fire_at, business_calendar, cas_task_version, payload)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING timer_id
            """,
            (timer_id, timer.idempotency_key, aggregate, aggregate_id, timer.task_id,
             timer.kind, timer.fire_at, timer.business_calendar, timer.cas_task_version,
             Jsonb(dict(timer.payload))),
        )
        row = cur.fetchone()
        if row is not None:
            return row[0]
        cur.execute("SELECT timer_id FROM timers WHERE idempotency_key = %s",
                    (timer.idempotency_key,))
        return cur.fetchone()[0]


def build_escalation_ladder(
    conn: DbConn,
    *,
    aggregate: str,
    aggregate_id: str,
    task_id: str,
    task_version: int,
    opened_at: datetime,
    sla: str,
    reminder: str,
    escalation: str,
    business_calendar: Optional[str] = None,
) -> tuple[NewTimer, ...]:
    """Compose the escalation ladder (§5.5) as durable timers, each CAS-stamped with
    task_version and keyed for idempotency, returned in FIRE-TIME order
    (reminder -> sla -> escalation -> auto_park; the reminder fires before the SLA deadline,
    so chronologically reminder < sla, while §5.5 lists the conceptual order
    SLA -> reminder -> escalation -> auto-park). The caller is open_task (Phase 07), whose
    contract signature is open_task(conn, spec, actor) -> str: it returns a task_id and
    itself schedules each returned rung via schedule_timer inside the §5.1 atomic step (it
    does NOT return timers in a HandlerResult). cancel_timers_for_task voids the unfired
    rungs on answer."""
    sla_at = resolve_business_deadline(conn, business_calendar, opened_at, sla)
    reminder_at = resolve_business_deadline(conn, business_calendar, opened_at, reminder)
    escalation_at = resolve_business_deadline(conn, business_calendar, sla_at, escalation)
    park_at = resolve_business_deadline(conn, business_calendar, escalation_at, escalation)
    rungs = (("reminder", reminder_at), ("sla", sla_at),
             ("escalation", escalation_at), ("auto_park", park_at))
    return tuple(
        NewTimer(
            kind=kind,
            fire_at=fire_at,
            idempotency_key=f"ladder:{task_id}:v{task_version}:{kind}",
            task_id=task_id,
            business_calendar=business_calendar,
            cas_task_version=task_version,
            payload={"gate_task_id": task_id, "rung": kind},
        )
        for kind, fire_at in rungs
    )


def poll_due_timers(
    conn: DbConn, *, owner: str, lease_seconds: int, batch: int, now: datetime
) -> list[str]:
    """Claim due AND overdue scheduled timers (fire_at <= now) plus timers whose lease has
    expired, via FOR UPDATE SKIP LOCKED so concurrent pollers never double-claim (§5.5).
    Overdue timers are picked up here regardless of how far past, giving crash-recovery
    catch-up. Returns the claimed timer_ids (status -> 'leased')."""
    lease_until = now + timedelta(seconds=lease_seconds)
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH due AS (
                SELECT timer_id FROM timers
                 WHERE (status = 'scheduled' AND fire_at <= %s)
                    OR (status = 'leased' AND lease_expires_at < %s)
                 ORDER BY fire_at
                 FOR UPDATE SKIP LOCKED
                 LIMIT %s
            )
            UPDATE timers t
               SET status = 'leased', lease_owner = %s, lease_expires_at = %s
              FROM due
             WHERE t.timer_id = due.timer_id
            RETURNING t.timer_id
            """,
            (now, now, batch, owner, lease_until),
        )
        return [r[0] for r in cur.fetchall()]
