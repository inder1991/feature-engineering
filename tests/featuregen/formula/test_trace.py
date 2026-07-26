"""Child-1 Task 11 — the write-once, crash-safe authoring trace (migration 1020 + trace.py).

Every test here is discriminating on one of the two load-bearing invariants:

* CRASH-SAFE HONESTY — ``run_status`` derives *incomplete* from the ABSENCE of a terminal event, so
  a process that dies mid-authoring leaves an incomplete run and durable ``llm_call`` rows that
  outlive a rolled-back request transaction can never make a run read as completed.
* WRITE-ONCE — both tables physically reject UPDATE / DELETE / TRUNCATE (trigger-enforced, not
  convention), and no event may be appended after a terminal event.
"""
from __future__ import annotations

import ast
import hashlib
import logging
import re
from pathlib import Path

import psycopg
import pytest
from tests.featuregen._helpers import make_actor

from featuregen.contracts.identity import identity_to_jsonb
from featuregen.formula import trace
from featuregen.formula._jcs import dumps as jcs_dumps
from featuregen.formula.trace import (
    TraceEventKind,
    append_event,
    open_authoring_run,
    run_status,
)
from featuregen.intake.llm import LLMRequest, record_llm_call

_VERSIONS = {
    "operation_grammar_version": "1",
    "capability_policy_version": 1,
    "critic_policy_version": 1,
}


@pytest.fixture(autouse=True, scope="module")
def no_dsn():
    """DSN-HERMETIC by default (precedent: tests/featuregen/overlay/upload/test_ingestion_run.py).

    Almost every test here assumes the request-connection fallback path, whose writes the ``db``
    fixture rolls back. With an ambient ``FEATUREGEN_DSN`` (a developer shell sourced from
    ``.env.demo`` / ``run-demo.sh``) they would instead COMMIT on the durable fresh connection — and
    ``authoring_trace_event`` rows physically CANNOT be cleaned up afterwards (that is the write-once
    guarantee), so a second run of the suite would fail on a stale idempotency key and stay red until
    the database was dropped. Module-scoped so it is stripped once for the whole file; the handful of
    tests that deliberately exercise the durable path re-arm it with the function-scoped
    ``monkeypatch`` (which restores "unset" at their teardown).

    Idempotency keys are additionally derived from the minted ``run_id`` everywhere, never a global
    constant, so even the deliberately-durable tests leave uniquely-keyed, inert rows behind."""
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("FEATUREGEN_DSN", raising=False)
        yield


def _open(db, *, intent_hash: str = "ih_1") -> str:
    return open_authoring_run(db, intent_hash=intent_hash, versions=_VERSIONS, actor=make_actor())


def _started(db, run_id: str, *, seq: int = 0, key: str | None = None) -> None:
    append_event(db, run_id, TraceEventKind.STARTED, seq=seq,
                 idempotency_key=key or f"{run_id}:{seq}", payload={"intent_hash": "ih_1"})


def _llm_call_ref(db, run_id: str) -> str:
    """A REAL immutable llm_call row so the FK target exists (never a synthetic ref)."""
    return record_llm_call(
        db, run_id=run_id,
        request=LLMRequest(task="formula_author", prompt_id="formula_author", prompt_version=1,
                           inputs={}, output_schema_id="author_turn_v1", output_schema_version=1,
                           generation_settings={"provider": "fake", "model": "fake"}),
        input_hash="ih", redaction_version="1", input_redaction={}, raw_output={},
        validation_result={"status": "ok"}, repair_attempts=[], latency_ms=1, cost_metadata=None,
        created_by=identity_to_jsonb(make_actor()))


# ── manifest-first ───────────────────────────────────────────────────────────────────────────────
def test_open_authoring_run_writes_the_manifest_first(db) -> None:
    run_id = _open(db, intent_hash="ih_manifest")
    row = db.execute(
        "SELECT intent_hash, versions, actor FROM authoring_run WHERE authoring_run_id = %s",
        (run_id,)).fetchone()
    assert row is not None
    assert row[0] == "ih_manifest"
    assert row[1] == _VERSIONS
    assert row[2]["subject"] == "user:raj"


def test_append_event_requires_an_existing_manifest(db) -> None:
    """FK to authoring_run: an event can never precede (or outlive the absence of) its manifest."""
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _started(db, "arun_never_opened")


# ── run_status derives incomplete from event ABSENCE ─────────────────────────────────────────────
def test_run_with_no_terminal_event_is_incomplete(db) -> None:
    run_id = _open(db)
    _started(db, run_id, seq=0)
    append_event(db, run_id, TraceEventKind.TOOL_CALLED, seq=1,
                 idempotency_key=f"{run_id}:1", payload={"tool": "search_columns"})
    assert run_status(db, run_id) == "incomplete"


