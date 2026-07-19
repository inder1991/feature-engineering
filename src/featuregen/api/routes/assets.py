"""Delivery F0 Task 2 — the asset READ MODEL route.

``GET /catalog/assets/{source}/{object_ref:path}`` returns the bounded sections about ONE catalog
asset (:func:`overlay.upload.asset_detail.build_asset_detail`) — identity, effective_metadata,
evidence, relationships, readiness, history, actions — assembled under ONE ``REPEATABLE READ``
transaction so all sections describe one torn-free snapshot. Gated by ``catalog:read``.

Read-scope: the assembler loads the anchor under the caller's sensitivity scope, so a hidden anchor
(a sensitivity these roles can't see) is indistinguishable from a missing one — both return ``None``,
which this route maps to 404. No existence leak. The ``ETag`` header carries the snapshot's
consistency token (also embedded in the body) so a client can revalidate.
"""
from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Response

from featuregen.api.deps import get_feature_gen_conn, get_identity, require_catalog_read
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.asset_detail import build_asset_detail

router = APIRouter()

# The RR read connection (Delivery C0): every section reads ONE consistent catalog snapshot.
_RRConn = Annotated[psycopg.Connection, Depends(get_feature_gen_conn, scope="function")]
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
        conn, source=source, object_ref=object_ref, roles=identity.role_claims, include=include
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="asset not found")
    response.headers["ETag"] = f'"{detail["consistency_token"]}"'
    return detail
