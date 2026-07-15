"""Read-only governance dashboard routes (Phase 4, Task 2) over the Task-1 read model.

`GET /governance/dashboard` exposes the cross-source :func:`compute_governance_dashboard`
plus the per-source :func:`list_source_governance_summaries` list under a ``sources`` key;
`GET /sources/{source}/governance/dashboard` exposes the single-source dashboard (an unknown
source is an all-zeros dashboard by design, never a 404 — the UI renders an empty state).
Both are pure reads over the recorded fact/task/ledger state (never write) and are gated by
``catalog:read``, mirroring `readiness.py`.

Serialization is plain :func:`dataclasses.asdict`: the analytics dataclasses hold only strs,
ints, floats, ``None``, plain dicts, and nested frozen dataclasses (``generated_at`` is already
an ISO string) — all JSON-safe under FastAPI's default encoder."""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends

from featuregen.api.deps import get_conn, require_catalog_read
from featuregen.overlay.upload.governance_analytics import (
    compute_governance_dashboard,
    list_source_governance_summaries,
)

router = APIRouter()
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]


@router.get("/governance/dashboard", dependencies=[Depends(require_catalog_read)])
def dashboard(conn: _Conn) -> dict:
    """The cross-source (catalog-scope) governance dashboard plus the per-source summary list."""
    dash = compute_governance_dashboard(conn, source=None)
    return {**asdict(dash),
            "sources": [asdict(s) for s in list_source_governance_summaries(conn)]}


@router.get("/sources/{source}/governance/dashboard",
            dependencies=[Depends(require_catalog_read)])
def source_dashboard(source: str, conn: _Conn) -> dict:
    """One source's governance dashboard (source normalized strip+lower by the read model)."""
    return asdict(compute_governance_dashboard(conn, source=source))
