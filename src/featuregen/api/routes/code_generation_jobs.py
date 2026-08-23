"""Step 5a — the coordinator's surface: plan (a QUESTION), request (THE act), watch, cancel.

**`/plan` asks; `/code-generation-jobs` acts.** The plan endpoint is a read-only cost/readiness
preview: it resolves each member's authoring strategy, refuses retired and unapproved-legacy
candidates BEFORE they are counted as costed LLM members (a cost estimate that quotes work the
platform will then refuse is a quote for a purchase nobody can make), READS whether an approved
spend ceiling covers the job — it never creates one — and `ask`s the decision service rather than
`decide`ing (§7.1: a plan call that wrote a durable decision row would fill the audit with
decisions nobody acted on).

The write endpoint is the ONE explicit write/spend action: it records the job, its ordered
members and its per-action rows in one transaction, records the user's cost confirmation as a
DURABLE `llm_spend_authorization_revision` when LLM members exist (§11.2 — a modal is not a money
guard), and the worker's coordinator lane drives everything else. Idempotent on exact request
content, scoped to live jobs: a double-click lands on the live row, a retry after FAILED is a new
job.

**A route must not run the chain** — `materialization_runs.py`'s rule, and this journey is five
chains long. Producers and readers only.
"""
from __future__ import annotations

from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from featuregen.api.deps import (
    get_conn,
    get_identity,
    require_feature_generate,
    require_feature_read,
)
from featuregen.api.routes.build_sets import require_generation_enabled
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.code_generation_job_store import (
    JobStatusV1,
    advance_job,
    job_content_identity,
    read_events,
    read_job,
    read_job_actions,
    read_members,
)
from featuregen.overlay.upload.formula_draft_service import (
    CandidateUnavailable,
    FormulaStrategy,
)
from featuregen.runtime.observability import counters, log

__all__ = ["router"]

#: The authoring call envelope per LLM member — imported from THE one per-draft bound (8 author
#: turns × 5 physical attempts + the critic's 5), so the /plan quote, the recorded ceiling and
#: the dev envelope can never disagree (Task 5 re-review, carried finding 1: this file carried a
#: stale 5, /plan quoted it, the confirm dialog sent the quote verbatim as the ceiling, and a
#: job member got one-repair-exhausts-mid-draft back).
from featuregen.materialize.code_generation_coordinator import CALL_ENVELOPE_PER_LLM_MEMBER

router = APIRouter(dependencies=[Depends(require_generation_enabled)])
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]


def _now(conn: psycopg.Connection) -> str:
    return str(conn.execute("SELECT now()").fetchone()[0])


class SpendApprovalIn(BaseModel):
    """The user's cost confirmation, DURABLY recorded — never just a dismissed modal (§11.2)."""

    model_config = ConfigDict(extra="forbid")

    max_calls: int = Field(gt=0)
    max_tokens: int = Field(gt=0)
    max_cost: str = Field(min_length=1)
    currency: str = Field(min_length=3, max_length=3)
    pricing_version: str = Field(min_length=1)
    expires_at: str = Field(min_length=1)


class JobRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    considered_revision_id: str = Field(min_length=1)
    target_reading_revision_id: str = Field(min_length=1)
    #: ORDERED — the order a person picked features in decides the published column order.
    selection_revision_ids: list[str] = Field(min_length=1)
    environment_id: str = Field(min_length=1)
    logical_group_name: str = Field(min_length=1)
    #: The five declarations a generation cannot derive (`build_set_declaration` shape).
    declaration: dict[str, Any]
    #: engine_id · physical_type_policy · empty_values · roles · target_mode · target_ref
    execution_parameters: dict[str, Any]
    #: Required exactly when the plan says LLM members exist; refused as a 409 otherwise.
    spend_approval: SpendApprovalIn | None = None





def _service_request(body: JobRequestIn):
    """The route's body, decoded to the coordinator's transport-free request — Task 4's seam:
    job creation lives in `code_generation_coordinator` as a callable service, and this route is
    its HTTP adapter (the run-spine trigger constructs the same request directly)."""
    from featuregen.materialize.code_generation_coordinator import CodeGenJobRequestV1

    return CodeGenJobRequestV1(
        considered_revision_id=body.considered_revision_id,
        target_reading_revision_id=body.target_reading_revision_id,
        selection_revision_ids=tuple(body.selection_revision_ids),
        environment_id=body.environment_id,
        logical_group_name=body.logical_group_name,
        declaration=body.declaration,
        execution_parameters=body.execution_parameters,
        spend_approval=None if body.spend_approval is None else body.spend_approval.model_dump())


