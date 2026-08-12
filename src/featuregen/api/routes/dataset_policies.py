"""Release-B Task 7 — the serving/temporal policy authoring surface (flag-gated).

``GET/PUT /catalog/dataset-policies/serving/{entity_id}/{need_role}/{serving_purpose}``
``GET/PUT /catalog/dataset-policies/temporal/{source}/{object_ref:path}``

**Why this surface exists at all.** "A policy store with no authoring surface is dead code" — and
worse than dead: the D12.7 escape hatch says the POLICY is the operational declaration for an
upload-only catalog, so without a way to write one, such a catalog stays unselectable or the
pressure goes back onto promoting an LLM proposal to load-bearing.

**Route prefix.** A distinct first segment, for the same reason ``profiles.py`` uses
``/catalog/asset-profiles``: ``assets.py`` owns ``/catalog/assets/{source}/{object_ref:path}`` with
a GREEDY converter, so a literal nested under it is ambiguous for any ref that itself ends in that
literal. The temporal route reuses the greedy converter for the object ref, which is why the
literal ``temporal`` precedes it rather than following it.

**Flag (D8).** Every route 404s while ``FEATUREGEN_SOURCE_TEMPORAL_SELECTION`` is off — the surface
is hidden, not forbidden, so the flag-off API is byte-identical to today's. The flag is FAIL-CLOSED
on its ``FEATUREGEN_DATASET_PROFILES`` dependency: requesting it without the dependency is an
invalid configuration, and these routes stay 404 rather than serving a half-enabled path.

**AuthZ.** GET is ``catalog:read`` plus read scope. PUT is the EXISTING platform-admin confirmer
gate (``require_confirmer`` — the raw ``platform-admin`` role claim), because a serving/temporal
policy is an OPERATIONAL declaration: it can decide which copy of a dataset answers a production
question. The subject that writes it is recorded on the revision (``authored_by``) and on the
pointer (``declared_by``), so a re-declaration that reuses an existing content-identical revision
still records who made it current.

**CAS.** ``expected_pointer_version`` is REQUIRED and rides in the BODY (the ``governance.py``
convention). It is deliberately not an optional query parameter: the optional-gate pattern in
``contract.py`` fails OPEN when omitted, which turns a concurrency guard into a suggestion. ``0``
CLAIMS first-write and is refused if a policy appeared meanwhile.

**Read scope.** A serving policy names datasets across catalogs, so it is returned only when the
caller can see EVERY dataset it names — a partial policy would hide an eligible candidate and read
as a narrower declaration than the one that governs, and revealing a hidden ref would be an
existence oracle. Same words for hidden and missing, always.

The TEMPORAL GET follows the identical whole-policy rule over the COLUMN refs its policy names. It
has to be decided here rather than upstream: the table anchor is visible under the D11 DERIVED scope
(a table is visible when >= 1 of its columns is), so a caller who may see one public column of a
mixed-visibility table reaches the route legitimately — and the policy body, which is nothing but a
list of column refs, would hand them the NAMES of the restricted columns. Withholding the offending
field alone would still answer "a column exists here that you may not see", one field at a time. So
the whole policy is withheld, in the same words as one that was never declared.
"""
from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from featuregen.api.deps import (
    get_conn,
    get_feature_gen_conn,
    get_identity,
    require_catalog_read,
    require_confirmer,
)
from featuregen.api.routes.profiles import _visible_table_anchor
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.column_authority import logical_ref_of
from featuregen.overlay.upload.object_ref import normalize_source_name
from featuregen.overlay.upload.serving_policy_store import (
    current_serving_policy,
    publish_serving_policy,
)
from featuregen.overlay.upload.source_selection import (
    DatasetNeedRole,
    DatasetServingPolicyRevisionV1,
    PolicyStoreConflict,
    SelectionError,
    ServingPurpose,
    assert_column_refs_readable,
    assert_dataset_refs_readable,
    human_declaration_provenance,
    source_temporal_selection_enabled,
)
from featuregen.overlay.upload.temporal_policy import (
    DatasetTemporalPolicyRevisionV1,
    TemporalSelectionKind,
)
from featuregen.overlay.upload.temporal_policy_store import (
    current_temporal_policy,
    load_bearing_temporal_model,
    publish_temporal_policy,
)

