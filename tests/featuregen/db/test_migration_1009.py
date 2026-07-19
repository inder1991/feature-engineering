"""Migration 1009 — feature-contract validation event/state/requirement tables (Delivery C4 Task 1).

C4 makes contract validation a version-scoped, APPEND-ONLY EVENT lifecycle whose current-state
PROJECTION is the authoritative effective stamp. It sits ON TOP of the shipped 1003
``contract.validation_status``/``requirements`` columns (the INITIAL stamp, unchanged). This task
creates the three tables; PostgreSQL enforces the invariants exercised here:
  * ``feature_contract_validation_event`` — APPEND-ONLY, write-once (UPDATE/DELETE raise), with a
    per-table monotonic ``seq`` (GENERATED ALWAYS AS IDENTITY, UNIQUE) the state projection folds;
  * ``feature_contract_validation_state`` — the REBUILDABLE projection (1 row/contract), which is
    UPSERT-able: an UPDATE MUST succeed (it is NOT write-once — replay overwrites it);
  * ``feature_validation_requirement`` — IMMUTABLE, write-once, version/fingerprint/hash-keyed.
The event_type / validation_status / effective_verification CHECKs, the FKs to ``contract``, and the
requirement identity UNIQUE are all exercised. Mirrors tests/featuregen/db/test_migration_1006.py.
"""
from __future__ import annotations

import psycopg
import pytest

_KEY_COLUMNS = {
    "feature_contract_validation_event": {
        "event_id", "contract_id", "seq", "event_type", "payload", "created_at",
    },
    "feature_contract_validation_state": {
        "contract_id", "validation_status", "effective_verification", "applied_seq", "updated_at",
    },
    "feature_validation_requirement": {
        "requirement_id", "contract_id", "requirement_schema_version",
        "metadata_input_fingerprint", "code", "subject_json", "params_json", "blocking",
        "content_hash", "created_at",
    },
}


def _contract(conn, contract_id: str = "c_c4_1") -> str:
    """A minimal parent contract row for the FKs. contract.feature_id FKs feature (0972), so a
    feature row is seeded first; feature_name/version are NOT NULL (0960)."""
    feature_id = f"f_{contract_id}"
    conn.execute("INSERT INTO feature (feature_id, name) VALUES (%s, %s)", (feature_id, "fx"))
    conn.execute(
        "INSERT INTO contract (contract_id, feature_id, feature_name, version) "
        "VALUES (%s, %s, 'fx', 1)", (contract_id, feature_id))
    return contract_id


def _event(conn, contract_id: str, *, event_id: str = "ev_c4_1",
           event_type: str = "ASSESSED") -> str:
    conn.execute(
        "INSERT INTO feature_contract_validation_event (event_id, contract_id, event_type) "
        "VALUES (%s, %s, %s)", (event_id, contract_id, event_type))
    return event_id


def _state(conn, contract_id: str, *, validation_status: str = "design_checked",
           effective_verification: str = "DESIGN-CHECKED") -> None:
    conn.execute(
        "INSERT INTO feature_contract_validation_state "
        "(contract_id, validation_status, effective_verification) VALUES (%s, %s, %s)",
        (contract_id, validation_status, effective_verification))


def _requirement(conn, contract_id: str, *, requirement_id: str = "req_c4_1",
                 content_hash: str = "sha256:req1") -> str:
    conn.execute(
        "INSERT INTO feature_validation_requirement (requirement_id, contract_id, "
        "requirement_schema_version, metadata_input_fingerprint, code, content_hash) "
        "VALUES (%s, %s, 'v1', 'fp:abc', 'TYPE_IS_NUMERIC', %s)",
        (requirement_id, contract_id, content_hash))
    return requirement_id


def test_1009_tables_exist_with_key_columns(conn) -> None:
    for table, expected in _KEY_COLUMNS.items():
        cols = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            (table,)).fetchall()}
        assert expected <= cols, f"{table}: missing {expected - cols}"


def test_1009_lookup_indexes_exist(conn) -> None:
    for index in ("feature_contract_validation_event_contract_idx",
                  "feature_validation_requirement_contract_idx"):
        assert conn.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname = %s", (index,)).fetchone() is not None


