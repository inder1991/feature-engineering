from __future__ import annotations

from sp0.contracts import Command, CommandResult, DbConn
from sp0.aggregates._append import append
from sp0.aggregates.concept_claims import claim_concept
from sp0.aggregates.ids import new_request_id
from sp0.aggregates.ids import new_run_id
from sp0.aggregates.ids import normalize_concept_key


def create_request_command(conn: DbConn, cmd: Command) -> CommandResult:
    request_id = new_request_id()
    concept_key = normalize_concept_key(cmd.args["feature_concept"])
    created = append(
        conn, aggregate="request", aggregate_id=request_id, type="REQUEST_CREATED",
        payload={"request_id": request_id, "concept_key": concept_key,
                 "intake_mode": cmd.args.get("intake_mode", "hypothesis")},
        actor=cmd.actor, request_id=request_id, expected_version=0,
    )
    produced = [created.event_id]
    winner = claim_concept(conn, concept_key, request_id)
    if winner is not None and winner != request_id:
        dup = append(
            conn, aggregate="request", aggregate_id=request_id, type="DUPLICATE_OF",
            payload={"request_id": request_id, "duplicate_of_request_id": winner,
                     "concept_key": concept_key},
            actor=cmd.actor, request_id=request_id,
        )
        produced.append(dup.event_id)
    return CommandResult(accepted=True, aggregate_id=request_id,
                         produced_event_ids=tuple(produced))


def create_run_command(conn: DbConn, cmd: Command) -> CommandResult:
    request_id = cmd.args["request_id"]
    run_id = new_run_id()
    run_event = append(
        conn, aggregate="run", aggregate_id=run_id, type="RUN_CREATED",
        payload={"run_id": run_id, "request_id": request_id, "reopened_from": None},
        actor=cmd.actor, request_id=request_id, run_id=run_id, expected_version=0,
    )
    added = append(
        conn, aggregate="request", aggregate_id=request_id, type="CANDIDATE_ADDED",
        payload={"request_id": request_id, "run_id": run_id},
        actor=cmd.actor, request_id=request_id, run_id=run_id,
    )
    return CommandResult(accepted=True, aggregate_id=run_id,
                         produced_event_ids=(run_event.event_id, added.event_id))


def duplicate_of_command(conn: DbConn, cmd: Command) -> CommandResult:
    request_id = cmd.aggregate_id
    dup = append(
        conn, aggregate="request", aggregate_id=request_id, type="DUPLICATE_OF",
        payload={"request_id": request_id,
                 "duplicate_of_request_id": cmd.args.get("duplicate_of_request_id"),
                 "duplicate_of_feature_id": cmd.args.get("duplicate_of_feature_id"),
                 "concept_key": cmd.args.get("concept_key")},
        actor=cmd.actor, request_id=request_id,
    )
    return CommandResult(accepted=True, aggregate_id=request_id,
                         produced_event_ids=(dup.event_id,))