router = APIRouter()
_RRConn = Annotated[psycopg.Connection, Depends(get_feature_gen_conn, scope="function")]
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]

_MAX_REFS = 64

#: The selection kinds the temporal editor may offer, served ON the GET so a caller does not have
#: to know which module owns the vocabulary. It was declared at the bottom of this file with no
#: reader at all until Task 8 wired it here — a re-export nothing imports is not an API, it is a
#: constant nobody can find. Every member is now IMPLEMENTED: `valid_at_report_cutoff` was reuse,
#: `current_record` and `latest_snapshot_as_of` are Task 8's two new engines, and `explicit_only`
#: is the honest terminal state for a dataset that cannot answer the shape of question asked.
SELECTION_KINDS: tuple[str, ...] = tuple(k.value for k in TemporalSelectionKind)


def require_source_temporal_selection() -> None:
    """404 (not 403) while the flag is off or its dependency is unmet: the surface does not exist,
    so the flag-off API stays byte-identical and un-probeable (D8)."""
    if not source_temporal_selection_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


def _policy_key(need_role: str, serving_purpose: str) -> tuple[str, str]:
    try:
        return (DatasetNeedRole(need_role.strip().lower()).value,
                ServingPurpose(serving_purpose.strip().lower()).value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"need_role must be one of {sorted(m.value for m in DatasetNeedRole)} and "
                   f"serving_purpose one of {sorted(m.value for m in ServingPurpose)}") from exc


def _entity_id(raw: str) -> str:
    entity = raw.strip().lower()
    if not entity or len(entity) > 200:
        raise HTTPException(status_code=400, detail="entity_id must be a non-empty short token")
    return entity


def _serving_payload(conn, *, entity_id: str, need_role: str, serving_purpose: str,
                     roles) -> dict:
    current = current_serving_policy(conn, entity_id=entity_id, need_role=need_role,
                                     serving_purpose=serving_purpose)
    base = {"entity_id": entity_id, "need_role": need_role, "serving_purpose": serving_purpose}
    if current is None:
        # NOT an error and NOT a gap: most needs are served by an explicit request or a single
        # unambiguous candidate and never need a declaration at all.
        return {**base, "pointer_version": 0, "policy": None, "ambiguous": False}
    revision, pointer = current
    try:
        assert_dataset_refs_readable(
            conn, set(revision.eligible_dataset_refs) | set(revision.preferred_dataset_refs),
            roles=roles)
    except SelectionError as exc:
        # A policy the caller may not fully see is not shown at all (module docstring).
        raise HTTPException(status_code=404, detail="policy not found") from exc
    return {
        **base,
        "pointer_version": pointer.pointer_version,
        "declared_by": pointer.declared_by,
        "ambiguous": revision.ambiguous,
        "policy": {
            "revision_id": revision.revision_id,
            "content_hash": revision.content_hash,
            "eligible_dataset_refs": list(revision.eligible_dataset_refs),
            "preferred_dataset_refs": list(revision.preferred_dataset_refs),
            "provenance": revision.provenance.payload(),
        },
    }


@router.get("/catalog/dataset-policies/serving/{entity_id}/{need_role}/{serving_purpose}",
            dependencies=[Depends(require_source_temporal_selection),
                          Depends(require_catalog_read)])
def get_serving_policy(entity_id: str, need_role: str, serving_purpose: str, conn: _RRConn,
                       identity: _Identity) -> dict:
    """The CURRENT serving policy for one need + its CAS pointer version (0 == none yet, the
    version a first PUT must carry)."""
    role, purpose = _policy_key(need_role, serving_purpose)
    return _serving_payload(conn, entity_id=_entity_id(entity_id), need_role=role,
                            serving_purpose=purpose, roles=identity.role_claims)


class ServingPolicyPutRequest(BaseModel):
    """``expected_pointer_version`` is REQUIRED (module docstring): 0 claims first-write, >= 1 names
    the exact version read. Both ref lists are re-validated server-side — normalized, deduped,
    sorted, `preferred ⊆ eligible`, every ref readable — before anything is written."""

    expected_pointer_version: int = Field(ge=0)
    eligible_dataset_refs: list[str] = Field(min_length=1, max_length=_MAX_REFS)
    preferred_dataset_refs: list[str] = Field(default_factory=list, max_length=_MAX_REFS)


