"""Migration 1014 — the immutable semantic-binding candidate store (Delivery D1).

Four tables: two IMMUTABLE (WORM) — ``semantic_binding_candidate_set`` and
``semantic_binding_candidate`` (per-row UPDATE/DELETE raise + a guarded TRUNCATE/UPDATE/DELETE revoke
from the production app role); one MUTABLE compare-and-swap projection
(``current_semantic_binding_candidate_set``); and an insert-only link
(``semantic_binding_candidate_proposal`` — UPDATE raises, DELETE stays open to retire a stale DRAFT
link). This suite exercises the DB-enforced invariants: the tables + key columns, the write-once
triggers, the guarded revokes, the kind-shape CHECK (currency needs a target + no free value; entity
needs a value + no target), the closed-registry CHECKs, and the deterministic-id UNIQUE keys.
Mirrors tests/featuregen/db/test_migration_1013.py.
"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

import featuregen.db.migrations as _migrations

_SET_COLUMNS = {
    "candidate_set_id", "catalog_source", "table_graph_ref", "ingestion_run_id", "attempt_no",
    "metadata_input_fingerprint", "task_version", "prompt_version", "schema_version",
    "config_version", "completion_status", "content_hash", "created_at",
}
_CANDIDATE_COLUMNS = {
    "candidate_id", "candidate_set_id", "catalog_source", "subject_graph_ref", "subject_logical_ref",
    "binding_kind", "target_graph_ref", "target_logical_ref", "proposed_value", "disposition",
    "reason_codes", "evidence_json", "input_hash", "model_version", "prompt_version",
    "schema_version", "config_version", "llm_call_ref", "created_at",
}
_CURRENT_COLUMNS = {
    "catalog_source", "table_graph_ref", "candidate_set_id", "metadata_input_fingerprint",
    "status", "projected_at",
}
_PROPOSAL_COLUMNS = {"candidate_id", "fact_key", "proposed_event_id", "created_at"}


def _set(conn, *, set_id="sbcs_1", ingestion_run_id="run_1", attempt_no=1, catalog_source="src",
         table_graph_ref="public.txn", fingerprint="fp_1", completion_status="complete",
         content_hash="ch_1") -> str:
    conn.execute(
        "INSERT INTO semantic_binding_candidate_set (candidate_set_id, catalog_source, "
        "table_graph_ref, ingestion_run_id, attempt_no, metadata_input_fingerprint, task_version, "
        "prompt_version, schema_version, config_version, completion_status, content_hash) "
        "VALUES (%s, %s, %s, %s, %s, %s, 'tv1', 'pv1', 'sv1', 'cv1', %s, %s)",
        (set_id, catalog_source, table_graph_ref, ingestion_run_id, attempt_no, fingerprint,
         completion_status, content_hash))
    return set_id


def _candidate(conn, *, candidate_id="sbc_1", set_id="sbcs_1", catalog_source="src",
               subject_graph_ref="public.txn.amt", subject_logical_ref="src::public.txn.amt",
               binding_kind="currency_binding", target_graph_ref="public.txn.ccy",
               target_logical_ref="src::public.txn.ccy", proposed_value=None,
               disposition="strong", input_hash="ih_1") -> str:
    conn.execute(
        "INSERT INTO semantic_binding_candidate (candidate_id, candidate_set_id, catalog_source, "
        "subject_graph_ref, subject_logical_ref, binding_kind, target_graph_ref, "
        "target_logical_ref, proposed_value, disposition, input_hash, model_version, "
        "prompt_version, schema_version, config_version) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'm1', 'pv1', 'sv1', 'cv1')",
        (candidate_id, set_id, catalog_source, subject_graph_ref, subject_logical_ref, binding_kind,
         target_graph_ref, target_logical_ref,
         None if proposed_value is None else Jsonb(proposed_value), disposition, input_hash))
    return candidate_id


# --------------------------------------------------------------------------------------------------
# 1) Structure — the four tables + key columns exist.
# --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("table,expected", [
    ("semantic_binding_candidate_set", _SET_COLUMNS),
    ("semantic_binding_candidate", _CANDIDATE_COLUMNS),
    ("current_semantic_binding_candidate_set", _CURRENT_COLUMNS),
    ("semantic_binding_candidate_proposal", _PROPOSAL_COLUMNS),
])
def test_1014_tables_have_key_columns(conn, table, expected) -> None:
    cols = {r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
        (table,)).fetchall()}
    assert expected <= cols, f"{table} missing {expected - cols}"


# --------------------------------------------------------------------------------------------------
# 2) WORM — the two immutable tables block UPDATE and DELETE.
# --------------------------------------------------------------------------------------------------
def test_1014_candidate_set_is_write_once(conn) -> None:
    _set(conn)
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"), conn.transaction():
        conn.execute("UPDATE semantic_binding_candidate_set SET completion_status = 'failed' "
                     "WHERE candidate_set_id = 'sbcs_1'")
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"), conn.transaction():
        conn.execute("DELETE FROM semantic_binding_candidate_set WHERE candidate_set_id = 'sbcs_1'")


def test_1014_candidate_is_write_once(conn) -> None:
    _set(conn)
    _candidate(conn)
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"), conn.transaction():
        conn.execute("UPDATE semantic_binding_candidate SET disposition = 'weak' "
                     "WHERE candidate_id = 'sbc_1'")
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"), conn.transaction():
        conn.execute("DELETE FROM semantic_binding_candidate WHERE candidate_id = 'sbc_1'")


# --------------------------------------------------------------------------------------------------
# 3) The proposal link is insert-only: UPDATE raises, DELETE is allowed (stale-link retirement).
# --------------------------------------------------------------------------------------------------
def test_1014_proposal_update_is_blocked_delete_allowed(conn) -> None:
    _set(conn)
    _candidate(conn)
    conn.execute(
        "INSERT INTO semantic_binding_candidate_proposal (candidate_id, fact_key, proposed_event_id) "
        "VALUES ('sbc_1', 'fk_1', 'evt_1')")
    with pytest.raises(psycopg.errors.RaiseException, match="insert-only"), conn.transaction():
        conn.execute("UPDATE semantic_binding_candidate_proposal SET fact_key = 'fk_2' "
                     "WHERE candidate_id = 'sbc_1'")
    # DELETE stays open — a stale DRAFT link is retired by removing the row.
    conn.execute("DELETE FROM semantic_binding_candidate_proposal WHERE candidate_id = 'sbc_1'")
    assert conn.execute("SELECT 1 FROM semantic_binding_candidate_proposal "
                        "WHERE candidate_id = 'sbc_1'").fetchone() is None


# --------------------------------------------------------------------------------------------------
# 4) The current-set projection is MUTABLE (the CAS target) — UPDATE succeeds.
# --------------------------------------------------------------------------------------------------
def test_1014_current_projection_is_mutable(conn) -> None:
    _set(conn)
    conn.execute(
        "INSERT INTO current_semantic_binding_candidate_set (catalog_source, table_graph_ref, "
        "candidate_set_id, metadata_input_fingerprint, status) "
        "VALUES ('src', 'public.txn', 'sbcs_1', 'fp_1', 'current')")
    conn.execute("UPDATE current_semantic_binding_candidate_set SET status = 'unverifiable', "
                 "candidate_set_id = NULL WHERE catalog_source = 'src'")
    assert conn.execute("SELECT status FROM current_semantic_binding_candidate_set "
                        "WHERE catalog_source = 'src'").fetchone()[0] == "unverifiable"


# --------------------------------------------------------------------------------------------------
# 5) Kind-shape CHECK — currency needs a target + no free value; entity needs a value + no target.
# --------------------------------------------------------------------------------------------------
def test_1014_currency_requires_target(conn) -> None:
    _set(conn)
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        _candidate(conn, binding_kind="currency_binding", target_graph_ref=None,
                   target_logical_ref=None, proposed_value=None)


def test_1014_currency_rejects_free_value(conn) -> None:
    _set(conn)
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        _candidate(conn, binding_kind="currency_binding", proposed_value={"iso": "USD"})


def test_1014_entity_requires_value(conn) -> None:
    _set(conn)
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        _candidate(conn, binding_kind="entity_assignment", target_graph_ref=None,
                   target_logical_ref=None, proposed_value=None)


def test_1014_entity_rejects_target_ref(conn) -> None:
    _set(conn)
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        _candidate(conn, binding_kind="entity_assignment", target_graph_ref="public.txn.ccy",
                   target_logical_ref="src::public.txn.ccy", proposed_value={"entity_id": "customer"})


def test_1014_entity_valid_shape_accepted(conn) -> None:
    _set(conn)
    _candidate(conn, candidate_id="sbc_ent", binding_kind="entity_assignment", target_graph_ref=None,
               target_logical_ref=None, proposed_value={"entity_id": "customer"})
    assert conn.execute("SELECT binding_kind FROM semantic_binding_candidate "
                        "WHERE candidate_id = 'sbc_ent'").fetchone()[0] == "entity_assignment"


# --------------------------------------------------------------------------------------------------
# 6) Closed-registry CHECKs — binding_kind / disposition / completion_status / status.
# --------------------------------------------------------------------------------------------------
def test_1014_unknown_binding_kind_rejected(conn) -> None:
    _set(conn)
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        _candidate(conn, binding_kind="grain_binding")


def test_1014_unknown_disposition_rejected(conn) -> None:
    _set(conn)
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        _candidate(conn, disposition="maybe")


def test_1014_unknown_completion_status_rejected(conn) -> None:
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        _set(conn, completion_status="in_progress")


def test_1014_unknown_current_status_rejected(conn) -> None:
    _set(conn)
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute(
            "INSERT INTO current_semantic_binding_candidate_set (catalog_source, table_graph_ref, "
            "candidate_set_id, metadata_input_fingerprint, status) "
            "VALUES ('src', 'public.txn', 'sbcs_1', 'fp_1', 'stale')")


def test_1014_current_status_shape_enforced(conn) -> None:
    _set(conn)
    # 'current' MUST point at a set.
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute(
            "INSERT INTO current_semantic_binding_candidate_set (catalog_source, table_graph_ref, "
            "candidate_set_id, metadata_input_fingerprint, status) "
            "VALUES ('src', 'public.txn', NULL, 'fp_1', 'current')")
    # 'unverifiable' MUST NOT point at a set.
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute(
            "INSERT INTO current_semantic_binding_candidate_set (catalog_source, table_graph_ref, "
            "candidate_set_id, metadata_input_fingerprint, status) "
            "VALUES ('src', 'public.txn', 'sbcs_1', 'fp_1', 'unverifiable')")


# --------------------------------------------------------------------------------------------------
# 7) Deterministic-id UNIQUE keys reject duplicate replay inserts.
# --------------------------------------------------------------------------------------------------
def test_1014_set_replay_tuple_is_unique(conn) -> None:
    _set(conn, set_id="sbcs_a")
    # a DIFFERENT id but the SAME identity tuple is a duplicate replay — rejected.
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        _set(conn, set_id="sbcs_b")
    # a new attempt_no is a legitimate NEW set (retry / supersession).
    _set(conn, set_id="sbcs_c", attempt_no=2)


def test_1014_candidate_replay_tuple_is_unique(conn) -> None:
    _set(conn)
    _candidate(conn, candidate_id="sbc_a")
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        _candidate(conn, candidate_id="sbc_b")   # same (set, kind, subject, target, input_hash)


def test_1014_candidate_unique_nulls_not_distinct(conn) -> None:
    # entity_assignment has a NULL target — NULLS NOT DISTINCT still collides the replay tuple.
    _set(conn)
    _candidate(conn, candidate_id="sbc_e1", binding_kind="entity_assignment", target_graph_ref=None,
               target_logical_ref=None, proposed_value={"entity_id": "customer"})
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        _candidate(conn, candidate_id="sbc_e2", binding_kind="entity_assignment",
                   target_graph_ref=None, target_logical_ref=None,
                   proposed_value={"entity_id": "customer"})


# --------------------------------------------------------------------------------------------------
# 8) Guarded revoke — destructive DML is stripped from the app role on the immutable tables; the
#    proposal link keeps DELETE (mirrors the 1012/1013 revoke tests). Rolled back on teardown.
# --------------------------------------------------------------------------------------------------
def _migration_1014_sql() -> str:
    return (Path(_migrations.__file__).resolve().parent / "migrations"
            / "1014_semantic_binding_candidate.sql").read_text(encoding="utf-8")


def test_1014_worm_revokes_destructive_dml_from_app_role(db) -> None:
    immutable = ("semantic_binding_candidate_set", "semantic_binding_candidate")
    db.execute("CREATE ROLE featuregen_app NOLOGIN")
    for table in (*immutable, "semantic_binding_candidate_proposal"):
        db.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON {table} TO featuregen_app")

    db.execute(_migration_1014_sql())   # applying with the role present strips destructive DML

    for table in immutable:
        for priv in ("UPDATE", "DELETE", "TRUNCATE"):
            assert db.execute("SELECT has_table_privilege('featuregen_app', %s, %s)",
                              (table, priv)).fetchone()[0] is False, \
                f"{priv} on {table} must be revoked from featuregen_app"
    # the proposal link loses UPDATE/TRUNCATE but KEEPS DELETE (stale-link retirement) + INSERT.
    proposal = "semantic_binding_candidate_proposal"
    for priv in ("UPDATE", "TRUNCATE"):
        assert db.execute("SELECT has_table_privilege('featuregen_app', %s, %s)",
                          (proposal, priv)).fetchone()[0] is False
    for priv in ("SELECT", "INSERT", "DELETE"):
        assert db.execute("SELECT has_table_privilege('featuregen_app', %s, %s)",
                          (proposal, priv)).fetchone()[0] is True, \
            f"{priv} on {proposal} must survive the revoke"


def test_1014_reapply_is_idempotent(db) -> None:
    db.execute(_migration_1014_sql())
    db.execute(_migration_1014_sql())
    assert db.execute("SELECT 1 FROM pg_trigger "
                      "WHERE tgname = 'semantic_binding_candidate_set_no_mutation'").fetchone() \
        is not None
