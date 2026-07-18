"""Slice-2 Task 3: durable, TOTAL per-field dispositions in the persisted ``pass_b`` stage detail.

The Task-1 collector (``make_ref_accept`` appends five per-field records for every table it
validates) and the Task-2 staling seam (``_propose_table_facts`` flips ``prior_value_staled``) are
threaded to PRODUCTION here: ``ingest_upload`` creates ONE collector per run, passes it through
``synthesize_tables`` into the ref-aware accept AND into ``_propose_table_facts``, totalizes it
with five ``not_evaluated`` records per unresolved table ([F12] — ``run_batched`` returns resolved
refs only, so a table that never reached validation would otherwise have NO record at all), and
persists it as ``pass_b`` stage ``detail["dispositions"]`` via the recorder/flush path the route
uses. ``not_evaluated`` is DISTINCT from ``abstained`` (abstained = evaluated, model gave nothing).

The scripted synthesis follows [F14]: only REAL schema keys (the synth object is
``additionalProperties: false`` — an extra key would fail ``reg.validate`` and whole-reject).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.stage_report import StageRecorder
from featuregen.overlay.upload.table_synth import DISPOSITION_FIELDS

_NOW = datetime(2026, 7, 18, tzinfo=UTC)
_SOURCE = "ftr_disp"
_FIELDS = {"grain", "availability_time", "table_role", "primary_entity", "event_or_snapshot"}


def _seal_config():
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def _uploader() -> IdentityEnvelope:
    return IdentityEnvelope(subject="user:uploader", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _open_run(db, run_id: str) -> str:
    db.execute(
        "INSERT INTO ingestion_run (id, origin_type, catalog_source, actor_subject, status, "
        "started_at, heartbeat_at) VALUES (%s, 'upload', %s, 'user:uploader', 'in_progress', "
        "%s, %s)", (run_id, _SOURCE, _NOW, _NOW))
    return run_id


def _client(results: list[dict]) -> FakeLLM:
    return FakeLLM(script={"table_synth": FakeResponse(output={"results": results})})


def _ingest_and_flush(db, rows, client, run_id) -> dict:
    """One Pass-B-enabled ingest with a recorder, flushed to ``ingestion_run_stage`` the way the
    route does at terminalize; returns the PERSISTED ``pass_b`` detail (read back from the DB —
    proving the collector is durable JSONB, not just an in-memory buffer)."""
    rec = StageRecorder()
    res = ingest_upload(db, _SOURCE, rows, actor=_uploader(), client=client, stage_recorder=rec)
    assert res.status == "ingested"
    assert rec.flush(db, run_id, now=_NOW) > 0
    row = db.execute(
        "SELECT detail FROM ingestion_run_stage"
        " WHERE ingestion_run_id = %s AND stage = 'pass_b' ORDER BY id DESC LIMIT 1",
        (run_id,)).fetchone()
    assert row is not None and row[0] is not None
    return row[0]


def _recs(detail: dict) -> dict[tuple[str, str], dict]:
    return {(d["table"], d["field"]): d for d in detail["dispositions"]}


def test_dispositions_persist_total_in_stage_detail(db, monkeypatch):
    """The exact scripted result: table ``txn`` returns ``grain_columns=["ghost"]`` (not a real
    column) and ``table_role="wat"`` (off-vocab) — both drop PER-FIELD with their reason codes,
    the other three fields abstain, and ALL FIVE fields are present (totality). Table ``orphan``
    is assembled but never appears in the batch result -> five ``not_evaluated`` records ([F12])."""
    _seal_config()
    monkeypatch.setenv("OVERLAY_TABLE_SYNTH", "1")
    rows = [CanonicalRow(_SOURCE, "txn", "id", "integer"),
            CanonicalRow(_SOURCE, "txn", "posted_at", "timestamp"),
            CanonicalRow(_SOURCE, "orphan", "ref_code", "varchar")]
    client = _client([{"ref": "txn",
                       "synthesis": {"grain_columns": ["ghost"], "table_role": "wat"}}])  # [F14]
    run_id = _open_run(db, "ingrun_DISP1")

    detail = _ingest_and_flush(db, rows, client, run_id)

    recs = _recs(detail)
    assert recs[("txn", "grain")]["status"] == "dropped_invalid"
    assert recs[("txn", "grain")]["reason"] == "grain_col_not_in_table"
    assert recs[("txn", "table_role")]["status"] == "dropped_invalid"
    assert recs[("txn", "table_role")]["reason"] == "role_off_vocab"
    # totality: all five fields present for the evaluated table
    assert {f for (t, f) in recs if t == "txn"} == _FIELDS
    for field in ("availability_time", "primary_entity", "event_or_snapshot"):
        assert recs[("txn", field)]["status"] == "abstained"     # absent == the model gave nothing
    # [F12]: the unresolved table gets the SAME five-field record shape, status not_evaluated
    assert {f for (t, f) in recs if t == "orphan"} == _FIELDS
    for field in _FIELDS:
        rec = recs[("orphan", field)]
        assert rec["status"] == "not_evaluated"
        assert rec["reason"] is None
        assert rec["prior_value_staled"] is False
    # uniform/total: exactly one record per (table, field) — 2 tables x 5 fields, no duplicates
    assert len(detail["dispositions"]) == 10
    # the existing outcome keys still ride the same detail (dispositions is additive)
    assert detail["resolved"] == 1 and detail["expected"] == 2 and detail["unresolved"] == 1


def test_prior_value_staled_is_live_on_the_ingest_path(db, monkeypatch):
    """Resolves the Task-2 Minor: the ingest path threads the SAME collector into
    ``_propose_table_facts``, so a re-upload whose accepted ``table_role`` supersedes the prior
    LLM rows persists ``prior_value_staled=True`` in the run-2 ``pass_b`` stage detail ([F9])."""
    _seal_config()
    monkeypatch.setenv("OVERLAY_TABLE_SYNTH", "1")
    rows = [CanonicalRow(_SOURCE, "txn", "id", "integer"),
            CanonicalRow(_SOURCE, "txn", "posted_at", "timestamp")]

    detail1 = _ingest_and_flush(
        db, rows, _client([{"ref": "txn", "synthesis": {"grain_columns": [],
                                                        "table_role": "dimension"}}]),
        _open_run(db, "ingrun_DISP2A"))
    rec1 = _recs(detail1)[("txn", "table_role")]
    assert rec1["status"] == "accepted" and rec1["prior_value_staled"] is False

    detail2 = _ingest_and_flush(
        db, rows, _client([{"ref": "txn", "synthesis": {"grain_columns": [],
                                                        "table_role": "reference"}}]),
        _open_run(db, "ingrun_DISP2B"))
    rec2 = _recs(detail2)[("txn", "table_role")]
    assert rec2["status"] == "accepted" and rec2["prior_value_staled"] is True


def test_disposition_fields_constant_is_the_five_field_contract():
    """The [F12] totalizer and the accept write the SAME five fields, from ONE constant."""
    assert DISPOSITION_FIELDS == ("grain", "availability_time", "table_role", "primary_entity",
                                  "event_or_snapshot")