def _mapped_member_plans(conn, body: JobRequestIn):
    """The typed refusals, mapped to the statuses this surface always answered."""
    from featuregen.materialize.code_generation_coordinator import (
        SelectionUnavailable,
        member_authoring_plans,
    )
    from featuregen.overlay.upload.formula_draft_service import CandidateUnavailable

    try:
        return member_authoring_plans(conn, _service_request(body))
    except SelectionUnavailable as exc:
        raise HTTPException(
            status_code=404 if exc.kind == "unknown_selection" else 409,
            detail=exc.detail) from exc
    except CandidateUnavailable as exc:
        raise HTTPException(
            status_code={"unknown_revision": 404,
                         "option_not_in_revision": 422}.get(exc.kind, 409),
            detail=exc.detail) from exc


@router.post("/code-generation-jobs/plan",
             dependencies=[Depends(require_feature_read)])
def plan_code_generation(body: JobRequestIn, conn: _Conn, identity: _Identity) -> dict[str, Any]:
    """The cost/readiness preview — a QUESTION, and everything about it stays read-only."""
    from featuregen.materialize.action_authorization import ActionV1
    from featuregen.materialize.action_decision import ActionRequestV1, ask
    from featuregen.materialize.build_set_declaration import (
        declaration_identity,
        decode_declaration,
    )

    try:
        declaration = decode_declaration(body.declaration)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    plans = _mapped_member_plans(conn, body)
    llm_members = [p for p in plans
                   if p["formula_strategy"] == str(FormulaStrategy.LLM_AUTHORED)
                   and not p["blockers"]]
    deterministic = [p for p in plans
                     if p["formula_strategy"] == str(FormulaStrategy.REVIEWED_RECIPE_BLUEPRINT)
                     and not p["blockers"]]

    identity_hash = job_content_identity(
        considered_revision_id=body.considered_revision_id,
        target_reading_revision_id=body.target_reading_revision_id,
        environment_id=body.environment_id, logical_group_name=body.logical_group_name,
        selection_revision_ids=body.selection_revision_ids,
        declaration_identity=declaration_identity(declaration),
        execution_parameters=body.execution_parameters)

    # ▲ READS a spend authorization; never creates one. The approval is what the WRITE endpoint
    # records; a plan call that minted its own ceiling would be the modal-as-money-guard defect
    # with a server address.
    approved = conn.execute(
        "SELECT spend_authorization_id, max_calls, max_tokens, max_cost, currency "
        "FROM llm_spend_authorization_revision WHERE job_identity = %s AND expires_at > now() "
        "ORDER BY authorized_at DESC LIMIT 1", (identity_hash,)).fetchone()

    # ▲ `ask`, not `decide` (§7.1): the preflight answer through the ONE service, member facts
    # folded per §5's disposition table — the same fold the request-time decision will run.
    preview = ask(conn, ActionRequestV1(
        action=ActionV1.GENERATE_PREVIEW,
        resource_identity_hash=identity_hash,
        member_names=tuple(p["selection_revision_id"] for p in plans),
        member_blockers={p["selection_revision_id"]: tuple(p["blockers"])
                         for p in plans if p["blockers"]},
        member_warnings={p["selection_revision_id"]: tuple(p["warnings"])
                         for p in plans if p["warnings"]}))

    estimated_calls = len(llm_members) * CALL_ENVELOPE_PER_LLM_MEMBER
    return {
        "job_content_identity_hash": identity_hash,
        "members": plans,
        "deterministic_members": len(deterministic),
        "llm_members": len(llm_members),
        "estimated_provider_calls": estimated_calls,
        "call_envelope_per_llm_member": CALL_ENVELOPE_PER_LLM_MEMBER,
        "spend_approval_required": bool(llm_members),
        "spend_approval": (None if approved is None else {
            "spend_authorization_id": approved[0], "max_calls": approved[1],
            "max_tokens": approved[2], "max_cost": str(approved[3]), "currency": approved[4]}),
        "decision_preview": {
            "allowed": preview.allowed,
            "blockers": list(preview.blockers),
            "warnings": list(preview.warnings),
        },
        "detail": ("this is a preview: nothing was recorded, queued or spent — POST "
                   "/code-generation-jobs is the explicit act"),
    }


@router.post("/code-generation-jobs", status_code=202,
             dependencies=[Depends(require_feature_generate)])
