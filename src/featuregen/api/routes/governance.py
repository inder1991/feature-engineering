"""Join-governance routes (confirmation surface, Task 5): list / confirm / reject discovered joins.

Thin HTTP wiring over the Task 3/4 domain functions — the queue read model
(`list_open_approved_join_proposals`), the fact_key -> typed-command bridge
(`load_join_confirmation_context`, whose `JoinGovernanceNotFound` maps to 404 BEFORE any event is
written, so this surface can never approve a non-join fact), the REAL overlay
`confirm_fact`/`reject_fact` commands, and the synchronous post-VERIFIED projection
(`project_verified_join`).

All three routes require the raw `platform-admin` role CLAIM (`require_confirmer`) — the exact
claim the overlay's dual-owner join confirm authorizes on, so the route gate and the overlay gate
can never disagree. Overlay denials (already-confirmed, CAS-stale) surface as 409 with a
human-readable detail; every other denial reason passes through verbatim. A denied
confirm/reject RETURNS its 409 (never raises) so `get_conn` COMMITS the request tx — persisting
the COMMAND_DENIED security_audit row the overlay wrote on this connection and releasing the
security-chain advisory lock (audit I-3); a deny appends no fact event, so the commit is safe.

Each handler self-ensures the upload-context catalog adapter: an API-only process registers no
adapter at startup (the worker does), and `resolve_authority` inside confirm/reject fails closed
without one. `ensure_upload_catalog_adapter` is idempotent and never clobbers a richer adapter
(same pattern as ingest.py).

The Pass B confirm surface (table-fact Task 2) adds the SINGLE-confirmer siblings for the
governed `grain`/`availability_time` facts Pass B proposes: `GET
/sources/{source}/governance/table-facts` + `POST /governance/table-facts/{fact_key}/confirm`
and `.../reject`, over the Task 1 domain functions (`table_fact_governance`). One platform-admin
confirm reaches VERIFIED directly (four-eyes holds: the proposer is the service enrichment
actor), then `project_verified_table_fact` makes the fact operational synchronously —
`operational_projection` reports "projected" only when the graph_node flag actually landed;
a stale drift watermark's correct refusal reports "pending".
"""
from __future__ import annotations

from typing import Annotated, Literal

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from featuregen.api.deps import get_conn, get_identity, require_confirmer
from featuregen.contracts.envelopes import Command, IdentityEnvelope
from featuregen.overlay.confirmation_commands import confirm_fact, reject_fact
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.join_drift import (
    acknowledge_governed_join_divergence,
    list_governed_join_divergences,
)
from featuregen.overlay.upload.join_governance import (
    JoinGovernanceNotFound,
    list_open_approved_join_proposals,
    load_join_confirmation_context,
    project_verified_join,
    read_join_approvals,
)
from featuregen.overlay.upload.semantic_binding_governance import (
    SemanticBindingGovernanceNotFound,
    correct_binding,
    list_semantic_binding_proposals,
    load_semantic_binding_confirmation_context,
    request_reverify,
    withdraw_binding,
)
from featuregen.overlay.upload.semantic_bindings.projection import (
    project_verified_semantic_binding,
)
from featuregen.overlay.upload.table_fact_governance import (
    TableFactGovernanceNotFound,
    list_open_table_fact_proposals_governance,
    load_table_fact_confirmation_context,
    project_verified_table_fact,
)
from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter

router = APIRouter()
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]


