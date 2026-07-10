"""Two-tier OpenMetadata connector routes (restructure of the flat v1 connector, 2026-07-10).

Grounded in OpenMetadata's real model — hierarchy DatabaseService -> Database -> Schema -> Table,
and a bot JWT token authenticates to the WHOLE instance (it sees every DatabaseService) — the
connection splits into two tiers:

  INTEGRATION  = one OpenMetadata instance: name, base_url, token_env (sealed ref), default tag_map.
                 Generic; sees all services. RBAC-managed. One row per instance.
  SYNC (child) = one DatabaseService (optionally narrowed by database/schema) -> one FeatureGen
                 catalog source, with a tag-map override + table naming. Many per integration.

Ingest pulls from a SYNC (by sync_id), not a flat connector. Preview-then-confirm is MANDATORY:
there is no direct-import path. Preview pulls + translates and never writes; import re-pulls,
re-translates, verifies the previewed snapshot hash (409 on drift) and then runs the UNCHANGED
ingest pipeline (``ingest_upload``) in the request's one transaction — the connector adds no new
write path.

RBAC (permissions, never role strings): creating/patching/deleting integrations and syncs and
confirming imports require ``catalog:write`` (data_owner / platform_admin); listing, getting,
service discovery and preview require ``catalog:read`` (catalog_viewer and up). Denials are audited
by ``require_permission``.

Identity (documented choice): the spec names a ``service:openmetadata-connector`` identity, but an
authenticated service envelope is only mintable via the sealed trust capability, whose call sites
are frozen by a grep-guard test — the API layer cannot mint one. Imports therefore ingest under the
APPROVING HUMAN's session identity (the sanctioned path every upload uses), and the import record
names the connector as the vehicle (``vehicle='openmetadata-connector'``).

Secrets (documented choice): the KMS module exposes only a destroy/rotate Protocol, so the bot
token is an ENV-VAR REFERENCE (``FEATUREGEN_OM_TOKEN__<NAME>``); rows store the reference, the
request models REJECT a plaintext token field, and no response ever carries the token value.

Egress + token namespace (SECURITY, fail-closed): two guards keep a merely-``catalog:write`` user
from turning the connector into a secret-exfiltration or SSRF primitive.
  1. ``token_env`` is constrained to the connector-token namespace
     (``^FEATUREGEN_OM_TOKEN__[A-Z0-9_]+$``) — an integration row can only ever reference a bot-
     token env var, never an arbitrary process secret (a DSN, a cloud/KMS key). Enforced on
     integration CREATE and PATCH. ``token_present`` then reveals only whether a connector-token var
     is set, an acceptable oracle.
  2. ``base_url`` must resolve to a host on the ops-controlled allowlist
     (``FEATUREGEN_OM_ALLOWED_HOSTS``, comma-separated ``host`` / ``host:port`` entries; a bare host
     matches only the scheme's default port). Enforced on integration CREATE and PATCH AND on every
     live OM call (service discovery, preview, import) — a row that predates the allowlist still
     cannot pull off it. When the env is unset/empty, every check fails 400 (fail-closed, never
     fail-open). The transport also refuses to follow redirects, so a 3xx to an off-allowlist host
     can't slip the guard.
"""
from __future__ import annotations

import os
import re
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

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
    fetch_services,
    fetch_tables,
    httpx_fetch,
    read_openmetadata,
    snapshot_hash,
)
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.intake.llm import LLMClient
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.read_scope import SENSITIVITY_ROLES

router = APIRouter()
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]

# The transport seam, module-level so tests monkeypatch it with fixture-backed fetchers.
_build_fetch = httpx_fetch

_VALID_SENSITIVITIES = frozenset({"", *SENSITIVITY_ROLES})

# A stored token reference may ONLY name the connector-token namespace: this is what stops an
# integration row from pointing at an arbitrary process secret (a DSN, a KMS/cloud key) and having
# it egress as a Bearer header. Kept in sync with `_default_token_env` below.
_TOKEN_ENV_RE = re.compile(r"FEATUREGEN_OM_TOKEN__[A-Z0-9_]+\Z")

