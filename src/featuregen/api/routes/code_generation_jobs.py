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

from featuregen.aggregates.ids import mint_id
from featuregen.api.deps import (
    get_conn,
    get_identity,
    require_feature_generate,
    require_feature_read,
)
from featuregen.api.routes.build_sets import require_generation_enabled
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.code_generation_job_store import (
    JobMemberSpecV1,
    JobStatusV1,
    advance_job,
    create_job,
    job_content_identity,
    read_events,
    read_job,
    read_job_actions,
    read_members,
)
from featuregen.overlay.upload.formula_draft_service import (
    CandidateUnavailable,
    FormulaStrategy,
    frozen_candidate,
)
from featuregen.runtime.observability import counters, log

__all__ = ["router"]

#: The authoring call envelope per LLM member — 1 call + 2 retries + 2 repairs, the same budgets
#: `intake/llm.py` enforces. Quoted by `/plan` so an approval's `max_calls` has a basis a person
#: can check against the enforcement rather than against a guess.
CALL_ENVELOPE_PER_LLM_MEMBER = 5

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



def _member_plans(conn, body: JobRequestIn) -> list[dict[str, Any]]:
    """Resolve each selection's frozen candidate and authoring strategy — READ-ONLY.

    Refusals that must land BEFORE costing (plan §5a item 1): a retirement tombstone covering the
    candidate (`FORMULA_DRAFT_RETIRED`), and a READY legacy V1 draft with no approved regeneration
    (`LEGACY_REGENERATION_NOT_APPROVED` — under identity V2 a re-author would mint a DIFFERENT
    identity, so the money guard alone cannot see that the answer was already bought once).
    """
    from featuregen.canonical import jcs_sha256
    from featuregen.overlay.upload.formula_strategy import resolve_formula_strategy
    from featuregen.overlay.upload.formula_strategy_facts import (
        assemble_strategy_facts,
        current_author_contract_hash,
    )
    from featuregen.overlay.upload.retirement_scope import (
        retirement_scope_key,
        tombstone_covering,
    )

    plans: list[dict[str, Any]] = []
    for position, selection_revision_id in enumerate(body.selection_revision_ids):
        row = conn.execute(
            "SELECT considered_revision_id, option_id FROM feature_selection_revision "
            "WHERE revision_id = %s", (selection_revision_id,)).fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"no selection {selection_revision_id!r}: a build is a build of "
                       f"selections, and this one does not exist")
        if row[0] != body.considered_revision_id:
            raise HTTPException(
                status_code=409,
                detail=f"selection {selection_revision_id!r} belongs to considered revision "
                       f"{row[0]!r}, not {body.considered_revision_id!r}: one build reads one "
                       f"frozen considered set")
        option_id = row[1]

        try:
            candidate = frozen_candidate(conn, body.considered_revision_id, option_id)
        except CandidateUnavailable as exc:
            raise HTTPException(
                status_code={"unknown_revision": 404,
                             "option_not_in_revision": 422}.get(exc.kind, 409),
                detail=exc.detail) from exc

        assembled = assemble_strategy_facts(
            conn, considered_revision_id=candidate.considered_revision_id, option_id=option_id,
            idea=candidate.idea, catalog_snapshot_hash=candidate.catalog_snapshot_hash)
        decision = resolve_formula_strategy(assembled.facts)

        blockers = list(decision.blockers)
        warnings = list(decision.warnings)
        if decision.strategy in (FormulaStrategy.NON_FORMULA, FormulaStrategy.MODEL_WORKFLOW):
            blockers = blockers or ["CONCEPTUAL_PATTERN_NOT_AUTHORABLE"]

        # The retirement check, at PLAN time — the same identity composition the write path uses,
        # so the plan refuses exactly what the request would refuse.
        provider_contract = (current_author_contract_hash()
                            if decision.strategy is FormulaStrategy.LLM_AUTHORED else None)
        config_payload: dict[str, Any] = {
            "identity_version": 2,
            "formula_strategy": str(decision.strategy),
            "strategy_identity_hash": decision.strategy_identity_hash,
        }
        if provider_contract is not None:
            config_payload["provider_contract_hash"] = provider_contract
        from featuregen.overlay.upload.formula_draft_store import formula_identity

        scope_key = retirement_scope_key(
            considered_revision_id=candidate.considered_revision_id, option_id=option_id,
            planning_request_hash=candidate.planning_request_hash,
            catalog_snapshot_hash=candidate.catalog_snapshot_hash,
            definition_revision=candidate.definition_revision)
        identity_hash = formula_identity(
            considered_revision_id=candidate.considered_revision_id, option_id=option_id,
            planning_request_hash=candidate.planning_request_hash,
            catalog_snapshot_hash=candidate.catalog_snapshot_hash,
            authoring_config_hash=jcs_sha256(config_payload),
            definition_revision=candidate.definition_revision)
        if tombstone_covering(conn, scope_key=scope_key,
                              formula_identity_hash=identity_hash) is not None:
            blockers.append("FORMULA_DRAFT_RETIRED")

        # The state-aware legacy rule (§11.1.2): only a READY, unretired V1 draft is a fact here —
        # it holds an answer the platform already bought, so re-buying under V2 needs an approved
        # regeneration exception. FAILED/BLOCKED legacy drafts bought nothing and say nothing.
        legacy_ready = conn.execute(
            "SELECT 1 FROM formula_draft_authoring_identity i "
            "  JOIN formula_draft d ON d.formula_draft_id = i.formula_draft_id "
            " WHERE i.identity_version = 1 AND i.retirement_scope_key = %s "
            "   AND d.state = 'READY' LIMIT 1", (scope_key,)).fetchone()
        if legacy_ready is not None and decision.strategy is FormulaStrategy.LLM_AUTHORED:
            # ▲ Through the ONE exception reader — its bindings are the point: an exception
            # authorizes one exact identity under one provider contract and one strategy. The
            # first cut hand-rolled this query against a column that does not exist
            # (`consumed_at`; the real ledger is uses_consumed/max_uses) — found by the run-spine
            # session mapping the frozen SHA, and exactly why a second composition of a governed
            # read is banned (§8.3, applied to reads).
            from featuregen.overlay.upload.retirement_scope import valid_exception_for

            exception = valid_exception_for(
                conn, target_formula_identity_hash=identity_hash,
                provider_contract_hash=provider_contract,
                strategy_identity_hash=decision.strategy_identity_hash,
                now=conn.execute("SELECT now()").fetchone()[0])
            if exception is None:
                blockers.append("LEGACY_REGENERATION_NOT_APPROVED")

        plans.append({
            "position": position,
            "selection_revision_id": selection_revision_id,
            "considered_revision_id": candidate.considered_revision_id,
            "option_id": option_id,
            "formula_strategy": str(decision.strategy),
            "blockers": blockers,
            "warnings": warnings,
        })
    return plans


