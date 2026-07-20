"""Migration 1013 — the durable, versioned recipe aggregation-declaration registry (Delivery H3a).

The registry is the governed source that populates ``CompilerContext.agg_declarations`` in
production (loaded once by ``build_compiler_context``). It is IMMUTABLE-PER-VERSION (WORM): a
change is a NEW version (a new row) with a fresh effective interval, never an in-place UPDATE. This
suite exercises the DB-enforced invariants: the table + key columns + lookup index, the write-once
row trigger (UPDATE/DELETE raise), the guarded TRUNCATE/UPDATE/DELETE revoke from the production
app role (a FOR EACH ROW trigger does not fire on statement-level TRUNCATE), the ``function`` vocab
CHECK, the well-formed-interval CHECK, and the (recipe_id, need_role, declaration_version) UNIQUE.
Mirrors tests/featuregen/db/test_migration_1009.py.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

import featuregen.db.migrations as _migrations
from featuregen.overlay.upload.planner.declarations import aggregation_declaration_content_hash

_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)

_KEY_COLUMNS = {
    "declaration_id", "recipe_id", "need_role", "function", "declaration_version",
    "authority", "provenance", "effective_from", "effective_to", "content_hash", "created_at",
}


def _insert(conn, *, declaration_id="ad_1", recipe_id="r1", need_role="rate", function="max",
            version=1, authority="governed:test", provenance=None,
            effective_from=_NOW - timedelta(days=1), effective_to=None,
            content_hash=None) -> str:
    ch = content_hash if content_hash is not None else aggregation_declaration_content_hash(
        recipe_id, need_role, function, version, authority)
    conn.execute(
        "INSERT INTO recipe_aggregation_declaration (declaration_id, recipe_id, need_role, "
        "function, declaration_version, authority, provenance, effective_from, effective_to, "
        "content_hash) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (declaration_id, recipe_id, need_role, function, version, authority, provenance,
         effective_from, effective_to, ch))
    return declaration_id


def test_1013_table_exists_with_key_columns(conn) -> None:
    cols = {r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'recipe_aggregation_declaration'").fetchall()}
    assert _KEY_COLUMNS <= cols, f"missing {_KEY_COLUMNS - cols}"


def test_1013_lookup_index_exists(conn) -> None:
    assert conn.execute(
        "SELECT 1 FROM pg_indexes WHERE indexname = %s",
        ("recipe_aggregation_declaration_key_idx",)).fetchone() is not None


def test_1013_row_is_write_once(conn) -> None:
    # IMMUTABLE-PER-VERSION: physically block row DML (a change is a new version, not a mutation).
    _insert(conn, declaration_id="ad_wo")
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"), conn.transaction():
        conn.execute("UPDATE recipe_aggregation_declaration SET function = 'sum' "
                     "WHERE declaration_id = 'ad_wo'")
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"), conn.transaction():
        conn.execute("DELETE FROM recipe_aggregation_declaration WHERE declaration_id = 'ad_wo'")


def test_1013_function_check_rejects_unknown_vocab(conn) -> None:
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        _insert(conn, declaration_id="ad_badfn", function="median")   # not an AggregationFunction


def test_1013_interval_check_rejects_backwards_window(conn) -> None:
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        _insert(conn, declaration_id="ad_badint",
                effective_from=_NOW, effective_to=_NOW - timedelta(days=1))


def test_1013_open_interval_is_allowed(conn) -> None:
    _insert(conn, declaration_id="ad_open", effective_to=None)   # NULL effective_to = open
    row = conn.execute("SELECT effective_to FROM recipe_aggregation_declaration "
                       "WHERE declaration_id = 'ad_open'").fetchone()
    assert row[0] is None


def test_1013_version_identity_unique_rejects_dup(conn) -> None:
    # UNIQUE (recipe_id, need_role, declaration_version): a (recipe, role, version) is minted once.
    _insert(conn, declaration_id="ad_v1a", version=1)
    _insert(conn, declaration_id="ad_v2", version=2)   # a new version is fine
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        _insert(conn, declaration_id="ad_v1b", version=1)   # same (recipe, role, version) — dup


def _migration_1013_sql() -> str:
    return (Path(_migrations.__file__).resolve().parent / "migrations"
            / "1013_recipe_aggregation_declaration.sql").read_text(encoding="utf-8")


def test_1013_worm_revokes_destructive_dml_from_app_role(db) -> None:
    """MF: the BEFORE UPDATE OR DELETE row trigger does NOT fire on a statement-level TRUNCATE, so
    the immutable registry must ALSO have UPDATE/DELETE/TRUNCATE revoked from the production app
    role (mirrors 0900/1002/1009/1012). The role is absent in the superuser test cluster (the
    migration's guarded REVOKE is a no-op there); this test creates it to exercise the guarded
    branch, then re-applies the migration SQL exactly as apply_migrations does. Rolled back on
    teardown."""
    table = "recipe_aggregation_declaration"
    db.execute("CREATE ROLE featuregen_app NOLOGIN")
    db.execute(f"GRANT UPDATE, DELETE, TRUNCATE ON {table} TO featuregen_app")
    for priv in ("UPDATE", "DELETE", "TRUNCATE"):
        assert db.execute("SELECT has_table_privilege('featuregen_app', %s, %s)",
                          (table, priv)).fetchone()[0] is True

    db.execute(_migration_1013_sql())   # applying with the role present must strip the privileges

    for priv in ("UPDATE", "DELETE", "TRUNCATE"):
        assert db.execute("SELECT has_table_privilege('featuregen_app', %s, %s)",
                          (table, priv)).fetchone()[0] is False, \
            f"{priv} on {table} should be revoked from featuregen_app by the WORM migration"
