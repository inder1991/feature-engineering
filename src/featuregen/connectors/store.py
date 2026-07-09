"""connector_config + connector_import storage.

Secrets (documented choice per the spec): ``featuregen.privacy.kms`` exposes only a
destroy/rotate ``KeyManager`` Protocol — there is no envelope seal/unseal API to reuse — so the
bot token is stored as an ENVIRONMENT REFERENCE (``token_env``, e.g.
``FEATUREGEN_OM_TOKEN__CARDS``), never plaintext. Config rows hold only the reference; the token
value itself is read from the environment at pull time and is never serialized in any response.

Import records are an AUDIT trail: they deliberately carry the connector id/name as plain text
(no FK), so deleting a connector configuration never erases the history of what it imported.
"""
from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from featuregen.idgen import mint_id


def _row_to_dict(row: tuple) -> dict[str, Any]:
    return {
        "connector_id": row[0],
        "name": row[1],
        "base_url": row[2],
        "target_source": row[3],
        "tag_map": row[4],
        "filters": row[5],
        "table_naming": row[6],
        "token_env": row[7],
        "created_by": row[8],
        "created_at": row[9].isoformat(),
    }


_COLS = ("connector_id, name, base_url, target_source, tag_map, filters, table_naming, "
         "token_env, created_by, created_at")


def create_connector(conn: Any, *, name: str, base_url: str, target_source: str,
                     tag_map: dict[str, str], filters: dict[str, str], table_naming: str,
                     token_env: str, created_by: str) -> dict[str, Any]:
    connector_id = mint_id("conn")
    conn.execute(
        "INSERT INTO connector_config (connector_id, name, base_url, target_source, tag_map, "
        "filters, table_naming, token_env, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (connector_id, name, base_url, target_source, Jsonb(tag_map), Jsonb(filters),
         table_naming, token_env, created_by))
    got = get_connector(conn, connector_id)
    assert got is not None
    return got


def name_exists(conn: Any, name: str) -> bool:
    return conn.execute("SELECT 1 FROM connector_config WHERE name = %s",
                        (name,)).fetchone() is not None


def get_connector(conn: Any, connector_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {_COLS} FROM connector_config WHERE connector_id = %s",
        (connector_id,)).fetchone()
    return _row_to_dict(row) if row else None


def list_connectors(conn: Any) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT {_COLS} FROM connector_config ORDER BY created_at, connector_id").fetchall()
    return [_row_to_dict(r) for r in rows]


def delete_connector(conn: Any, connector_id: str) -> bool:
    row = conn.execute(
        "DELETE FROM connector_config WHERE connector_id = %s RETURNING connector_id",
        (connector_id,)).fetchone()
    return row is not None


def record_import(conn: Any, *, connector: dict[str, Any], snapshot_hash: str,
                  approved_by: str, result: dict[str, Any]) -> str:
    """Persist one import's audit record: what ran, under whose approval, with what outcome.
    The ingest events are attributed to the approving human (the sanctioned identity path — see
    api/routes/connectors.py); the record here names the connector as the VEHICLE."""
    import_id = mint_id("omimp")
    conn.execute(
        "INSERT INTO connector_import (import_id, connector_id, connector_name, target_source, "
        "snapshot_hash, approved_by, vehicle, result) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (import_id, connector["connector_id"], connector["name"], connector["target_source"],
         snapshot_hash, approved_by, "openmetadata-connector", Jsonb(result)))
    return import_id
