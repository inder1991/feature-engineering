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
import threading
from pathlib import Path

import psycopg
import pytest
from tests.featuregen._helpers import make_actor

from featuregen.aggregates.ids import mint_id
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


def test_one_terminal_event_partial_unique_index_is_exactly_as_shipped(db) -> None:
    """The trigger raises first for a SEQUENTIAL second terminal; the partial UNIQUE index is what
    holds under CONCURRENCY (see the two-session test below).

    The PREDICATE is pinned, not just the index's existence and uniqueness (review MINOR 1): the
    reviewer mutated it to ``WHERE kind IN ('COMPLETED','FAILED') AND seq > 1000000`` — which gives
    ZERO concurrency protection — and the whole suite still passed. Exact-match the full indexdef so
    any narrowing of the predicate, any added column, or a switch to a non-unique index fails HERE."""
    defs = [r[0] for r in db.execute(
        "SELECT indexdef FROM pg_indexes WHERE tablename = 'authoring_trace_event'").fetchall()]
    terminal = [d for d in defs if "COMPLETED" in d and "FAILED" in d]
    assert terminal, defs
    assert " ".join(terminal[0].split()) == (
        "CREATE UNIQUE INDEX authoring_trace_event_one_terminal_idx "
        "ON public.authoring_trace_event USING btree (authoring_run_id) "
        "WHERE (kind = ANY (ARRAY['COMPLETED'::text, 'FAILED'::text]))")


def test_one_terminal_per_run_holds_under_concurrency(db, monkeypatch, _dsn) -> None:
    """The ONLY test that proves the partial UNIQUE index's actual concurrency guarantee (review
    MINOR 1). The sequential case is caught by the BEFORE INSERT trigger, so it cannot distinguish
    the two enforcers — and the trigger's ``EXISTS`` probe is exactly what two concurrent sessions
    both pass, because neither sees the other's UNCOMMITTED terminal.

    Session A inserts COMPLETED and holds it uncommitted; session B inserts FAILED for the same run,
    passes the trigger, and BLOCKS on A's index entry; A commits; B raises ``UniqueViolation``.
    Raw INSERTs on purpose: routing them through ``append_event`` would commit each on its own fresh
    connection, which is precisely the interleaving this test must NOT have."""
    monkeypatch.setenv("FEATUREGEN_DSN", _dsn)
    run_id = _open(db, intent_hash="ih_concurrent")   # the manifest must be COMMITTED for the FK
    insert = ("INSERT INTO authoring_trace_event (authoring_trace_event_id, authoring_run_id, seq, "
              "kind, idempotency_key, payload, payload_hash) VALUES (%s, %s, %s, %s, %s, '{}', 'h')")
    b_error: list[BaseException] = []

    with psycopg.connect(_dsn) as a, psycopg.connect(_dsn) as b:
        a.execute(insert, (f"atev_a_{run_id}", run_id, 0, "COMPLETED", f"{run_id}:a"))

        def insert_in_b() -> None:
            try:
                b.execute(insert, (f"atev_b_{run_id}", run_id, 1, "FAILED", f"{run_id}:b"))
            except BaseException as exc:              # noqa: BLE001 — recorded, asserted below
                b_error.append(exc)

        thread = threading.Thread(target=insert_in_b, daemon=True)
        thread.start()
        thread.join(1.0)
        assert thread.is_alive(), "session B did not BLOCK — the partial unique index is not holding"
        assert not b_error, b_error
        a.commit()                                    # A wins the index entry
        thread.join(10.0)
        assert not thread.is_alive(), "session B never unblocked after A committed"
        assert isinstance(b_error[0], psycopg.errors.UniqueViolation), b_error
        b.rollback()

    with psycopg.connect(_dsn) as fresh:              # exactly ONE terminal survived
        assert fresh.execute(
            "SELECT count(*) FROM authoring_trace_event WHERE authoring_run_id = %s "
            "AND kind IN ('COMPLETED', 'FAILED')", (run_id,)).fetchone()[0] == 1


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
    assert caplog.text.count("durable authoring-trace write failed") == 3
    assert run_status(db, run_id) == "completed"
    assert db.execute("SELECT count(*) FROM authoring_run WHERE authoring_run_id = %s",
                      (run_id,)).fetchone()[0] == 1


