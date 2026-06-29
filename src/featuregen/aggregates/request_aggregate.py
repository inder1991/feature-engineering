from __future__ import annotations

from featuregen.aggregates._append import append, provenance_for
from featuregen.aggregates.concept_claims import claim_concept
from featuregen.aggregates.ids import (
    new_feature_id,
    new_request_id,
    new_run_id,
    normalize_concept_key,
)
from featuregen.aggregates.run_lifecycle import run_is_terminal
from featuregen.contracts import Command, CommandResult, DbConn
from featuregen.events.store import load_stream


def create_request_command(conn: DbConn, cmd: Command) -> CommandResult:
    request_id = new_request_id()
    concept_key = normalize_concept_key(cmd.args["feature_concept"])
    created = append(
        conn,
        aggregate="request",
        aggregate_id=request_id,
        type="REQUEST_CREATED",
        payload={
            "request_id": request_id,
            "concept_key": concept_key,
            "intake_mode": cmd.args.get("intake_mode", "hypothesis"),
        },
        actor=cmd.actor,
        request_id=request_id,
        expected_version=0,
    )
    produced = [created.event_id]
    winner = claim_concept(conn, concept_key, request_id)
    if winner is not None and winner != request_id:
        dup = append(
            conn,
            aggregate="request",
            aggregate_id=request_id,
            type="DUPLICATE_OF",
            payload={
                "request_id": request_id,
                "duplicate_of_request_id": winner,
                "concept_key": concept_key,
            },
            actor=cmd.actor,
            request_id=request_id,
        )
        produced.append(dup.event_id)
    return CommandResult(accepted=True, aggregate_id=request_id, produced_event_ids=tuple(produced))


def _request_is_open(conn: DbConn, request_id: str) -> bool:
    """A request is open iff it was created and has not been closed.

    Closure is currently expressed by a DUPLICATE_OF link (the request was
    resolved as a duplicate of another request/feature, §3.1).
    """
    stream = load_stream(conn, "request", request_id)
    if not any(e.type == "REQUEST_CREATED" for e in stream):
        return False
    return not any(e.type == "DUPLICATE_OF" for e in stream)


def create_run_command(conn: DbConn, cmd: Command) -> CommandResult:
    request_id = cmd.args["request_id"]
    if not _request_is_open(conn, request_id):
        return CommandResult(
            accepted=False,
            aggregate_id=request_id,
            denied_reason="request does not exist or is not open",
        )
    run_id = new_run_id()
    run_event = append(
        conn,
        aggregate="run",
        aggregate_id=run_id,
        type="RUN_CREATED",
        payload={"run_id": run_id, "request_id": request_id, "reopened_from": None},
        actor=cmd.actor,
        request_id=request_id,
        run_id=run_id,
        expected_version=0,
    )
    added = append(
        conn,
        aggregate="request",
        aggregate_id=request_id,
        type="CANDIDATE_ADDED",
        payload={"request_id": request_id, "run_id": run_id},
        actor=cmd.actor,
        request_id=request_id,
        run_id=run_id,
    )
    return CommandResult(
        accepted=True, aggregate_id=run_id, produced_event_ids=(run_event.event_id, added.event_id)
    )


def duplicate_of_command(conn: DbConn, cmd: Command) -> CommandResult:
    request_id = cmd.aggregate_id
    dup = append(
        conn,
        aggregate="request",
        aggregate_id=request_id,
        type="DUPLICATE_OF",
        payload={
            "request_id": request_id,
            "duplicate_of_request_id": cmd.args.get("duplicate_of_request_id"),
            "duplicate_of_feature_id": cmd.args.get("duplicate_of_feature_id"),
            "concept_key": cmd.args.get("concept_key"),
        },
        actor=cmd.actor,
        request_id=request_id,
    )
    return CommandResult(accepted=True, aggregate_id=request_id, produced_event_ids=(dup.event_id,))


def select_candidate_command(conn: DbConn, cmd: Command) -> CommandResult:
    request_id = cmd.aggregate_id
    stream = load_stream(conn, "request", request_id)
    all_runs = [e.payload["run_id"] for e in stream if e.type == "CANDIDATE_ADDED"]
    selections = cmd.args["selections"]
    selected_ids = {s["run_id"] for s in selections}
    not_candidates = selected_ids - set(all_runs)
    if not_candidates:
        return CommandResult(
            accepted=False,
            aggregate_id=request_id,
            denied_reason=f"selected runs are not candidates of this request: "
            f"{sorted(not_candidates)}",
        )
    explored = cmd.args.get("candidates_explored_count", len(all_runs))
    produced: list[str] = []
    for sel in selections:
        run_id = sel["run_id"]
        feature_id = sel.get("feature_id")
        if feature_id is None:
            feature_id = new_feature_id()
            created = append(
                conn,
                aggregate="feature",
                aggregate_id=feature_id,
                type="FEATURE_CREATED",
                payload={
                    "feature_id": feature_id,
                    "request_id": request_id,
                    "concept_key": cmd.args.get("concept_key"),
                    "origin_run_id": run_id,
                },
                actor=cmd.actor,
                request_id=request_id,
                feature_id=feature_id,
                run_id=run_id,
                expected_version=0,
            )
            produced.append(created.event_id)
        chosen = append(
            conn,
            aggregate="request",
            aggregate_id=request_id,
            type="CANDIDATE_SELECTED",
            payload={
                "request_id": request_id,
                "selected_run_id": run_id,
                "feature_id": feature_id,
                "candidates_explored_count": explored,
            },
            actor=cmd.actor,
            provenance=provenance_for(candidates_explored_count=explored),
            request_id=request_id,
            feature_id=feature_id,
            run_id=run_id,
        )
        produced.append(chosen.event_id)
    for run_id in all_runs:
        if run_id in selected_ids or run_is_terminal(conn, run_id):
            continue
        rej_req = append(
            conn,
            aggregate="request",
            aggregate_id=request_id,
            type="CANDIDATE_REJECTED",
            payload={"request_id": request_id, "run_id": run_id, "reason": "sibling_closed"},
            actor=cmd.actor,
            request_id=request_id,
            run_id=run_id,
        )
        rej_run = append(
            conn,
            aggregate="run",
            aggregate_id=run_id,
            type="RUN_REJECTED",
            payload={"run_id": run_id, "reason": "sibling_closed"},
            actor=cmd.actor,
            run_id=run_id,
        )
        produced.extend([rej_req.event_id, rej_run.event_id])
    return CommandResult(accepted=True, aggregate_id=request_id, produced_event_ids=tuple(produced))
