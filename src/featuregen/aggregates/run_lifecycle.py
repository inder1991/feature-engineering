from __future__ import annotations

from featuregen.contracts import Command, CommandResult, DbConn
from featuregen.events.store import load_stream
from featuregen.aggregates._append import append
from featuregen.aggregates.ids import new_run_id

_TERMINAL_RUN_TYPES = ("RUN_REJECTED", "RUN_CANCELLED", "RUN_WITHDRAWN")


def run_is_terminal(conn: DbConn, run_id: str) -> bool:
    return any(e.type in _TERMINAL_RUN_TYPES for e in load_stream(conn, "run", run_id))


def _terminal_command(event_type: str):
    def handler(conn: DbConn, cmd: Command) -> CommandResult:
        run_id = cmd.aggregate_id
        if run_is_terminal(conn, run_id):
            return CommandResult(accepted=False, aggregate_id=run_id,
                                 denied_reason="run already terminal")
        evt = append(
            conn, aggregate="run", aggregate_id=run_id, type=event_type,
            payload={"run_id": run_id, "reason": cmd.args.get("reason")},
            actor=cmd.actor, run_id=run_id,
        )
        return CommandResult(accepted=True, aggregate_id=run_id,
                             produced_event_ids=(evt.event_id,))
    return handler


reject_command = _terminal_command("RUN_REJECTED")
cancel_command = _terminal_command("RUN_CANCELLED")
withdraw_command = _terminal_command("RUN_WITHDRAWN")


def park_command(conn: DbConn, cmd: Command) -> CommandResult:
    run_id = cmd.aggregate_id
    evt = append(
        conn, aggregate="run", aggregate_id=run_id, type="RUN_PARKED",
        payload={"run_id": run_id, "owner": cmd.args.get("owner"),
                 "waiting_on_fact": cmd.args.get("waiting_on_fact")},
        actor=cmd.actor, run_id=run_id,
    )
    return CommandResult(accepted=True, aggregate_id=run_id, produced_event_ids=(evt.event_id,))


def unpark_command(conn: DbConn, cmd: Command) -> CommandResult:
    run_id = cmd.aggregate_id
    evt = append(
        conn, aggregate="run", aggregate_id=run_id, type="RUN_UNPARKED",
        payload={"run_id": run_id}, actor=cmd.actor, run_id=run_id,
    )
    return CommandResult(accepted=True, aggregate_id=run_id, produced_event_ids=(evt.event_id,))


def reopen_as_new_run_command(conn: DbConn, cmd: Command) -> CommandResult:
    source_run = cmd.args["source_run_id"]
    src_stream = load_stream(conn, "run", source_run)
    if not any(e.type == "RUN_REJECTED" for e in src_stream):
        return CommandResult(accepted=False, aggregate_id=source_run,
                             denied_reason="reopen requires a rejected run")
    request_id = next((e.request_id for e in src_stream if e.type == "RUN_CREATED"), None)
    new_run = new_run_id()
    created = append(
        conn, aggregate="run", aggregate_id=new_run, type="RUN_CREATED",
        payload={"run_id": new_run, "request_id": request_id, "reopened_from": source_run},
        actor=cmd.actor, request_id=request_id, run_id=new_run, expected_version=0,
    )
    produced = [created.event_id]
    if request_id is not None:
        added = append(
            conn, aggregate="request", aggregate_id=request_id, type="CANDIDATE_ADDED",
            payload={"request_id": request_id, "run_id": new_run},
            actor=cmd.actor, request_id=request_id, run_id=new_run,
        )
        produced.append(added.event_id)
    return CommandResult(accepted=True, aggregate_id=new_run, produced_event_ids=tuple(produced))


def _runs_parked_on_fact(conn: DbConn, fact_key: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT run_id FROM events "
        "WHERE type = 'RUN_PARKED' AND payload->>'waiting_on_fact' = %s "
        "AND run_id NOT IN ("
        "  SELECT run_id FROM events WHERE type = 'RUN_UNPARKED' AND run_id IS NOT NULL)",
        (fact_key,),
    ).fetchall()
    return [r[0] for r in rows]


def fact_confirmed_resume_command(conn: DbConn, cmd: Command) -> CommandResult:
    fact_key = cmd.args["fact_key"]
    produced: list[str] = []
    for run_id in _runs_parked_on_fact(conn, fact_key):
        resume = append(
            conn, aggregate="run", aggregate_id=run_id, type="FACT_CONFIRMED_RESUME",
            payload={"run_id": run_id, "fact_key": fact_key}, actor=cmd.actor, run_id=run_id,
        )
        unparked = append(
            conn, aggregate="run", aggregate_id=run_id, type="RUN_UNPARKED",
            payload={"run_id": run_id}, actor=cmd.actor, run_id=run_id,
        )
        produced.extend([resume.event_id, unparked.event_id])
    return CommandResult(accepted=True, aggregate_id=cmd.aggregate_id or fact_key,
                         produced_event_ids=tuple(produced))


def source_changed_revalidate_command(conn: DbConn, cmd: Command) -> CommandResult:
    run_id = cmd.aggregate_id
    if run_is_terminal(conn, run_id):
        return CommandResult(accepted=False, aggregate_id=run_id,
                             denied_reason="run is terminal; nothing to revalidate")
    evt = append(
        conn, aggregate="run", aggregate_id=run_id, type="SOURCE_CHANGED_REVALIDATE",
        payload={"run_id": run_id, "source_ref": cmd.args["source_ref"],
                 "new_snapshot": cmd.args.get("new_snapshot")},
        actor=cmd.actor, run_id=run_id,
    )
    return CommandResult(accepted=True, aggregate_id=run_id, produced_event_ids=(evt.event_id,))


def resolve_degraded_command(conn: DbConn, cmd: Command) -> CommandResult:
    """Clear a `degraded` projection entry after remediation (§3.6/§4.4). This is a projection
    repair (no domain event): it un-blocks the aggregate's commands. `execute_command`
    special-cases this action so it is NOT itself blocked by the degraded gate. Scope here is the
    `run_workflow_state` sample projection (the only degraded-bearing projection in this phase);
    other aggregates' degraded handling is owned by their projection phases."""
    run_id = cmd.aggregate_id
    conn.execute(
        "UPDATE run_workflow_state SET degraded = false, degraded_reason = NULL, "
        "degraded_event_id = NULL, updated_at = now() WHERE run_id = %s",
        (run_id,),
    )
    return CommandResult(accepted=True, aggregate_id=run_id or "")
