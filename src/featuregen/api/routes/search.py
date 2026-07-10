from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, Query

from featuregen.api.deps import get_conn, get_identity, require_catalog_read
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.search import SearchResult, search

router = APIRouter()

# Repeated-value query param: ?source=deposits&source=cards. default_factory (not a mutable `= []`)
# gives each request a fresh empty list, and keeping Query inside Annotated avoids a call-in-default.
_Facet = Annotated[list[str], Query(default_factory=list)]


@router.get("/search", dependencies=[Depends(require_catalog_read)])
def search_catalog(
    conn: Annotated[psycopg.Connection, Depends(get_conn, scope="function")],
    identity: Annotated[IdentityEnvelope, Depends(get_identity)],
    source: _Facet,
    domain: _Facet,
    sensitivity: _Facet,
    additivity: _Facet,
    entity: _Facet,
    kind: _Facet,
    q: str = "",
    grain: bool = False,
    as_of: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> SearchResult:
    # AND across facet groups, OR within one. q is optional — an empty q browses ALL fresh,
    # read-scoped rows. Roles come from the authenticated session — NEVER from the request (M6
    # read-scope); freshness fail-closed and read-scope are enforced inside search().
    filters: dict[str, list[str]] = {
        name: values
        for name, values in (
            ("source", source), ("domain", domain), ("sensitivity", sensitivity),
            ("additivity", additivity), ("entity", entity), ("kind", kind),
        )
        if values
    }
    if grain:
        filters["grain"] = ["true"]
    if as_of:
        filters["as_of"] = ["true"]
    return search(conn, q, now=datetime.now(UTC), roles=identity.role_claims,
                  filters=filters, limit=limit)