_NO_ALLOWLIST_DETAIL = "no OpenMetadata hosts are allowlisted: set FEATUREGEN_OM_ALLOWED_HOSTS"
_DEFAULT_PORTS = {"https": 443, "http": 80}


# ---- Egress allowlist (fail-closed) ----------------------------------------------------------


def _allowlisted_hosts() -> set[str]:
    """Ops-controlled egress allowlist from ``FEATUREGEN_OM_ALLOWED_HOSTS`` (comma-separated
    ``host`` / ``host:port`` entries), lowercased. Empty set means fail-closed."""
    raw = os.environ.get("FEATUREGEN_OM_ALLOWED_HOSTS", "")
    return {entry.strip().lower() for entry in raw.split(",") if entry.strip()}


def _url_authorities(base_url: str) -> set[str]:
    """The forms of a URL's authority to match against the allowlist: ``host:port`` with the
    scheme's default port filled in, plus the bare ``host`` when the URL uses that default port
    (so an ``host`` entry matches ``https://host`` but NOT ``https://host:8443``)."""
    parsed = urlsplit(base_url)
    host = (parsed.hostname or "").lower()
    default = _DEFAULT_PORTS.get(parsed.scheme.lower())
    try:
        port = parsed.port if parsed.port is not None else default
    except ValueError:
        return set()   # a non-numeric port is unparseable -> matches nothing -> fail closed (400)
    forms = {f"{host}:{port}"}
    if port is not None and port == default:
        forms.add(host)
    return forms


def _enforce_egress_allowlist(base_url: str) -> None:
    """Fail-closed host allowlist check, enforced on integration CREATE/PATCH and on every pull.
    Raises 400 when the allowlist is unset/empty or the URL's host:port is not on it."""
    allowed = _allowlisted_hosts()
    if not allowed:
        raise HTTPException(status_code=400, detail=_NO_ALLOWLIST_DETAIL)
    if not (_url_authorities(base_url) & allowed):
        host = urlsplit(base_url).hostname or base_url
        raise HTTPException(
            status_code=400,
            detail=f"OpenMetadata host '{host}' is not allowlisted "
                   "(set FEATUREGEN_OM_ALLOWED_HOSTS); ask ops to add it")


# ---- Request models --------------------------------------------------------------------------


class IntegrationIn(BaseModel):
    # extra='forbid' is load-bearing: a caller posting a plaintext `token` field gets a 422
    # instead of the secret silently landing in a stored integration row.
    model_config = ConfigDict(extra="forbid")

    name: str
    base_url: str
    tag_map: dict[str, str] = {}
    token_env: str | None = None    # env var REFERENCE; defaults to FEATUREGEN_OM_TOKEN__<NAME>


class IntegrationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    base_url: str | None = None
    tag_map: dict[str, str] | None = None
    token_env: str | None = None


class SyncIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_name: str
    target_source: str
    database_filter: str | None = None
    schema_filter: str | None = None
    tag_map_override: dict[str, str] | None = None
    table_naming: Literal["table", "schema_table"] = "table"


class SyncPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_name: str | None = None
    target_source: str | None = None
    database_filter: str | None = None
    schema_filter: str | None = None
    tag_map_override: dict[str, str] | None = None
    table_naming: Literal["table", "schema_table"] | None = None


class ImportIn(BaseModel):
    snapshot_hash: str


# ---- Validators ------------------------------------------------------------------------------


def _default_token_env(name: str) -> str:
    return "FEATUREGEN_OM_TOKEN__" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()


def _validate_name(name: str) -> None:
    if not name.strip():
        raise HTTPException(status_code=400, detail="integration name is required")


def _validate_base_url(base_url: str) -> None:
    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="base_url must be an http(s) URL")


def _validate_token_env(token_env: str) -> None:
    if not _TOKEN_ENV_RE.match(token_env):
        raise HTTPException(
            status_code=400,
            detail="token_env must name the connector-token namespace "
                   "(match ^FEATUREGEN_OM_TOKEN__[A-Z0-9_]+$, e.g. FEATUREGEN_OM_TOKEN__CORP)")