@router.put("/catalog/dataset-policies/serving/{entity_id}/{need_role}/{serving_purpose}",
            dependencies=[Depends(require_source_temporal_selection),
                          Depends(require_catalog_read), Depends(require_confirmer)])
def put_serving_policy(entity_id: str, need_role: str, serving_purpose: str,
                       body: ServingPolicyPutRequest, conn: _Conn, identity: _Identity) -> dict:
    """Declare which datasets may serve one need. Platform-admin only: this is an OPERATIONAL
    declaration, not a description. 400 on any invalid or unreadable ref, 409 on a CAS miss."""
    role, purpose = _policy_key(need_role, serving_purpose)
    entity = _entity_id(entity_id)
    try:
        revision = DatasetServingPolicyRevisionV1(
            entity_id=entity, need_role=DatasetNeedRole(role),
            serving_purpose=ServingPurpose(purpose),
            eligible_dataset_refs=tuple(body.eligible_dataset_refs),
            preferred_dataset_refs=tuple(body.preferred_dataset_refs),
            provenance=human_declaration_provenance(producer_ref=identity.subject))
        revision_id, version = publish_serving_policy(
            conn, revision, expected_pointer_version=body.expected_pointer_version,
            actor=identity.subject, roles=identity.role_claims)
    # 400 covers `PolicyValidationError` too (it IS a `SelectionError`): a store-level refusal that
    # no retry can fix is the request being wrong, not a race. 409 is reserved for the race.
    except SelectionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PolicyStoreConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"entity_id": entity, "need_role": role, "serving_purpose": purpose,
            "revision_id": revision_id, "pointer_version": version,
            "ambiguous": revision.ambiguous}


def _resolve_dataset(conn, source: str, object_ref: str, roles) -> tuple[str, str]:
    """(normalized source, dataset logical ref) for a visible TABLE asset; 404/400 otherwise.

    Reuses ``profiles._visible_table_anchor`` rather than re-deriving it: "is this a table asset
    you may see" must have ONE answer, and a second copy is how two surfaces end up disagreeing
    about which assets exist."""
    try:
        src = normalize_source_name(source)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _visible_table_anchor(conn, src, object_ref, roles)
    return src, logical_ref_of(conn, src, object_ref.lower())


def _temporal_payload(conn, *, source: str, dataset_logical_ref: str, roles) -> dict:
    current = current_temporal_policy(conn, dataset_logical_ref)
    load_bearing, displayed = load_bearing_temporal_model(
        conn, source=source, dataset_logical_ref=dataset_logical_ref)
    base = {
        "dataset_logical_ref": dataset_logical_ref,
        # The profile's answer, carried so the editor can say WHY a model choice is locked (a
        # load-bearing value) or free (nobody has decided, so the policy is the declaration).
        "load_bearing_temporal_storage_model": load_bearing,
        "displayed_temporal_storage_model": displayed,
        # The editor's select options, from the module that OWNS the vocabulary.
        "selection_kinds": list(SELECTION_KINDS),
    }
    if current is None:
        return {**base, "pointer_version": 0, "policy": None}
    revision, pointer = current
    try:
        assert_column_refs_readable(conn, revision.column_refs(), roles=roles)
    except SelectionError as exc:
        # THE WHOLE POLICY, not the offending field. A temporal policy IS a list of column refs, so
        # redacting one and serving the rest would still confirm "there is a column here you may not
        # see" — the existence oracle the sensitivity tag exists to prevent, and one a caller can
        # probe field by field. The table anchor is visible under the D11 DERIVED scope (>= 1
        # visible column), which is exactly why the anchor check upstream cannot decide this: the
        # anchor says "you may look at this table", the refs say "these particular columns". Same
        # rule and the same words as the serving GET (module docstring).
        raise HTTPException(status_code=404, detail="policy not found") from exc
    return {
        **base,
        "pointer_version": pointer.pointer_version,
        "declared_by": pointer.declared_by,
        "policy": {
            "revision_id": revision.revision_id,
            "content_hash": revision.content_hash,
            "temporal_storage_model": revision.temporal_storage_model.value,
            "current_selection": revision.current_selection.value,
            "historical_selection": revision.historical_selection.value,
            "effective_from_ref": revision.effective_from_ref,
            "effective_to_ref": revision.effective_to_ref,
            "snapshot_ref": revision.snapshot_ref,
            "current_flag_ref": revision.current_flag_ref,
            "availability_ref": revision.availability_ref,
            "tie_break_refs": list(revision.tie_break_refs),
            "provenance": revision.provenance.payload(),
        },
    }


