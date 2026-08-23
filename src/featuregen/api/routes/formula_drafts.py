"""Draft formula — the ASYNC per-candidate authoring request, and its polling read.

**Two endpoints, and neither runs a model.**

* ``POST /considered-revisions/{revision_id}/options/{option_id}/formula-drafts`` validates the
  candidate against the frozen revision, records the request in a SHORT transaction, enqueues the
  work and returns ``202``.
* ``GET /formula-drafts/{formula_draft_id}`` reports where it has got to.

The route holds ``get_conn``'s transaction for one INSERT and one outbox write. Two provider calls
plus validation plus admission happen in the worker, which commits between every stage — so a client
disconnect orphans nothing and the database is never held open across a model.

**DRAFTING IS NOT SELECTING, and this module cannot make it so.** ``POST /contract/draft`` records a
Gate-1 choice as its first act: on that route, drafting IS selecting. The product rule is the
opposite — a user must be able to inspect a formula and then decide — so this route exists separately
and imports no selection writer at all. A test asserts that absence, because "we simply do not call
it" is a habit and an import is a fact.

**Double-clicking must not buy two answers.** ``request_draft`` is idempotent on the FORMULA
IDENTITY, not on a caller-supplied key: a client minting a fresh key per click would defeat a
key-based guard, and what is being protected is money. The second call returns ``202`` with the same
id and ``created: false`` — the client's question ("is a draft coming for this candidate?") has the
same answer either way.

**The candidate comes from the SERVER's frozen record.** The revision and option are looked up, never
taken on trust from the body — the rule ``contract.py`` calls BLOCKER 1: a client payload naming its
own candidate would author against a definition nobody froze.
"""
from __future__ import annotations

from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from featuregen.aggregates.ids import mint_id
from featuregen.api.deps import get_conn, get_identity, require_permission
from featuregen.api.routes.code_generation_jobs import MoneyCeiling
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.formula_draft_service import (
    FORMULA_DRAFT_HANDLER,
    FORMULA_DRAFT_TOPIC,
    AuthoringRefused,
    CandidateUnavailable,
    FormulaStrategy,
    NotAFormulaCandidate,
    NotAnAnswerAtRequest,
    RetiredAtRequest,
    StrategyRefused,
    request_draft_for_candidate,
)
from featuregen.overlay.upload.formula_draft_store import read_draft
from featuregen.runtime.observability import counters, log

__all__ = ["FORMULA_DRAFT_HANDLER", "FORMULA_DRAFT_TOPIC", "router"]

# FORMULA_DRAFT_TOPIC / FORMULA_DRAFT_HANDLER now live in `formula_draft_service` BESIDE the one
# producer (the shared write composition); re-exported here unchanged for the worker's route map.

DRAFT_ID_HEADER = "X-Formula-Draft-Id"
_DRAFT_PREFIX = "fd"

router = APIRouter()
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]


