"""Migration 1012 — contract immutability (WORM). Delivery H2d.

A confirmed contract VERSION is now physically immutable: a BEFORE UPDATE OR DELETE row trigger RAISEs
(blocking row DML for EVERY role, incl. a superuser — a trigger is not bypassed by grants), and
destructive DML (UPDATE/DELETE/TRUNCATE) is REVOKED from the production ``featuregen_app`` role (a row
trigger does NOT fire on a statement-level TRUNCATE). INSERT (a new version) + SELECT stay allowed.
Mirrors tests/featuregen/db/test_migration_1011.py.
"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

import featuregen.db.migrations as _migrations


def _migration_1012_sql() -> str:
    return (Path(_migrations.__file__).resolve().parent / "migrations"
            / "1012_contract_worm.sql").read_text(encoding="utf-8")


def _feature(conn, feature_id: str) -> str:
    conn.execute("INSERT INTO feature (feature_id, name) VALUES (%s, %s)", (feature_id, feature_id))
    return feature_id


def _contract(conn, contract_id: str, *, feature_id: str, version: int = 1) -> str:
    conn.execute(
        "INSERT INTO contract (contract_id, feature_id, feature_name, version) VALUES (%s, %s, %s, %s)",
        (contract_id, feature_id, contract_id, version))
    return contract_id


# --------------------------------------------------------------------------------------------------
# 1) Structure — the WORM function + trigger are installed.
# --------------------------------------------------------------------------------------------------
def test_1012_worm_trigger_and_function_exist(conn) -> None:
    assert conn.execute(
        "SELECT 1 FROM pg_trigger WHERE tgname = 'contract_no_mutation'").fetchone() is not None
    assert conn.execute(
        "SELECT 1 FROM pg_proc WHERE proname = 'contract_write_once'").fetchone() is not None


# --------------------------------------------------------------------------------------------------
# 2) The row trigger blocks UPDATE and DELETE (RAISE), even for the superuser test role.
# --------------------------------------------------------------------------------------------------
def test_contract_update_is_blocked(conn) -> None:
    f = _feature(conn, "f_worm_upd")
    cid = _contract(conn, "c_worm_upd", feature_id=f)
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"), conn.transaction():
        conn.execute("UPDATE contract SET definition = 'tampered' WHERE contract_id = %s", (cid,))
    # the row is unchanged (the aborted savepoint discarded the attempt).
    assert conn.execute("SELECT definition FROM contract WHERE contract_id = %s",
                        (cid,)).fetchone()[0] == ""


def test_contract_delete_is_blocked(conn) -> None:
    f = _feature(conn, "f_worm_del")
    cid = _contract(conn, "c_worm_del", feature_id=f)
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"), conn.transaction():
        conn.execute("DELETE FROM contract WHERE contract_id = %s", (cid,))
    assert conn.execute("SELECT 1 FROM contract WHERE contract_id = %s", (cid,)).fetchone() is not None


def test_contract_insert_of_a_new_version_still_allowed(conn) -> None:
    # WORM blocks UPDATE/DELETE only — appending a NEW version (how confirm records history) is unaffected.
    f = _feature(conn, "f_worm_ins")
    _contract(conn, "c_worm_ins_v1", feature_id=f, version=1)
    _contract(conn, "c_worm_ins_v2", feature_id=f, version=2)
    assert conn.execute("SELECT count(*) FROM contract WHERE feature_id = %s", (f,)).fetchone()[0] == 2


# --------------------------------------------------------------------------------------------------
# 3) WORM TRUNCATE guard: destructive DML is revoked from featuregen_app; SELECT/INSERT survive
#    (mirrors the 1009/1011 revoke tests). The role is absent in the superuser session cluster, so the
#    revoke is exercised by creating it, granting, re-applying the migration, then rolling back.
# --------------------------------------------------------------------------------------------------
def test_1012_worm_revokes_destructive_dml_from_app_role(db) -> None:
    db.execute("CREATE ROLE featuregen_app NOLOGIN")
    db.execute("GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON contract TO featuregen_app")
    for priv in ("UPDATE", "DELETE", "TRUNCATE"):
        assert db.execute("SELECT has_table_privilege('featuregen_app', 'contract', %s)",
                          (priv,)).fetchone()[0] is True

    db.execute(_migration_1012_sql())   # applying with the role present strips destructive DML

    for priv in ("UPDATE", "DELETE", "TRUNCATE"):
        assert db.execute("SELECT has_table_privilege('featuregen_app', 'contract', %s)",
                          (priv,)).fetchone()[0] is False, \
            f"{priv} on contract must be revoked from featuregen_app by the WORM migration"
    # SELECT + INSERT are NOT revoked — the app still reads contracts and confirms new versions.
    for priv in ("SELECT", "INSERT"):
        assert db.execute("SELECT has_table_privilege('featuregen_app', 'contract', %s)",
                          (priv,)).fetchone()[0] is True, \
            f"{priv} on contract must survive the WORM revoke"


def test_1012_reapply_is_idempotent(db) -> None:
    # Re-runnable in the repo style (CREATE OR REPLACE + guarded REVOKE): a second application is clean.
    db.execute(_migration_1012_sql())
    db.execute(_migration_1012_sql())
    assert db.execute(
        "SELECT 1 FROM pg_trigger WHERE tgname = 'contract_no_mutation'").fetchone() is not None
