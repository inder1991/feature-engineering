"""Migration 1115: the spine's three tables, their triggers, and the chain's composite FKs."""
import psycopg
import pytest
from tests.featuregen.runs._chain import seed_run_chain

_IDENTITY_COLS = (
    "generation_run_id, workflow_definition_version, intent_id, confirmed_scope_id, "
    "generation_input_content_hash, considered_revision_id, considered_content_hash, "
    "metadata_snapshot_id, metadata_snapshot_content_hash, owner_subject, owner_tenant, "
    "root_generation_run_id, run_identity_hash, created_by")


def _insert_identity(conn, c, run_id=None):
    rid = run_id or c["run_id"]
    conn.execute(
        f"INSERT INTO feature_run_identity ({_IDENTITY_COLS}) "
        "VALUES (%s, 'V1', %s, %s, 'gh', %s, 'cch', %s, 'ch', %s, NULL, %s, 'idh', 'test')",
        (rid, c["intent_id"], c["scope_id"], c["considered_revision_id"],
         c["snapshot_id"], c["subject"], rid))


def test_identity_row_inserts_when_the_chain_exists(db):
    c = seed_run_chain(db, run_id="m1115-a")
    _insert_identity(db, c)
    row = db.execute("SELECT workflow_definition_version, owner_subject "
                     "FROM feature_run_identity WHERE generation_run_id='m1115-a'").fetchone()
    assert row == ("V1", "u1")


def test_identity_is_write_once(db):
    c = seed_run_chain(db, run_id="m1115-b")
    _insert_identity(db, c)
    with pytest.raises(psycopg.errors.RaiseException):
        db.execute("UPDATE feature_run_identity SET owner_subject='x' "
                   "WHERE generation_run_id='m1115-b'")


def test_chain_fk_refuses_a_considered_revision_from_another_run(db):
    a = seed_run_chain(db, run_id="m1115-c1")
    b = seed_run_chain(db, run_id="m1115-c2")
    mixed = {**a, "considered_revision_id": b["considered_revision_id"]}
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _insert_identity(db, mixed)


def test_root_parent_check(db):
    c = seed_run_chain(db, run_id="m1115-d")
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            f"INSERT INTO feature_run_identity ({_IDENTITY_COLS}) "
            "VALUES ('m1115-d', 'V1', %s, %s, 'gh', %s, 'cch', %s, 'ch', 'u1', NULL, "
            "'someone-else', 'idh', 'test')",  # parent NULL but root != self
            (c["intent_id"], c["scope_id"], c["considered_revision_id"], c["snapshot_id"]))


def test_profile_and_state_tables_exist_and_state_ships_empty(db):
    db.execute("INSERT INTO feature_run_profile (generation_run_id, display_name) "
               "VALUES ('m1115-e', 'My run')")
    assert db.execute("SELECT count(*) FROM feature_run_state").fetchone()[0] == 0