# ── the degrade is RECOVERABLE: connection-DEPENDENT failures fall back, deterministic ones raise ──
def test_fk_violation_on_an_uncommitted_manifest_recovers_on_the_request_connection(
        db, monkeypatch, _dsn) -> None:
    """Review IMPORTANT 1(a) — WITHIN this module. A transient connect blip degrades the MANIFEST
    onto the caller's UNCOMMITTED transaction. When the blip clears, the next append opens a healthy
    fresh connection whose FK CANNOT SEE that manifest. Before the fix that raised
    ``ForeignKeyViolation`` and was NOT retried on the caller's connection — where it would have
    succeeded — so every subsequent append for the run failed identically and the whole run's trace
    was lost. Now it degrades to the caller's connection and the trace survives."""
    monkeypatch.setenv("FEATUREGEN_DSN", "host=127.0.0.1 port=1 dbname=nope connect_timeout=1")
    run_id = _open(db, intent_hash="ih_blip")          # manifest lands on db's uncommitted tx
    monkeypatch.setenv("FEATUREGEN_DSN", _dsn)         # blip clears: fresh connections are healthy
    with psycopg.connect(_dsn) as probe:               # the manifest really is invisible elsewhere
        assert probe.execute("SELECT count(*) FROM authoring_run WHERE authoring_run_id = %s",
                             (run_id,)).fetchone()[0] == 0
    _started(db, run_id, seq=0)                        # would raise ForeignKeyViolation pre-fix
    append_event(db, run_id, TraceEventKind.COMPLETED, seq=1,
                 idempotency_key=f"{run_id}:1", payload={"disposition": "RESOLVED"})
    assert db.execute("SELECT count(*) FROM authoring_trace_event WHERE authoring_run_id = %s",
                      (run_id,)).fetchone()[0] == 2


def test_fk_violation_on_a_degraded_llm_call_recovers_on_the_request_connection(
        db, monkeypatch, _dsn) -> None:
    """Review IMPORTANT 1(b) — ACROSS SEAMS. ``enrich_llm._record_llm_call_durable`` has its own
    degrade path that this codebase treats as a real production event (it COUNTS them:
    ``consume_audit_degradations``). When THAT degrades, the ``llm_call`` row sits on the caller's
    uncommitted transaction while the trace connection is healthy, so ``llm_call_ref``'s FK is
    unsatisfiable there. Modelled exactly: record the call on the request connection (as the degraded
    seam does), then append with a healthy DSN armed."""
    monkeypatch.setenv("FEATUREGEN_DSN", _dsn)
    run_id = _open(db, intent_hash="ih_degraded_call")   # manifest commits durably (seam healthy)
    monkeypatch.delenv("FEATUREGEN_DSN")
    ref = _llm_call_ref(db, run_id)                      # the DEGRADED audit write: caller's tx
    monkeypatch.setenv("FEATUREGEN_DSN", _dsn)
    with psycopg.connect(_dsn) as probe:
        assert probe.execute("SELECT count(*) FROM llm_call WHERE llm_call_ref = %s",
                             (ref,)).fetchone()[0] == 0
    append_event(db, run_id, TraceEventKind.LLM_CALL_RECORDED, seq=0,
                 idempotency_key=f"{run_id}:0", llm_call_ref=ref, payload={"turn": 0})
    assert db.execute("SELECT llm_call_ref FROM authoring_trace_event WHERE authoring_run_id = %s",
                      (run_id,)).fetchone()[0] == ref


def test_a_genuinely_absent_referent_still_raises_on_the_durable_path(db, monkeypatch, _dsn) -> None:
    """The fallback is a RECOVERY, not a swallow: a referent that exists on NO connection still
    surfaces ``ForeignKeyViolation`` (from the replay on the caller's connection)."""
    monkeypatch.setenv("FEATUREGEN_DSN", _dsn)
    run_id = _open(db)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        append_event(db, run_id, TraceEventKind.LLM_CALL_RECORDED, seq=0,
                     idempotency_key=f"{run_id}:0", llm_call_ref="llmc_ghost", payload={})


def test_a_rejected_fallback_replay_leaves_an_idle_caller_connection_usable(
        db, monkeypatch, _dsn) -> None:
    """Fix-wave-2 IMPORTANT 1. The fallback deliberately replays statements the database has ALREADY
    rejected once (the FK class), so the replay itself must not poison the caller's connection. It
    used to: the bare ``conn.execute`` left the implicit transaction ABORTED, so the caller could not
    execute another statement — including its own terminal ``FAILED`` event through this very
    degraded path. Pre-wave that same call raised from the TRACE connection and left the caller
    usable, so the recovery had made the caller strictly worse off."""
    monkeypatch.setenv("FEATUREGEN_DSN", _dsn)
    run_id = _open(db)                                  # commits durably: the caller stays IDLE
    assert db.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        append_event(db, run_id, TraceEventKind.LLM_CALL_RECORDED, seq=0,
                     idempotency_key=f"{run_id}:0", llm_call_ref="llmc_ghost", payload={})
    assert db.execute("SELECT 1").fetchone()[0] == 1     # the caller's connection is still USABLE


