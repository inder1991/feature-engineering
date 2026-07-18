"""Phase-2 Slice 2, Task 4 — the slice ACCEPTANCE gate on the committed SYNTHETIC FTR sample.

Drives the WHOLE upload path (real FTR reader -> validate -> Pass A -> the two-phase wide-table
Pass B) through real ``ingest_upload`` calls over the committed synthetic fixture (126 columns,
one ``comp_fin_tran`` table, schema ``dpl_eib_compliance``) and proves the Slice-2 contract
end-to-end:

(a) PER-FIELD SALVAGE: a synthesis naming a ghost grain column drops the GRAIN ONLY — its valid
    ``table_role``/``primary_entity`` still land as advisory evidence and project into the graph.
(b) VOCAB ALIASES: ``dim`` / ``fact`` / ``reference`` are all accepted and STORED as their
    canonical values — ``dimension`` / ``event_fact`` (the event-signal split) / ``reference``.
(c) EXACT DROP REASONS: an off-vocab role drops with ``role_off_vocab``; a non-registry entity
    drops with ``entity_not_registered`` — each independently, never whole-rejecting.
(d) GRAPH-CLEARING STALE LIFECYCLE: a second upload that OMITS a previously-proposed
    ``table_role`` clears the flat display (``graph_node.table_role IS NULL``), records a
    lowercase ``"staled"`` decision ([F14]) whose ``supersedes_event_id`` points at the round-1
    RESOLVED decision read from the DURABLE log ([F2] — the graph link is NULL after the
    ``build_graph`` rebuild), leaves NO active LLM evidence, and persists
    ``prior_value_staled=True`` on the run-2 disposition record ([F9]).
(e) TOTAL DISPOSITIONS: every run's persisted ``pass_b`` stage ``detail["dispositions"]`` carries
    ALL FIVE per-field records for the evaluated table.

Hermetic: the request-capturing scripted FakeLLM harness (``synthetic_ftr_upload``), no network;
the real bank CSV is never used. Scripted syntheses carry only REAL v2 schema keys ([F14] — the
synth object is ``additionalProperties: false``).
"""
from __future__ import annotations

from datetime import UTC, datetime

from featuregen.overlay.evidence import EvidenceProducer
from featuregen.overlay.field_decision import FieldDecisionEventType, read_field_decisions
from featuregen.overlay.field_evidence import read_active_field_evidence
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.stage_report import StageRecorder
from featuregen.overlay.upload.table_synth import DISPOSITION_FIELDS

_NOW = datetime(2026, 7, 18, tzinfo=UTC)
_TABLE = "comp_fin_tran"                 # validate_rows lowercases identifiers
_SCHEMA = "dpl_eib_compliance"           # the fixture's REAL (non-public) FTR schema


def _table_ref(source: str) -> str:
    """The SCHEMA-PRESERVING logical_ref evidence/decisions are keyed under. The graph node itself
    is public-flattened (``field_resolution._graph_key``) — the two are distinct on purpose."""
    return normalize_ref(source, _SCHEMA, _TABLE)


def _graph_row(db, source: str) -> tuple:
    """(table_role, primary_entity, event_or_snapshot, table_role_decision_id) from the
    public-flattened TABLE node ``build_graph`` stored for this source."""
    return db.execute(
        "SELECT table_role, primary_entity, event_or_snapshot, table_role_decision_id"
        " FROM graph_node WHERE catalog_source = %s AND object_ref = %s AND kind = 'table'",
        (source, f"public.{_TABLE}")).fetchone()