class ConfirmJoinRequest(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class RejectJoinRequest(BaseModel):
    category: Literal["wrong_direction", "wrong_cardinality", "different_entity",
                      "not_a_real_key", "needs_data_check"]
    note: str | None = Field(default=None, max_length=1000)


class ConfirmTableFactRequest(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class RejectTableFactRequest(BaseModel):
    category: Literal["wrong_grain_columns", "wrong_as_of_column", "not_unique",
                      "needs_data_check"]
    note: str | None = Field(default=None, max_length=1000)


class ConfirmSemanticBindingRequest(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class RejectSemanticBindingRequest(BaseModel):
    category: Literal["wrong_entity", "wrong_currency_column", "not_a_binding", "needs_data_check"]
    note: str | None = Field(default=None, max_length=1000)


class ReverifySemanticBindingRequest(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class WithdrawSemanticBindingRequest(BaseModel):
    category: Literal["no_longer_valid", "wrong_binding", "superseded", "needs_data_check"]
    note: str | None = Field(default=None, max_length=1000)


class CorrectSemanticBindingRequest(BaseModel):
    # The corrected binding value: {"entity_id": <known entity>} for an entity_assignment, or
    # {"currency_column": {catalog_source, object_kind, schema, table, column}} for a
    # currency_binding. Shape is VALIDATED by the E1 write gate inside correct_binding
    # (fail-closed) — a bad value touches nothing.
    value: dict
    note: str | None = Field(default=None, max_length=1000)


def _clean(s: str | None) -> str | None:
    """Strip; an empty/whitespace-only note becomes None (absent, not '')."""
    s = (s or "").strip()
    return s or None


def _deny_to_detail(reason: str | None) -> str:
    """Map the overlay's STABLE deny substrings to reviewer-facing text; anything else passes
    through verbatim. "already confirmed" = join_confirmation.py's repeat-subject denial;
    "has been superseded" covers BOTH CAS-stale variants ("stale confirmation: ..." on confirm,
    "stale rejection: ..." on reject — confirmation_commands.py)."""
    reason = reason or "confirmation denied"
    if "already confirmed" in reason:
        return "You already approved this — a different admin must confirm."
    if "has been superseded" in reason:
        return "Changed since you loaded it — refresh."
    return reason


def _load_context_or_404(conn: psycopg.Connection, fact_key: str) -> dict:
    ensure_upload_catalog_adapter()
    try:
        return load_join_confirmation_context(conn, fact_key)
    except JoinGovernanceNotFound:
        raise HTTPException(status_code=404, detail="No such join proposal.") from None


@router.get("/sources/{source}/governance/joins", dependencies=[Depends(require_confirmer)])
def list_joins(source: str, conn: _Conn,
               limit: int = Query(default=100, ge=1, le=500)) -> dict:
    ensure_upload_catalog_adapter()
    proposals = list_open_approved_join_proposals(conn, source, limit=limit)
    # ADDITIVE: the source's OPEN governed-join divergences (a re-upload retargeted/dropped a
    # joins_to humans VERIFIED — advisory only, the verified join stays operational; see
    # join_drift.py). Rendered beside the open proposals so ONE screen shows the reviewer both
    # what awaits confirmation and what a re-upload now disputes.
    divergences = list_governed_join_divergences(conn, source.strip().lower())
    return {"source": source.strip().lower(), "proposals": proposals,
            "divergences": divergences, "next_cursor": None}


@router.post("/governance/joins/{fact_key}/confirm", dependencies=[Depends(require_confirmer)])
def confirm_join(fact_key: str, body: ConfirmJoinRequest, conn: _Conn,
                 identity: _Identity) -> dict:
    ctx = _load_context_or_404(conn, fact_key)
    cmd = Command(
        action="confirm_fact", aggregate="overlay_fact", aggregate_id=fact_key,
        args={"ref": ctx["ref"], "fact_type": "approved_join", "use_case": ctx["use_case"],
              "target_event_id": ctx["target_event_id"], "note": _clean(body.note)},
        actor=identity, idempotency_key=f"confirm:{fact_key}:{identity.subject}",
        expected_version=None)
    result = confirm_fact(conn, cmd)
    if not result.accepted:
        # RETURN the 409, never raise it (audit I-3): get_conn COMMITS the request tx on a normal
        # return, which persists the COMMAND_DENIED security_audit row the overlay's
        # `_deny_audited` just wrote on THIS connection — a denied SoD/four-eyes probe leaves a
        # DURABLE trace — and releases the security-chain advisory lock cleanly. Raising would
        # roll both back; re-writing the audit on a SECOND connection self-deadlocks on
        # pg_advisory_xact_lock(7000007) still held by this idle-in-transaction session. Safe to
        # commit: a deny appends NO fact event (only the audit row, or nothing for benign
        # CAS-stale/wrong-state denials). The body is byte-identical to HTTPException's
        # {"detail": ...} rendering, so the client contract is unchanged.
        return JSONResponse(status_code=409,
                            content={"detail": _deny_to_detail(result.denied_reason)})
    status = fold_overlay_state(load_fact(conn, fact_key)).status
    projection = "not_applicable"
    if status == "VERIFIED":
        # The second confirm just VERIFIED the join — make it operational NOW (lag-guarded,
        # fail-soft; "pending" defers to the next caught-up ingest re-projection).
        projection = project_verified_join(
            conn, ctx["ref"].from_ref.catalog_source, ctx["ref"], now=None)
    return {"governance_status": status, "operational_projection": projection,
            "approvals": read_join_approvals(conn, fact_key)}


@router.post("/governance/joins/{fact_key}/reject", dependencies=[Depends(require_confirmer)])
def reject_join(fact_key: str, body: RejectJoinRequest, conn: _Conn,
                identity: _Identity) -> dict:
    ctx = _load_context_or_404(conn, fact_key)
    # `category` is a first-class field on OVERLAY_FACT_REJECTED (Task 5 review): a reliable
    # analytics key. `reason` carries ONLY the free-text note (or None).
    cmd = Command(
        action="reject_fact", aggregate="overlay_fact", aggregate_id=fact_key,
        args={"ref": ctx["ref"], "fact_type": "approved_join", "use_case": ctx["use_case"],
              "target_event_id": ctx["target_event_id"], "reason": _clean(body.note),
              "category": body.category},
        actor=identity, idempotency_key=f"reject:{fact_key}:{identity.subject}",
        expected_version=None)
    result = reject_fact(conn, cmd)
    if not result.accepted:
        # RETURN, don't raise — the commit persists the overlay's COMMAND_DENIED audit row
        # and releases the advisory lock (audit I-3; full rationale in confirm_join).
        return JSONResponse(status_code=409,
                            content={"detail": _deny_to_detail(result.denied_reason)})
    return {"governance_status": "REJECTED", "category": body.category}


@router.post("/governance/joins/divergences/{divergence_id}/acknowledge",
             dependencies=[Depends(require_confirmer)])
def acknowledge_join_divergence(divergence_id: int, conn: _Conn, identity: _Identity) -> dict:
    """Mark a governed-join divergence acknowledged ("seen — the verified join stands / is being
    handled"). ADVISORY bookkeeping only: it never touches the approved_join fact or its edge —
    retiring/re-verifying the join is a separate governance action. A later re-upload that still
    diverges RE-OPENS the row (join_drift.py resets acknowledged_* on a fresh detection)."""
    row = acknowledge_governed_join_divergence(
        conn, divergence_id, subject=identity.subject, now=None)
    if row is None:
        raise HTTPException(status_code=404, detail="No such divergence.")
    return row


# ── Table-fact routes (Pass B confirm surface, Task 2) ───────────────────────────────────────────


def _load_table_fact_context_or_404(conn: psycopg.Connection, fact_key: str) -> dict:
    """The fact_type-VALIDATED context bridge: a fact_key that is not a loadable
    grain/availability_time proposal 404s BEFORE any command dispatch, so this surface can never
    be used to approve a join/policy fact."""
    ensure_upload_catalog_adapter()
    try:
        return load_table_fact_confirmation_context(conn, fact_key)
    except TableFactGovernanceNotFound:
        raise HTTPException(status_code=404, detail="No such table-fact proposal.") from None


@router.get("/sources/{source}/governance/table-facts",
            dependencies=[Depends(require_confirmer)])
def list_table_facts(source: str, conn: _Conn,
                     limit: int = Query(default=100, ge=1, le=500)) -> dict:
    ensure_upload_catalog_adapter()
    proposals = list_open_table_fact_proposals_governance(conn, source, limit=limit)
    return {"source": source.strip().lower(), "proposals": proposals, "next_cursor": None}


@router.post("/governance/table-facts/{fact_key}/confirm",
             dependencies=[Depends(require_confirmer)])
def confirm_table_fact(fact_key: str, body: ConfirmTableFactRequest, conn: _Conn,
                       identity: _Identity) -> dict:
    """SINGLE-confirmer: one platform-admin confirm reaches VERIFIED directly (grain/availability
    is a data fact on one table — no dual-owner split; four-eyes holds because the proposer is
    the service enrichment actor, never the confirmer). No approvals array in the response."""
    ctx = _load_table_fact_context_or_404(conn, fact_key)
    cmd = Command(
        action="confirm_fact", aggregate="overlay_fact", aggregate_id=fact_key,
        args={"ref": ctx["ref"], "fact_type": ctx["fact_type"], "use_case": ctx["use_case"],
              "target_event_id": ctx["target_event_id"], "note": _clean(body.note)},
        actor=identity, idempotency_key=f"confirm:{fact_key}:{identity.subject}",
        expected_version=None)
    result = confirm_fact(conn, cmd)
    if not result.accepted:
        # RETURN, don't raise — the commit persists the overlay's COMMAND_DENIED audit row
        # and releases the advisory lock (audit I-3; full rationale in confirm_join).
        return JSONResponse(status_code=409,
                            content={"detail": _deny_to_detail(result.denied_reason)})
    status = fold_overlay_state(load_fact(conn, fact_key)).status
    projection = "not_applicable"
    if status == "VERIFIED":
        # The confirm just VERIFIED the fact — make it operational NOW (drain-then-project,
        # fail-soft; "pending" defers to the next caught-up ingest re-projection, e.g. when the
        # drift watermark is stale and resolve_fact correctly refuses to serve).
        projection = project_verified_table_fact(
            conn, ctx["ref"].catalog_source, ctx["ref"], ctx["fact_type"], now=None)
    return {"governance_status": status, "operational_projection": projection}


@router.post("/governance/table-facts/{fact_key}/reject",
             dependencies=[Depends(require_confirmer)])
def reject_table_fact(fact_key: str, body: RejectTableFactRequest, conn: _Conn,
                      identity: _Identity) -> dict:
    ctx = _load_table_fact_context_or_404(conn, fact_key)
    # `category` is a first-class field on OVERLAY_FACT_REJECTED (a reliable analytics key);
    # `reason` carries ONLY the free-text note (or None) — same shape as the join reject.
    cmd = Command(
        action="reject_fact", aggregate="overlay_fact", aggregate_id=fact_key,
        args={"ref": ctx["ref"], "fact_type": ctx["fact_type"], "use_case": ctx["use_case"],
              "target_event_id": ctx["target_event_id"], "reason": _clean(body.note),
              "category": body.category},
        actor=identity, idempotency_key=f"reject:{fact_key}:{identity.subject}",
        expected_version=None)
    result = reject_fact(conn, cmd)
    if not result.accepted:
        # RETURN, don't raise — the commit persists the overlay's COMMAND_DENIED audit row
        # and releases the advisory lock (audit I-3; full rationale in confirm_join).
        return JSONResponse(status_code=409,
                            content={"detail": _deny_to_detail(result.denied_reason)})
    return {"governance_status": "REJECTED", "category": body.category}


# ══════════════ Semantic-binding routes (Delivery E, Task E2 — owner-or-admin four-eyes) ══════════
# The governed entity_assignment / currency_binding facts D2 proposes + E3 projects. Mirrors the
# join + table-fact surfaces (require_confirmer, idempotency_key, target-event CAS, _deny_audited,
# return-the-409), with two additions: the GET lists BOTH pending AND VERIFIED bindings (so the
# asset UI can offer reverify/withdraw/correct), and the three VERIFIED-binding actions reuse the
# overlay's own expiry/reverify transition + reject_fact/propose_fact — never hand-writing fact
# state. Owner-or-admin authority (E1) is enforced inside confirm_fact/reject_fact and re-checked in
# the reverify/withdraw/correct service; the route claim gate stays require_confirmer (like peers).


def _load_semantic_binding_context_or_404(conn: psycopg.Connection, fact_key: str) -> dict:
    """The fact_type-VALIDATED context bridge: a fact_key that is not a loadable
    entity_assignment/currency_binding proposal 404s BEFORE any command dispatch, so this surface
    can never be used to approve a join / grain / policy fact."""
    ensure_upload_catalog_adapter()
    try:
        return load_semantic_binding_confirmation_context(conn, fact_key)
    except SemanticBindingGovernanceNotFound:
        raise HTTPException(status_code=404,
                            detail="No such semantic-binding proposal.") from None


@router.get("/sources/{source}/governance/semantic-bindings",
            dependencies=[Depends(require_confirmer)])
def list_semantic_bindings(source: str, conn: _Conn,
                           limit: int = Query(default=100, ge=1, le=500)) -> dict:
    ensure_upload_catalog_adapter()
    proposals = list_semantic_binding_proposals(conn, source, limit=limit)
    return {"source": source.strip().lower(), "proposals": proposals, "next_cursor": None}


@router.post("/governance/semantic-bindings/{fact_key}/confirm",
             dependencies=[Depends(require_confirmer)])
def confirm_semantic_binding(fact_key: str, body: ConfirmSemanticBindingRequest, conn: _Conn,
                             identity: _Identity) -> dict:
    """Confirm a DRAFT (or re-affirm a REVERIFY/STALE) governed semantic binding → VERIFIED via
    E1's confirm_fact (which triggers E3's projection). SINGLE authorized confirmer (owner-or-admin,
    E1), four-eyes preserved: the proposer (the D2 service candidate, or the correcting human) can
    never be the confirmer. Reports the operational projection honestly ("projected"/"pending")."""
    ctx = _load_semantic_binding_context_or_404(conn, fact_key)
    cmd = Command(
        action="confirm_fact", aggregate="overlay_fact", aggregate_id=fact_key,
        args={"ref": ctx["ref"], "fact_type": ctx["fact_type"], "use_case": ctx["use_case"],
              "target_event_id": ctx["target_event_id"], "note": _clean(body.note)},
        actor=identity, idempotency_key=f"confirm:{fact_key}:{identity.subject}",
        expected_version=None)
    result = confirm_fact(conn, cmd)
    if not result.accepted:
        # RETURN, don't raise — the commit persists the overlay's COMMAND_DENIED audit row and
        # releases the advisory lock (audit I-3; full rationale in confirm_join).
        return JSONResponse(status_code=409,
                            content={"detail": _deny_to_detail(result.denied_reason)})
    status = fold_overlay_state(load_fact(conn, fact_key)).status
    projection = "not_applicable"
    if status == "VERIFIED":
        # The confirm just VERIFIED the binding — make it operational NOW (drain-then-project,
        # idempotent + fail-soft; confirm_fact already projected internally, this reports honestly).
        projection = project_verified_semantic_binding(
            conn, ctx["ref"].catalog_source, ctx["ref"], ctx["fact_type"], now=None)
    return {"governance_status": status, "operational_projection": projection}


@router.post("/governance/semantic-bindings/{fact_key}/reject",
             dependencies=[Depends(require_confirmer)])
def reject_semantic_binding(fact_key: str, body: RejectSemanticBindingRequest, conn: _Conn,
                            identity: _Identity) -> dict:
    ctx = _load_semantic_binding_context_or_404(conn, fact_key)
    # `category` is a first-class field on OVERLAY_FACT_REJECTED (a reliable analytics key);
    # `reason` carries ONLY the free-text note (or None) — same shape as the join/table-fact reject.
    cmd = Command(
        action="reject_fact", aggregate="overlay_fact", aggregate_id=fact_key,
        args={"ref": ctx["ref"], "fact_type": ctx["fact_type"], "use_case": ctx["use_case"],
              "target_event_id": ctx["target_event_id"], "reason": _clean(body.note),
              "category": body.category},
        actor=identity, idempotency_key=f"reject:{fact_key}:{identity.subject}",
        expected_version=None)
    result = reject_fact(conn, cmd)
    if not result.accepted:
        return JSONResponse(status_code=409,
                            content={"detail": _deny_to_detail(result.denied_reason)})
    # A pre-VERIFIED reject has no projection (no-op); a REVERIFY/STALE reject demotes (E3).
    return {"governance_status": "REJECTED", "category": body.category,
            "operational_projection": "demoted"}


@router.post("/governance/semantic-bindings/{fact_key}/reverify",
             dependencies=[Depends(require_confirmer)])
def reverify_semantic_binding(fact_key: str, body: ReverifySemanticBindingRequest, conn: _Conn,
                              identity: _Identity) -> dict:
    """Reopen a fresh re-verification cycle on a VERIFIED binding (VERIFIED → REVERIFY), demoting
    the projection until a DIFFERENT authorized human re-confirms via the confirm route. Reuses the
    overlay's own expiry/reverify transition — never hand-writes fact state."""
    del body  # a bounded reviewer note field is accepted for surface symmetry; not persisted here
    _load_semantic_binding_context_or_404(conn, fact_key)  # 404 opaque before any state change
    result = request_reverify(conn, fact_key=fact_key, actor=identity)
    if not result["accepted"]:
        return JSONResponse(status_code=409,
                            content={"detail": _deny_to_detail(result["denied_reason"])})
    return {"governance_status": result["governance_status"],
            "operational_projection": result["operational_projection"]}


@router.post("/governance/semantic-bindings/{fact_key}/withdraw",
             dependencies=[Depends(require_confirmer)])
def withdraw_semantic_binding(fact_key: str, body: WithdrawSemanticBindingRequest, conn: _Conn,
                              identity: _Identity) -> dict:
    """Retire a VERIFIED binding → REJECTED + demote (restore the file entity / demote the currency
    edge). Dispatches through the overlay reject_fact after the sanctioned reverify reopen."""
    _load_semantic_binding_context_or_404(conn, fact_key)
    result = withdraw_binding(conn, fact_key=fact_key, actor=identity,
                              category=body.category, note=_clean(body.note))
    if not result["accepted"]:
        return JSONResponse(status_code=409,
                            content={"detail": _deny_to_detail(result["denied_reason"])})
    return {"governance_status": "REJECTED", "category": body.category,
            "operational_projection": result["operational_projection"]}


@router.post("/governance/semantic-bindings/{fact_key}/correct",
             dependencies=[Depends(require_confirmer)])
def correct_semantic_binding(fact_key: str, body: CorrectSemanticBindingRequest, conn: _Conn,
                             identity: _Identity) -> dict:
    """Correct a VERIFIED binding: retire the prior value and open a NEW proposal for the corrected
    value — requiring a DIFFERENT authorized human to confirm (four-eyes: the correcting human is
    the proposer). The corrected value is validated against the E1 write gate before any retire."""
    _load_semantic_binding_context_or_404(conn, fact_key)
    result = correct_binding(conn, fact_key=fact_key, actor=identity,
                             value=body.value, note=_clean(body.note))
    if not result["accepted"]:
        if result.get("rollback_required"):
            # The corrected value passed the write gate but the re-proposal was denied (e.g. a
            # sticky fingerprint) AFTER the prior value was retired — RAISE so get_conn rolls the
            # retire back and the binding is left unharmed (atomic correct).
            raise HTTPException(status_code=409, detail=_deny_to_detail(result["denied_reason"]))
        return JSONResponse(status_code=409,
                            content={"detail": _deny_to_detail(result["denied_reason"])})
    return {"governance_status": "PROPOSED", "fact_key": result["fact_key"],
            "proposed_event_id": result["proposed_event_id"],
            "requires_distinct_confirmer": True,
            "operational_projection": result["operational_projection"]}
