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
from featuregen.overlay.upload.ingest import dismiss_quarantine_row, resolve_quarantine_row
from featuregen.overlay.upload.review_queue import QuarantineItem, list_quarantine

router = APIRouter()
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]


class ResolveIn(BaseModel):
    edits: dict[str, str] = {}   # the reviewer's corrected field values, merged onto the raw row


@router.get("/sources/{source}/quarantine", dependencies=[Depends(require_catalog_read)])
def source_quarantine(source: str, conn: _Conn, identity: _Identity) -> list[QuarantineItem]:
    return list_quarantine(conn, source)


@router.post("/sources/{source}/quarantine/{row_index}/resolve",
             dependencies=[Depends(require_catalog_write)])
def resolve_row(source: str, row_index: int, body: ResolveIn, conn: _Conn,
                identity: _Identity) -> dict:
    """Apply the reviewer's inline fix: re-validate SERVER-side; if it now passes, the row enters the
    catalog and leaves the queue. `resolved=false` + a reason when the fix still doesn't validate."""
    resolved, reason = resolve_quarantine_row(conn, source, row_index, body.edits, actor=identity)
    return {"resolved": resolved, "reason": reason}


@router.post("/sources/{source}/quarantine/{row_index}/dismiss",
             dependencies=[Depends(require_catalog_write)])
def dismiss_row(source: str, row_index: int, conn: _Conn, identity: _Identity) -> dict:
    if not dismiss_quarantine_row(conn, source, row_index):
        raise HTTPException(status_code=404, detail="no such quarantined row")
    return {"dismissed": True}
