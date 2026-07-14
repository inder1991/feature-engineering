"""Read-only readiness routes (Tier-1 polish, audit I-5: the views existed with NO route).

`GET /sources/{source}/readiness/relationships` exposes the per-table five-value relationship
diagnostic (:func:`compute_relationship_readiness`); `GET /sources/{source}/readiness` exposes the
blocker-based :class:`FeatureReadiness` verdict (:func:`compute_readiness`) — CATALOG scope, or
TABLE when ``?subset`` narrows to one table. Both are pure reads over the recorded decision /
fact state (never write) and are gated by ``catalog:read``, mirroring `quarantine.py`.

Serialization is plain :func:`dataclasses.asdict`: the readiness dataclasses hold only strs,
StrEnums (str subclasses), tuples of strs, nested frozen dataclasses, and ``dict[str, float]`` —
all JSON-safe under FastAPI's default encoder. A malformed or ambiguous ``subset`` selector
(``a.b.c``, or a bare table name shared across schemas — `_scoped_refs` raises ``ValueError``)
maps to 422, never a 500."""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query

from featuregen.api.deps import get_conn, require_catalog_read
from featuregen.overlay.upload.readiness import (
    ReadinessScopeType,
    compute_readiness,
    compute_relationship_readiness,
)

router = APIRouter()
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Subset = Annotated[str | None, Query(description="TABLE selector: 'table' or 'schema.table'")]


@router.get("/sources/{source}/readiness/relationships",
            dependencies=[Depends(require_catalog_read)])
def source_relationship_readiness(source: str, conn: _Conn, subset: _Subset = None) -> dict:
    """The per-table relationship diagnostic: one row per in-scope table with the precedence-folded
    five-value ``status`` plus the disjoint confirmed/proposed/weak/conflicting pair lists."""
    try:
        rels = compute_relationship_readiness(conn, source=source, subset=subset)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"source": source.strip().lower(), "relationships": [asdict(r) for r in rels]}


@router.get("/sources/{source}/readiness", dependencies=[Depends(require_catalog_read)])
def source_readiness(source: str, conn: _Conn, subset: _Subset = None) -> dict:
    """The blocker-based readiness verdict for the source (or one table via ``subset``):
    ``operational_status`` plus the blocking / review / advisory requirement lists."""
    scope = ReadinessScopeType.TABLE if subset is not None else ReadinessScopeType.CATALOG
    try:
        verdict = compute_readiness(conn, source=source, scope=scope, subset=subset)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return asdict(verdict)
