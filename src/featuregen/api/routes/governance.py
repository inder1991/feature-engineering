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
"""
from __future__ import annotations

from typing import Annotated, Literal

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from featuregen.api.deps import get_conn, get_identity, require_confirmer
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
        raise HTTPException(status_code=409, detail=_deny_to_detail(result.denied_reason))
    return {"governance_status": "REJECTED", "category": body.category}
