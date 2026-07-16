"""Read-only ingestion-run manifest route (first-release hardening design #3).

``GET /ingestion-runs/{run_id}`` returns the durable run record — who ingested what, when, under
which effective config, with what outcome — plus its append-only status history. This is the
PRIMARY surface for "what did that ingestion attempt do": the caller got the run id from the
``X-Ingestion-Run-Id`` response header, which POST /uploads sets on success AND on every failure
after the run is opened, so a request that 4xx/5xx'd is still fully explorable here. Gated by
``catalog:read`` (the manifest holds catalog metadata — filenames, sources, actor subjects — not
row data). Timestamps serialize via FastAPI's default encoder; ``effective_config`` is the
allowlisted flag snapshot (never secrets, enforced at write time in overlay/upload/ingestion_run).
"""

from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException

from featuregen.api.deps import get_conn, require_catalog_read
from featuregen.overlay.upload.ingestion_run import get_run

router = APIRouter()
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]


@router.get("/ingestion-runs/{run_id}", dependencies=[Depends(require_catalog_read)])
def ingestion_run_detail(run_id: str, conn: _Conn) -> dict:
    """The run row + its ``status_history``, or 404 for an unknown id."""
    run = get_run(conn, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="ingestion run not found")
    return run