@router.post("/code-generation-jobs/plan",
             dependencies=[Depends(require_feature_read)])
def plan_code_generation(body: JobRequestIn, conn: _Conn, identity: _Identity) -> dict[str, Any]:
    """The cost/readiness preview — a QUESTION, and everything about it stays read-only."""
    from featuregen.materialize.action_authorization import ActionV1
    from featuregen.materialize.action_decision import ActionRequestV1, ask
    from featuregen.materialize.build_set_declaration import (
        decode_declaration,
        declaration_identity,
    )

    try:
        declaration = decode_declaration(body.declaration)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    plans = _member_plans(conn, body)
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
    from featuregen.materialize.build_set_declaration import (
        decode_declaration,
        declaration_identity,
    )
    from featuregen.overlay.upload.formula_strategy_facts import current_author_contract_hash
    from featuregen.overlay.upload.llm_spend import authorize_spend

    try:
        declaration = decode_declaration(body.declaration)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    for required in ("engine_id", "physical_type_policy", "empty_values"):
        if required not in body.execution_parameters:
            raise HTTPException(
                status_code=422,
                detail=f"execution_parameters is missing {required!r} — each decides how the "
                       f"build runs, and a defaulted one would decide it on the caller's behalf")

    plans = _member_plans(conn, body)
    refused = [p for p in plans if p["blockers"]]
    if refused:
        # The same refusals the plan endpoint previews, ENFORCED: a costed job containing a
        # member the platform will refuse is a quote for a purchase nobody can make.
        raise HTTPException(status_code=409, detail={
            "code": refused[0]["blockers"][0],
            "members": [{"selection_revision_id": p["selection_revision_id"],
                         "blockers": p["blockers"]} for p in refused],
            "detail": "these members are refused before any spend; remove them or resolve the "
                      "named blockers, then plan again"})

    llm_members = [p for p in plans
                   if p["formula_strategy"] == str(FormulaStrategy.LLM_AUTHORED)]
    if llm_members and body.spend_approval is None:
        raise HTTPException(status_code=409, detail={
            "code": "COST_AUTHORIZATION_MISSING",
            "llm_members": len(llm_members),
            "estimated_provider_calls": len(llm_members) * CALL_ENVELOPE_PER_LLM_MEMBER,
            "detail": "this job authors with the LLM, and spend is an authorized act (§11.2): "
                      "confirm the ceiling by including spend_approval"})

    identity_payload = declaration_identity(declaration)
    job_identity = job_content_identity(
        considered_revision_id=body.considered_revision_id,
        target_reading_revision_id=body.target_reading_revision_id,
        environment_id=body.environment_id, logical_group_name=body.logical_group_name,
        selection_revision_ids=body.selection_revision_ids,
        declaration_identity=identity_payload,
        execution_parameters=body.execution_parameters)

    if llm_members:
        # ▲ The confirmation, made DURABLE and content-addressed — idempotent, so a redelivered
        # request cannot re-authorize a second ceiling for the same job.
        authorize_spend(
            conn, action="AUTHOR_FORMULA", actor_subject=identity.subject,
            job_identity=job_identity,
            member_identities=[p["selection_revision_id"] for p in llm_members],
            provider_contract_hash=current_author_contract_hash(),
            max_calls=body.spend_approval.max_calls, max_tokens=body.spend_approval.max_tokens,
            currency=body.spend_approval.currency, max_cost=body.spend_approval.max_cost,
            pricing_version=body.spend_approval.pricing_version,
            expires_at=body.spend_approval.expires_at)

    try:
        job_id, created = create_job(
            conn, job_id=mint_id("cgj"),
            considered_revision_id=body.considered_revision_id,
            target_reading_revision_id=body.target_reading_revision_id,
            environment_id=body.environment_id, logical_group_name=body.logical_group_name,
            declaration=body.declaration, declaration_identity=identity_payload,
            execution_parameters=body.execution_parameters,
            members=tuple(
                JobMemberSpecV1(
                    position=p["position"],
                    selection_revision_id=p["selection_revision_id"],
                    considered_revision_id=p["considered_revision_id"],
                    option_id=p["option_id"], formula_strategy=p["formula_strategy"],
                    strategy_warnings=tuple(p["warnings"])) for p in plans),
            requested_by=identity.subject, requested_at=_now(conn))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    counters.incr("featuregen.code_generation_job.requested" if created
                  else "featuregen.code_generation_job.deduplicated")
    log("featuregen.code_generation_job.requested", job_id=job_id, created=created,
        members=len(plans), llm_members=len(llm_members))
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
