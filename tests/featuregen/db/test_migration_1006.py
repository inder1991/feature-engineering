"""Migration 1006 — catalog_metadata_snapshot + feature_generation_run tables (Delivery C0 Task 1).

C0 gives feature generation a REPRODUCIBLE, drift-aware read of committed catalog state. The
feature-generation workflow (NEVER ingestion) creates ``feature_generation_run`` first, then
persists the EXACT catalog state it consumed as an immutable, hashed snapshot: a header
(``catalog_metadata_snapshot`` — write-once, governance-retained) and one row per consumed
value/ref (``catalog_metadata_snapshot_item`` — write-once). PostgreSQL enforces the invariants
exercised here: the write-once triggers (mirroring llm_call_write_once, 0510), the FKs to real
parents (snapshot -> run, item -> snapshot), and the per-snapshot item-hash UNIQUE.
"""
from __future__ import annotations

import psycopg
import pytest

_KEY_COLUMNS = {
    "feature_generation_run": {
        "generation_run_id", "intent_id", "actor", "flags", "created_at",
    },
    "catalog_metadata_snapshot": {
        "snapshot_id", "generation_run_id", "read_scope_hash", "isolation_level",
        "projection_watermarks", "policy_version", "registry_version", "config_version",
        "content_hash", "created_at",
    },
    "catalog_metadata_snapshot_item": {
        "id", "snapshot_id", "catalog_source", "graph_ref", "logical_ref", "physical_ref",
        "item_kind", "field_or_fact_type", "value_json", "authority_json", "decision_event_id",
        "fact_key", "fact_event_id", "item_hash",
    },
}


def _run(conn, run_id: str = "genrun_c0_1") -> str:
    conn.execute(
        "INSERT INTO feature_generation_run (generation_run_id, actor) "
        "VALUES (%s, '{\"kind\": \"user\", \"id\": \"tester\"}'::jsonb)", (run_id,))
    return run_id


def _snapshot(conn, snapshot_id: str = "snap_c0_1", *, run_id: str | None = None) -> str:
    if run_id is None:
        run_id = _run(conn)
    conn.execute(
        "INSERT INTO catalog_metadata_snapshot (snapshot_id, generation_run_id, read_scope_hash, "
        "isolation_level, content_hash) VALUES (%s, %s, 'sha256:scope', 'repeatable read', "
        "'sha256:content')", (snapshot_id, run_id))
    return snapshot_id


def _item(conn, snapshot_id: str, *, item_hash: str = "sha256:item1") -> None:
    conn.execute(
        "INSERT INTO catalog_metadata_snapshot_item (snapshot_id, catalog_source, graph_ref, "
        "item_kind, field_or_fact_type, item_hash) VALUES (%s, 'deposits', 'graph:accounts', "
        "'field', 'balance', %s)", (snapshot_id, item_hash))


def test_1006_tables_exist_with_key_columns(conn) -> None:
    for table, expected in _KEY_COLUMNS.items():
        cols = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            (table,)).fetchall()}
        assert expected <= cols, f"{table}: missing {expected - cols}"


def test_1006_lookup_indexes_exist(conn) -> None:
    for index in ("catalog_metadata_snapshot_run_idx",
                  "catalog_metadata_snapshot_item_snapshot_idx"):
        assert conn.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname = %s", (index,)).fetchone() is not None


def test_snapshot_requires_a_real_run(conn) -> None:
    # The header FKs the durable run manifest (created FIRST in the feature tx).
    run_id = _run(conn)
    _snapshot(conn, "snap_ok", run_id=run_id)
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        _snapshot(conn, "snap_bad", run_id="genrun_MISSING")


def test_item_requires_a_real_snapshot(conn) -> None:
    conn.execute("SELECT 1")   # open the outer tx before the savepoint (0993/0994/1005 pattern)
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        _item(conn, "snap_MISSING")


def test_catalog_metadata_snapshot_is_write_once(conn) -> None:
    # IMMUTABLE replay header — physically immutable, mirroring llm_call_write_once (0510).
    _snapshot(conn, "snap_wo")
    with pytest.raises(psycopg.errors.RaiseException, match="write-once"), conn.transaction():
        conn.execute("UPDATE catalog_metadata_snapshot SET content_hash = 'x' "
                     "WHERE snapshot_id = 'snap_wo'")
    with pytest.raises(psycopg.errors.RaiseException, match="write-once"), conn.transaction():
        conn.execute("DELETE FROM catalog_metadata_snapshot WHERE snapshot_id = 'snap_wo'")


def test_catalog_metadata_snapshot_item_is_write_once(conn) -> None:
    snapshot_id = _snapshot(conn, "snap_item_wo")
    _item(conn, snapshot_id)
    with pytest.raises(psycopg.errors.RaiseException, match="write-once"), conn.transaction():
        conn.execute("UPDATE catalog_metadata_snapshot_item SET value_json = '{\"x\": 1}'::jsonb "
                     "WHERE snapshot_id = 'snap_item_wo'")
    with pytest.raises(psycopg.errors.RaiseException, match="write-once"), conn.transaction():
        conn.execute("DELETE FROM catalog_metadata_snapshot_item WHERE snapshot_id = 'snap_item_wo'")


def test_item_unique_per_snapshot_and_hash(conn) -> None:
    # An item appears at most once per snapshot: UNIQUE (snapshot_id, item_hash).
    snapshot_id = _snapshot(conn, "snap_uniq")
    _item(conn, snapshot_id, item_hash="sha256:h1")
    _item(conn, snapshot_id, item_hash="sha256:h2")   # a different item hash — fine
    conn.execute("SELECT 1")
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        _item(conn, snapshot_id, item_hash="sha256:h1")
