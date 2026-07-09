"""Connector CRUD + OpenMetadata preview/import (binding spec 2026-07-09).

Preview-then-confirm is MANDATORY: there is no direct-import path. Preview pulls + translates and
never writes; import re-pulls, re-translates, verifies the previewed snapshot hash (409 on drift)
and then runs the UNCHANGED ingest pipeline (``ingest_upload``) in the request's one transaction —
the connector adds no new write path.

RBAC (permissions, never role strings): configuring a connector and confirming imports require
``catalog:write`` (held by data_owner / platform_admin); preview and listing require
``catalog:read`` (held by catalog_viewer and up). Denials are audited by ``require_permission``.

Identity (documented choice): the spec names a ``service:openmetadata-connector`` identity, but an
authenticated service envelope is only mintable via the sealed trust capability
(identity/_trust.py), whose call sites are frozen by a grep-guard test — the API layer cannot mint
one without weakening that boundary. Imports therefore ingest under the APPROVING HUMAN's session
identity (the sanctioned path every upload already uses), and the import record names the
connector as the vehicle (``vehicle='openmetadata-connector'``) — honest attribution both ways.

Secrets (documented choice): the KMS module exposes only a destroy/rotate Protocol, so the bot
token is an ENV-VAR REFERENCE (``FEATUREGEN_OM_TOKEN__<NAME>``); rows store the reference, the
request model REJECTS a plaintext token field, and no response ever carries the token value.
"""
from __future__ import annotations

import os
import re
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from featuregen.api.deps import (
    get_conn,
    get_identity,
    get_llm_optional,
    require_catalog_read,
    require_catalog_write,
)
from featuregen.connectors import store
from featuregen.connectors.openmetadata import (
    OMAuthRejected,
    OMConfig,
    OMUnreachable,
    Translation,
    build_preview,
    fetch_tables,
    httpx_fetch,
    read_openmetadata,
    semantics_pending_count,
    snapshot_hash,
)
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.intake.llm import LLMClient
from featuregen.overlay.upload.canonical import validate_rows
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.read_scope import SENSITIVITY_ROLES

router = APIRouter()
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]

# The transport seam, module-level so tests monkeypatch it with fixture-backed fetchers.
_build_fetch = httpx_fetch

_VALID_SENSITIVITIES = frozenset({"", *SENSITIVITY_ROLES})
_VALID_FILTER_KEYS = frozenset({"service", "database", "schema"})


class ConnectorIn(BaseModel):
    # extra='forbid' is load-bearing: a caller posting a plaintext `token` field gets a 422
    # instead of the secret silently landing in a stored config row.
    model_config = ConfigDict(extra="forbid")

    name: str
    base_url: str
    target_source: str
    tag_map: dict[str, str] = {}
    filters: dict[str, str] = {}
    table_naming: Literal["table", "schema_table"] = "table"
    token_env: str | None = None    # env var REFERENCE; defaults to FEATUREGEN_OM_TOKEN__<NAME>


class PreviewIn(BaseModel):
    connector_id: str


class ImportIn(BaseModel):
    connector_id: str
    snapshot_hash: str


def _default_token_env(name: str) -> str:
    return "FEATUREGEN_OM_TOKEN__" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()


def _validate_config(body: ConnectorIn) -> None:
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="connector name is required")
    if not body.base_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="base_url must be an http(s) URL")
    if not body.target_source.strip():
        raise HTTPException(status_code=400, detail="target_source is required")
    bad = sorted(v for v in body.tag_map.values() if v not in _VALID_SENSITIVITIES)
    if bad:
        raise HTTPException(
            status_code=400,
            detail=f"tag_map values must be one of: {', '.join(sorted(SENSITIVITY_ROLES))} "
                   f"(or '' to ignore); got: {', '.join(bad)}")
    unknown = sorted(k for k in body.filters if k not in _VALID_FILTER_KEYS)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"filters keys must be among: {', '.join(sorted(_VALID_FILTER_KEYS))}; "
                   f"got: {', '.join(unknown)}")


def _serialize(cfg: dict[str, Any]) -> dict[str, Any]:
    """A connector for the wire: the stored row (which never contains the token) plus whether the
    referenced env var is actually set, so the UI can tell the operator what to fix."""
    return {**cfg, "token_present": bool(os.environ.get(cfg["token_env"]))}


