"""Semantics-pending queue + owner completion routes (#22).

GET lists a source's columns that arrived without their semantic facts (as-of / additivity /
unit / currency / entity — the connector's `semantics_pending_count` definition, one shared
predicate). POST lets a data owner fill them in: a direct catalog_write edit of the column's
flat graph_node attributes, exactly how a file declaration would have set them. Values are
validated against the SAME closed vocabularies validate_rows enforces; the write rebuilds the
node's search_doc and lands one SEMANTICS_COMPLETED event on the tamper-evident audit chain.
Grain/availability facts stay governed (Pass B) and are NOT reachable from here; a declared
as_of_basis is validated + audited, never node-written (the basis lives in the governed
availability_time fact stream only)."""
from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from featuregen.api.deps import (
    get_conn,
    get_identity,
    require_catalog_read,
    require_catalog_write,
)
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.semantics import (
    AsOfConflict,
    InvalidSemanticValue,
    SemanticsPendingItem,
    complete_semantics,
    list_semantics_pending,
)

router = APIRouter()
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]


class SemanticsIn(BaseModel):
    """The semantic values the owner is setting — any subset. A present-but-blank string is a
    422 (completion SETS values; clearing is not a thing this surface does)."""
    additivity: str | None = None
    unit: str | None = None
    currency: str | None = None
    entity: str | None = None
    as_of_basis: str | None = None
    is_as_of: bool | None = None


def _normalize_source(source: str) -> str:
    """Ingest normalizes the source before anything keys on it (uploads.py: strip().lower()), so
    graph nodes live under the lowercased id. Normalize the path param the SAME way, or a caller
    asking for /sources/Ledger/semantics-pending silently misses the queue stored under 'ledger'."""
    return source.strip().lower()


@router.get("/sources/{source}/semantics-pending",
            dependencies=[Depends(require_catalog_read)])
def semantics_pending_queue(source: str, conn: _Conn,
                            identity: _Identity) -> list[SemanticsPendingItem]:
    """The source's semantics-pending columns, read-scoped on the caller's roles (a pending
    column whose sensitivity the caller can't see is withheld, like search)."""
    return list_semantics_pending(conn, _normalize_source(source), roles=identity.role_claims)


@router.post("/sources/{source}/columns/{object_ref}/semantics",
             dependencies=[Depends(require_catalog_write)])
def complete_column_semantics(source: str, object_ref: str, body: SemanticsIn, conn: _Conn,
                              identity: _Identity) -> dict:
    """Fill in the column's declared semantics. Fail-closed: values outside the upload
    vocabularies are a 422 with nothing written; a second as-of axis for the table is a 409
    (#17 — one availability basis per table); an unknown column ref is a 404."""
    fields = body.model_dump(exclude_none=True)
    for name, value in fields.items():
        if isinstance(value, str):
            fields[name] = value.strip()
            if not fields[name]:
                raise HTTPException(status_code=422, detail=f"blank value for '{name}'")
    if not fields:
        raise HTTPException(
            status_code=422,
            detail="no semantic values provided (any of: additivity, unit, currency, "
                   "entity, as_of_basis, is_as_of)")
    try:
        applied = complete_semantics(conn, _normalize_source(source), object_ref,
                                     actor=identity, **fields)
    except InvalidSemanticValue as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AsOfConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if applied is None:
        raise HTTPException(status_code=404, detail="no such column in this source's catalog")
    return {"completed": True, "applied": applied}
