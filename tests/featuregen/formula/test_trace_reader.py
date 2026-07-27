"""Spec A Task 2 — the trace's public EVENT reader (``read_terminal_event`` / ``read_run_intent_hash``).

``trace.py`` shipped with no way to read an event back: ``run_status`` collapses the terminal event
to three words and throws the payload away. Materialization's Gate 1 (spec §1.2) needs the payload
itself, because that is the only immutable record of what an authoring run decided.

Every test here is discriminating on one of four things:

* ABSENCE is ``None`` — a run with no terminal event, and a run this module has never heard of;
* the record is returned WHOLE — kind, payload and the 64-hex ``payload_hash``;
* the hash is the one ``append_event`` computed, over the SAME construction a verifier will use
  (``sha256`` of the RFC 8785 bytes) — if those two ever diverge, Gate 1's tamper check inverts and
  starts refusing honest runs;
* ``kind`` alone is not a disposition — a REJECTED run's terminal event is ``COMPLETED``.
"""
from __future__ import annotations

import hashlib

import pytest
from tests.featuregen._helpers import make_actor

from featuregen.formula._jcs import dumps as jcs_dumps
from featuregen.formula.trace import (
    TraceEventKind,
    append_event,
    open_authoring_run,
    read_run_intent_hash,
    read_terminal_event,
)

_VERSIONS = {"disposition_policy_version": 1}


@pytest.fixture(autouse=True, scope="module")
def no_dsn():
    """DSN-HERMETIC (same rationale as ``test_trace.py``): with an ambient ``FEATUREGEN_DSN`` these
    writes would COMMIT on the durable fresh connection, and ``authoring_trace_event`` rows can
    physically never be cleaned up — a second run of the suite would then fail on stale keys."""
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("FEATUREGEN_DSN", raising=False)
        yield


def _open(db, *, intent_hash: str = "ih_reader") -> str:
    return open_authoring_run(db, intent_hash=intent_hash, versions=_VERSIONS, actor=make_actor())


def _append(db, run_id: str, kind: TraceEventKind, payload: dict, *, seq: int) -> None:
    append_event(db, run_id, kind, seq=seq, idempotency_key=f"{run_id}:{seq}", payload=payload)


# ── absence ──────────────────────────────────────────────────────────────────────────────────────

def test_a_run_with_no_terminal_event_reads_as_None(db) -> None:
    run_id = _open(db)
    _append(db, run_id, TraceEventKind.STARTED, {"intent_hash": "ih_reader"}, seq=0)
    assert read_terminal_event(db, run_id) is None


def test_an_unknown_run_reads_as_None(db) -> None:
    """Symmetric with ``run_status``: the derivation can only ever fail TOWARD absence."""
    assert read_terminal_event(db, "arun_never_opened") is None
    assert read_run_intent_hash(db, "arun_never_opened") is None


# ── the whole record ─────────────────────────────────────────────────────────────────────────────

def test_the_terminal_event_is_returned_whole(db) -> None:
    run_id = _open(db)
    _append(db, run_id, TraceEventKind.STARTED, {"intent_hash": "ih_reader"}, seq=0)
    payload = {"authoring_disposition": "RESOLVED", "candidate_formula_hash": "a" * 64,
               "output_requirements": [], "authority_failures": []}
    _append(db, run_id, TraceEventKind.COMPLETED, payload, seq=1)

    event = read_terminal_event(db, run_id)

    assert event is not None
    assert event.kind is TraceEventKind.COMPLETED
    assert dict(event.payload) == payload
    assert len(event.payload_hash) == 64
    assert all(c in "0123456789abcdef" for c in event.payload_hash)


def test_the_payload_hash_is_recomputable_from_the_read_back_payload(db) -> None:
    """THE tamper-evidence contract. Gate 1 check 2 recomputes this digest over the read-back
    jsonb; if the reader's round-trip did not reproduce the bytes ``append_event`` hashed, that
    check would refuse every honest run with ``TERMINAL_PAYLOAD_TAMPERED``."""
    run_id = _open(db)
    payload = {"authoring_disposition": "REJECTED", "structural_status": "invalid_formula",
               "candidate_formula_hash": None, "count": 3, "nested": {"b": 2, "a": 1}}
    _append(db, run_id, TraceEventKind.COMPLETED, payload, seq=0)

    event = read_terminal_event(db, run_id)

    assert event is not None
    assert hashlib.sha256(jcs_dumps(dict(event.payload))).hexdigest() == event.payload_hash


def test_the_returned_payload_cannot_be_mutated_in_place(db) -> None:
    """A tamper-evident record a caller can edit before hashing it is not evidence of anything."""
    run_id = _open(db)
    _append(db, run_id, TraceEventKind.COMPLETED, {"authoring_disposition": "RESOLVED"}, seq=0)

    event = read_terminal_event(db, run_id)

    assert event is not None
    with pytest.raises(TypeError):
        event.payload["authoring_disposition"] = "TAMPERED"  # type: ignore[index]


def test_a_failed_terminal_is_read_as_FAILED(db) -> None:
    run_id = _open(db)
    _append(db, run_id, TraceEventKind.FAILED,
            {"authoring_disposition": "TECHNICAL_FAILURE"}, seq=0)

    event = read_terminal_event(db, run_id)

    assert event is not None
    assert event.kind is TraceEventKind.FAILED


def test_kind_alone_does_not_say_the_run_resolved(db) -> None:
    """VERIFIED: ``authoring._TERMINAL_FOR_DISPOSITION`` maps ONLY ``TECHNICAL_FAILURE`` to
    ``FAILED``, so a REJECTED run's terminal event is ``COMPLETED`` too. The disposition lives in
    the payload — reading ``kind`` as a verdict is the defect this reader exists to make avoidable."""
    run_id = _open(db)
    _append(db, run_id, TraceEventKind.COMPLETED, {"authoring_disposition": "REJECTED"}, seq=0)

    event = read_terminal_event(db, run_id)

    assert event is not None
    assert event.kind is TraceEventKind.COMPLETED
    assert event.payload["authoring_disposition"] == "REJECTED"


# ── the manifest's intent hash ───────────────────────────────────────────────────────────────────

def test_the_manifest_intent_hash_is_readable(db) -> None:
    run_id = _open(db, intent_hash="ih_specific")
    assert read_run_intent_hash(db, run_id) == "ih_specific"