def test_a_rejected_fallback_replay_preserves_the_callers_uncommitted_work(
        db, monkeypatch, _dsn) -> None:
    """Fix-wave-2 IMPORTANT 1, the shape that matters in a request: the caller is mid-transaction
    with a DEGRADED manifest and event of its own. A rejected replay must be contained by a SAVEPOINT
    — usable connection AND that uncommitted work intact. A blanket ``conn.rollback()`` would keep
    the connection usable while silently destroying the trace evidence it is there to protect, and a
    bare replay poisons the transaction outright."""
    monkeypatch.setenv("FEATUREGEN_DSN", "host=127.0.0.1 port=1 dbname=nope connect_timeout=1")
    run_id = _open(db, intent_hash="ih_savepoint")       # manifest degrades onto the caller's tx
    _started(db, run_id, seq=0)                          # ...and so does the first event
    monkeypatch.setenv("FEATUREGEN_DSN", _dsn)           # the blip clears
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        append_event(db, run_id, TraceEventKind.LLM_CALL_RECORDED, seq=1,
                     idempotency_key=f"{run_id}:1", llm_call_ref="llmc_ghost", payload={})
    assert db.execute("SELECT 1").fetchone()[0] == 1     # usable
    assert db.execute("SELECT count(*) FROM authoring_trace_event WHERE authoring_run_id = %s",
                      (run_id,)).fetchone()[0] == 1     # the degraded evidence SURVIVED the replay
    # And the caller can still write its own terminal event through the degraded path (Task 12).
    append_event(db, run_id, TraceEventKind.FAILED, seq=2,
                 idempotency_key=f"{run_id}:2", payload={"disposition": "TECHNICAL_FAILURE"})
    assert db.execute("SELECT kind FROM authoring_trace_event WHERE authoring_run_id = %s "
                      "AND seq = 2", (run_id,)).fetchone()[0] == "FAILED"


def test_a_lost_ack_event_replay_converges_instead_of_raising(db, monkeypatch, _dsn) -> None:
    """Fix-wave-2 IMPORTANT 1, coupled half: COMMIT AMBIGUITY. The fresh connection's INSERT commits
    server-side but the ack never arrives, so the fallback replays a statement the database HAS
    durably recorded. Unguarded that hit the ``idempotency_key`` UNIQUE and handed the caller a
    ``UniqueViolation`` (plus, pre-savepoint, an aborted transaction) for an event that IS recorded.
    ``idempotency_key`` is the documented "globally unique retry/replay key" — the REPLAY now actually
    uses it and converges.

    ``mint_id`` is pinned so the replayed statement is BYTE-IDENTICAL to the committed one (the true
    lost-ack shape): the ``idempotency_key`` arbiter must win over the PK and (run, seq) indexes."""
    monkeypatch.setenv("FEATUREGEN_DSN", _dsn)
    run_id = _open(db, intent_hash="ih_lost_ack_event")
    monkeypatch.setattr(trace, "mint_id", lambda prefix: f"atev_lostack_{run_id}")
    _started(db, run_id, seq=0)                        # commits server-side; the ack is then LOST
    monkeypatch.setenv("FEATUREGEN_DSN", "host=127.0.0.1 port=1 dbname=nope connect_timeout=1")
    _started(db, run_id, seq=0)                        # the identical statement is replayed
    assert db.execute("SELECT 1").fetchone()[0] == 1
    with psycopg.connect(_dsn) as fresh:               # converged on the ONE durable row
        assert fresh.execute("SELECT count(*) FROM authoring_trace_event "
                             "WHERE authoring_run_id = %s", (run_id,)).fetchone()[0] == 1


def test_a_lost_ack_manifest_replay_converges_instead_of_raising(db, monkeypatch, _dsn) -> None:
    """The same commit ambiguity on the MANIFEST, whose replay key is its ``authoring_run_id`` PK: a
    manifest that committed server-side before the ack was lost must not raise a duplicate-key error
    at the caller either. ``mint_id`` is pinned so ``open_authoring_run`` replays the id that is
    already durable — the only way that call can repeat a PK."""
    durable_id = mint_id("arun")
    monkeypatch.setattr(trace, "mint_id", lambda prefix: durable_id)
    with psycopg.connect(_dsn) as pre:                 # the INSERT that committed server-side
        pre.execute("INSERT INTO authoring_run (authoring_run_id, intent_hash, versions, actor) "
                    "VALUES (%s, 'ih_lost_ack_manifest', '{}'::jsonb, '{}'::jsonb)", (durable_id,))
    monkeypatch.setenv("FEATUREGEN_DSN", "host=127.0.0.1 port=1 dbname=nope connect_timeout=1")
    assert _open(db, intent_hash="ih_lost_ack_manifest") == durable_id   # must not raise
    assert db.execute("SELECT 1").fetchone()[0] == 1
    with psycopg.connect(_dsn) as fresh:
        assert fresh.execute("SELECT count(*) FROM authoring_run WHERE authoring_run_id = %s",
                             (durable_id,)).fetchone()[0] == 1


