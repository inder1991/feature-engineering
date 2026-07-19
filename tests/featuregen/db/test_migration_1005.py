"""Migration 1005 — llm_dispatch provenance tables (Delivery C5 Task 1).

BEFORE each physical LLM request during ingestion enrichment the writer records an immutable
dispatch header (``llm_dispatch`` — write-once, SENSITIVE: inherits llm_call's 0510
read-controlled/retention classification and stores ONLY the egress-approved redacted request)
plus subject attribution (``llm_dispatch_subject``, write-once); AFTER egress it appends the
transport outcome (``llm_dispatch_outcome``, append-only, closed vocabulary) and associates the
logical ``llm_call`` back to its run (``ingestion_run_llm_call``) and to its physical dispatches
(``llm_call_dispatch``). PostgreSQL enforces the invariants exercised here: the write-once
triggers, the closed outcome CHECK, the retry-idempotency UNIQUE keys, and FKs to real parents.
"""
from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest

_NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)

_KEY_COLUMNS = {
    "llm_dispatch": {
        "dispatch_ref", "logical_call_ref", "attempt_no", "ingestion_run_id", "stage", "task",
        "input_hash", "redacted_input", "redaction_version", "provider", "model",
        "prompt_version", "schema_version", "created_at",
    },
    "llm_dispatch_subject": {
        "id", "dispatch_ref", "catalog_source", "object_ref", "logical_ref", "field_names",
    },
    "llm_dispatch_outcome": {"id", "dispatch_ref", "outcome", "recorded_at"},
    "ingestion_run_llm_call": {"id", "ingestion_run_id", "llm_call_ref", "stage", "at"},
    "llm_call_dispatch": {"id", "llm_call_ref", "dispatch_ref"},
}

# how many FOREIGN KEY constraints each table must carry (nullable FKs still count)
_FK_COUNTS = {
    "llm_dispatch": 1,             # -> ingestion_run (nullable)
    "llm_dispatch_subject": 1,     # -> llm_dispatch
    "llm_dispatch_outcome": 1,     # -> llm_dispatch
    "ingestion_run_llm_call": 2,   # -> ingestion_run, llm_call
    "llm_call_dispatch": 2,        # -> llm_call, llm_dispatch
}


def _run(conn, run_id: str = "ingrun_C5") -> str:
    conn.execute(
        "INSERT INTO ingestion_run (id, origin_type, catalog_source, actor_subject, status, "
        "started_at, heartbeat_at) VALUES (%s, 'upload', 'deposits', 'user:tester', "
        "'in_progress', %s, %s)", (run_id, _NOW, _NOW))
    return run_id


def _llm_call(conn, call_ref: str = "call_c5_1") -> str:
    conn.execute(
        "INSERT INTO llm_call (llm_call_ref, run_id, task, provider, model, prompt_id, "
        "prompt_version, output_schema_id, output_schema_version, redaction_version, "
        "input_hash, redacted_input, created_by) VALUES "
        "(%s, 'run_c5', 'enrich_columns', 'fake', 'fake-1', 'p1', 1, 's1', 1, "
        "'rv1', 'sha256:h', '{}'::jsonb, '{}'::jsonb)", (call_ref,))
    return call_ref


def _dispatch(conn, dispatch_ref: str = "disp_c5_1", *, logical: str = "log_c5_1",
              attempt: int = 1, run_id: str | None = None) -> str:
    conn.execute(
        "INSERT INTO llm_dispatch (dispatch_ref, logical_call_ref, attempt_no, "
        "ingestion_run_id, stage, task, input_hash, redacted_input) VALUES "
        "(%s, %s, %s, %s, 'enrichment', 'enrich_columns', 'sha256:h', '{}'::jsonb)",
        (dispatch_ref, logical, attempt, run_id))
    return dispatch_ref


def test_1005_tables_exist_with_key_columns(conn) -> None:
    for table, expected in _KEY_COLUMNS.items():
        cols = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            (table,)).fetchall()}
        assert expected <= cols, f"{table}: missing {expected - cols}"


