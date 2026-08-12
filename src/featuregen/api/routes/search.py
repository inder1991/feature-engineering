from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, Query

from featuregen.api.deps import get_conn, get_identity, require_catalog_read
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.config import current_overlay_config
from featuregen.overlay.upload.search import SearchResult, column_facets, search

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
    # The projected display axis — a facet in its own right, beside (never instead of) the
    # enforcement tag above. Read scope gates its counts like every other facet.
    sensitivity_display: _Facet,
    additivity: _Facet,
    entity: _Facet,
    kind: _Facet,
    # Release-A profile facets (profile Task 5 + D13.1/D13.2). Always ACCEPTED as params, but only
    # APPLIED while `FEATUREGEN_DATASET_PROFILES` is on — `search.column_facets()` is the one
    # definition of what is active, and a filter it does not name is ignored rather than erroring.
    # A 422 here would make the flag state probeable from an unauthenticated-shaped request; being
    # ignored is the same answer the surface gives for any facet it does not have.
    data_role: _Facet,
    authority_role: _Facet,
    temporal_storage_model: _Facet,
    bian_path: _Facet,
    process_path: _Facet,
    sub_domain: _Facet,
    q: str = "",
    grain: bool = False,
    as_of: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    # Windows the hits only; `total` and the facet counts still describe the whole set, so a client
    # can tell from any page whether another exists. No upper bound: paging past the end returns an
    # empty page, which is the honest answer and cheaper than rejecting it.
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SearchResult:
    # AND across facet groups, OR within one. q is optional — an empty q browses ALL fresh,
    # read-scoped rows. Roles come from the authenticated session — NEVER from the request (M6
    # read-scope); freshness fail-closed and read-scope are enforced inside search().
    active = column_facets()
    filters: dict[str, list[str]] = {
        name: values
        for name, values in (
            ("source", source), ("domain", domain), ("sensitivity", sensitivity),
            ("sensitivity_display", sensitivity_display),
            ("additivity", additivity), ("entity", entity), ("kind", kind),
            ("data_role", data_role), ("authority_role", authority_role),
            ("temporal_storage_model", temporal_storage_model), ("bian_path", bian_path),
            ("process_path", process_path), ("sub_domain", sub_domain),
        )
        if values and name in active
    }
    if grain:
        filters["grain"] = ["true"]
    if as_of:
        filters["as_of"] = ["true"]
    # Freshness comes from the CONFIGURED drift SLA, the same one every other governed read uses
    # (`resolve.py`'s watermark check, `declarations.py`'s `participating_catalog_stale`). Before
    # this, the route passed nothing and `search()`'s own 24-hour default governed instead — a
    # second, private, non-configurable freshness window nobody could see. On a deployment whose
    # watermark only advances at ingest (there is no drift scanner), raising
    # OVERLAY_DRIFT_FRESHNESS_SLA_MIN kept every governed read working while search silently went
    # empty the moment a catalog crossed 24 hours. Two windows is the defect; this removes one.
    return search(conn, q, now=datetime.now(UTC), roles=identity.role_claims,
                  fresh_within=current_overlay_config().drift_freshness_sla,
                  filters=filters, limit=limit, offset=offset)
