from __future__ import annotations

from featuregen.aggregates._append import append
from featuregen.aggregates.ids import new_run_id
from featuregen.contracts import Command, CommandResult, DbConn
from featuregen.events.store import load_stream

_LIFECYCLE_TYPES = (
    "MONITORING_ALERT_RAISED",
    "REVALIDATION_REQUIRED",
    "REVALIDATION_OUTCOME_RECORDED",
)


def _last_lifecycle(conn: DbConn, feature_id: str) -> str | None:
    for event in reversed(load_stream(conn, "feature", feature_id)):
        if event.type in _LIFECYCLE_TYPES:
            return event.type
    return None


def raise_monitoring_alert_command(conn: DbConn, cmd: Command) -> CommandResult:
    feature_id = cmd.aggregate_id
    evt = append(
        conn,
        aggregate="feature",
        aggregate_id=feature_id,
        type="MONITORING_ALERT_RAISED",
        payload={
            "feature_id": feature_id,
            "feature_version_id": cmd.args.get("feature_version_id"),
            "alert_ref": cmd.args.get("alert_ref"),
        },
        actor=cmd.actor,
        feature_id=feature_id,
    )
    return CommandResult(accepted=True, aggregate_id=feature_id, produced_event_ids=(evt.event_id,))


def require_revalidation_command(conn: DbConn, cmd: Command) -> CommandResult:
    feature_id = cmd.aggregate_id
    if _last_lifecycle(conn, feature_id) != "MONITORING_ALERT_RAISED":
        return CommandResult(
            accepted=False,
            aggregate_id=feature_id,
            denied_reason="require_revalidation requires a prior MONITORING_ALERT",
        )
    evt = append(
        conn,
        aggregate="feature",
        aggregate_id=feature_id,
        type="REVALIDATION_REQUIRED",
        payload={
            "feature_id": feature_id,
            "feature_version_id": cmd.args.get("feature_version_id"),
            "reason": cmd.args.get("reason"),
        },
        actor=cmd.actor,
        feature_id=feature_id,
    )
    return CommandResult(accepted=True, aggregate_id=feature_id, produced_event_ids=(evt.event_id,))


def record_revalidation_outcome_command(conn: DbConn, cmd: Command) -> CommandResult:
    feature_id = cmd.aggregate_id
    outcome = cmd.args["outcome"]
    # Bound ONCE (DRY) and reused for both the event payload and the deprecate UPDATE. Nullable for
    # non-deprecate outcomes (revalidated/requires_change run no scoped UPDATE, so a null slot id is
    # harmless there), but a "deprecate" outcome scopes an UPDATE by it — see the guard below.
    feature_version_id = cmd.args.get("feature_version_id")
    if _last_lifecycle(conn, feature_id) != "REVALIDATION_REQUIRED":
        return CommandResult(
            accepted=False,
            aggregate_id=feature_id,
            denied_reason="record_revalidation_outcome requires REVALIDATION_REQUIRED",
        )
    # Fail LOUD before emitting anything: a null feature_version_id would make the deprecate UPDATE
    # (... AND feature_version_id = NULL) match ZERO rows — silently deprecating nothing while
    # claiming success (under-deprecation trap). A deprecate outcome must name its version or be
    # rejected; it must never emit REVALIDATION_OUTCOME_RECORDED and then no-op the deprecation.
    if outcome == "deprecate" and feature_version_id is None:
        return CommandResult(
            accepted=False,
            aggregate_id=feature_id,
            denied_reason="deprecate outcome requires feature_version_id",
        )
    produced: list[str] = []
    new_run = None
    if outcome == "deprecate":
        # Deprecate the revalidated version's slot FIRST and require it actually matched a row.
        # Otherwise a 0-row UPDATE (version no longer the active slot) would still emit
        # REVALIDATION_OUTCOME_RECORDED and return success — a false audit trail that deprecates
        # nothing (SP-0.5 round-2). Scope to the revalidated version: feature_active_versions'
        # grain is (feature_id, use_case) and this command carries no use_case, so we scope by
        # the tightest available discriminator, feature_version_id — mirroring _deprecate_now
        # (consumers.py) minus the use_case term. Follow-up: carry use_case to reach that grain.
        updated = conn.execute(
            "UPDATE feature_active_versions SET activation_state='DEPRECATED' "
            "WHERE feature_id=%s AND feature_version_id=%s",
            (feature_id, feature_version_id),
        ).rowcount
        if updated == 0:
            return CommandResult(
                accepted=False,
                aggregate_id=feature_id,
                denied_reason="deprecate outcome matched no active version (already superseded?)",
            )
    if outcome == "requires_change":
        new_run = new_run_id()
        created = append(
            conn,
            aggregate="run",
            aggregate_id=new_run,
            type="RUN_CREATED",
            payload={
                "run_id": new_run,
                "request_id": cmd.args.get("request_id"),
                "feature_id": feature_id,
                "reopened_from": None,
                "origin": "revalidation",
            },
            actor=cmd.actor,
            request_id=cmd.args.get("request_id"),
            feature_id=feature_id,
            run_id=new_run,
            expected_version=0,
        )
        produced.append(created.event_id)
    evt = append(
        conn,
        aggregate="feature",
        aggregate_id=feature_id,
        type="REVALIDATION_OUTCOME_RECORDED",
        payload={
            "feature_id": feature_id,
            "feature_version_id": feature_version_id,
            "outcome": outcome,
            "new_run_id": new_run,
        },
        actor=cmd.actor,
        feature_id=feature_id,
    )
    produced.append(evt.event_id)
    return CommandResult(accepted=True, aggregate_id=feature_id, produced_event_ids=tuple(produced))
