"""Release-A profile Task 3 — the table-asset profile surface (flag-gated).

``GET/PUT /catalog/asset-profiles/{source}/{object_ref:path}``.

**Why the distinct ``asset-profiles`` prefix (review D, Release C route-collision finding):**
``assets.py`` already owns ``/catalog/assets/{source}/{object_ref:path}`` with a GREEDY path
converter, so nesting a literal under it (``/catalog/assets/.../profile``) is ambiguous for any
object_ref that itself ends in ``/profile`` — the greedy converter would swallow the literal.
A separate first-segment prefix cannot collide with the greedy route at all.

**Flag (D8):** every route here 404s while ``FEATUREGEN_DATASET_PROFILES`` is off — the surface is
hidden, not forbidden, so the flag-off API is byte-identical to today's.

**AuthZ (D12.7):** GET is ``catalog:read`` + the derived anchor visibility (a hidden table is
indistinguishable from a missing one — 404, no existence oracle). PUT is the ``data_owner``
proposal surface: it is gated by the SAME ``catalog:write`` permission the upload surface grants
and writes ordinary bounded ``HUMAN/PROPOSED`` field evidence — displayable (labeled, usable —
the no-"blocked" rule) but never load-bearing; promotion to load-bearing stays with the EXISTING
four-eyes confirm on the field-decision route (``require_confirmer`` + distinct subject).

**Locking:** the PUT takes the ingest SOURCE advisory lock first, then the one table-ref lock —
the ``field_correction.py`` ordering, so the two writers can never deadlock. Be aware the source
lock is held by ``ingest_upload`` THROUGH LLM enrichment, so a PUT issued during an ingest of the
same catalog blocks until that ingest commits; that is deliberate (serializing against the
whole-graph DELETE+rebuild), not an oversight.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from featuregen.api.deps import (
    get_conn,
    get_feature_gen_conn,
    get_identity,
    require_catalog_read,
    require_catalog_write,
)
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.column_authority import logical_ref_of
from featuregen.overlay.upload.dataset_profiles import build_dataset_profile
from featuregen.overlay.upload.field_correction import (
    FieldCorrectionError,
    _lock_key,
    propose_profile_field,
)
from featuregen.overlay.upload.field_resolution import resolve_and_project
from featuregen.overlay.upload.ingest import ingest_source_lock_key
from featuregen.overlay.upload.object_ref import normalize_source_name
from featuregen.overlay.upload.profile_store import current_catalog_profile_revision_id
from featuregen.overlay.upload.profile_vocab import (
    dataset_profiles_enabled,
    normalize_authority_role,
    normalize_temporal_storage_model,
)
from featuregen.overlay.upload.read_scope import allowed_sensitivities

router = APIRouter()
_RRConn = Annotated[psycopg.Connection, Depends(get_feature_gen_conn, scope="function")]
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]

# Bounded PUT fields: the three NEW profile fields only (profile plan Task 1's propose_advisory
# scope). Everything else keeps its existing surface: descriptions via the four-eyes
# field-decision route; table_role/primary_entity/event_or_snapshot stay source/LLM-authored.
_PUT_FIELDS = ("business_context", "authority_role", "temporal_storage_model")
_MAX_BUSINESS_CONTEXT = 4000


def require_dataset_profiles() -> None:
    """404 (not 403) while the flag is off: the surface does not exist, so the flag-off API stays
    byte-identical and un-probeable (D8)."""
    if not dataset_profiles_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


def _visible_table_anchor(conn, source: str, object_ref: str, roles) -> None:
    """404 unless ``(source, object_ref)`` is a TABLE node the caller may see (derived scope —
    visible iff >= 1 visible column; D11). A COLUMN ref is a 400: profiles are table-scoped."""
    row = conn.execute(
        "SELECT kind FROM graph_node WHERE catalog_source = %s AND object_ref = lower(%s)",
        (source, object_ref)).fetchone()
    if row is not None and row[0] == "column":
        raise HTTPException(status_code=400,
                            detail="asset profiles are table-scoped; pass the table ref")
    allowed = allowed_sensitivities(roles)
    visible = conn.execute(
        "SELECT 1 FROM graph_node gn WHERE gn.catalog_source = %s "
        "AND gn.object_ref = lower(%s) AND gn.kind = 'table' AND EXISTS("
        "  SELECT 1 FROM graph_node c WHERE c.catalog_source = gn.catalog_source "
        "  AND c.kind = 'column' AND c.table_name = gn.table_name "
        "  AND COALESCE(c.visible_requires, '{}') <@ %s)",
        (source, object_ref, allowed)).fetchone()
    if visible is None:
        raise HTTPException(status_code=404, detail="asset not found")


def _assemble(conn, source: str, logical_ref: str):
    profile = build_dataset_profile(
        conn, source=source, dataset_logical_ref=logical_ref,
        catalog_profile_revision_id=current_catalog_profile_revision_id(conn, source))
    if profile is None:
        raise HTTPException(status_code=404, detail="asset not found")
    return profile


@router.get("/catalog/asset-profiles/{source}/{object_ref:path}",
            dependencies=[Depends(require_dataset_profiles), Depends(require_catalog_read)])
def get_asset_profile(source: str, object_ref: str, conn: _RRConn, identity: _Identity) -> dict:
    """The assembled effective semantic profile for one TABLE (§6.3), under one REPEATABLE READ
    snapshot. Proposed values are returned with their authority triple — visible and labeled,
    never hidden pending review."""
    try:
        src = normalize_source_name(source)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _visible_table_anchor(conn, src, object_ref, identity.role_claims)
    logical_ref = logical_ref_of(conn, src, object_ref.lower())
    return asdict(_assemble(conn, src, logical_ref))


class AssetProfilePutRequest(BaseModel):
    """The data_owner proposal body. ``expected_dataset_profile_hash`` is the aggregate CAS anchor:
    the hash the caller assembled the profile at; ANY drift 409s before a single write."""

    expected_dataset_profile_hash: str = Field(min_length=1, max_length=200)
    business_context: str | None = Field(default=None, max_length=_MAX_BUSINESS_CONTEXT)
    authority_role: str | None = Field(default=None, max_length=100)
    temporal_storage_model: str | None = Field(default=None, max_length=100)
    note: str | None = Field(default=None, max_length=2000)


@router.put("/catalog/asset-profiles/{source}/{object_ref:path}",
            dependencies=[Depends(require_dataset_profiles), Depends(require_catalog_write)])
def put_asset_profile(
    source: str, object_ref: str, body: AssetProfilePutRequest, conn: _Conn,
    identity: _Identity,
) -> dict:
    """Propose profile values for one table: validates EVERY field + the aggregate
    ``dataset_profile_hash`` first, takes the source-then-table-ref locks (module docstring),
    appends ordinary ``HUMAN/PROPOSED`` field evidence + re-resolves/projects in ONE transaction,
    and returns the NEW assembled ``dataset_profile_hash``. A zero-row display projection is a
    server error, never a silent 200."""
    try:
        src = normalize_source_name(source)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 1. FULL field validation before any lock or write.
    writes: dict[str, str] = {}
    if body.business_context is not None:
        text = body.business_context.strip()
        if not text:
            raise HTTPException(status_code=400, detail="business_context must be non-empty")
        writes["business_context"] = text
    if body.authority_role is not None:
        norm = normalize_authority_role(body.authority_role)
        if norm is None:
            raise HTTPException(status_code=400,
                                detail=f"unknown authority_role {body.authority_role!r}")
        writes["authority_role"] = norm
    if body.temporal_storage_model is not None:
        norm = normalize_temporal_storage_model(body.temporal_storage_model)
        if norm is None:
            raise HTTPException(
                status_code=400,
                detail=f"unknown temporal_storage_model {body.temporal_storage_model!r}")
        writes["temporal_storage_model"] = norm
    if not writes:
        raise HTTPException(status_code=400, detail="no profile fields to write")

    # 2. Anchor exists + visible (404 otherwise; no existence oracle), then the real logical ref.
    _visible_table_anchor(conn, src, object_ref, identity.role_claims)
    logical_ref = logical_ref_of(conn, src, object_ref.lower())

    # 3. Locks — SOURCE first, then the table-ref key (field_correction ordering; no deadlock
    #    against ingest). NB: an in-flight ingest holds the source lock through LLM enrichment.
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (ingest_source_lock_key(src),))
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_key(logical_ref),))

    # 4. Aggregate CAS under the locks: the profile the caller edited must still be current.
    current = _assemble(conn, src, logical_ref)
    if current.dataset_profile_hash != body.expected_dataset_profile_hash:
        raise HTTPException(
            status_code=409,
            detail="the profile changed since you loaded it — refresh and retry")

    # 5. Ordinary bounded HUMAN/PROPOSED evidence, through the ONE seam that owns HUMAN evidence
    #    writes and idempotency-key discipline ([F10] — this route must never key evidence itself;
    #    it collided with the correction command's private replay namespace when it did). The seam
    #    carries the [F1] self-supersession and same-value idempotency, and reports whether the
    #    ACTIVE set changed so exactly the moved fields re-project.
    written: list[str] = []
    for field, value in writes.items():
        try:
            if propose_profile_field(conn, logical_ref=logical_ref, field=field, value=value,
                                     actor=identity, note=body.note):
                written.append(field)
        except FieldCorrectionError as exc:   # defense in depth: step 1 already bounded every field
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    if written:
        resolve_and_project(conn, source=src, logical_refs=[logical_ref], fields=written)

    # 6. Prove the display projection LANDED (zero-row projection = error, not 200): for each
    #    written field with a graph display column, the table node must now show the re-resolved
    #    display value.
    updated = _assemble(conn, src, logical_ref)
    check_cols = [f for f in written if f in ("authority_role", "temporal_storage_model")]
    if check_cols:
        row = conn.execute(
            "SELECT authority_role, temporal_storage_model FROM graph_node "
            "WHERE catalog_source = %s AND object_ref = lower(%s) AND kind = 'table'",
            (src, object_ref)).fetchone()
        projected = dict(zip(("authority_role", "temporal_storage_model"), row or (None, None),
                             strict=True))
        for field in check_cols:
            expected_display = getattr(updated, field).display
            expected_value = expected_display.value if expected_display is not None else None
            if projected[field] != expected_value:
                raise HTTPException(
                    status_code=500,
                    detail=f"profile write for {field!r} did not project onto the table node")
    return {
        "written": sorted(written),
        "dataset_profile_hash": updated.dataset_profile_hash,
        "profile": asdict(updated),
    }