@router.post(
    "/considered-revisions/{revision_id}/options/{option_id}/formula-drafts",
    status_code=202,
    dependencies=[Depends(require_permission("feature:generate"))],
)
def request_formula_draft(
    revision_id: str, option_id: str, response: Response, conn: _Conn, identity: _Identity,
) -> dict[str, Any]:
    """Record a formula-draft request and enqueue it. Returns ``202`` — never a formula.

    **This route never selects.** It records that a formula was ASKED FOR; the Gate-1 choice is a
    separate user action on a separate route. Inspecting must be free.

    The request row and the queue message are written in the SAME transaction, so there is no window
    where a draft exists with nobody to drive it, and none where a queue row names a draft that is
    not there.
    """
    # ▲ THE ONE COMPOSITION lives in `formula_draft_service.request_draft_for_candidate` — shared
    # verbatim with the step-5 coordinator, because a second composition of one act is how the two
    # drift (§8.3). This handler is the HTTP adapter: it maps the service's typed refusals onto the
    # exact statuses and bodies the tests pin, and adds nothing durable of its own.
    try:
        result = request_draft_for_candidate(
            conn, revision_id=revision_id, option_id=option_id,
            formula_draft_id=mint_id(_DRAFT_PREFIX), requested_by=identity.subject,
            now=_now(conn))
    except CandidateUnavailable as exc:
        # 404 the revision, 422 a stale tab's option, 409 a revision that predates exact option
        # identity (the remedy is to regenerate) — the same three answers this route always gave.
        status = {"unknown_revision": 404, "option_not_in_revision": 422}.get(exc.kind, 409)
        raise HTTPException(status_code=status, detail=exc.detail) from exc
    except NotAFormulaCandidate as exc:
        # 409, and the message is a NEXT STEP, not a dead end: a conceptual pattern is saved or
        # specified, a governed model output goes to the model workflow. Neither is a formula, so
        # neither may mint a draft.
        raise HTTPException(status_code=409, detail={
            "code": exc.code,
            "formula_strategy": str(exc.strategy),
            "detail": ("this candidate is not a deterministic formula, so no formula can be "
                       "drafted for it"),
            "next_step": ("save the idea or specify the computation"
                          if exc.strategy is FormulaStrategy.NON_FORMULA
                          else "configure it through the model workflow")}) from exc
    except StrategyRefused as exc:
        raise HTTPException(status_code=409, detail={
            "code": exc.blockers[0],
            "formula_strategy": str(exc.strategy),
            "blockers": list(exc.blockers),
            "detail": "the authoring strategy could not be resolved for this candidate"}) from exc
    except AuthoringRefused as exc:
        # The AUTHOR_FORMULA decision refused — typed, with the service's blockers (Task 5 AC2).
        raise HTTPException(status_code=409, detail={
            "code": exc.blockers[0] if exc.blockers else "AUTHORING_REFUSED",
            "blockers": list(exc.blockers),
            "detail": "the authoring decision refused; nothing was recorded or spent"}) from exc
    except NotAnAnswerAtRequest as exc:
        # 409 — the identity's draft is a recorded FAILURE, and buying the answer again is an
        # approved act (§11.1.2), not a retry button. This used to escape as a 500.
        raise HTTPException(status_code=409, detail={
            "code": "FORMULA_DRAFT_NOT_AN_ANSWER",
            "message": str(exc),
            "remedy": ("the previous attempt at this exact formula failed; re-authoring spends "
                       "again, so it needs an approved, cost-confirmed regeneration exception"),
        }) from exc
    except RetiredAtRequest as exc:
        # ▲ 409, NOT a 500 — the identity belongs to a RETIRED draft. 409 rather than 422: the
        # request is well-formed and would have been valid yesterday; what conflicts is the state
        # of the world. The body carries what somebody actually needs to act — which draft, why,
        # what replaced it, and WHICH input has to change — because `formula_identity_hash` is
        # unique, so retrying with a new draft id lands on the same row.
        retired = _retired_detail(conn, exc.candidate, option_id, config_hash=exc.config_hash)
        raise HTTPException(status_code=409, detail={
            "code": "FORMULA_DRAFT_RETIRED",
            "message": str(exc),
            **retired,
            "identity_bearing_inputs": [
                "authoring_config_hash", "catalog_snapshot_hash",
                "planning_request_hash", "definition_revision"],
            "remedy": ("this identity was retired, and a new draft id does not change it — either "
                       "use the replacement, or re-request once an identity-bearing input has "
                       "genuinely changed"),
        }) from exc

    response.headers[DRAFT_ID_HEADER] = result.formula_draft_id
    return {
        "formula_draft_id": result.formula_draft_id,
        "status": "requested",
        "stage": "queued",
        # FALSE is the double-click answer, and it is reported rather than hidden: a client that
        # showed "started" for a request that started nothing would be describing a spend that did
        # not happen.
        "created": result.created,
        # ▲ The RESOLVED method and the reasons, surfaced — the plan row is the durable record,
        # this is the same answer at the moment of asking. A client never sends a strategy; it
        # reads the one the server chose.
        "formula_strategy": str(result.strategy),
        "strategy_warnings": list(result.warnings),
        "detail": ("the formula draft was requested; a worker authors it" if result.created else
                   "an identical draft already exists for this candidate, catalog snapshot and "
                   "configuration — nothing was queued and nothing was spent"),
    }