def _validate_tag_map(tag_map: dict[str, str]) -> None:
    bad = sorted(v for v in tag_map.values() if v not in _VALID_SENSITIVITIES)
    if bad:
        raise HTTPException(
            status_code=400,
            detail=f"tag_map values must be one of: {', '.join(sorted(SENSITIVITY_ROLES))} "
                   f"(or '' to ignore); got: {', '.join(bad)}")


def _serialize_integration(row: dict[str, Any]) -> dict[str, Any]:
    """An integration for the wire: the stored row (which never contains the token) plus whether the
    referenced env var is actually set, so the UI can tell the operator what to fix."""
    return {**row, "token_present": bool(os.environ.get(row["token_env"]))}


# ---- Integration CRUD ------------------------------------------------------------------------


@router.get("/integrations", dependencies=[Depends(require_catalog_read)])
def get_integrations(conn: _Conn, identity: _Identity) -> list[dict]:
    return [_serialize_integration(i) for i in store.list_integrations(conn)]


@router.post("/integrations", dependencies=[Depends(require_catalog_write)])
def create_integration(body: IntegrationIn, conn: _Conn, identity: _Identity) -> dict:
    _validate_name(body.name)
    _validate_base_url(body.base_url)
    token_env = body.token_env or _default_token_env(body.name)
    _validate_token_env(token_env)
    _validate_tag_map(body.tag_map)
    _enforce_egress_allowlist(body.base_url)
    if store.integration_name_exists(conn, body.name):
        raise HTTPException(status_code=409, detail=f"integration '{body.name}' already exists")
    try:
        integ = store.create_integration(
            conn, name=body.name, base_url=body.base_url, token_env=token_env,
            tag_map=body.tag_map, created_by=identity.subject)
    except store.IntegrationNameConflict as exc:   # lost the race after the pre-check passed
        raise HTTPException(
            status_code=409, detail=f"integration '{body.name}' already exists") from exc
    return _serialize_integration(integ)


def _get_integration(conn: psycopg.Connection, integration_id: str) -> dict[str, Any]:
    integ = store.get_integration(conn, integration_id)
    if integ is None:
        raise HTTPException(status_code=404, detail="no such integration")
    return integ


@router.get("/integrations/{integration_id}", dependencies=[Depends(require_catalog_read)])
def get_integration_by_id(integration_id: str, conn: _Conn, identity: _Identity) -> dict:
    return _serialize_integration(_get_integration(conn, integration_id))


@router.patch("/integrations/{integration_id}", dependencies=[Depends(require_catalog_write)])
def patch_integration(integration_id: str, body: IntegrationPatch, conn: _Conn,
                      identity: _Identity) -> dict:
    """Update name/base_url/token_env/tag_map (re-validated). Every provided field is merged over
    the current row and the RESULT is re-validated — so a PATCH can never leave a row off-namespace
    or off-allowlist."""
    current = _get_integration(conn, integration_id)
    name = body.name if body.name is not None else current["name"]
    base_url = body.base_url if body.base_url is not None else current["base_url"]
    token_env = body.token_env if body.token_env is not None else current["token_env"]
    tag_map = body.tag_map if body.tag_map is not None else (current["tag_map"] or {})

    _validate_name(name)
    _validate_base_url(base_url)
    _validate_token_env(token_env)
    _validate_tag_map(tag_map)
    _enforce_egress_allowlist(base_url)
    if body.name is not None and store.integration_name_exists(
            conn, name, exclude_id=integration_id):
        raise HTTPException(status_code=409, detail=f"integration '{name}' already exists")

    updated = store.update_integration(
        conn, integration_id, name=name, base_url=base_url, token_env=token_env, tag_map=tag_map)
    assert updated is not None
    return _serialize_integration(updated)