def test_1005_foreign_keys_exist(conn) -> None:
    for table, expected in _FK_COUNTS.items():
        n = conn.execute(
            "SELECT count(*) FROM information_schema.table_constraints "
            "WHERE table_name = %s AND constraint_type = 'FOREIGN KEY'", (table,)).fetchone()[0]
        assert n == expected, f"{table}: expected {expected} FKs, found {n}"


def test_1005_lookup_indexes_exist(conn) -> None:
    for index in ("llm_dispatch_subject_dispatch_idx", "llm_dispatch_outcome_dispatch_idx",
                  "ingestion_run_llm_call_ref_idx", "llm_call_dispatch_ref_idx"):
        assert conn.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname = %s", (index,)).fetchone() is not None


def test_dispatch_ingestion_run_id_is_nullable(conn) -> None:
    # feature-generation dispatches aren't ingestion runs — NULL is recorded honestly.
    _dispatch(conn, "disp_no_run", run_id=None)
    assert conn.execute(
        "SELECT ingestion_run_id FROM llm_dispatch WHERE dispatch_ref = 'disp_no_run'"
    ).fetchone() == (None,)


def test_dispatch_accepts_a_real_run_and_rejects_a_missing_one(conn) -> None:
    run_id = _run(conn)
    _dispatch(conn, "disp_with_run", run_id=run_id)
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        _dispatch(conn, "disp_bad_run", logical="log_bad", run_id="ingrun_MISSING")


def test_llm_dispatch_is_write_once(conn) -> None:
    # SENSITIVE audit header — physically immutable, mirroring llm_call_write_once (0510).
    _dispatch(conn, "disp_wo")
    with pytest.raises(psycopg.errors.RaiseException, match="write-once"), conn.transaction():
        conn.execute("UPDATE llm_dispatch SET model = 'x' WHERE dispatch_ref = 'disp_wo'")
    with pytest.raises(psycopg.errors.RaiseException, match="write-once"), conn.transaction():
        conn.execute("DELETE FROM llm_dispatch WHERE dispatch_ref = 'disp_wo'")


def test_llm_dispatch_subject_is_write_once(conn) -> None:
    _dispatch(conn, "disp_subj")
    conn.execute(
        "INSERT INTO llm_dispatch_subject (dispatch_ref, catalog_source, object_ref) "
        "VALUES ('disp_subj', 'deposits', 'public.accounts')")
    with pytest.raises(psycopg.errors.RaiseException, match="write-once"), conn.transaction():
        conn.execute("UPDATE llm_dispatch_subject SET object_ref = 'x' "
                     "WHERE dispatch_ref = 'disp_subj'")
    with pytest.raises(psycopg.errors.RaiseException, match="write-once"), conn.transaction():
        conn.execute("DELETE FROM llm_dispatch_subject WHERE dispatch_ref = 'disp_subj'")


def test_dispatch_unique_per_logical_call_and_attempt(conn) -> None:
    # the retry/replay idempotency key: one physical dispatch per (logical_call_ref, attempt_no).
    _dispatch(conn, "disp_a1", logical="log_u", attempt=1)
    _dispatch(conn, "disp_a2", logical="log_u", attempt=2)   # a retry is a NEW attempt — fine
    conn.execute("SELECT 1")   # open the outer tx before the savepoint (0993/0994 pattern)
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        _dispatch(conn, "disp_a1_dup", logical="log_u", attempt=1)


def test_outcome_vocabulary_closed(conn) -> None:
    _dispatch(conn, "disp_out")
    for outcome in ("response_received", "transport_failed"):
        conn.execute(
            "INSERT INTO llm_dispatch_outcome (dispatch_ref, outcome) VALUES (%s, %s)",
            ("disp_out", outcome))   # append-only: one row PER attempt boundary, no UNIQUE
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute("INSERT INTO llm_dispatch_outcome (dispatch_ref, outcome) "
                     "VALUES ('disp_out', 'lost_interest')")


