"""SE-4b — the bulk concept-confirmation routes: the funnel's read and its decisions.

``GET /governance/concept-confirmations`` serves the queue (proposed concepts grouped by
concept, load-bearing first) with per-column CAS anchors and the funnel metric. ``POST``
applies a BATCH of decisions — but bulk is a UI affordance, never a blanket fact: each item
runs through the EXISTING field-correction command (:func:`apply_field_correction` — four-eyes,
CAS, audit, one attributable decision event per column), and one column's 409 or denial never
touches its batch siblings. No new authority machinery exists here; this surface adds
THROUGHPUT to machinery the asset-detail screen already uses one field at a time.
"""
from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from featuregen.api.deps import get_conn, get_identity, require_confirmer
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.concept_confirmation_queue import concept_confirmation_queue
from featuregen.overlay.upload.field_correction import (
    FieldCorrectionError,
    apply_field_correction,
)

router = APIRouter()
_Conn = Annotated[psycopg.Connection, Depends(get_conn)]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]


@router.get("/governance/concept-confirmations",
            dependencies=[Depends(require_confirmer)])
def get_concept_confirmations(
    conn: _Conn, identity: _Identity, source: str,
    include_unreferenced: bool = Query(default=False),
) -> dict:
    queue = concept_confirmation_queue(
        conn, catalog_source=source, roles=identity.role_claims,
        include_unreferenced=include_unreferenced)
    return {
        "catalog_source": queue["catalog_source"],
        "unreferenced_groups_omitted": queue["unreferenced_groups_omitted"],
        "funnel": queue["funnel"],
        "groups": [{
            "concept": group.concept,
            "operand_reference_count": group.operand_reference_count,
            "columns": [{
                "object_ref": col.object_ref, "table": col.table, "column": col.column,
                "evidence_id": col.evidence_id, "producer": col.producer,
                "strength": col.strength,
                "latest_decision_id": col.latest_decision_id,
                "evidence_set_hash": col.evidence_set_hash,
                "policy_version": col.policy_version,
            } for col in group.columns],
        } for group in queue["groups"]],
    }


class ConceptDecisionItem(BaseModel):
    """One column's decision, with the CAS anchor the caller LOADED it at."""

    model_config = ConfigDict(extra="forbid")

    object_ref: str
    action: str = Field(description="confirm_existing | reject")
    evidence_id: str
    expected_latest_decision_id: str | None = None
    expected_evidence_set_hash: str
    expected_policy_version: str


class ConceptConfirmationBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    reason: str | None = Field(default=None, max_length=2000)
    items: list[ConceptDecisionItem] = Field(min_length=1, max_length=500)


@router.post("/governance/concept-confirmations",
             dependencies=[Depends(require_confirmer)])
def post_concept_confirmations(
    body: ConceptConfirmationBatch, conn: _Conn, identity: _Identity,
) -> dict:
    """Apply the batch, one attributable decision per column, fail-soft per item."""
    results = []
    accepted = 0
    for item in body.items:
        if item.action not in ("confirm_existing", "reject"):
            results.append({"object_ref": item.object_ref, "accepted": False,
                            "status_code": 400,
                            "detail": f"unknown action {item.action!r}"})
            continue
        try:
            outcome = apply_field_correction(
                conn, source=body.source, object_ref=item.object_ref, field="concept",
                action=item.action, actor=identity,
                idempotency_key=(f"ccq:{body.source}:{item.object_ref}:{item.action}:"
                                 f"{item.expected_evidence_set_hash[:16]}"),
                expected_latest_decision_id=item.expected_latest_decision_id,
                expected_evidence_set_hash=item.expected_evidence_set_hash,
                expected_policy_version=item.expected_policy_version,
                selected_evidence_ids=[item.evidence_id],
                note=body.reason)
        except FieldCorrectionError as exc:
            results.append({"object_ref": item.object_ref, "accepted": False,
                            "status_code": exc.status_code, "detail": exc.detail})
            continue
        if outcome["accepted"]:
            accepted += 1
            results.append({"object_ref": item.object_ref, "accepted": True,
                            "status_code": 200,
                            "decision_event_id": outcome["body"].get("decision_event_id")})
        else:
            results.append({"object_ref": item.object_ref, "accepted": False,
                            "status_code": outcome["status_code"],
                            "detail": outcome["denied_reason"]})
    # The funnel AFTER this batch — the number the SE-0 authority gate watches move.
    queue = concept_confirmation_queue(
        conn, catalog_source=body.source, roles=identity.role_claims)
    return {"results": results, "accepted_count": accepted,
            "declined_count": len(results) - accepted, "funnel": queue["funnel"]}


__all__ = ["router", "get_concept_confirmations", "post_concept_confirmations"]