@router.delete("/integrations/{integration_id}", dependencies=[Depends(require_catalog_write)])
def delete_integration(integration_id: str, conn: _Conn, identity: _Identity) -> dict:
    """Delete an integration; its syncs cascade (documented). Import history survives (no FK)."""
    if not store.delete_integration(conn, integration_id):
        raise HTTPException(status_code=404, detail="no such integration")
    return {"deleted": True}


# ---- Service discovery -----------------------------------------------------------------------


@router.get("/integrations/{integration_id}/services",
            dependencies=[Depends(require_catalog_read)])
def discover_services(integration_id: str, conn: _Conn, identity: _Identity) -> list[dict]:
    """Live call to OM ``GET /api/v1/services/databaseServices`` with the sealed token: list every
    DatabaseService this integration's bot token can see, flagged with whether a sync already binds
    it. Host-allowlisted and no-redirect like every pull; auth rejected -> 401, unreachable -> 502.
    Discovery is a convenience: the sync-create path never depends on it (a service_name can be typed
    by hand), so an OM outage degrades gracefully."""
    integ = _get_integration(conn, integration_id)
    _enforce_egress_allowlist(integ["base_url"])
    token = os.environ.get(integ["token_env"], "")
    if not token:
        raise HTTPException(
            status_code=400,
            detail=f"integration token is not configured: set the {integ['token_env']} "
                   "environment variable")
    fetch = _build_fetch(integ["base_url"], token)
    try:
        services = fetch_services(fetch)
    except OMAuthRejected as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except OMUnreachable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    synced_by_service = {s["service_name"]: s["sync_id"]
                         for s in store.list_syncs(conn, integration_id)}
    out: list[dict] = []
    for svc in services:
        if not isinstance(svc, dict):
            continue
        name = str(svc.get("name") or "")
        sync_id = synced_by_service.get(name)
        out.append({
            "service_name": name,
            "service_type": str(svc.get("serviceType") or ""),
            "fqn": str(svc.get("fullyQualifiedName") or name),
            "synced": sync_id is not None,
            "sync_id": sync_id,
        })
    return out


# ---- Sync CRUD -------------------------------------------------------------------------------


@router.get("/integrations/{integration_id}/syncs", dependencies=[Depends(require_catalog_read)])
def get_syncs(integration_id: str, conn: _Conn, identity: _Identity) -> list[dict]:
    _get_integration(conn, integration_id)
    return store.list_syncs(conn, integration_id)


@router.post("/integrations/{integration_id}/syncs", dependencies=[Depends(require_catalog_write)])
def create_sync(integration_id: str, body: SyncIn, conn: _Conn, identity: _Identity) -> dict:
    """Bind one service (optionally narrowed) to one catalog source. Does NOT contact OM — the
    service_name may be typed by hand, so a sync can be created even while OM discovery is down. One
    sync per (integration, service_name): a duplicate is a 409."""
    _get_integration(conn, integration_id)
    if not body.service_name.strip():
        raise HTTPException(status_code=400, detail="service_name is required")
    if not body.target_source.strip():
        raise HTTPException(status_code=400, detail="target_source is required")
    if body.tag_map_override is not None:
        _validate_tag_map(body.tag_map_override)
    if store.sync_exists_for_service(conn, integration_id, body.service_name):
        raise HTTPException(
            status_code=409,
            detail=f"a sync for service '{body.service_name}' already exists on this integration")
    try:
        sync = store.create_sync(
            conn, integration_id=integration_id, service_name=body.service_name,
            database_filter=body.database_filter, schema_filter=body.schema_filter,
            target_source=body.target_source, tag_map_override=body.tag_map_override,
            table_naming=body.table_naming, created_by=identity.subject)
    except store.SyncServiceConflict as exc:   # lost the race after the pre-check passed
        raise HTTPException(
            status_code=409,
            detail=f"a sync for service '{body.service_name}' already exists on this "
                   "integration") from exc
    return sync


def _get_sync_of_integration(conn: psycopg.Connection, integration_id: str,
                             sync_id: str) -> dict[str, Any]:
    _get_integration(conn, integration_id)
    sync = store.get_sync(conn, sync_id)
    if sync is None or sync["integration_id"] != integration_id:
        raise HTTPException(status_code=404, detail="no such sync")
    return sync