def _ingest(db, synthetic_ftr_upload, *, source: str, run_id: str, synthesis: dict | None):
    """One Pass-B-enabled synthetic-FTR ingest with a flushed stage recorder (the way the route
    terminalizes); returns ``(IngestResult, persisted pass_b detail)`` — the detail is READ BACK
    from ``ingestion_run_stage``, proving the collector is durable JSONB, not an in-memory buffer."""
    db.execute(
        "INSERT INTO ingestion_run (id, origin_type, catalog_source, actor_subject, status,"
        " started_at, heartbeat_at) VALUES (%s, 'upload', %s, 'user:uploader', 'in_progress',"
        " %s, %s)", (run_id, source, _NOW, _NOW))
    rec = StageRecorder()
    r = synthetic_ftr_upload(db, source=source, synthesis=synthesis, stage_recorder=rec)
    assert r.status == "ingested"
    assert rec.flush(db, run_id, now=_NOW) > 0
    row = db.execute(
        "SELECT detail FROM ingestion_run_stage"
        " WHERE ingestion_run_id = %s AND stage = 'pass_b' ORDER BY id DESC LIMIT 1",
        (run_id,)).fetchone()
    assert row is not None and row[0] is not None
    return r, row[0]


def _recs(detail: dict) -> dict[tuple[str, str], dict]:
    """The persisted dispositions keyed ``(table, field)`` — asserting no duplicate records."""
    recs = {(d["table"], d["field"]): d for d in detail["dispositions"]}
    assert len(recs) == len(detail["dispositions"])
    return recs


def _assert_total(detail: dict) -> dict[tuple[str, str], dict]:
    """(e) TOTALITY: the persisted ``pass_b`` detail carries exactly the five per-field records
    for the fixture's one evaluated table — no field missing, none duplicated."""
    recs = _recs(detail)
    assert {f for (t, f) in recs if t == _TABLE} == set(DISPOSITION_FIELDS)
    assert len(detail["dispositions"]) == 5
    return recs


def _active_llm(db, ref: str, field: str) -> list:
    return [e for e in read_active_field_evidence(db, ref, field)
            if e.producer == EvidenceProducer.LLM.value]


def test_ghost_grain_salvage_then_omitted_role_stales_and_clears(db, synthetic_ftr_upload):
    """(a) + (b:dim) + (d) + (e) on one source, two rounds. Round 1: a ghost grain column drops
    the grain ONLY while the valid ``dim``/``transaction`` advisory fields land and project.
    Round 2: the re-upload's synthesis OMITS them — display cleared, lowercase ``staled`` decision
    superseding the round-1 RESOLVED one, no active LLM evidence, ``prior_value_staled=True``."""
    source = "ftr_s2_stale"
    ref = _table_ref(source)

    # ── round 1: ghost grain + valid advisory fields ──
    r1, detail1 = _ingest(db, synthetic_ftr_upload, source=source, run_id="ingrun_S2A1",
                          synthesis={"grain_columns": ["ghost_col"], "table_role": "dim",
                                     "primary_entity": "transaction"})
    recs1 = _assert_total(detail1)
    # (a) the ghost grain dropped THAT FIELD ONLY — the advisory fields keep their own verdicts.
    assert recs1[(_TABLE, "grain")]["status"] == "dropped_invalid"
    assert recs1[(_TABLE, "grain")]["reason"] == "grain_col_not_in_table"
    assert recs1[(_TABLE, "table_role")]["status"] == "accepted"
    assert recs1[(_TABLE, "primary_entity")]["status"] == "accepted"
    for field in ("availability_time", "event_or_snapshot"):
        assert recs1[(_TABLE, field)]["status"] == "abstained"
    # no grain/availability fact proposed; the salvaged synthesis counts as an honest abstention
    assert (r1.passb_proposed, r1.passb_abstained) == (0, 1)
    # (b) the "dim" alias STORED canonical: "dimension" projected, the registry entity alongside
    role, entity, eos, _link = _graph_row(db, source)
    assert (role, entity, eos) == ("dimension", "transaction", None)
    assert {e.proposed_value for e in _active_llm(db, ref, "table_role")} == {"dimension"}

    # ── round 2: the re-upload OMITS table_role/primary_entity (full abstain synthesis) ──
    _r2, detail2 = _ingest(db, synthetic_ftr_upload, source=source, run_id="ingrun_S2A2",
                           synthesis=None)
    # (d) the flat display is CLEARED — the dropped value did not survive the graph rebuild
    role, entity, _eos, link = _graph_row(db, source)
    assert role is None and entity is None
    decisions = read_field_decisions(db, ref, "table_role")
    latest = decisions[-1]
    assert latest.event_type == "staled"                    # [F14] the stored enum is LOWERCASE
    assert latest.event_type == FieldDecisionEventType.STALED.value
    # [F2] supersedes comes from the DURABLE log (the graph link is NULL after build_graph):
    # it is non-NULL and points at the round-1 RESOLVED decision.
    assert latest.supersedes_event_id is not None
    resolved_ids = {d.decision_event_id for d in decisions if d.event_type == "resolved"}
    assert latest.supersedes_event_id in resolved_ids
    assert link == latest.decision_event_id                 # link repointed AT the staling event
    assert _active_llm(db, ref, "table_role") == []         # no active LLM evidence remains
    # (d)+(e): the run-2 persisted record carries the [F9] staling flip, and stays TOTAL
    recs2 = _assert_total(detail2)
    rec = recs2[(_TABLE, "table_role")]
    assert rec["status"] == "abstained" and rec["prior_value_staled"] is True
    # the OTHER previously-accepted advisory field staled the same way (field-general lifecycle)
    assert recs2[(_TABLE, "primary_entity")]["prior_value_staled"] is True
    # a field that never had a value has nothing to stale
    assert recs2[(_TABLE, "event_or_snapshot")]["prior_value_staled"] is False