@router.get("/catalog/dataset-policies/temporal/{source}/{object_ref:path}",
            dependencies=[Depends(require_source_temporal_selection),
                          Depends(require_catalog_read)])
def get_temporal_policy(source: str, object_ref: str, conn: _RRConn,
                        identity: _Identity) -> dict:
    """The CURRENT temporal policy for one dataset + its CAS pointer version, alongside the
    profile's load-bearing/displayed temporal model."""
    src, logical_ref = _resolve_dataset(conn, source, object_ref, identity.role_claims)
    return _temporal_payload(conn, source=src, dataset_logical_ref=logical_ref,
                             roles=identity.role_claims)


class TemporalPolicyPutRequest(BaseModel):
    """Every ref is a normalized logical COLUMN ref; the runtime cutoff is a PARAMETER and has no
    slot here at all (§6.5 — no free SQL, no live cutoff value in a policy)."""

    expected_pointer_version: int = Field(ge=0)
    temporal_storage_model: str = Field(min_length=1, max_length=100)
    current_selection: str = Field(min_length=1, max_length=100)
    historical_selection: str = Field(min_length=1, max_length=100)
    effective_from_ref: str | None = Field(default=None, max_length=500)
    effective_to_ref: str | None = Field(default=None, max_length=500)
    snapshot_ref: str | None = Field(default=None, max_length=500)
    current_flag_ref: str | None = Field(default=None, max_length=500)
    availability_ref: str | None = Field(default=None, max_length=500)
    tie_break_refs: list[str] = Field(default_factory=list, max_length=8)


@router.put("/catalog/dataset-policies/temporal/{source}/{object_ref:path}",
            dependencies=[Depends(require_source_temporal_selection),
                          Depends(require_catalog_read), Depends(require_confirmer)])
def put_temporal_policy(source: str, object_ref: str, body: TemporalPolicyPutRequest,
                        conn: _Conn, identity: _Identity) -> dict:
    """Declare how one dataset stores history and which rows answer current vs historical
    questions. Platform-admin only. Refuses (400) when a LOAD-BEARING profile value contradicts the
    declaration — a policy may not overrule a governed classification; 409 on a CAS miss."""
    src, logical_ref = _resolve_dataset(conn, source, object_ref, identity.role_claims)
    try:
        revision = DatasetTemporalPolicyRevisionV1(
            dataset_logical_ref=logical_ref,
            temporal_storage_model=body.temporal_storage_model,
            current_selection=body.current_selection,
            historical_selection=body.historical_selection,
            effective_from_ref=body.effective_from_ref,
            effective_to_ref=body.effective_to_ref,
            snapshot_ref=body.snapshot_ref,
            current_flag_ref=body.current_flag_ref,
            availability_ref=body.availability_ref,
            tie_break_refs=tuple(body.tie_break_refs),
            provenance=human_declaration_provenance(producer_ref=identity.subject))
        revision_id, version = publish_temporal_policy(
            conn, revision, expected_pointer_version=body.expected_pointer_version,
            actor=identity.subject, roles=identity.role_claims)
    # 400 covers `PolicyValidationError` too (it IS a `SelectionError`): a store-level refusal that
    # no retry can fix is the request being wrong, not a race. 409 is reserved for the race.
    except SelectionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PolicyStoreConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"dataset_logical_ref": logical_ref, "revision_id": revision_id,
            "pointer_version": version,
            "selection_kinds": {"current": revision.current_selection.value,
                                "historical": revision.historical_selection.value}}


# `SELECTION_KINDS` is declared at the top of this module, beside the other constants, and is READ
# by the temporal GET — it was a bottom-of-file re-export with no reader in `src/` or `tests/`.
__all__ = ["SELECTION_KINDS", "require_source_temporal_selection", "router"]
