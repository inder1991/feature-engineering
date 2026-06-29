from __future__ import annotations

from typing import Optional

from featuregen.contracts import Command, CommandResult, DbConn
from featuregen.events.store import load_stream
from featuregen.aggregates._append import append
from featuregen.aggregates.ids import new_run_id

_LIFECYCLE_TYPES = ("MONITORING_ALERT_RAISED", "REVALIDATION_REQUIRED",
                    "REVALIDATION_OUTCOME_RECORDED")


def _last_lifecycle(conn: DbConn, feature_id: str) -> Optional[str]:
    for event in reversed(load_stream(conn, "feature", feature_id)):
        if event.type in _LIFECYCLE_TYPES:
            return event.type
    return None


def raise_monitoring_alert_command(conn: DbConn, cmd: Command) -> CommandResult:
    feature_id = cmd.aggregate_id
    evt = append(
        conn, aggregate="feature", aggregate_id=feature_id, type="MONITORING_ALERT_RAISED",
        payload={"feature_id": feature_id,
                 "feature_version_id": cmd.args.get("feature_version_id"),
                 "alert_ref": cmd.args.get("alert_ref")},
        actor=cmd.actor, feature_id=feature_id,
    )
    return CommandResult(accepted=True, aggregate_id=feature_id, produced_event_ids=(evt.event_id,))


def require_revalidation_command(conn: DbConn, cmd: Command) -> CommandResult:
    feature_id = cmd.aggregate_id
    if _last_lifecycle(conn, feature_id) != "MONITORING_ALERT_RAISED":
        return CommandResult(accepted=False, aggregate_id=feature_id,
                             denied_reason="require_revalidation requires a prior MONITORING_ALERT")
    evt = append(
        conn, aggregate="feature", aggregate_id=feature_id, type="REVALIDATION_REQUIRED",
        payload={"feature_id": feature_id,
                 "feature_version_id": cmd.args.get("feature_version_id"),
                 "reason": cmd.args.get("reason")},
        actor=cmd.actor, feature_id=feature_id,
    )
    return CommandResult(accepted=True, aggregate_id=feature_id, produced_event_ids=(evt.event_id,))


def record_revalidation_outcome_command(conn: DbConn, cmd: Command) -> CommandResult:
    feature_id = cmd.aggregate_id
    outcome = cmd.args["outcome"]
    if _last_lifecycle(conn, feature_id) != "REVALIDATION_REQUIRED":
        return CommandResult(accepted=False, aggregate_id=feature_id,
                             denied_reason="record_revalidation_outcome requires REVALIDATION_REQUIRED")
    produced: list[str] = []
    new_run = None
    if outcome == "requires_change":
        new_run = new_run_id()
        created = append(
            conn, aggregate="run", aggregate_id=new_run, type="RUN_CREATED",
            payload={"run_id": new_run, "request_id": cmd.args.get("request_id"),
                     "feature_id": feature_id, "reopened_from": None, "origin": "revalidation"},
            actor=cmd.actor, request_id=cmd.args.get("request_id"), feature_id=feature_id,
            run_id=new_run, expected_version=0,
        )
        produced.append(created.event_id)
    evt = append(
        conn, aggregate="feature", aggregate_id=feature_id, type="REVALIDATION_OUTCOME_RECORDED",
        payload={"feature_id": feature_id,
                 "feature_version_id": cmd.args.get("feature_version_id"),
                 "outcome": outcome, "new_run_id": new_run},
        actor=cmd.actor, feature_id=feature_id,
    )
    produced.append(evt.event_id)
    if outcome == "deprecate":
        conn.execute(
            "UPDATE feature_active_versions SET activation_state='DEPRECATED' WHERE feature_id=%s",
            (feature_id,),
        )
    return CommandResult(accepted=True, aggregate_id=feature_id, produced_event_ids=tuple(produced))