def test_completed_event_completes_the_run(db) -> None:
    run_id = _open(db)
    _started(db, run_id, seq=0)
    assert run_status(db, run_id) == "incomplete"
    append_event(db, run_id, TraceEventKind.COMPLETED, seq=1,
                 idempotency_key=f"{run_id}:1", payload={"disposition": "RESOLVED"})
    assert run_status(db, run_id) == "completed"


def test_failed_event_fails_the_run(db) -> None:
    run_id = _open(db)
    _started(db, run_id, seq=0)
    append_event(db, run_id, TraceEventKind.FAILED, seq=1,
                 idempotency_key=f"{run_id}:1", payload={"disposition": "TECHNICAL_FAILURE"})
    assert run_status(db, run_id) == "failed"


def test_unknown_run_is_incomplete_never_completed(db) -> None:
    """Fail TOWARD incomplete: a run with no rows at all has no terminal event."""
    assert run_status(db, "arun_does_not_exist") == "incomplete"


def test_durable_llm_call_rows_never_complete_a_run(db) -> None:
    """The [c12] hazard: llm_call rows are written durably and outlive a rolled-back request tx.
    A run with many recorded calls and NO terminal event is still incomplete — there is no status
    column for those durable rows to drift against."""
    run_id = _open(db)
    _started(db, run_id, seq=0)
    for seq in (1, 2, 3):
        append_event(db, run_id, TraceEventKind.LLM_CALL_RECORDED, seq=seq,
                     idempotency_key=f"{run_id}:{seq}", llm_call_ref=_llm_call_ref(db, run_id),
                     payload={"turn": seq})
    assert run_status(db, run_id) == "incomplete"
    assert db.execute("SELECT count(*) FROM llm_call WHERE run_id = %s",
                      (run_id,)).fetchone()[0] == 3


# ── no event after a terminal event ──────────────────────────────────────────────────────────────
def test_event_after_terminal_event_is_rejected(db) -> None:
    run_id = _open(db)
    _started(db, run_id, seq=0)
    append_event(db, run_id, TraceEventKind.COMPLETED, seq=1,
                 idempotency_key=f"{run_id}:1", payload={})
    with pytest.raises(psycopg.errors.RaiseException, match="terminal"):
        append_event(db, run_id, TraceEventKind.CRITIC_RECORDED, seq=2,
                     idempotency_key=f"{run_id}:2", payload={})


def test_second_terminal_event_is_rejected(db) -> None:
    run_id = _open(db)
    append_event(db, run_id, TraceEventKind.FAILED, seq=0,
                 idempotency_key=f"{run_id}:0", payload={})
    with pytest.raises(psycopg.Error):
        append_event(db, run_id, TraceEventKind.COMPLETED, seq=1,
                     idempotency_key=f"{run_id}:1", payload={})


def test_one_terminal_event_partial_unique_index_exists(db) -> None:
    """The trigger raises first for a SEQUENTIAL second terminal; the partial UNIQUE index is what
    holds under CONCURRENCY (two sessions both passing the EXISTS check). Assert it physically
    exists, since no single-session test can distinguish the two enforcers."""
    defs = [r[0] for r in db.execute(
        "SELECT indexdef FROM pg_indexes WHERE tablename = 'authoring_trace_event'").fetchall()]
    terminal = [d for d in defs if "COMPLETED" in d and "FAILED" in d]
    assert terminal, defs
    assert terminal[0].startswith("CREATE UNIQUE INDEX")
    assert "(authoring_run_id)" in terminal[0].replace("USING btree ", "")


# ── uniqueness ───────────────────────────────────────────────────────────────────────────────────
def test_duplicate_seq_for_one_run_is_rejected(db) -> None:
    run_id = _open(db)
    _started(db, run_id, seq=0)
    with pytest.raises(psycopg.errors.UniqueViolation):
        append_event(db, run_id, TraceEventKind.TOOL_CALLED, seq=0,
                     idempotency_key=f"{run_id}:dup-seq", payload={})


def test_same_seq_in_two_runs_is_allowed(db) -> None:
    run_a, run_b = _open(db), _open(db)
    _started(db, run_a, seq=0)   # keys default to f"{run_id}:{seq}" — run-scoped, never a constant
    _started(db, run_b, seq=0)
    assert run_status(db, run_a) == "incomplete"
    assert run_status(db, run_b) == "incomplete"


def test_duplicate_idempotency_key_is_rejected(db) -> None:
    run_a, run_b = _open(db), _open(db)
    shared = f"{run_a}:shared"   # shared between the two runs, unique to this test invocation
    _started(db, run_a, seq=0, key=shared)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _started(db, run_b, seq=0, key=shared)


