from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, Query

from featuregen.api.deps import get_conn, get_identity, require_catalog_read
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.search import SearchHit, search

router = APIRouter()


@router.get("/search", dependencies=[Depends(require_catalog_read)])
def search_catalog(
    q: str,
    conn: Annotated[psycopg.Connection, Depends(get_conn, scope="function")],
    identity: Annotated[IdentityEnvelope, Depends(get_identity)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[SearchHit]:
    # Roles come from the authenticated session — NEVER from the request (M6 read-scope).
    # Freshness fail-closed is enforced inside search(): a stale source's rows are absent.
    return search(conn, q, now=datetime.now(UTC),
                  roles=identity.role_claims, limit=limit)
