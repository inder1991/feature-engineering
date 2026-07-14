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
human-readable detail; every other denial reason passes through verbatim.

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

import logging
from typing import Annotated, Literal

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from featuregen.api.deps import _auth_stub_enabled, get_conn, get_identity, require_confirmer
from featuregen.config import get_settings
from featuregen.contracts.envelopes import Command, IdentityEnvelope
from featuregen.overlay.confirmation_commands import confirm_fact, reject_fact
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.join_governance import (
    JoinGovernanceNotFound,
    list_open_approved_join_proposals,
    load_join_confirmation_context,
    project_verified_join,
    read_join_approvals,
)
from featuregen.overlay.upload.table_fact_governance import (
    TableFactGovernanceNotFound,
    list_open_table_fact_proposals_governance,
    load_table_fact_confirmation_context,
    project_verified_table_fact,
)
from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter
from featuregen.security.audit import record_security_event

logger = logging.getLogger(__name__)

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


def _clean(s: str | None) -> str | None:
    """Strip; an empty/whitespace-only note becomes None (absent, not '')."""
    s = (s or "").strip()
    return s or None


def _audit_governance_denial(identity: IdentityEnvelope, action: str,
                             reason: str | None) -> None:
    """Durably record a governance denial (same-subject re-confirm, four-eyes, not-owner, stale
    CAS...). The overlay already wrote a COMMAND_DENIED row via `_deny_audited` — but on the
    REQUEST connection, and the 409 the route raises next makes `get_conn` roll that back, so a
    denied SoD-bypass probe would leave ZERO durable trace. Mirror `deps.audit_access_denied`
    (the 403 path): write on a SEPARATE committing connection that survives the request rollback.
    Best-effort — an audit failure must never turn a correct 409 into a 500 — and skipped under
    the dev auth stub (a production control; a separate committing connection would pollute the
    rolled-back test DB)."""
    if _auth_stub_enabled():
        return
    dsn = get_settings().dsn
    if not dsn:
        return
    try:
        with psycopg.connect(dsn) as conn:   # own tx, committed on exit — survives the 409 rollback
            record_security_event(conn, event_type="COMMAND_DENIED", actor=identity,
                                  attempted_action=action, decision="denied", reason=reason,
                                  aggregate="overlay_fact")
    except Exception:  # noqa: BLE001 — never let an audit failure mask the (correct) denial
        logger.warning("failed to durably record governance denial for %s", action, exc_info=True)


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
    return {"source": source.strip().lower(), "proposals": proposals, "next_cursor": None}


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
        # Durable BEFORE the 409: the overlay's own denial audit rides the request tx, which
        # the 409 rolls back (get_conn) — re-record on a separate committing connection.
        _audit_governance_denial(identity, f"confirm_fact approved_join {fact_key}",
                                 result.denied_reason)
        raise HTTPException(status_code=409, detail=_deny_to_detail(result.denied_reason))
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
        # Durable BEFORE the 409 — see _audit_governance_denial (the request tx rolls back).
        _audit_governance_denial(identity, f"reject_fact approved_join {fact_key}",
                                 result.denied_reason)
        raise HTTPException(status_code=409, detail=_deny_to_detail(result.denied_reason))
    return {"governance_status": "REJECTED", "category": body.category}


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
        # Durable BEFORE the 409 — see _audit_governance_denial (the request tx rolls back).
        _audit_governance_denial(identity, f"confirm_fact {ctx['fact_type']} {fact_key}",
                                 result.denied_reason)
        raise HTTPException(status_code=409, detail=_deny_to_detail(result.denied_reason))
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
        # Durable BEFORE the 409 — see _audit_governance_denial (the request tx rolls back).
        _audit_governance_denial(identity, f"reject_fact {ctx['fact_type']} {fact_key}",
                                 result.denied_reason)
        raise HTTPException(status_code=409, detail=_deny_to_detail(result.denied_reason))
    return {"governance_status": "REJECTED", "category": body.category}