@router.get("/integrations/{integration_id}/syncs/{sync_id}",
            dependencies=[Depends(require_catalog_read)])
def get_sync_by_id(integration_id: str, sync_id: str, conn: _Conn, identity: _Identity) -> dict:
    return _get_sync_of_integration(conn, integration_id, sync_id)


@router.patch("/integrations/{integration_id}/syncs/{sync_id}",
              dependencies=[Depends(require_catalog_write)])
def patch_sync(integration_id: str, sync_id: str, body: SyncPatch, conn: _Conn,
               identity: _Identity) -> dict:
    current = _get_sync_of_integration(conn, integration_id, sync_id)
    service_name = body.service_name if body.service_name is not None else current["service_name"]
    target_source = (body.target_source if body.target_source is not None
                     else current["target_source"])
    table_naming = body.table_naming if body.table_naming is not None else current["table_naming"]
    database_filter = (body.database_filter if body.database_filter is not None
                       else current["database_filter"])
    schema_filter = (body.schema_filter if body.schema_filter is not None
                     else current["schema_filter"])
    tag_map_override = (body.tag_map_override if body.tag_map_override is not None
                        else current["tag_map_override"])

    if not service_name.strip():
        raise HTTPException(status_code=400, detail="service_name is required")
    if not target_source.strip():
        raise HTTPException(status_code=400, detail="target_source is required")
    if tag_map_override is not None:
        _validate_tag_map(tag_map_override)
    if body.service_name is not None and store.sync_exists_for_service(
            conn, integration_id, service_name, exclude_id=sync_id):
        raise HTTPException(
            status_code=409,
            detail=f"a sync for service '{service_name}' already exists on this integration")

    updated = store.update_sync(
        conn, sync_id, service_name=service_name, database_filter=database_filter,
        schema_filter=schema_filter, target_source=target_source,
        tag_map_override=tag_map_override, table_naming=table_naming)
    assert updated is not None
    return updated


@router.delete("/integrations/{integration_id}/syncs/{sync_id}",
               dependencies=[Depends(require_catalog_write)])
def delete_sync(integration_id: str, sync_id: str, conn: _Conn, identity: _Identity) -> dict:
    _get_sync_of_integration(conn, integration_id, sync_id)
    store.delete_sync(conn, sync_id)
    return {"deleted": True}


# ---- Pull (shared by preview + import) -------------------------------------------------------