def _retired_detail(conn, candidate, option_id: str, *, config_hash: str) -> dict[str, object]:
    """Which draft was retired, why, and what replaced it — read for the 409 body.

    A refusal that named only "retired" would send the caller to ask three more questions, and the
    replacement is usually the answer they need.
    """
    from featuregen.overlay.upload.formula_draft_store import formula_identity

    identity = formula_identity(
        considered_revision_id=candidate.considered_revision_id, option_id=option_id,
        planning_request_hash=candidate.planning_request_hash,
        catalog_snapshot_hash=candidate.catalog_snapshot_hash,
        # The SAME V2 hash the request computed — recomputing the identity here under a different
        # composition would look up a row the refusal was never about. Legacy V1 retirements are
        # found by the tombstone path in `request_draft` itself, whose scope key ignores the
        # configuration entirely; this lookup only decorates the 409 body.
        authoring_config_hash=config_hash,
        definition_revision=candidate.definition_revision)
    row = conn.execute(
        "SELECT d.formula_draft_id, r.reason, r.detail, r.replacement_draft_id "
        "  FROM formula_draft d "
        "  JOIN formula_draft_retirement r ON r.formula_draft_id = d.formula_draft_id "
        " WHERE d.formula_identity_hash = %s", (identity,)).fetchone()
    if row is None:
        return {"formula_identity_hash": identity}
    return {
        "formula_identity_hash": identity,
        "retired_draft_id": row[0],
        "reason": row[1],
        "detail": row[2],
        "replacement_draft_id": row[3],
    }


class MethodOverrideIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refused_formula_draft_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    llm_spend_authorization_id: str = Field(min_length=1)
    #: Bounded server-side: an approval given inside a triage window must not still authorize an
    #: LLM retry weeks later — the blueprint may have been fixed, and the correct answer then is
    #: the deterministic one.
    expires_in_hours: int = Field(default=24, ge=1, le=168)


@router.post(
    "/considered-revisions/{revision_id}/options/{option_id}/formula-method-overrides",
    status_code=201,
    dependencies=[Depends(require_permission("feature:generate"))])
