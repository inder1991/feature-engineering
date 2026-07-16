"""Two-tier connector storage: integration (one OpenMetadata instance) + integration_sync (one
DatabaseService -> one catalog source) + integration_import (the approved-import audit trail).

Secrets (documented choice per the spec): ``featuregen.privacy.kms`` exposes only a
destroy/rotate ``KeyManager`` Protocol — there is no envelope seal/unseal API to reuse — so the
bot token is stored as an ENVIRONMENT REFERENCE (``token_env``, e.g.
``FEATUREGEN_OM_TOKEN__CORP``), never plaintext. Integration rows hold only the reference; the
token value itself is read from the environment at pull time and is never serialized in any
response.

Import records are an AUDIT trail: they deliberately carry the sync/integration ids as plain text
(no FK), so deleting a sync or integration never erases the history of what it imported.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from featuregen.idgen import mint_id

# ---- Uniqueness conflicts (concurrent-insert loser) ------------------------------------------
#
# The routes pre-check uniqueness (integration name, one sync per service) and 409 on the common
# path. But a read-then-insert races: two callers can both pass the pre-check, and the DB UNIQUE
# constraint then fails ONE of the inserts with a psycopg UniqueViolation — which, left raw, FastAPI
# turns into a 500. The create_* functions catch that violation and re-raise it as one of these
# clean DOMAIN errors, which the route maps to the SAME 409 its pre-check returns; the aborted
# request transaction is then rolled back by the caller (``get_conn`` in prod). Race and common
# path converge on one verdict.


class ConnectorConflict(Exception):
    """Base: a store-level uniqueness conflict (a concurrent-insert loser)."""


class IntegrationNameConflict(ConnectorConflict):
    """An integration ``name`` already exists (the UNIQUE(name) constraint fired)."""


class SyncServiceConflict(ConnectorConflict):
    """A sync for this ``(integration_id, service_name)`` already exists (the UNIQUE constraint)."""


# ---- Integration (tier 1) --------------------------------------------------------------------

_INTEGRATION_COLS = "integration_id, name, base_url, token_env, tag_map, created_by, created_at"


def _integration_to_dict(row: tuple) -> dict[str, Any]:
    return {
        "integration_id": row[0],
        "name": row[1],
        "base_url": row[2],
        "token_env": row[3],
        "tag_map": row[4],
        "created_by": row[5],
        "created_at": row[6].isoformat(),
    }


def create_integration(conn: Any, *, name: str, base_url: str, token_env: str,
                       tag_map: dict[str, str], created_by: str) -> dict[str, Any]:
    integration_id = mint_id("intg")
    try:
        conn.execute(
            "INSERT INTO integration "
            "(integration_id, name, base_url, token_env, tag_map, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (integration_id, name, base_url, token_env, Jsonb(tag_map), created_by))
    except psycopg.errors.UniqueViolation as exc:   # lost a UNIQUE(name) race -> clean 409, not 500
        raise IntegrationNameConflict(name) from exc
    got = get_integration(conn, integration_id)
    assert got is not None
    return got


def integration_name_exists(conn: Any, name: str, *, exclude_id: str | None = None) -> bool:
    if exclude_id is None:
        row = conn.execute("SELECT 1 FROM integration WHERE name = %s", (name,)).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM integration WHERE name = %s AND integration_id <> %s",
            (name, exclude_id)).fetchone()
    return row is not None


def get_integration(conn: Any, integration_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {_INTEGRATION_COLS} FROM integration WHERE integration_id = %s",
        (integration_id,)).fetchone()
    return _integration_to_dict(row) if row else None


def list_integrations(conn: Any) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT {_INTEGRATION_COLS} FROM integration "
        "ORDER BY created_at, integration_id").fetchall()
    return [_integration_to_dict(r) for r in rows]


def update_integration(conn: Any, integration_id: str, *, name: str, base_url: str,
                       token_env: str, tag_map: dict[str, str]) -> dict[str, Any] | None:
    """Replace the mutable fields of an integration (PATCH already merged provided-over-current in
    the route). Returns the fresh row, or None if the integration does not exist."""
    row = conn.execute(
        "UPDATE integration SET name = %s, base_url = %s, token_env = %s, tag_map = %s "
        "WHERE integration_id = %s RETURNING integration_id",
        (name, base_url, token_env, Jsonb(tag_map), integration_id)).fetchone()
    if row is None:
        return None
    return get_integration(conn, integration_id)


def delete_integration(conn: Any, integration_id: str) -> bool:
    """Delete an integration; its syncs cascade (integration_sync FK ON DELETE CASCADE). Import
    history is plain-text (no FK) and survives."""
    row = conn.execute(
        "DELETE FROM integration WHERE integration_id = %s RETURNING integration_id",
        (integration_id,)).fetchone()
    return row is not None


# ---- Sync (tier 2) ---------------------------------------------------------------------------

_SYNC_COLS = ("sync_id, integration_id, service_name, database_filter, schema_filter, "
              "target_source, tag_map_override, table_naming, created_by, created_at, "
              "last_import_at")


def _sync_to_dict(row: tuple) -> dict[str, Any]:
    return {
        "sync_id": row[0],
        "integration_id": row[1],
        "service_name": row[2],
        "database_filter": row[3],
        "schema_filter": row[4],
        "target_source": row[5],
        "tag_map_override": row[6],
        "table_naming": row[7],
        "created_by": row[8],
        "created_at": row[9].isoformat(),
        "last_import_at": row[10].isoformat() if row[10] else None,
    }


def create_sync(conn: Any, *, integration_id: str, service_name: str,
                database_filter: str | None, schema_filter: str | None, target_source: str,
                tag_map_override: dict[str, str] | None, table_naming: str,
                created_by: str) -> dict[str, Any]:
    sync_id = mint_id("sync")
    try:
        conn.execute(
            "INSERT INTO integration_sync (sync_id, integration_id, service_name, "
            "database_filter, schema_filter, target_source, tag_map_override, table_naming, "
            "created_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (sync_id, integration_id, service_name, database_filter, schema_filter,
             target_source, Jsonb(tag_map_override) if tag_map_override is not None else None,
             table_naming, created_by))
    except psycopg.errors.UniqueViolation as exc:   # lost a UNIQUE(service) race -> clean 409
        raise SyncServiceConflict(service_name) from exc
    got = get_sync(conn, sync_id)
    assert got is not None
    return got


def sync_exists_for_service(conn: Any, integration_id: str, service_name: str,
                            *, exclude_id: str | None = None) -> bool:
    """One sync per (integration, service_name) — the default binding. Checked BEFORE insert so a
    duplicate is a clean 409, not a UNIQUE violation that aborts the request transaction."""
    if exclude_id is None:
        row = conn.execute(
            "SELECT 1 FROM integration_sync WHERE integration_id = %s AND service_name = %s",
            (integration_id, service_name)).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM integration_sync WHERE integration_id = %s AND service_name = %s "
            "AND sync_id <> %s", (integration_id, service_name, exclude_id)).fetchone()
    return row is not None


def get_sync(conn: Any, sync_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {_SYNC_COLS} FROM integration_sync WHERE sync_id = %s", (sync_id,)).fetchone()
    return _sync_to_dict(row) if row else None


def list_syncs(conn: Any, integration_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT {_SYNC_COLS} FROM integration_sync WHERE integration_id = %s "
        "ORDER BY created_at, sync_id", (integration_id,)).fetchall()
    return [_sync_to_dict(r) for r in rows]


def update_sync(conn: Any, sync_id: str, *, service_name: str, database_filter: str | None,
                schema_filter: str | None, target_source: str,
                tag_map_override: dict[str, str] | None,
                table_naming: str) -> dict[str, Any] | None:
    row = conn.execute(
        "UPDATE integration_sync SET service_name = %s, database_filter = %s, schema_filter = %s, "
        "target_source = %s, tag_map_override = %s, table_naming = %s WHERE sync_id = %s "
        "RETURNING sync_id",
        (service_name, database_filter, schema_filter, target_source,
         Jsonb(tag_map_override) if tag_map_override is not None else None, table_naming,
         sync_id)).fetchone()
    if row is None:
        return None
    return get_sync(conn, sync_id)


def delete_sync(conn: Any, sync_id: str) -> bool:
    row = conn.execute(
        "DELETE FROM integration_sync WHERE sync_id = %s RETURNING sync_id",
        (sync_id,)).fetchone()
    return row is not None


def touch_sync_last_import(conn: Any, sync_id: str, when: datetime) -> None:
    """Record that an import ran through this sync (stamps last_import_at, surfaced in the UI)."""
    conn.execute("UPDATE integration_sync SET last_import_at = %s WHERE sync_id = %s",
                 (when, sync_id))


# ---- Import audit ----------------------------------------------------------------------------


def record_import(conn: Any, *, sync: dict[str, Any], integration_id: str, snapshot_hash: str,
                  approved_by: str, result: dict[str, Any],
                  ingestion_run_id: str | None = None) -> str:
    """Persist one import's audit record: what ran (which sync/integration), under whose approval,
    with what outcome. The ingest events are attributed to the approving human (the sanctioned
    identity path — see api/routes/integrations.py); the record here names the connector as the
    VEHICLE and carries the sync/integration ids as plain text so the history outlives them.
    ``ingestion_run_id`` links the run manifest (design #3): the run is opened BEFORE the pull, so
    the import row points at the run — a failed pull has its run with no import row at all."""
    import_id = mint_id("omimp")
    conn.execute(
        "INSERT INTO integration_import (import_id, sync_id, integration_id, target_source, "
        "snapshot_hash, approved_by, vehicle, result, ingestion_run_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (import_id, sync["sync_id"], integration_id, sync["target_source"], snapshot_hash,
         approved_by, "openmetadata-connector", Jsonb(result), ingestion_run_id))
    return import_id