@router.get("/connectors", dependencies=[Depends(require_catalog_read)])
def get_connectors(conn: _Conn, identity: _Identity) -> list[dict]:
    return [_serialize(c) for c in store.list_connectors(conn)]


@router.post("/connectors", dependencies=[Depends(require_catalog_write)])
def create_connector(body: ConnectorIn, conn: _Conn, identity: _Identity) -> dict:
    _validate_config(body)
    if store.name_exists(conn, body.name):
        raise HTTPException(status_code=409, detail=f"connector '{body.name}' already exists")
    cfg = store.create_connector(
        conn, name=body.name, base_url=body.base_url, target_source=body.target_source,
        tag_map=body.tag_map, filters=body.filters, table_naming=body.table_naming,
        token_env=body.token_env or _default_token_env(body.name), created_by=identity.subject)
    return _serialize(cfg)


@router.delete("/connectors/{connector_id}", dependencies=[Depends(require_catalog_write)])
def delete_connector(connector_id: str, conn: _Conn, identity: _Identity) -> dict:
    if not store.delete_connector(conn, connector_id):
        raise HTTPException(status_code=404, detail="no such connector")
    return {"deleted": True}


def _get_config(conn: psycopg.Connection, connector_id: str) -> dict[str, Any]:
    cfg = store.get_connector(conn, connector_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail="no such connector")
    return cfg


def _pull(cfg: dict[str, Any]) -> tuple[OMConfig, Translation]:
    """Pull + translate one configured connection. Clean failure surface per the spec: missing
    token reference -> 400, OM auth rejected -> 401, OM unreachable / bad pages -> 502. A page
    failure inside fetch_tables fails the WHOLE pull; nothing is ever partially translated."""
    token = os.environ.get(cfg["token_env"], "")
    if not token:
        raise HTTPException(
            status_code=400,
            detail=f"connector token is not configured: set the {cfg['token_env']} "
                   "environment variable")
    om_config = OMConfig(
        base_url=cfg["base_url"], target_source=cfg["target_source"],
        tag_map=cfg["tag_map"] or {}, filters=cfg["filters"] or {},
        table_naming=cfg["table_naming"])
    fetch = _build_fetch(cfg["base_url"], token)
    try:
        tables = fetch_tables(fetch)
    except OMAuthRejected as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except OMUnreachable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return om_config, read_openmetadata(tables, om_config)


@router.post("/connectors/openmetadata/preview", dependencies=[Depends(require_catalog_read)])
def preview_connector(body: PreviewIn, conn: _Conn, identity: _Identity) -> dict:
    """Dry run: pull + translate + predict every ingest verdict WITHOUT ingesting. The brake
    verdict comes from the same large_change_brake the pipeline runs; quarantine from the same
    validate_rows; the diff from the live graph_node catalog. Nothing is written."""
    cfg = _get_config(conn, body.connector_id)
    om_config, translation = _pull(cfg)
    try:
        return build_preview(conn, om_config, translation)
    except ValueError as exc:   # empty pull (scope matched nothing) — a client-fixable condition
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/connectors/openmetadata/import", dependencies=[Depends(require_catalog_write)])
def import_connector(body: ImportIn, conn: _Conn, identity: _Identity,
                     client: Annotated[LLMClient | None, Depends(get_llm_optional)]) -> dict:
    """Confirmed import: re-pull, re-translate, verify the previewed snapshot hash, then run the
    UNCHANGED ingest pipeline in this request's one transaction. Suggestion is never ingestion:
    as-of hints from the preview are NOT applied here — rows carry blank semantics."""
    cfg = _get_config(conn, body.connector_id)
    _, translation = _pull(cfg)
    current_hash = snapshot_hash(translation.rows)
    if current_hash != body.snapshot_hash:
        raise HTTPException(
            status_code=409,
            detail="OpenMetadata changed since this preview (snapshot hash mismatch). "
                   "Run preview again and approve the fresh dry run.")
    result = ingest_upload(conn, cfg["target_source"], translation.rows,
                           actor=identity, now=datetime.now(UTC), client=client)
    import_id = store.record_import(conn, connector=cfg, snapshot_hash=current_hash,
                                    approved_by=identity.subject, result=asdict(result))
    pending = 0
    if result.status == "ingested":
        vr = validate_rows(list(translation.rows), cfg["target_source"])
        pending = semantics_pending_count(vr.good)
    return {
        "result": asdict(result),
        "import_id": import_id,
        "review_queue": {"quarantined": result.quarantined, "semantics_pending": pending},
    }