def test_fact_alias_with_event_signal_stores_event_fact(db, synthetic_ftr_upload):
    """(b:fact) — ``fact`` + an ``event`` signal is accepted and STORED as canonical
    ``event_fact`` (the event/snapshot split), with the registered entity alongside."""
    source = "ftr_s2_fact"
    _r, detail = _ingest(db, synthetic_ftr_upload, source=source, run_id="ingrun_S2B",
                         synthesis={"grain_columns": [], "table_role": "fact",
                                    "primary_entity": "customer", "event_or_snapshot": "event"})
    role, entity, eos, _link = _graph_row(db, source)
    assert (role, entity, eos) == ("event_fact", "customer", "event")
    # the STORED evidence value is the canonical role, not the raw alias
    assert {e.proposed_value for e in
            _active_llm(db, _table_ref(source), "table_role")} == {"event_fact"}
    recs = _assert_total(detail)
    for field in ("table_role", "primary_entity", "event_or_snapshot"):
        assert recs[(_TABLE, field)]["status"] == "accepted"


def test_reference_role_kept_while_unregistered_entity_drops(db, synthetic_ftr_upload):
    """(b:reference) + (c:entity) — ``reference`` passes through canonical while the non-registry
    ``narwhal_pod`` entity drops with EXACTLY ``entity_not_registered``; per-field independence."""
    source = "ftr_s2_ref"
    _r, detail = _ingest(db, synthetic_ftr_upload, source=source, run_id="ingrun_S2C",
                         synthesis={"grain_columns": [], "table_role": "reference",
                                    "primary_entity": "narwhal_pod"})
    role, entity, _eos, _link = _graph_row(db, source)
    assert (role, entity) == ("reference", None)
    recs = _assert_total(detail)
    assert recs[(_TABLE, "table_role")]["status"] == "accepted"
    rec = recs[(_TABLE, "primary_entity")]
    assert rec["status"] == "dropped_invalid" and rec["reason"] == "entity_not_registered"


def test_off_vocab_role_drops_with_exact_reason_code(db, synthetic_ftr_upload):
    """(c:role) — an off-vocab role drops with EXACTLY ``role_off_vocab`` while the valid
    registry entity in the SAME synthesis still lands (never a whole-reject)."""
    source = "ftr_s2_wat"
    _r, detail = _ingest(db, synthetic_ftr_upload, source=source, run_id="ingrun_S2D",
                         synthesis={"grain_columns": [], "table_role": "wat",
                                    "primary_entity": "transaction"})
    role, entity, _eos, _link = _graph_row(db, source)
    assert (role, entity) == (None, "transaction")
    recs = _assert_total(detail)
    rec = recs[(_TABLE, "table_role")]
    assert rec["status"] == "dropped_invalid" and rec["reason"] == "role_off_vocab"
    assert recs[(_TABLE, "primary_entity")]["status"] == "accepted"