def request_code_generation(
    body: JobRequestIn, conn: _Conn, identity: _Identity,
) -> dict[str, Any]:
    """THE explicit write/spend action — everything durable in one transaction, then the worker.

    `202`: a job is ACCEPTED, not completed. The response carries the job id to watch.
    """
    from featuregen.materialize.code_generation_coordinator import (
        CodeGenMembersRefused,
        CodeGenRequestInvalid,
        SelectionUnavailable,
        SpendApprovalRequired,
        request_code_generation_job,
    )
    try:
        job_id, created = request_code_generation_job(
            conn, _service_request(body), actor_subject=identity.subject)
    except SelectionUnavailable as exc:
        # ▲ The Task 5 review's adapter regression: these two escaped the WRITE route as 500s
        # while the plan route mapped them — same refusals, same statuses, both routes.
        raise HTTPException(
            status_code=404 if exc.kind == "unknown_selection" else 409,
            detail=exc.detail) from exc
    except CandidateUnavailable as exc:
        raise HTTPException(
            status_code={"unknown_revision": 404,
                         "option_not_in_revision": 422}.get(exc.kind, 409),
            detail=exc.detail) from exc
    except CodeGenRequestInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CodeGenMembersRefused as exc:
        raise HTTPException(status_code=409, detail={
            "code": exc.refused[0]["blockers"][0],
            "members": [{"selection_revision_id": p["selection_revision_id"],
                         "blockers": p["blockers"]} for p in exc.refused],
            "detail": "these members are refused before any spend; remove them or resolve the "
                      "named blockers, then plan again"}) from exc
    except SpendApprovalRequired as exc:
        raise HTTPException(status_code=409, detail={
            "code": "COST_AUTHORIZATION_MISSING",
            "llm_members": exc.llm_members,
            "estimated_provider_calls": exc.estimated_provider_calls,
            "detail": "this job authors with the LLM, and spend is an authorized act (§11.2): "
                      "confirm the ceiling by including spend_approval"}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    counters.incr("featuregen.code_generation_job.requested" if created
                  else "featuregen.code_generation_job.deduplicated")
    log("featuregen.code_generation_job.requested", job_id=job_id, created=created,
        members=len(body.selection_revision_ids))
    return {
        "job_id": job_id,
        "created": created,
        "detail": ("the job was recorded; the worker drives it — watch GET "
                   "/code-generation-jobs/{job_id}" if created else
                   "this exact request is already live; its job is the answer, and nothing "
                   "was queued or spent again"),
    }


@router.get("/code-generation-jobs/{job_id}",
            dependencies=[Depends(require_feature_read)])
def read_code_generation_job(job_id: str, conn: _Conn) -> dict[str, Any]:
    """The whole journey as stored: status, members, per-action decisions, event history."""
    job = read_job(conn, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no code-generation job {job_id!r}")
    # The sealed artifact, once the generation the job watches has one — read from the request
    # row, never derived: the workspace's "Code ready" stage and its link into the execution
    # screen both hang off this, and honest absence (null) is the answer until sealing happens.
    sealed = None if job.generation_request_id is None else conn.execute(
        "SELECT sealed_artifact_id FROM generation_request WHERE request_id = %s",
        (job.generation_request_id,)).fetchone()
    return {
        "job_id": job.job_id,
        "status": job.status.value,
        "terminal": job.status.is_terminal,
        "requested_by": job.requested_by,
        "build_set_revision_id": job.build_set_revision_id,
        "generation_request_id": job.generation_request_id,
        "sealed_artifact_id": None if sealed is None else sealed[0],
        "environment_id": job.environment_id,
        "logical_group_name": job.logical_group_name,
        "terminal_detail": job.terminal_detail,
        "members": [
            {"position": m.position, "selection_revision_id": m.selection_revision_id,
             "option_id": m.option_id, "formula_strategy": m.formula_strategy,
             "member_state": m.member_state.value, "formula_draft_id": m.formula_draft_id,
             "selection_formula_binding_id": m.selection_formula_binding_id,
             "blockers": list(m.blockers), "warnings": list(m.strategy_warnings)}
            for m in read_members(conn, job_id)],
        "actions": list(read_job_actions(conn, job_id)),
        "events": list(read_events(conn, job_id)),
    }


@router.post("/code-generation-jobs/{job_id}/cancel",
             dependencies=[Depends(require_feature_generate)])
def cancel_code_generation_job(job_id: str, conn: _Conn, identity: _Identity) -> dict[str, Any]:
    """Stop future stages. CANNOT claim to cancel a provider call already in flight — a draft
    the worker is authoring finishes or fails on its own lane; this job just stops consuming."""
    from featuregen.overlay.upload.code_generation_job_store import JobMovedUnderneath

    job = read_job(conn, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no code-generation job {job_id!r}")
    if job.status.is_terminal:
        raise HTTPException(status_code=409, detail={
            "code": "ALREADY_TERMINAL", "status": job.status.value,
            "detail": "this job already ended; there is nothing left to stop"})
    try:
        advance_job(conn, job_id, job.status, JobStatusV1.CANCELLED,
                    terminal_detail={"cancelled_by": identity.subject})
    except JobMovedUnderneath as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    counters.incr("featuregen.code_generation_job.cancelled")
    return {"job_id": job_id, "status": JobStatusV1.CANCELLED.value,
            "detail": "future stages stop; provider calls already in flight finish on their own "
                      "lane and their drafts remain reusable"}
