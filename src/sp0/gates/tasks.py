from __future__ import annotations

from datetime import datetime, timezone

from psycopg.types.json import Json

from sp0.contracts.db import DbConn
from sp0.contracts.gates import GateTaskSpec
from sp0.contracts.identity import IdentityEnvelope
from sp0.gates.duration import parse_duration
from sp0.idgen import mint_id


class GateError(Exception):
    """Raised on malformed/unknown human-gate task operations (§7)."""


def _task_aggregate(run_id, feature_id) -> tuple[str, str]:
    if run_id:
        return "run", run_id
    return "feature", feature_id


def open_task(conn: DbConn, spec: GateTaskSpec, actor: IdentityEnvelope) -> str:
    task_id = mint_id("task")
    conn.execute(
        """
        INSERT INTO human_tasks
            (task_id, task_version, run_id, feature_id, gate, required_inputs,
             eligible_assignees, allowed_responses, quorum_required, quorum_of_role,
             delegation_allowed, sla, status)
        VALUES (%s,1,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'open')
        """,
        (
            task_id, spec.run_id, spec.feature_id, spec.gate,
            list(spec.required_inputs), Json(dict(spec.eligible_assignees)),
            list(spec.allowed_responses), spec.quorum_required, spec.quorum_of_role,
            spec.delegation_allowed, spec.sla,
        ),
    )
    if spec.sla:
        base = datetime.now(timezone.utc)
        sla = parse_duration(spec.sla)
        agg, agg_id = _task_aggregate(spec.run_id, spec.feature_id)
        ladder = {
            "reminder": base + sla / 2,
            "sla": base + sla,
            "escalation": base + sla + sla / 2,
            "auto_park": base + sla * 2,
        }
        for kind, fire_at in ladder.items():
            conn.execute(
                """
                INSERT INTO timers
                    (timer_id, idempotency_key, aggregate, aggregate_id, task_id, kind,
                     fire_at, status, cas_task_version)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'scheduled',1)
                """,
                (mint_id("tmr"), f"{task_id}:{kind}", agg, agg_id, task_id, kind, fire_at),
            )
    return task_id


def bump_task_version(conn: DbConn, task_id: str) -> int:
    row = conn.execute(
        "UPDATE human_tasks SET task_version = task_version + 1, updated_at=now() "
        "WHERE task_id=%s RETURNING task_version",
        (task_id,),
    ).fetchone()
    if row is None:
        raise GateError(f"unknown task {task_id}")
    return row[0]


def cancel_task(
    conn: DbConn,
    task_id: str,
    *,
    reason: str,
    new_status: str = "cancelled",
) -> None:
    if new_status not in ("cancelled", "superseded"):
        raise GateError(f"invalid cancel status {new_status!r}")
    conn.execute(
        "UPDATE human_tasks SET status=%s, updated_at=now() WHERE task_id=%s AND status='open'",
        (new_status, task_id),
    )
    conn.execute(
        "UPDATE timers SET status='cancelled' WHERE task_id=%s AND status='scheduled'",
        (task_id,),
    )


def cancel_tasks_on_run_advance(
    conn: DbConn,
    run_id: str,
    *,
    reason: str = "run advanced past gate",
    new_status: str = "cancelled",
) -> int:
    """Cancel every OPEN gate task (and its scheduled timers) for a run when the run advances
    past their gate — the §7 "cancellation on run advance" clause, made concrete.

    PHASE BOUNDARY: the advancing event / transition is emitted by the Phase 06 lifecycle
    command (or the Phase 03 state machine); that owner CALLS this Phase-07 mechanism inside the
    same §5.1 atomic step transaction. Phase 07 owns the cancellation effect; the trigger is
    upstream. Returns the number of tasks cancelled."""
    open_ids = conn.execute(
        "SELECT task_id FROM human_tasks WHERE run_id=%s AND status='open'",
        (run_id,),
    ).fetchall()
    for (task_id,) in open_ids:
        cancel_task(conn, task_id, reason=reason, new_status=new_status)
    return len(open_ids)