# ── closed kind vocabulary ───────────────────────────────────────────────────────────────────────
def test_unknown_kind_rejected_before_the_db(db) -> None:
    run_id = _open(db)
    with pytest.raises(ValueError):
        append_event(db, run_id, "NOT_A_KIND", seq=0, idempotency_key=f"{run_id}:0", payload={})


def test_kind_check_is_closed_at_the_db_level(db) -> None:
    """Bypass trace.py entirely: the DB itself must refuse a kind outside the §H vocabulary."""
    run_id = _open(db)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "INSERT INTO authoring_trace_event (authoring_trace_event_id, authoring_run_id, seq, "
            "kind, idempotency_key, payload, payload_hash) "
            "VALUES (%s, %s, 0, 'SMUGGLED', %s, '{}'::jsonb, 'h')",
            (f"atev_{run_id}", run_id, f"{run_id}:smuggled"))


def test_kind_check_covers_exactly_the_seven_h_kinds(db) -> None:
    """Closed over EXACTLY the §H vocabulary — no extra kind smuggled in, none missing."""
    defn = db.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = 'authoring_trace_event'::regclass AND contype = 'c' "
        "AND pg_get_constraintdef(oid) LIKE '%%kind%%'").fetchone()[0]
    assert set(re.findall(r"'([A-Z_]+)'", defn)) == {k.value for k in TraceEventKind}
    assert len(list(TraceEventKind)) == 7


def test_every_kind_in_the_vocabulary_appends(db) -> None:
    """Every §H kind is really insertable (COMPLETED excluded: it is a terminal, and only one
    terminal per run is permitted — test_completed_event_completes_the_run covers it)."""
    run_id = _open(db)
    for seq, kind in enumerate(k for k in TraceEventKind if k is not TraceEventKind.COMPLETED):
        append_event(db, run_id, kind, seq=seq, idempotency_key=f"{run_id}:{seq}", payload={})
    assert run_status(db, run_id) == "failed"   # FAILED is the last non-COMPLETED kind


# ── llm_call FK ──────────────────────────────────────────────────────────────────────────────────
def test_llm_call_ref_must_reference_a_real_llm_call(db) -> None:
    run_id = _open(db)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        append_event(db, run_id, TraceEventKind.LLM_CALL_RECORDED, seq=0,
                     idempotency_key=f"{run_id}:0", llm_call_ref="llmc_ghost", payload={})


def test_llm_call_ref_is_nullable(db) -> None:
    run_id = _open(db)
    append_event(db, run_id, TraceEventKind.TOOL_RESULT_RECORDED, seq=0,
                 idempotency_key=f"{run_id}:0", payload={"result_hash": "abc"})
    assert db.execute("SELECT llm_call_ref FROM authoring_trace_event WHERE authoring_run_id = %s",
                      (run_id,)).fetchone()[0] is None


# ── write-once: UPDATE / DELETE / TRUNCATE on BOTH tables ────────────────────────────────────────
def test_manifest_update_is_rejected(db) -> None:
    run_id = _open(db)
    with pytest.raises(psycopg.errors.RaiseException, match="write-once"):
        db.execute("UPDATE authoring_run SET intent_hash = 'tampered' WHERE authoring_run_id = %s",
                   (run_id,))


def test_manifest_delete_is_rejected(db) -> None:
    run_id = _open(db)
    with pytest.raises(psycopg.errors.RaiseException, match="write-once"):
        db.execute("DELETE FROM authoring_run WHERE authoring_run_id = %s", (run_id,))


def test_manifest_truncate_is_rejected(db) -> None:
    """Truncating BOTH tables in one statement satisfies Postgres' FK-truncate restriction, so the
    only thing that can reject this is our BEFORE TRUNCATE statement trigger."""
    _open(db)
    with pytest.raises(psycopg.errors.RaiseException, match="authoring_run is write-once"):
        db.execute("TRUNCATE authoring_run, authoring_trace_event")


def test_event_update_is_rejected(db) -> None:
    run_id = _open(db)
    _started(db, run_id, seq=0)
    with pytest.raises(psycopg.errors.RaiseException, match="write-once"):
        db.execute("UPDATE authoring_trace_event SET kind = 'COMPLETED' WHERE authoring_run_id = %s",
                   (run_id,))


def test_event_delete_is_rejected(db) -> None:
    run_id = _open(db)
    _started(db, run_id, seq=0)
    with pytest.raises(psycopg.errors.RaiseException, match="write-once"):
        db.execute("DELETE FROM authoring_trace_event WHERE authoring_run_id = %s", (run_id,))


def test_event_truncate_is_rejected(db) -> None:
    run_id = _open(db)
    _started(db, run_id, seq=0)
    with pytest.raises(psycopg.errors.RaiseException, match="write-once"):
        db.execute("TRUNCATE authoring_trace_event")


