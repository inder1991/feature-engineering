"""Migration 1011 — contract-version pointer-model schema (Delivery H, ADDITIVE ONLY).

This task lays the SCHEMA foundation for Delivery H's immutable contract-version + pointer model:
  * additive NULLABLE columns on ``contract`` (metadata_input_fingerprint, generation_source,
    recipe_id, physical_plan_id, planner_declaration_id, initial_validation_status,
    initial_verification) — no writer, no CHECK, no backfill;
  * ``contract_input_column`` + ``contract_metadata_dependency`` — physically write-once tables
    (BEFORE UPDATE OR DELETE row triggers RAISE) whose destructive DML is ALSO revoked from the
    production ``featuregen_app`` role (a row trigger does NOT fire on a statement-level TRUNCATE),
    mirroring 0900/1002/1009;
  * ``feature_current_contract`` — the MUTABLE CAS pointer (NOT write-once; an UPDATE must succeed),
    whose composite FK (feature_id, contract_id) -> ``contract`` guarantees a contract can never
    become current for a feature it does not belong to;
  * ``feature_versions.contract_id`` — a nullable FK -> ``contract``;
  * an ORPHAN AUDIT that FAILS LOUD (RAISE) rather than deleting/reparenting a contract whose
    feature_id has no matching feature.
PostgreSQL enforces the invariants exercised here. Mirrors tests/featuregen/db/test_migration_1009.py.
"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

import featuregen.db.migrations as _migrations

# The new columns 1011 adds to `contract` (metadata_snapshot_id is DELIBERATELY absent — 1008 added it).
_NEW_CONTRACT_COLUMNS = {
    "metadata_input_fingerprint", "generation_source", "recipe_id", "physical_plan_id",
    "planner_declaration_id", "initial_validation_status", "initial_verification",
}

_KEY_COLUMNS = {
    "contract_input_column": {
        "contract_id", "source", "graph_ref", "logical_ref", "physical_ref", "role",
        "decision_id", "fact_id", "item_hash", "created_at",
    },
    "contract_metadata_dependency": {
        "contract_id", "catalog_source", "graph_ref", "logical_ref", "decision_id", "fact_id",
        "event_id", "item_hash", "created_at",
    },
    "feature_current_contract": {"feature_id", "contract_id", "pointer_version", "set_at"},
}


def _migration_1011_sql() -> str:
    return (Path(_migrations.__file__).resolve().parent / "migrations"
            / "1011_contract_pointer_model.sql").read_text(encoding="utf-8")


def _feature(conn, feature_id: str) -> str:
    # feature.name carries a UNIQUE constraint (0970), so name is derived from the (unique) feature_id.
    conn.execute("INSERT INTO feature (feature_id, name) VALUES (%s, %s)", (feature_id, feature_id))
    return feature_id


def _contract(conn, contract_id: str, *, feature_id: str, version: int = 1) -> str:
    """A parent contract row. contract.feature_id FKs feature (0972); feature_name/version are NOT NULL
    (0960). feature_name is set to the (unique) contract_id so the 0961 UNIQUE(feature_name, version)
    never collides when a test needs several contracts under one feature."""
    conn.execute(
        "INSERT INTO contract (contract_id, feature_id, feature_name, version) VALUES (%s, %s, %s, %s)",
        (contract_id, feature_id, contract_id, version))
    return contract_id


def _input(conn, contract_id: str, *, item_hash: str = "h1", source: str = "cat") -> None:
    conn.execute(
        "INSERT INTO contract_input_column (contract_id, source, item_hash) VALUES (%s, %s, %s)",
        (contract_id, source, item_hash))


def _dep(conn, contract_id: str, *, item_hash: str = "h1", catalog_source: str = "cat") -> None:
    conn.execute(
        "INSERT INTO contract_metadata_dependency (contract_id, catalog_source, item_hash) "
        "VALUES (%s, %s, %s)", (contract_id, catalog_source, item_hash))


# --------------------------------------------------------------------------------------------------
# 1) Structure: new tables + columns exist with the right nullability + write-once triggers + the FK.
# --------------------------------------------------------------------------------------------------
def test_1011_new_tables_exist_with_key_columns(conn) -> None:
    for table, expected in _KEY_COLUMNS.items():
        cols = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            (table,)).fetchall()}
        assert expected <= cols, f"{table}: missing {expected - cols}"


def test_1011_contract_columns_added_and_nullable(conn) -> None:
    rows = dict(conn.execute(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'contract' AND column_name = ANY(%s)",
        (list(_NEW_CONTRACT_COLUMNS),)).fetchall())
    assert set(rows) == _NEW_CONTRACT_COLUMNS, f"missing {_NEW_CONTRACT_COLUMNS - set(rows)}"
    assert all(v == "YES" for v in rows.values()), f"non-nullable added columns: {rows}"


def test_1011_metadata_snapshot_id_not_redefined_here(conn) -> None:
    # metadata_snapshot_id was added by 1008 (skipped here); it must still exist as text.
    row = conn.execute(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name = 'contract' AND column_name = 'metadata_snapshot_id'").fetchone()
    assert row is not None and row[0] == "text"


def test_1011_lookup_indexes_exist(conn) -> None:
    for index in ("contract_input_column_contract_idx", "contract_metadata_dependency_contract_idx"):
        assert conn.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname = %s", (index,)).fetchone() is not None


def test_1011_composite_unique_and_fk_present(conn) -> None:
    for conname in ("contract_feature_contract_unique", "contract_feature_id_fk",
                    "feature_versions_contract_id_fk"):
        assert conn.execute(
            "SELECT 1 FROM pg_constraint WHERE conname = %s", (conname,)).fetchone() is not None


def test_1011_feature_versions_contract_id_added_and_nullable(conn) -> None:
    row = conn.execute(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name = 'feature_versions' AND column_name = 'contract_id'").fetchone()
    assert row is not None and row[0] == "YES"


# --------------------------------------------------------------------------------------------------
# 2) Write-once holds on both dependency tables (UPDATE + DELETE raise via the row trigger).
# --------------------------------------------------------------------------------------------------
def test_input_column_is_write_once(conn) -> None:
    f = _feature(conn, "f_ic_wo")
    cid = _contract(conn, "c_ic_wo", feature_id=f)
    _input(conn, cid, item_hash="h_wo")
    with pytest.raises(psycopg.errors.RaiseException, match="write-once"), conn.transaction():
        conn.execute("UPDATE contract_input_column SET role = 'x' WHERE contract_id = %s", (cid,))
    with pytest.raises(psycopg.errors.RaiseException, match="write-once"), conn.transaction():
        conn.execute("DELETE FROM contract_input_column WHERE contract_id = %s", (cid,))


def test_metadata_dependency_is_write_once(conn) -> None:
    f = _feature(conn, "f_md_wo")
    cid = _contract(conn, "c_md_wo", feature_id=f)
    _dep(conn, cid, item_hash="h_wo")
    with pytest.raises(psycopg.errors.RaiseException, match="write-once"), conn.transaction():
        conn.execute("UPDATE contract_metadata_dependency SET logical_ref = 'x' "
                     "WHERE contract_id = %s", (cid,))
    with pytest.raises(psycopg.errors.RaiseException, match="write-once"), conn.transaction():
        conn.execute("DELETE FROM contract_metadata_dependency WHERE contract_id = %s", (cid,))


def test_input_column_requires_real_contract(conn) -> None:
    conn.execute("SELECT 1")
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        conn.execute("INSERT INTO contract_input_column (contract_id, source, item_hash) "
                     "VALUES ('c_MISSING', 'cat', 'h')")


def test_input_column_pk_rejects_dup_item_hash(conn) -> None:
    f = _feature(conn, "f_ic_dup")
    cid = _contract(conn, "c_ic_dup", feature_id=f)
    _input(conn, cid, item_hash="dup")
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        _input(conn, cid, item_hash="dup", source="other")


# --------------------------------------------------------------------------------------------------
# 3) WORM TRUNCATE guard: the two write-once tables are revoked from featuregen_app; the mutable
#    pointer is NOT (mirrors the 1009 test exactly).
# --------------------------------------------------------------------------------------------------
def test_1011_worm_revokes_destructive_dml_on_dependency_tables(db) -> None:
    worm_tables = ("contract_input_column", "contract_metadata_dependency")
    db.execute("CREATE ROLE featuregen_app NOLOGIN")
    for table in worm_tables:
        db.execute(f"GRANT UPDATE, DELETE, TRUNCATE ON {table} TO featuregen_app")
        for priv in ("UPDATE", "DELETE", "TRUNCATE"):
            assert db.execute("SELECT has_table_privilege('featuregen_app', %s, %s)",
                              (table, priv)).fetchone()[0] is True

    db.execute(_migration_1011_sql())   # applying with the role present must strip the privileges

    for table in worm_tables:
        for priv in ("UPDATE", "DELETE", "TRUNCATE"):
            assert db.execute("SELECT has_table_privilege('featuregen_app', %s, %s)",
                              (table, priv)).fetchone()[0] is False, \
                f"{priv} on {table} should be revoked from featuregen_app by the WORM migration"

    # The mutable CAS pointer is NOT revoked — repointing to a new contract version is an UPDATE.
    db.execute("GRANT UPDATE, TRUNCATE ON feature_current_contract TO featuregen_app")
    db.execute(_migration_1011_sql())
    for priv in ("UPDATE", "TRUNCATE"):
        assert db.execute(
            "SELECT has_table_privilege('featuregen_app', 'feature_current_contract', %s)",
            (priv,)).fetchone()[0] is True, \
            f"{priv} on the mutable feature_current_contract pointer must NOT be revoked"


# --------------------------------------------------------------------------------------------------
# 4) feature_current_contract — the mutable pointer + its composite-FK integrity.
# --------------------------------------------------------------------------------------------------
def test_pointer_accepts_a_real_feature_contract_pair(conn) -> None:
    f = _feature(conn, "f_fcc_ok")
    cid = _contract(conn, "c_fcc_ok", feature_id=f)
    conn.execute("INSERT INTO feature_current_contract (feature_id, contract_id, pointer_version) "
                 "VALUES (%s, %s, 1)", (f, cid))
    row = conn.execute("SELECT contract_id, pointer_version FROM feature_current_contract "
                       "WHERE feature_id = %s", (f,)).fetchone()
    assert row == (cid, 1)


def test_pointer_is_mutable_not_write_once(conn) -> None:
    # The CAS pointer MUST be UPDATE-able (repoint to a new version) — it is DELIBERATELY not write-once.
    f = _feature(conn, "f_fcc_mut")
    c1 = _contract(conn, "c_fcc_mut1", feature_id=f, version=1)
    c2 = _contract(conn, "c_fcc_mut2", feature_id=f, version=2)
    conn.execute("INSERT INTO feature_current_contract (feature_id, contract_id, pointer_version) "
                 "VALUES (%s, %s, 1)", (f, c1))
    conn.execute("UPDATE feature_current_contract SET contract_id = %s, pointer_version = 2 "
                 "WHERE feature_id = %s", (c2, f))
    row = conn.execute("SELECT contract_id, pointer_version FROM feature_current_contract "
                       "WHERE feature_id = %s", (f,)).fetchone()
    assert row == (c2, 2)


def test_pointer_rejects_cross_feature_pair(conn) -> None:
    # A contract that belongs to a DIFFERENT feature can never become current for this one: (f_other,
    # c2) is not a real contract, so the composite FK -> contract(feature_id, contract_id) rejects it.
    f2 = _feature(conn, "f_fcc_x2")
    c2 = _contract(conn, "c_fcc_x2", feature_id=f2)
    f_other = _feature(conn, "f_fcc_other")
    conn.execute("SELECT 1")
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        conn.execute("INSERT INTO feature_current_contract (feature_id, contract_id, pointer_version) "
                     "VALUES (%s, %s, 1)", (f_other, c2))


def test_pointer_rejects_nonexistent_contract(conn) -> None:
    f = _feature(conn, "f_fcc_miss")
    conn.execute("SELECT 1")
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        conn.execute("INSERT INTO feature_current_contract (feature_id, contract_id, pointer_version) "
                     "VALUES (%s, 'c_NOPE', 1)", (f,))


# --------------------------------------------------------------------------------------------------
# 5) Orphan audit FAILS LOUD (does not delete / reparent). The FK from 0972 already blocks orphans, so
#    the audit is exercised by dropping the FK, seeding an orphan, and re-applying the migration: its
#    audit runs BEFORE the guarded FK re-statement and RAISEs. All rolled back on teardown.
# --------------------------------------------------------------------------------------------------
def test_orphan_audit_fails_loudly(db) -> None:
    db.execute("SELECT 1")
    db.execute("ALTER TABLE contract DROP CONSTRAINT contract_feature_id_fk")
    db.execute("INSERT INTO contract (contract_id, feature_id, feature_name, version) "
               "VALUES ('c_orphan_1011', 'f_missing_1011', 'c_orphan_1011', 1)")
    with pytest.raises(psycopg.errors.RaiseException, match="orphan audit"):
        db.execute(_migration_1011_sql())
    # The migration ABORTS on the audit RAISE — it never deletes/reparents the orphan. The dropped FK,
    # the seeded orphan, and the aborted-tx state are all discarded by the fixture's teardown rollback.
    db.rollback()


def test_orphan_free_db_applies_clean_and_fk_rejects_orphan_insert(conn) -> None:
    # Positive side of the audit: the real (orphan-free) DB migrated clean, and the now-present FK
    # rejects an orphan contract insert outright.
    conn.execute("SELECT 1")
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        conn.execute("INSERT INTO contract (contract_id, feature_id, feature_name, version) "
                     "VALUES ('c_fk_orphan', 'f_no_such_feature', 'c_fk_orphan', 1)")
