"""Delivery F0 Task 2 — the asset READ MODEL route + Delivery F field-correction command.

``GET /catalog/assets/{source}/{object_ref:path}`` returns the bounded sections about ONE catalog
asset (:func:`overlay.upload.asset_detail.build_asset_detail`) — identity, effective_metadata,
evidence, relationships, readiness, history, actions — assembled under ONE ``REPEATABLE READ``
transaction so all sections describe one torn-free snapshot. Gated by ``catalog:read``.

Read-scope: the assembler loads the anchor under the caller's sensitivity scope, so a hidden anchor
(a sensitivity these roles can't see) is indistinguishable from a missing one — both return ``None``,
which this route maps to 404. No existence leak. The ``ETag`` header carries the snapshot's
consistency token (also embedded in the body) so a client can revalidate.

``POST /catalog/assets/{source}/{object_ref:path}/fields/{field}/decisions`` (Delivery F) is the
GENERIC scalar field-correction command over a ``field_evidence``-governed field — confirm_existing /
propose_override / confirm_override / reject, with CAS concurrency (409, fail-closed — including on a
concurrent evidence append), four-eyes on a load-bearing confirm, and a ``human_editable`` policy
opt-in (see :mod:`overlay.upload.field_correction`). Gated by ``require_confirmer`` — the SAME raw
``platform-admin`` confirmer claim the peer join / table-fact / semantic-binding surfaces authorize
on (the upload-context source owner resolves to the platform-admin governance queue); the write never
trusts a client-supplied authority label. A four-eyes / authz denial RETURNS its 4xx (never raises) so
``get_conn`` COMMITS the ``COMMAND_DENIED`` audit row (audit I-3).
"""
from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from featuregen.api.deps import (
    get_conn,
    get_feature_gen_conn,
    get_identity,
    require_catalog_read,
    require_confirmer,
)
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.asset_detail import build_asset_detail
from featuregen.overlay.upload.field_correction import (
    FieldCorrectionError,
    apply_field_correction,
)

router = APIRouter()

# The RR read connection (Delivery C0): every section reads ONE consistent catalog snapshot.
_RRConn = Annotated[psycopg.Connection, Depends(get_feature_gen_conn, scope="function")]
# The read/write transaction for the correction command (commit-on-return, so an audited deny persists).
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]
_Include = Annotated[
    list[str] | None,
    Query(description="Sections to build; repeatable. Default: all F0 sections."),
]


@router.get("/catalog/assets/{source}/{object_ref:path}",
            dependencies=[Depends(require_catalog_read)])
def get_asset_detail(
    source: str,
    object_ref: str,
    conn: _RRConn,
    identity: Annotated[IdentityEnvelope, Depends(get_identity)],
    response: Response,
    include: _Include = None,
) -> dict:
    """The bounded asset detail for ``(source, object_ref)``. Roles come from the authenticated
    session (NEVER the request), so read-scope is enforced from the real identity. A hidden or
    absent anchor -> 404 (no existence leak); otherwise the assembled sections + version + token."""
    detail = build_asset_detail(
        conn, source=source, object_ref=object_ref, roles=identity.role_claims, include=include,
        identity=identity,
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="asset not found")
    response.headers["ETag"] = f'"{detail["consistency_token"]}"'
    return detail


class FieldDecisionRequest(BaseModel):
    """The generic field-correction command body. ``expected_*`` are the CAS anchor the caller loaded
    the field at; ANY drift 409s (fail-closed). No authority label is accepted — authority is the
    server-rechecked ``platform-admin`` confirmer claim + four-eyes, never a client field."""

    action: str = Field(description="confirm_existing | propose_override | confirm_override | reject")
    selected_evidence_ids: list[str] = Field(default_factory=list)
    replacement_value: str | None = Field(default=None, max_length=8000)
    reason: str | None = Field(default=None, max_length=2000)
    idempotency_key: str = Field(min_length=1, max_length=200)
    expected_latest_decision_id: str | None = Field(default=None)
    expected_evidence_set_hash: str = Field(min_length=1, max_length=200)
    expected_policy_version: str = Field(min_length=1, max_length=200)


@router.post("/catalog/assets/{source}/{object_ref:path}/fields/{field}/decisions",
             dependencies=[Depends(require_confirmer)])
def post_field_decision(
    source: str, object_ref: str, field: str, body: FieldDecisionRequest, conn: _Conn,
    identity: _Identity,
) -> dict:
    """Apply one scalar field-correction over a ``human_editable`` ``field_evidence`` field. The route
    gate is the ``platform-admin`` confirmer claim (server-side, never a body label); four-eyes, CAS,
    bounds, and the ``human_editable`` opt-in are enforced in
    :func:`overlay.upload.field_correction.apply_field_correction`. A benign pre-write refusal
    (unregistered / not-editable / 404 / bounds / CAS-409) raises; a four-eyes/authz denial RETURNS a
    403 so the ``COMMAND_DENIED`` audit row commits."""
    try:
        result = apply_field_correction(
            conn, source=source, object_ref=object_ref, field=field, action=body.action,
            actor=identity, idempotency_key=body.idempotency_key,
            expected_latest_decision_id=body.expected_latest_decision_id,
            expected_evidence_set_hash=body.expected_evidence_set_hash,
            expected_policy_version=body.expected_policy_version,
            selected_evidence_ids=body.selected_evidence_ids,
            replacement_value=body.replacement_value, note=body.reason)
    except FieldCorrectionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
    if not result["accepted"]:
        return JSONResponse(status_code=result["status_code"],
                            content={"detail": result["denied_reason"]})
    return result["body"]