def request_formula_method_override(
    revision_id: str, option_id: str, body: MethodOverrideIn, conn: _Conn, identity: _Identity,
) -> dict[str, Any]:
    """§11.3 — "Try AI formula", as an act the server VERIFIES and records.

    The browser never sends a formula method (parent §7). It names the refused draft; the server
    checks that draft is BLOCKED by the deterministic instantiation refusal for THIS candidate,
    that a live spend ceiling covers the retry (§11.2), and records the override append-only with
    an expiry — the strategy resolver then consumes it as an input fact on the NEXT draft
    request, which mints a NEW draft identity (child D2): nothing re-labels the refused one.
    """
    from featuregen.overlay.upload.llm_spend import canonical_approval_expiry
    from featuregen.overlay.upload.method_override import (
        OverrideRefusalUnverified,
        request_method_override,
    )

    # Day-floored for the same replay reason as spend approvals: a replayed override request
    # computing a fresh now()+N would otherwise mint a new revision per replay, quietly
    # extending the override's life one replay at a time.
    expires = canonical_approval_expiry(
        conn,
        conn.execute("SELECT (now() + make_interval(hours => %s))::text",
                     (body.expires_in_hours,)).fetchone()[0])
    try:
        override_id, created = request_method_override(
            conn, considered_revision_id=revision_id, option_id=option_id,
            refused_formula_draft_id=body.refused_formula_draft_id,
            actor_subject=identity.subject, reason=body.reason,
            llm_spend_authorization_id=body.llm_spend_authorization_id,
            expires_at=expires)
    except OverrideRefusalUnverified as exc:
        # 409: the request is well-formed; what is wrong is the RELATION between it and the
        # recorded world — no such refusal, wrong candidate, or no live ceiling.
        raise HTTPException(status_code=409, detail={
            "code": "OVERRIDE_REFUSAL_UNVERIFIED", "detail": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    counters.incr("featuregen.formula_method_override.recorded" if created
                  else "featuregen.formula_method_override.deduplicated")
    log("featuregen.formula_method_override.recorded", override_id=override_id,
        considered_revision_id=revision_id, option_id=option_id, created=created)
    return {
        "override_id": override_id,
        "created": created,
        "detail": ("the override is recorded and expires on schedule — request the formula "
                   "again and the resolver will author by LLM, with the override named in the "
                   "draft's identity" if created else
                   "this exact override is already recorded; it is the one in effect"),
    }


class RegenerationApprovalIn(BaseModel):
    """The approver's cost confirmation — everything else is DERIVED from the target draft."""

    model_config = ConfigDict(extra="forbid")

    max_calls: int = Field(gt=0)
    max_tokens: int = Field(gt=0)
    #: THE one declaration of a confirmed amount of money, read-only from the code-generation
    #: route's body types — the reason `feature_runs` imports `SpendApprovalIn` rather than
    #: re-declaring it. Without it this field carried the same raw-500 hole: a string that only
    #: fails at the `numeric` cast, three layers below the person who typed it.
    max_cost: MoneyCeiling
    currency: str = Field(min_length=3, max_length=3)
    pricing_version: str = Field(min_length=1)
    expires_at: str = Field(min_length=1)
    max_uses: int = Field(default=1, ge=1, le=10)


@router.post(
    "/formula-drafts/{formula_draft_id}/regeneration-exceptions", status_code=201,
    dependencies=[Depends(require_permission("governance:confirm"))])
def approve_regeneration(
    formula_draft_id: str, body: RegenerationApprovalIn, conn: _Conn, identity: _Identity,
) -> dict[str, Any]:
    """§11.1's approval surface, LLM-lane-only by the owner's Option 2 ruling.

    A GOVERNANCE act (`governance:confirm` — the recipe-review primitive), because approving a
    re-spend after a recorded failure is somebody taking responsibility for buying the answer
    again. The three bindings — target identity, provider contract, strategy — are derived
    server-side from the target draft's candidate under the CURRENT resolution; the body carries
    only the cost confirmation. One-time-consumption semantics are 1103's, consumed only by the
    transaction that mints the replacement.
    """
    from featuregen.overlay.upload.formula_draft_service import (
        RegenerationNotApprovable,
        approve_regeneration_for_draft,
    )

    try:
        exception_ids, spend_authorization_id, created = approve_regeneration_for_draft(
            conn, formula_draft_id=formula_draft_id, actor_subject=identity.subject,
            max_calls=body.max_calls, max_tokens=body.max_tokens, max_cost=body.max_cost,
            currency=body.currency, pricing_version=body.pricing_version,
            expires_at=body.expires_at, max_uses=body.max_uses)
    except RegenerationNotApprovable as exc:
        raise HTTPException(
            status_code=404 if exc.kind == "unknown_draft" else 409,
            detail={"code": {"deterministic_lane": "DETERMINISTIC_RETRY_IS_FREE",
                             "not_a_formula": "NOT_A_FORMULA",
                             "unknown_draft": "UNKNOWN_DRAFT"}.get(exc.kind, exc.kind.upper()),
                    "detail": exc.detail}) from exc
    except CandidateUnavailable as exc:
        raise HTTPException(
            status_code={"unknown_revision": 404,
                         "option_not_in_revision": 422}.get(exc.kind, 409),
            detail=exc.detail) from exc

    counters.incr("featuregen.regeneration.approved" if created
                  else "featuregen.regeneration.deduplicated")
    return {
        # ▲ ONE approval act binds the FULL covering set (round-4's one law) — one exception per
        # covering withdrawal, or the single plain-retry row when nothing covers. The first id
        # is kept under the old key for existing readers.
        "exception_ids": list(exception_ids),
        "exception_id": exception_ids[0],
        "spend_authorization_id": spend_authorization_id,
        "created": created,
        "detail": ("the regeneration is approved and cost-confirmed; re-request the draft and "
                   "the exception is consumed by the mint it authorizes" if created else
                   "this exact approval already exists; its one budget is the one in effect"),
    }


@router.get("/formula-drafts/{formula_draft_id}",
            dependencies=[Depends(require_permission("feature:generate"))])
def formula_draft_status(formula_draft_id: str, conn: _Conn) -> dict[str, Any]:
    """Where a draft has got to, and what it produced once it has.

    Polling is the first release's answer deliberately: it needs no connection held open, survives a
    client reload, and the row it reads is the same durable record the worker advances. SSE can be
    added over the identical state later without changing what a state MEANS.
    """
    draft = read_draft(conn, formula_draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="unknown formula draft")

    return {
        "formula_draft_id": draft.formula_draft_id,
        "considered_revision_id": draft.considered_revision_id,
        "option_id": draft.option_id,
        "state": draft.state.value,
        # The words the card shows, from the SERVER, so the API and the screen cannot describe one
        # state with two different sentences.
        #
        # ▲ RETIREMENT OVERRIDES THE LABEL. Retirement is an append BESIDE the draft, never an edit
        # of it, so a retired draft's `state` still says READY — and a card rendered from `state`
        # alone said "Formula ready" about something an operator had withdrawn. The state is still
        # reported truthfully; what the screen is told to SHOW accounts for the retirement.
        "stage": ("Retired" if draft.is_retired else draft.stage_label),
        "terminal": draft.state.is_terminal or draft.is_retired,
        # The whole retirement, not a boolean: "retired" alone sends a person to ask why, and the
        # replacement is usually the answer they actually need.
        "retired": draft.is_retired,
        "retirement": (None if draft.retirement is None else {
            "reason": draft.retirement.reason,
            "detail": draft.retirement.detail,
            "replacement_draft_id": draft.retirement.replacement_draft_id,
            "retired_by": draft.retirement.retired_by,
            "retired_at": draft.retirement.retired_at,
        }),
        "formula_source": "llm_authored",
        "authoring_run_id": draft.authoring_run_id,
        "formula_content_hash": draft.formula_content_hash,
        "formula": draft.formula_json,
        # BLOCKED is a product result and its blockers are the answer, not an error payload.
        "blockers": list(draft.blockers),
        "failure_reason": draft.failure_reason,
    }


# ▲ `_authoring_config_hash` IS GONE, and what it was is recorded where its victims are. It called
# `getattr` on a DICT, so `model`, `max_tokens` and `prompt_id` all fell to their defaults on every
# deployment and the "identity" was the constant
# f5c34b84d694062755f4b88605f9fc8d67e2f4ac1699054f99f6ccd09bfdc3c8 — the money guard blind to
# model, prompts and method since it shipped. Identity V2 (computed inline in the request route)
# folds the FROZEN provider contract and the resolved strategy instead. Migration 1109 records the
# constant era explicitly on every pre-V2 draft, as the defect it was. Deleting the function rather
# than fixing it is deliberate: a corrected version would still be a SECOND composition beside the
# route's, and two compositions of one identity is how they drift.


def _now(conn: psycopg.Connection) -> str:
    """The database's clock, so two application instances cannot disagree about when."""
    return str(conn.execute("SELECT now()").fetchone()[0])
