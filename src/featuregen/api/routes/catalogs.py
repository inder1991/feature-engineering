"""``GET /catalogs`` — the catalog pick-list every source-keyed surface needed and none had.

Every governance / readiness / semantics route is keyed by ``{source}``, and the Governance Review
screen consequently opened on an empty text input: nothing rendered until the operator guessed a slug
(the live ones are ``cib`` and ``ftr``). This route enumerates the slugs the caller is allowed to know
about, so the screen can offer them.

READ-SCOPED, and the scope is DERIVED. There is no catalog-level visibility in this system; the only
mechanism is column-level (migration 1032's ``visible_requires``). So a catalog is returned iff the
caller can see at least one column in it, and a caller who can see none does not learn it exists — the
listing is a ``GROUP BY`` over read-scoped column rows, so a fully-hidden catalog yields no group at
all rather than a filtered-out row (see :mod:`overlay.upload.catalogs`). Roles come from the
authenticated session, NEVER the request, so this surface cannot be asked for someone else's scope.

Gated by ``catalog:read`` — the same permission as its ``/sources/{source}/...`` peers, since knowing a
catalog exists is strictly less than reading inside it.

The payload is the slug plus scope-honest ``tables`` / ``columns`` counts. With
``FEATUREGEN_DATASET_PROFILES`` ON (Release-A profile Task 3) each entry ADDITIONALLY carries
``display_name`` (from the current catalog-narrative revision — the first real display-name source
this system has had) and ``has_profile``; with the flag OFF the payload stays byte-identical to the
pre-profile shape. Pending-governance counts stay deliberately absent (the queue surface owns those).

``GET/PUT /catalogs/{source}/profile`` (flag-gated) read/author the catalog NARRATIVE — immutable
revisions + a CAS current pointer (migration 1047). The PUT carries ``expected_pointer_version`` IN
THE BODY (the ``governance.py`` convention; the optional-gate fail-open is the anti-pattern) and
409s on any CAS miss. Reads are scoped by the SAME derived visibility as the listing, so a
narrative can never reveal a catalog whose every column is hidden (no orphan existence oracle).
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from featuregen.api.deps import (
    get_conn,
    get_identity,
    require_catalog_read,
    require_catalog_write,
)
from featuregen.api.routes.profiles import require_dataset_profiles
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.catalog_profiles import (
    CatalogProfileError,
    build_catalog_profile_revision,
    parse_narrative_payload,
)
from featuregen.overlay.upload.catalogs import list_visible_catalogs
from featuregen.overlay.upload.object_ref import normalize_source_name
from featuregen.overlay.upload.profile_store import (
    CatalogProfileConflict,
    catalog_visible,
    current_catalog_profile,
    record_catalog_profile_revision,
    set_current_catalog_profile,
)
from featuregen.overlay.upload.profile_vocab import dataset_profiles_enabled

router = APIRouter()
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]


@router.get("/catalogs", dependencies=[Depends(require_catalog_read)])
def list_catalogs(conn: _Conn, identity: _Identity) -> dict:
    """The catalogs this caller may see: slug + read-scoped table/column counts, slug-sorted.

    Empty list when the caller can see nothing — including when nothing has been uploaded. Never a
    404 and never an error: "no catalogs you may see" and "no catalogs" are deliberately the same
    answer, so the response cannot be used to probe for hidden catalogs.
    """
    catalogs = list_visible_catalogs(conn, roles=identity.role_claims)
    entries = [asdict(c) for c in catalogs]
    if dataset_profiles_enabled() and entries:
        # One query over the visible slugs only (the derived scope already filtered them), so the
        # narrative join can never resurrect a hidden catalog. Flag OFF: byte-identical payload.
        rows = conn.execute(
            "SELECT c.catalog_source, r.display_name FROM catalog_profile_current c "
            "JOIN catalog_profile_revision r ON r.revision_id = c.revision_id "
            "WHERE c.catalog_source = ANY(%s)",
            ([e["source"] for e in entries],)).fetchall()
        names = {r[0]: r[1] for r in rows}
        for entry in entries:
            entry["display_name"] = names.get(entry["source"])
            entry["has_profile"] = entry["source"] in names
    return {"catalogs": entries}


@router.get("/catalogs/{source}/profile",
            dependencies=[Depends(require_dataset_profiles), Depends(require_catalog_read)])
def get_catalog_profile(source: str, conn: _Conn, identity: _Identity) -> dict:
    """The CURRENT catalog narrative + its CAS pointer version (0 == no narrative yet, the version
    a first PUT must carry). 404 for a catalog the caller cannot see (derived scope)."""
    try:
        src = normalize_source_name(source)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not catalog_visible(conn, src, roles=identity.role_claims):
        raise HTTPException(status_code=404, detail="catalog not found")
    current = current_catalog_profile(conn, src)
    if current is None:
        return {"source": src, "pointer_version": 0, "profile": None}
    revision, pointer = current
    return {"source": src, "pointer_version": pointer.pointer_version,
            "profile": asdict(revision)}


class CatalogProfilePutRequest(BaseModel):
    """The narrative author body. ``expected_pointer_version`` rides IN THE BODY (repo convention,
    ``governance.py``): 0 claims first-write; >= 1 names the exact version read. Any CAS miss 409s.
    Field bounds are re-validated server-side by ``catalog_profiles`` before any write."""

    expected_pointer_version: int = Field(ge=0)
    display_name: str | None = Field(default=None, max_length=1000)
    description: str | None = Field(default=None, max_length=8000)
    business_context: str | None = Field(default=None, max_length=8000)
    business_domains: list[str] = Field(default_factory=list, max_length=64)


@router.put("/catalogs/{source}/profile",
            dependencies=[Depends(require_dataset_profiles), Depends(require_catalog_write)])
def put_catalog_profile(
    source: str, body: CatalogProfilePutRequest, conn: _Conn, identity: _Identity,
) -> dict:
    """Author a catalog-narrative revision (``catalog:write`` — the data_owner surface) and advance
    the CAS pointer. The revision is HUMAN/PROPOSED — descriptive prose with its authority labeled;
    it never defaults any dataset's role, authority or temporal model. 409 on a CAS miss."""
    try:
        src = normalize_source_name(source)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not catalog_visible(conn, src, roles=identity.role_claims):
        raise HTTPException(status_code=404, detail="catalog not found")
    try:
        fields = parse_narrative_payload({
            "display_name": body.display_name, "description": body.description,
            "business_context": body.business_context,
            "business_domains": body.business_domains})
        revision = build_catalog_profile_revision(
            catalog_source=src, producer_ref=identity.subject, **fields)
    except CatalogProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    record_catalog_profile_revision(conn, revision)
    try:
        new_version = set_current_catalog_profile(
            conn, catalog_source=src, revision_id=revision.revision_id,
            expected_pointer_version=body.expected_pointer_version)
    except CatalogProfileConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"source": src, "revision_id": revision.revision_id,
            "pointer_version": new_version}
