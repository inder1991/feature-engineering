"""Catalog lineage graph view (additive): GET /graph/lineage.

Permission mechanics are exactly search's post-RBAC pattern: catalog:read gates the route,
roles come from the authenticated session (NEVER the request), and read-scope hard-filters
sensitivity-tagged nodes. One addition on the same axis: the features layer surfaces the
feature registry, which feature:read gates everywhere else (GET /features et al.), so a
caller without feature:read gets the graph WITHOUT that layer — absent, not a 403, exactly
how read-scope treats columns.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query

from featuregen.api.deps import get_conn, get_identity, require_catalog_read
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.identity.permissions import FEATURE_READ, has_permission
from featuregen.overlay.upload.lineage import LAYERS, lineage_graph

router = APIRouter()


@router.get("/graph/lineage", dependencies=[Depends(require_catalog_read)])
def lineage(
    ref: str,
    source: str,
    conn: Annotated[psycopg.Connection, Depends(get_conn, scope="function")],
    identity: Annotated[IdentityEnvelope, Depends(get_identity)],
    direction: Literal["up", "down", "both"] = "both",
    depth: Annotated[int, Query(ge=1, le=3)] = 1,
    layers: str = "joins,entity,features",
) -> dict:
    """The lineage graph around one anchor (a table or column ref in `source`).

    404 for an unknown anchor — including one hidden by read-scope, so absence is
    indistinguishable from nonexistence. 200 with the anchor's own table unit when it
    exists but has no edges. Stale sources are SHOWN, flagged stale (unlike search).
    """
    requested = {token.strip() for token in layers.split(",") if token.strip()}
    if not requested or requested - LAYERS:
        raise HTTPException(status_code=422,
                            detail="layers must be a comma-separated subset of "
                                   "joins,entity,features")
    if not has_permission(identity.role_claims, FEATURE_READ):
        requested -= {"features"}   # the features layer is registry data; feature:read gates it
    graph = lineage_graph(conn, source, ref, now=datetime.now(UTC), direction=direction,
                          depth=depth, layers=requested, roles=identity.role_claims)
    if graph is None:
        raise HTTPException(status_code=404,
                            detail=f"unknown object {ref!r} in source {source!r}")
    return graph
