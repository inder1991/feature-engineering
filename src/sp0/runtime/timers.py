from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional

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


TaskVersionResolver = Callable[[DbConn, str], Optional[int]]


def _default_task_version(conn: DbConn, task_id: str) -> Optional[int]:
    """Read the current task_version of an OPEN gate task. Returns None if the task is
    gone/answered/cancelled, which suppresses a late timer. NOTE: this is the LIBRARY
    DEFAULT only and queries Phase 07's `human_tasks` table — a *runtime* dependency on
    Phase 07. Callers running before Phase 07 exists (and every Task-4 test) inject
    `resolve_task_version` to avoid it; there is no compile-time/import dependency."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT task_version FROM human_tasks WHERE task_id = %s AND status = 'open'",
            (task_id,),
        )
        row = cur.fetchone()
    return None if row is None else row[0]


@dataclass(frozen=True, slots=True)
class TimerFireOutcome:
    timer_id: str
    fired: bool
    suppressed_reason: Optional[str] = None  # already_fired|cas_mismatch|task_closed|not_found


def fire_timer(
    conn: DbConn,
    timer_id: str,
    *,
    now: datetime,
    resolve_task_version: TaskVersionResolver = _default_task_version,
) -> TimerFireOutcome:
    """Apply one timer's effect IDEMPOTENTLY (§5.5). CAS on the gate task version: if the
    timer guards a task whose required_inputs changed (task_version bumped) or that is no
    longer open, the timer is voided ('cancelled') and produces NO effect — a late timer
    cannot escalate an answered/changed gate. Otherwise it enqueues exactly one work message
    keyed by idempotency_key (overdue re-fire => one effect) and marks the timer 'fired'."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT idempotency_key, aggregate, aggregate_id, task_id, kind, cas_task_version, status "
            "FROM timers WHERE timer_id = %s FOR UPDATE",
            (timer_id,),
        )
        row = cur.fetchone()
        if row is None:
            return TimerFireOutcome(timer_id, False, "not_found")
        idem, aggregate, aggregate_id, task_id, kind, cas_version, status = row
        if status == "fired":
            return TimerFireOutcome(timer_id, False, "already_fired")
        if status == "cancelled":
            return TimerFireOutcome(timer_id, False, "task_closed")
        if task_id is not None and cas_version is not None:
            current = resolve_task_version(conn, task_id)
            if current is None:
                cur.execute("UPDATE timers SET status='cancelled' WHERE timer_id=%s", (timer_id,))
                return TimerFireOutcome(timer_id, False, "task_closed")
            if current != cas_version:
                cur.execute("UPDATE timers SET status='cancelled' WHERE timer_id=%s", (timer_id,))
                return TimerFireOutcome(timer_id, False, "cas_mismatch")
        # The auto_park rung shares ONE canonical park handler with the cost breaker
        # (trip_cost_breaker, Task 10), which enqueues 'runtime.auto_park' — §5.6 says the
        # breaker "mirrors the §5.5 ladder", so both park effects MUST land on the same
        # handler. Downstream therefore registers a single 'runtime.auto_park' handler; all
        # other rungs route to their per-kind 'timer.<kind>' handler.
        handler = "runtime.auto_park" if kind == "auto_park" else f"timer.{kind}"
        cur.execute(
            """
            INSERT INTO queue (message_id, partition_key, handler, payload)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (message_id) DO NOTHING
            """,
            (idem, f"{aggregate}:{aggregate_id}", handler,
             Jsonb({"timer_id": timer_id, "kind": kind, "task_id": task_id})),
        )
        cur.execute("UPDATE timers SET status='fired' WHERE timer_id=%s", (timer_id,))
    return TimerFireOutcome(timer_id, True)


def cancel_timers_for_task(conn: DbConn, task_id: str) -> int:
    """Atomically void all unfired timers guarding a gate task (cancel-on-answer, §5.5).
    Called in the SAME transaction as the answer (submit_human_signal, Phase 07) so a
    leased-but-not-yet-fired escalation cannot fire after the gate is answered. Returns the
    number of timers cancelled."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE timers SET status='cancelled' "
            "WHERE task_id=%s AND status IN ('scheduled','leased')",
            (task_id,),
        )
        return cur.rowcount
