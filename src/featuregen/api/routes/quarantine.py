from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends

from featuregen.api.deps import get_conn, get_identity
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.review_queue import QuarantineItem, list_quarantine

router = APIRouter()


@router.get("/sources/{source}/quarantine")
def source_quarantine(
    source: str,
    conn: Annotated[psycopg.Connection, Depends(get_conn, scope="function")],
    identity: Annotated[IdentityEnvelope, Depends(get_identity)],
) -> list[QuarantineItem]:
    # Roles come from the authenticated session, not the request; auth alone gates the queue.
    return list_quarantine(conn, source)