def test_event_is_write_once(conn) -> None:
    # APPEND-ONLY lifecycle log — physically immutable, mirroring the 1006 write-once assertions.
    cid = _contract(conn, "c_ev_wo")
    _event(conn, cid, event_id="ev_wo")
    with pytest.raises(psycopg.errors.RaiseException, match="write-once"), conn.transaction():
        conn.execute("UPDATE feature_contract_validation_event SET event_type = 'SUPERSEDED' "
                     "WHERE event_id = 'ev_wo'")
    with pytest.raises(psycopg.errors.RaiseException, match="write-once"), conn.transaction():
        conn.execute("DELETE FROM feature_contract_validation_event WHERE event_id = 'ev_wo'")


def test_requirement_is_write_once(conn) -> None:
    cid = _contract(conn, "c_req_wo")
    _requirement(conn, cid, requirement_id="req_wo")
    with pytest.raises(psycopg.errors.RaiseException, match="write-once"), conn.transaction():
        conn.execute("UPDATE feature_validation_requirement SET blocking = false "
                     "WHERE requirement_id = 'req_wo'")
    with pytest.raises(psycopg.errors.RaiseException, match="write-once"), conn.transaction():
        conn.execute("DELETE FROM feature_validation_requirement WHERE requirement_id = 'req_wo'")


def test_state_is_upsertable_not_write_once(conn) -> None:
    # The REBUILDABLE projection MUST be overwritable by replay — an UPDATE succeeds (no write-once
    # trigger). This is the axis that distinguishes the derived read model from the authority log.
    cid = _contract(conn, "c_state_up")
    _state(conn, cid)
    conn.execute(
        "UPDATE feature_contract_validation_state SET validation_status = 'rejected', "
        "effective_verification = 'UNVERIFIED', applied_seq = 5 WHERE contract_id = %s", (cid,))
    row = conn.execute(
        "SELECT validation_status, effective_verification, applied_seq "
        "FROM feature_contract_validation_state WHERE contract_id = %s", (cid,)).fetchone()
    assert row == ("rejected", "UNVERIFIED", 5)


def test_event_type_check_rejects_unknown(conn) -> None:
    cid = _contract(conn, "c_ev_ck")
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        _event(conn, cid, event_id="ev_bad", event_type="BOGUS")


def test_state_validation_status_check_rejects_unknown(conn) -> None:
    cid = _contract(conn, "c_st_vs_ck")
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        _state(conn, cid, validation_status="DESIGN_CHECKED")   # underscore/upper — not the vocab


def test_state_effective_verification_check_rejects_unknown(conn) -> None:
    cid = _contract(conn, "c_st_ev_ck")
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        _state(conn, cid, effective_verification="design-checked")   # lowercase — not the vocab


def test_event_requires_a_real_contract(conn) -> None:
    conn.execute("SELECT 1")   # open the outer tx before the savepoint
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        _event(conn, "c_MISSING", event_id="ev_fk")


def test_state_requires_a_real_contract(conn) -> None:
    conn.execute("SELECT 1")
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        _state(conn, "c_MISSING")


def test_requirement_requires_a_real_contract(conn) -> None:
    conn.execute("SELECT 1")
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        _requirement(conn, "c_MISSING", requirement_id="req_fk")


def test_requirement_identity_unique_rejects_dup(conn) -> None:
    # UNIQUE (contract_id, requirement_schema_version, metadata_input_fingerprint, content_hash):
    # the same requirement identity may not be recorded twice for a contract.
    cid = _contract(conn, "c_req_uniq")
    _requirement(conn, cid, requirement_id="req_u1", content_hash="sha256:same")
    _requirement(conn, cid, requirement_id="req_u2", content_hash="sha256:other")   # differs — fine
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        # same identity key (content_hash back to the first), new requirement_id — still a dup.
        _requirement(conn, cid, requirement_id="req_u3", content_hash="sha256:same")


def test_event_seq_is_monotonic_per_table(conn) -> None:
    # The projection folds events in `seq` order; IDENTITY hands out a strictly increasing value the
    # writer cannot set, so ordering is DB-owned. Two events -> the second carries the larger seq.
    cid = _contract(conn, "c_seq")
    _event(conn, cid, event_id="ev_seq_1")
    _event(conn, cid, event_id="ev_seq_2")
    seqs = [r[0] for r in conn.execute(
        "SELECT seq FROM feature_contract_validation_event WHERE contract_id = %s "
        "ORDER BY seq", (cid,)).fetchall()]
    assert len(seqs) == 2 and seqs[0] < seqs[1]