# ── payload discipline ───────────────────────────────────────────────────────────────────────────
def test_payload_hash_is_the_jcs_hash_of_the_stored_payload(db) -> None:
    run_id = _open(db)
    payload = {"tool": "get_column_metadata", "result_ref": "cat::t.c", "b": [3, 1]}
    append_event(db, run_id, TraceEventKind.TOOL_RESULT_RECORDED, seq=0,
                 idempotency_key=f"{run_id}:0", payload=payload)
    stored, stored_hash = db.execute(
        "SELECT payload, payload_hash FROM authoring_trace_event WHERE authoring_run_id = %s",
        (run_id,)).fetchone()
    assert stored == payload
    # Recomputable from the READ-BACK jsonb (order-independent canonicalization) — tamper-evident.
    assert stored_hash == hashlib.sha256(jcs_dumps(stored)).hexdigest()


def test_non_object_payload_is_rejected(db) -> None:
    run_id = _open(db)
    with pytest.raises(TypeError):
        append_event(db, run_id, TraceEventKind.TOOL_CALLED, seq=0,
                     idempotency_key=f"{run_id}:0", payload=["not", "an", "object"])


# ── durable fresh-connection pattern ─────────────────────────────────────────────────────────────
def test_durable_manifest_and_trace_survive_a_rolled_back_request(db, monkeypatch, _dsn) -> None:
    """THE point of the durable fresh-connection pattern: the provider audit (``llm_call``) commits
    on its own connection, so the manifest + trace must too — otherwise a rolled-back request leaves
    audited provider calls with no run to attribute them to.

    Arm the real DSN and assert all three durable-path properties on one run: (1) a manifest +
    STARTED committed elsewhere still read as ``incomplete`` (crash-safe honesty), (2) a terminal
    committed on another connection IS visible to the request connection (READ COMMITTED), and
    (3) everything survives the request transaction being rolled back. The committed rows are
    deliberately NOT cleaned up — they physically cannot be (that IS the write-once guarantee);
    they are inert, uniquely-keyed telemetry in the throwaway test database."""
    monkeypatch.setenv("FEATUREGEN_DSN", _dsn)
    run_id = _open(db, intent_hash="ih_durable")
    _started(db, run_id, seq=0)
    assert run_status(db, run_id) == "incomplete"
    append_event(db, run_id, TraceEventKind.COMPLETED, seq=1,
                 idempotency_key=f"{run_id}:1", payload={"disposition": "RESOLVED"})
    assert run_status(db, run_id) == "completed"
    db.rollback()
    with psycopg.connect(_dsn) as fresh:
        assert fresh.execute("SELECT intent_hash FROM authoring_run WHERE authoring_run_id = %s",
                             (run_id,)).fetchone()[0] == "ih_durable"
        assert fresh.execute("SELECT count(*) FROM authoring_trace_event "
                             "WHERE authoring_run_id = %s", (run_id,)).fetchone()[0] == 2
        assert run_status(fresh, run_id) == "completed"


def test_durable_write_degrades_to_the_request_connection(db, monkeypatch, caplog) -> None:
    """A DSN is configured but unreachable: the connect failure is LOGGED and the write degrades to
    the request connection (transactional evidence beats none). The log line proves the configured
    DSN was really attempted rather than ignored."""
    monkeypatch.setenv("FEATUREGEN_DSN", "host=127.0.0.1 port=1 dbname=nope connect_timeout=1")
    with caplog.at_level(logging.ERROR, logger="featuregen.formula.trace"):
        run_id = _open(db)
        _started(db, run_id, seq=0)
        append_event(db, run_id, TraceEventKind.COMPLETED, seq=1,
                     idempotency_key=f"{run_id}:1", payload={})
    assert caplog.text.count("durable authoring-trace connection failed") == 3
    assert run_status(db, run_id) == "completed"
    assert db.execute("SELECT count(*) FROM authoring_run WHERE authoring_run_id = %s",
                      (run_id,)).fetchone()[0] == 1


def test_durable_writes_use_the_full_dsn_and_no_advisory_lock(db) -> None:
    """Two structural guards on the durable pattern, asserted over the AST (so prose in docstrings
    can't satisfy or break them): the DSN read is ``get_settings().dsn`` — never the password-less
    ``conn.info.dsn`` — and no advisory lock is taken (the program-audit I-3 self-deadlock: a fresh
    connection that re-acquires the request's advisory lock hangs forever)."""
    source = Path(trace.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    attributes = {ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "get_settings().dsn" in attributes
    assert not [a for a in attributes if a.endswith("info.dsn")]
    assert "pg_advisory" not in source