def test_deterministic_rejections_still_raise_on_the_durable_path(db, monkeypatch, _dsn) -> None:
    """The refinement's other half: a CONNECTION-INDEPENDENT rejection must PROPAGATE, never be
    pointlessly replayed onto the caller's transaction (and never silently swallowed). All three
    classes, on the real durable path with a healthy DSN: UniqueViolation (duplicate idempotency
    key), the post-terminal guard, and the write-once trigger."""
    monkeypatch.setenv("FEATUREGEN_DSN", _dsn)
    run_id = _open(db, intent_hash="ih_deterministic")
    _started(db, run_id, seq=0)
    with pytest.raises(psycopg.errors.UniqueViolation):        # same idempotency key, new seq
        append_event(db, run_id, TraceEventKind.TOOL_CALLED, seq=1,
                     idempotency_key=f"{run_id}:0", payload={})
    append_event(db, run_id, TraceEventKind.FAILED, seq=2,
                 idempotency_key=f"{run_id}:2", payload={})
    with pytest.raises(psycopg.errors.RaiseException, match="terminal"):   # post-terminal guard
        append_event(db, run_id, TraceEventKind.CRITIC_RECORDED, seq=3,
                     idempotency_key=f"{run_id}:3", payload={})
    with pytest.raises(psycopg.errors.RaiseException, match="write-once"), \
            psycopg.connect(_dsn) as fresh:
        fresh.execute("UPDATE authoring_trace_event SET kind = 'COMPLETED' "
                      "WHERE authoring_run_id = %s", (run_id,))
    # Nothing was replayed onto the caller's transaction: it is still usable, and the run holds
    # exactly the two appends that were accepted.
    assert db.execute("SELECT 1").fetchone()[0] == 1
    with psycopg.connect(_dsn) as fresh:
        assert fresh.execute("SELECT count(*) FROM authoring_trace_event "
                             "WHERE authoring_run_id = %s", (run_id,)).fetchone()[0] == 2


def test_run_status_sees_a_durable_terminal_under_repeatable_read(db, monkeypatch, _dsn) -> None:
    """Review IMPORTANT 2 — the read must be SYMMETRIC with the writes. ``api.deps.py:135`` pins the
    feature-generation connection (``api.routes.contract._FeatureGenConn``, the natural host for
    authoring) to ``REPEATABLE READ``. Reading on the caller's connection there returned
    ``"incomplete"`` for a run this very request had just COMPLETED: the RR snapshot predates the
    durable commits. It failed safe, but an orchestrator reading its own terminal event would
    mislabel the run or re-author it.

    Discriminating: the caller's own snapshot is asserted BLIND to the committed rows, so a
    caller-connection read could only have said ``"incomplete"``."""
    monkeypatch.setenv("FEATUREGEN_DSN", _dsn)
    db.rollback()                                    # no tx in flight before changing isolation
    db.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    # The request's FIRST table read is what PINS the RR snapshot — exactly what the feature-gen
    # request does (the C0 metadata snapshot) before authoring starts.
    assert db.execute("SELECT count(*) FROM authoring_run").fetchone()[0] >= 0
    assert db.execute("SHOW transaction_isolation").fetchone()[0] == "repeatable read"
    run_id = _open(db, intent_hash="ih_rr")           # all three commit on fresh connections,
    _started(db, run_id, seq=0)                       # i.e. AFTER the caller's snapshot was taken
    append_event(db, run_id, TraceEventKind.COMPLETED, seq=1,
                 idempotency_key=f"{run_id}:1", payload={"disposition": "RESOLVED"})
    assert db.execute("SELECT count(*) FROM authoring_trace_event WHERE authoring_run_id = %s",
                      (run_id,)).fetchone()[0] == 0   # the RR snapshot really is blind
    assert run_status(db, run_id) == "completed"


def test_run_status_falls_back_to_the_request_connection_when_the_dsn_is_unreachable(
        db, monkeypatch, caplog) -> None:
    """The read degrades exactly like the write: an unreachable DSN must never turn a status read
    into a request-failing exception, and the events it needs are on the caller's connection anyway
    (the write degraded there too)."""
    monkeypatch.setenv("FEATUREGEN_DSN", "host=127.0.0.1 port=1 dbname=nope connect_timeout=1")
    with caplog.at_level(logging.ERROR, logger="featuregen.formula.trace"):
        run_id = _open(db)
        append_event(db, run_id, TraceEventKind.FAILED, seq=0,
                     idempotency_key=f"{run_id}:0", payload={})
        assert run_status(db, run_id) == "failed"
    assert caplog.text.count("durable authoring-trace read failed") == 1


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