def _resolve_sync(conn: psycopg.Connection, sync_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    sync = store.get_sync(conn, sync_id)
    if sync is None:
        raise HTTPException(status_code=404, detail="no such sync")
    integ = store.get_integration(conn, sync["integration_id"])
    if integ is None:   # a sync always has its integration (FK cascade) — defensive
        raise HTTPException(status_code=404, detail="no such integration")
    return sync, integ


def _effective_tag_map(integ: dict[str, Any], sync: dict[str, Any]) -> dict[str, str]:
    """integration.tag_map is the default; sync.tag_map_override WINS per tag. A NULL override
    inherits the integration map wholesale."""
    merged = dict(integ["tag_map"] or {})
    merged.update(sync["tag_map_override"] or {})
    return merged


def _sync_filters(sync: dict[str, Any]) -> dict[str, str]:
    """The scope filters a sync narrows to: always its service (an EXACT bind — ``_in_scope``
    matches the service literally, never as a glob), plus optional database/schema fnmatch
    patterns."""
    filters: dict[str, str] = {"service": sync["service_name"]}
    if sync["database_filter"]:
        filters["database"] = sync["database_filter"]
    if sync["schema_filter"]:
        filters["schema"] = sync["schema_filter"]
    return filters


def _pull(sync: dict[str, Any], integ: dict[str, Any]) -> tuple[OMConfig, Translation]:
    """Pull + translate one sync. Clean failure surface per the spec: off-allowlist / no allowlist
    -> 400, missing token reference -> 400, OM auth rejected -> 401, OM unreachable / bad pages ->
    502. A page failure inside fetch_tables fails the WHOLE pull; nothing is ever partially
    translated. The egress allowlist is re-checked here (not only at integration create/patch) so a
    row that predates the allowlist can never pull off an unlisted host."""
    _enforce_egress_allowlist(integ["base_url"])
    token = os.environ.get(integ["token_env"], "")
    if not token:
        raise HTTPException(
            status_code=400,
            detail=f"integration token is not configured: set the {integ['token_env']} "
                   "environment variable")
    om_config = OMConfig(
        base_url=integ["base_url"], target_source=sync["target_source"],
        tag_map=_effective_tag_map(integ, sync), filters=_sync_filters(sync),
        table_naming=sync["table_naming"])
    fetch = _build_fetch(integ["base_url"], token)
    try:
        tables = fetch_tables(fetch)
    except OMAuthRejected as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except OMUnreachable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return om_config, read_openmetadata(tables, om_config)


# ---- Preview / import (by sync_id) -----------------------------------------------------------


@router.post("/syncs/{sync_id}/preview", dependencies=[Depends(require_catalog_read)])
def preview_sync(sync_id: str, conn: _Conn, identity: _Identity) -> dict:
    """Dry run: pull + translate + predict every ingest verdict WITHOUT ingesting. The brake verdict
    comes from the same large_change_brake the pipeline runs; quarantine from the same validate_rows;
    the diff from the live graph_node catalog. Nothing is written."""
    sync, integ = _resolve_sync(conn, sync_id)
    om_config, translation = _pull(sync, integ)
    try:
        return build_preview(conn, om_config, translation)
    except ValueError as exc:   # empty pull (scope matched nothing) — a client-fixable condition
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/syncs/{sync_id}/import", dependencies=[Depends(require_catalog_write)])
def import_sync(sync_id: str, body: ImportIn, conn: _Conn, identity: _Identity,
                client: Annotated[LLMClient | None, Depends(get_llm_optional)]) -> dict:
    """Confirmed import: re-pull, re-translate, verify the previewed snapshot hash, then run the
    UNCHANGED ingest pipeline in this request's one transaction. Suggestion is never ingestion:
    as-of hints from the preview are NOT applied here — rows carry blank semantics. Records
    integration_import for every attempt (audit), but stamps last_import_at ONLY when rows
    actually landed (status 'ingested') — a brake-held / rejected attempt wrote nothing."""
    sync, integ = _resolve_sync(conn, sync_id)
    _, translation = _pull(sync, integ)
    current_hash = snapshot_hash(translation.rows)
    if current_hash != body.snapshot_hash:
        raise HTTPException(
            status_code=409,
            detail="OpenMetadata changed since this preview (snapshot hash mismatch). "
                   "Run preview again and approve the fresh dry run.")
    result = ingest_upload(conn, sync["target_source"], translation.rows,
                           actor=identity, now=datetime.now(UTC), client=client)
    import_id = store.record_import(
        conn, sync=sync, integration_id=integ["integration_id"], snapshot_hash=current_hash,
        approved_by=identity.subject, result=asdict(result))
    pending = 0
    if result.status == "ingested":
        # Only a real ingest lands rows, so only 'ingested' advances last_import_at: stamping a
        # brake-HELD / REJECTED attempt (which wrote nothing) would falsely claim a synced source.
        # The attempt itself is still audited by record_import above.
        store.touch_sync_last_import(conn, sync["sync_id"], datetime.now(UTC))
        # Every OM row arrives semantics-blank — the translator never sets as-of/additivity/unit/
        # currency/entity — so every row that WASN'T quarantined is semantics-pending. Derive that
        # from the pipeline's own quarantine count rather than re-running validate_rows.
        pending = len(translation.rows) - result.quarantined
    return {
        "result": asdict(result),
        "import_id": import_id,
        "review_queue": {"quarantined": result.quarantined, "semantics_pending": pending},
    }
