from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from featuregen.api.deps import get_conn, get_identity, get_llm_optional, require_catalog_write
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.intake.llm import LLMClient
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.csv_reader import read_csv_rows
from featuregen.overlay.upload.excel_reader import read_excel_rows
from featuregen.overlay.upload.ingest import IngestResult, ingest_upload

router = APIRouter()

# A catalog upload is a SCHEMA export (column names/types/grain), not a data extract, so a modest cap
# bounds the whole-file in-memory read + parse against an accidental or malicious oversized upload.
_MAX_UPLOAD_BYTES = int(os.environ.get("FEATUREGEN_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))


def _read_capped(file: UploadFile) -> bytes:
    data = file.file.read(_MAX_UPLOAD_BYTES + 1)   # read one past the cap to detect an over-limit file
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"upload exceeds the {_MAX_UPLOAD_BYTES // (1024 * 1024)} MiB limit")
    return data


def _read_rows(filename: str, data: bytes, source: str) -> list[CanonicalRow]:
    name = filename.lower()
    if name.endswith((".xlsx", ".xlsm")):
        return read_excel_rows(data, source=source)
    if name.endswith(".csv"):
        return read_csv_rows(data.decode("utf-8-sig"), source=source)
    raise HTTPException(status_code=400, detail="unsupported file type (expected .csv or .xlsx)")


@router.post("/uploads", dependencies=[Depends(require_catalog_write)])
def create_upload(
    file: Annotated[UploadFile, File(...)],
    source: Annotated[str, Form(...)],
    conn: Annotated[psycopg.Connection, Depends(get_conn, scope="function")],
    identity: Annotated[IdentityEnvelope, Depends(get_identity)],
    client: Annotated[LLMClient | None, Depends(get_llm_optional)],
) -> IngestResult:
    try:
        rows = _read_rows(file.filename or "", _read_capped(file), source)
    except HTTPException:
        raise
    except Exception as exc:   # a malformed file is a client error, not a 500
        raise HTTPException(status_code=400, detail=f"could not parse upload: {exc}") from exc
    # client=None (no provider configured) -> enrichment is skipped; a configured client runs the
    # governed, audited enrichment path (M2/M4). Either way the upload itself succeeds or brakes.
    return ingest_upload(conn, source, rows, actor=identity,
                         now=datetime.now(UTC), client=client)