def test_llm_dispatch_outcome_is_write_once(conn) -> None:
    # A recorded outcome is tamper-evident: INSERT (append per attempt) is allowed, but a bank-grade
    # trail must forbid silently flipping 'transport_failed' -> 'response_received' or erasing a row.
    _dispatch(conn, "disp_wo_out")
    conn.execute("INSERT INTO llm_dispatch_outcome (dispatch_ref, outcome) "
                 "VALUES ('disp_wo_out', 'transport_failed')")
    conn.execute("INSERT INTO llm_dispatch_outcome (dispatch_ref, outcome) "
                 "VALUES ('disp_wo_out', 'response_received')")   # append still allowed
    with pytest.raises(psycopg.errors.RaiseException, match="write-once"), conn.transaction():
        conn.execute("UPDATE llm_dispatch_outcome SET outcome = 'response_received' "
                     "WHERE dispatch_ref = 'disp_wo_out' AND outcome = 'transport_failed'")
    with pytest.raises(psycopg.errors.RaiseException, match="write-once"), conn.transaction():
        conn.execute("DELETE FROM llm_dispatch_outcome WHERE dispatch_ref = 'disp_wo_out'")


def test_child_rows_require_a_real_dispatch(conn) -> None:
    conn.execute("SELECT 1")
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        conn.execute("INSERT INTO llm_dispatch_subject (dispatch_ref) VALUES ('disp_MISSING')")
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        conn.execute("INSERT INTO llm_dispatch_outcome (dispatch_ref, outcome) "
                     "VALUES ('disp_MISSING', 'response_received')")


def test_run_llm_call_association_unique_per_run_call_stage(conn) -> None:
    run_id = _run(conn)
    call_ref = _llm_call(conn)
    conn.execute(
        "INSERT INTO ingestion_run_llm_call (ingestion_run_id, llm_call_ref, stage) "
        "VALUES (%s, %s, 'enrichment')", (run_id, call_ref))
    conn.execute(
        "INSERT INTO ingestion_run_llm_call (ingestion_run_id, llm_call_ref, stage) "
        "VALUES (%s, %s, 'synthesis')", (run_id, call_ref))   # another stage — fine
    conn.execute("SELECT 1")
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        conn.execute(
            "INSERT INTO ingestion_run_llm_call (ingestion_run_id, llm_call_ref, stage) "
            "VALUES (%s, %s, 'enrichment')", (run_id, call_ref))
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        conn.execute(
            "INSERT INTO ingestion_run_llm_call (ingestion_run_id, llm_call_ref, stage) "
            "VALUES ('ingrun_MISSING', %s, 'enrichment')", (call_ref,))
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        conn.execute(
            "INSERT INTO ingestion_run_llm_call (ingestion_run_id, llm_call_ref, stage) "
            "VALUES (%s, 'call_MISSING', 'enrichment')", (run_id,))


def test_llm_call_dispatch_association_unique_per_pair(conn) -> None:
    call_ref = _llm_call(conn, "call_c5_assoc")
    _dispatch(conn, "disp_assoc")
    conn.execute(
        "INSERT INTO llm_call_dispatch (llm_call_ref, dispatch_ref) VALUES (%s, 'disp_assoc')",
        (call_ref,))
    conn.execute("SELECT 1")
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        conn.execute(
            "INSERT INTO llm_call_dispatch (llm_call_ref, dispatch_ref) "
            "VALUES (%s, 'disp_assoc')", (call_ref,))
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        conn.execute(
            "INSERT INTO llm_call_dispatch (llm_call_ref, dispatch_ref) "
            "VALUES ('call_MISSING', 'disp_assoc')")
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        conn.execute(
            "INSERT INTO llm_call_dispatch (llm_call_ref, dispatch_ref) "
            "VALUES (%s, 'disp_MISSING')", (call_ref,))
