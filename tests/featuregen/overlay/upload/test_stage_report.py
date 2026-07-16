"""StageReport/StageRecorder unit slice (first-release hardening #22).

The recorder BUFFERS stage outcomes in memory during ingest and flushes them to
``ingestion_run_stage`` only when the route terminalizes the run — so stage rows always commit
with the run's terminal state and the ingest hot path never writes them. Everything here is
defensive by contract: a ``None`` recorder is a no-op, a record/flush failure can never affect
the ingest that carried it, and a double flush never violates the (run, stage, attempt) key.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from featuregen.overlay.upload.stage_report import StageRecorder, StageReport, record_stage

_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


def _open_run(db, run_id: str = "ingrun_SR1") -> str:
    db.execute(
        "INSERT INTO ingestion_run (id, origin_type, catalog_source, actor_subject, status, "
        "started_at, heartbeat_at) VALUES (%s, 'upload', 'deposits', 'user:tester', "
        "'in_progress', %s, %s)", (run_id, _NOW, _NOW))
    return run_id


def _rows(db, run_id: str):
    return db.execute(
        "SELECT stage, attempt, state, reason_code, detail, completed_at "
        "FROM ingestion_run_stage WHERE ingestion_run_id = %s ORDER BY id", (run_id,)).fetchall()


# ── recording ─────────────────────────────────────────────────────────────────────────────────────


def test_record_buffers_in_memory_and_flush_persists(db):
    run_id = _open_run(db)
    rec = StageRecorder()
    rec.record("validation", "partial", detail={"good": 3, "quarantined": 1})
    rec.record("brake", "deferred", reason_code="held")
    assert _rows(db, run_id) == []                    # buffered: nothing written during "ingest"

    assert rec.flush(db, run_id, now=_NOW) == 2
    rows = _rows(db, run_id)
    assert [(r[0], r[1], r[2], r[3]) for r in rows] == [
        ("validation", 1, "partial", None), ("brake", 1, "deferred", "held")]
    assert rows[0][4] == {"good": 3, "quarantined": 1}
    assert rows[0][5] is not None                     # a completed_at is always stamped


def test_repeated_stage_records_get_increasing_attempts(db):
    run_id = _open_run(db)
    rec = StageRecorder()
    rec.record("drift", "lagged", reason_code="projection_lag")
    rec.record("drift", "succeeded")
    rec.flush(db, run_id, now=_NOW)
    assert [(r[0], r[1], r[2]) for r in _rows(db, run_id)] == [
        ("drift", 1, "lagged"), ("drift", 2, "succeeded")]


def test_record_rejects_a_state_outside_the_taxonomy():
    rec = StageRecorder()
    with pytest.raises(ValueError):
        rec.record("validation", "held")   # a RUN status, not a stage state
    assert rec.reports == ()


def test_stage_report_is_frozen():
    report = StageReport("validation", "succeeded")
    with pytest.raises(Exception):
        report.state = "failed"   # type: ignore[misc]


# ── the defensive seam ingest relies on ───────────────────────────────────────────────────────────


def test_record_stage_none_recorder_is_a_noop():
    record_stage(None, "validation", "succeeded")   # must not raise


def test_record_stage_swallows_recorder_failures():
    class _Boom(StageRecorder):
        def record(self, *a, **k):
            raise RuntimeError("recorder broke")

    record_stage(_Boom(), "validation", "succeeded")   # a recorder failure never reaches ingest


def test_record_stage_swallows_a_bad_state_too():
    rec = StageRecorder()
    record_stage(rec, "validation", "not-a-state")   # invalid state -> warned, not raised
    assert rec.reports == ()


def test_flush_failure_is_contained_and_keeps_the_buffer(db):
    """A flush fault (here: no such run -> FK violation) must not raise NOR poison the caller's
    transaction (savepointed), and must keep the buffer so a later durable flush can still land."""
    rec = StageRecorder()
    rec.record("validation", "succeeded")
    assert rec.flush(db, "ingrun_NO_SUCH_RUN", now=_NOW) == 0
    db.execute("SELECT 1")                            # the tx is NOT aborted (savepoint contained it)
    run_id = _open_run(db)
    assert rec.flush(db, run_id, now=_NOW) == 1       # buffer survived the failed flush


def test_double_flush_writes_nothing_new(db):
    run_id = _open_run(db)
    rec = StageRecorder()
    rec.record("validation", "succeeded")
    assert rec.flush(db, run_id, now=_NOW) == 1
    assert rec.flush(db, run_id, now=_NOW) == 0       # drained: no duplicate rows, no UNIQUE breach
    assert len(_rows(db, run_id)) == 1


def test_record_after_flush_continues_the_attempt_sequence(db):
    run_id = _open_run(db)
    rec = StageRecorder()
    rec.record("drift", "lagged")
    rec.flush(db, run_id, now=_NOW)
    rec.record("drift", "succeeded")                  # a later record of the SAME stage
    rec.flush(db, run_id, now=_NOW)
    assert [(r[1], r[2]) for r in _rows(db, run_id)] == [(1, "lagged"), (2, "succeeded")]


def test_flush_durable_falls_back_to_the_given_connection(db):
    """No DSN configured (the rolled-back harness): flush_durable degrades to fallback_conn —
    the same best-effort ladder terminalize_run_durable uses."""
    run_id = _open_run(db, "ingrun_SR_DUR")
    rec = StageRecorder()
    rec.record("parse", "failed", reason_code="http_400")
    rec.flush_durable(run_id, now=_NOW, fallback_conn=db)
    assert [(r[0], r[2]) for r in _rows(db, run_id)] == [("parse", "failed")]
