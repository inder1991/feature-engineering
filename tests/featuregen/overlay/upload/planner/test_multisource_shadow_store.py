"""Phase 3C.2b-i-A · Task 10 — the multi-source assembly shadow STORE (migration 1010).

Mirrors ``test_shadow_store.py`` (the single-source 0999 store): a durable, append-only (WORM)
telemetry store proven by manifest<->result reconciliation, extended with PER-CANDIDATE +
PER-OPERAND rows and a read-back-compare **payload-hash** divergent-duplicate guard (never a silent
``ON CONFLICT DO NOTHING`` on conflicting telemetry).

Covers: a manifest with an expected set of 2 intents -> write 1 intent_result (>=2 candidate rows)
-> ``reconcile`` reports the missing intent; a re-write with the SAME payload is idempotent; a
re-write with a DIFFERENT payload raises (divergent-duplicate); the four axis columns persist; and
the CRITICAL two-axis rule — a resolved-ASSEMBLY-but-unresolved-CONTRACT plan lands as
compile-INCOMPLETE on the contract axis, NOT a clean resolve (axes are not collapsed)."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from featuregen.overlay.upload.planner import multisource_shadow_store as ms
from featuregen.overlay.upload.planner.multisource_shadow_store import (
    CandidateRowV1,
    CaptureStatus,
    CompileCompleteness,
    DivergentDuplicateError,
    IntentResultRowV1,
    ManifestRecordV1,
    OperandObservationRowV1,
    SemanticOutcome,
    TechnicalStatus,
)

_NOW = datetime(2026, 7, 19, tzinfo=UTC)


def _manifest(run_id="mrun_1", intent_ids=("i1", "i2")) -> ManifestRecordV1:
    return ManifestRecordV1(
        run_id=run_id,
        expected_intent_ids=tuple(intent_ids),
        versions={"multisource_assembly": "1.0.0", "operation_policy": "1.0.0"},
        shadow_flag=True,
        producer_commit="deadbeef",
        created_at=_NOW,
    )


def _intent(
    run_id="mrun_1",
    intent_id="i1",
    *,
    semantic_outcome=SemanticOutcome.resolved,
    compile_completeness=CompileCompleteness.complete,
    technical_status=TechnicalStatus.ok,
    selected_plan_id="bp_1",
    reason_codes=(),
) -> IntentResultRowV1:
    return IntentResultRowV1(
        run_id=run_id,
        intent_id=intent_id,
        semantic_outcome=semantic_outcome,
        compile_completeness=compile_completeness,
        technical_status=technical_status,
        capture_status=CaptureStatus.persisted,   # store-determined; dataclass carries the axis
        normalized_intent_hash="nih_1",
        selected_plan_id=selected_plan_id,
        reason_codes=tuple(reason_codes),
        created_at=_NOW,
    )


def _candidate(intent_id="i1", plan_id="bp_1", rank=0, output_hash="coh_1") -> CandidateRowV1:
    return CandidateRowV1(
        run_id="mrun_1",
        intent_id=intent_id,
        plan_id=plan_id,
        physical_landing={"catalog": "c1", "table_ref": "t_landing", "grain_key_refs": ["k1", "k2"]},
        contract_input_hash="cih_1",
        contract_output_hash=output_hash,
        read_set_hash="rsh_1",
        replay_envelope_hash="reh_1",
        rank=rank,
        declaration_evidence={"final_verdict": "resolved", "final_reason_codes": []},
        created_at=_NOW,
    )


def _operand(intent_id="i1", plan_id="bp_1", slot_id="s1") -> OperandObservationRowV1:
    return OperandObservationRowV1(
        run_id="mrun_1",
        intent_id=intent_id,
        plan_id=plan_id,
        slot_id=slot_id,
        pin={"concept": "transaction_amount", "object_ref": "c1.tx.amount"},
        role="measure",
        path_strategy={"aggregation": "sum", "output_type": "money"},
        governed_endpoints=[{"catalog": "c1", "table_ref": "t_src", "grain_fact_key": "gfk_1"}],
        source_binding={"source_grain_entity": "customer", "grain_fact_key": "gfk_src"},
        created_at=_NOW,
    )


# ── manifest ──


def test_manifest_roundtrip_reconcile_all_missing(db) -> None:
    ms.write_manifest(db, _manifest())
    rec = ms.reconcile(db, "mrun_1")
    assert rec.expected == 2
    assert rec.present == 0
    assert rec.missing_intent_ids == ("i1", "i2")
    assert not rec.complete


def test_manifest_idempotent_same_payload(db) -> None:
    ms.write_manifest(db, _manifest())
    ms.write_manifest(db, _manifest())   # no error, single row
    n = db.execute(
        "SELECT count(*) FROM multisource_assembly_shadow_dispatch WHERE run_id = 'mrun_1'"
    ).fetchone()[0]
    assert n == 1


def test_manifest_divergent_payload_raises(db) -> None:
    ms.write_manifest(db, _manifest())
    with pytest.raises(DivergentDuplicateError):
        ms.write_manifest(db, _manifest(intent_ids=("i1", "i2", "i3")))


# ── intent_result (+ candidates + operands) ──


def test_intent_result_written_reconcile_reports_missing(db) -> None:
    ms.write_manifest(db, _manifest())   # expected set = {i1, i2}
    status = ms.write_intent_result(
        db,
        _intent(intent_id="i1"),
        [_candidate(plan_id="bp_1", rank=0, output_hash="coh_a"),
         _candidate(plan_id="bp_2", rank=1, output_hash="coh_b")],
        [_operand(plan_id="bp_1", slot_id="s1"), _operand(plan_id="bp_2", slot_id="s1")],
    )
    assert status is CaptureStatus.persisted

    cands = ms.read_candidates(db, "mrun_1", "i1")
    assert len(cands) == 2
    assert {c["plan_id"] for c in cands} == {"bp_1", "bp_2"}

    # i2 was in the manifest expected set but never written -> reconcile surfaces the loss.
    rec = ms.reconcile(db, "mrun_1")
    assert rec.expected == 2
    assert rec.present == 1
    assert rec.missing_intent_ids == ("i2",)


def test_intent_result_idempotent_same_payload(db) -> None:
    ms.write_manifest(db, _manifest())
    args = (_intent(), [_candidate(plan_id="bp_1"), _candidate(plan_id="bp_2", output_hash="coh_b")],
            [_operand()])
    ms.write_intent_result(db, *args)
    ms.write_intent_result(db, *args)   # SAME payload -> idempotent, no raise
    n = db.execute(
        "SELECT count(*) FROM multisource_assembly_shadow_intent_result "
        "WHERE run_id = 'mrun_1' AND intent_id = 'i1'"
    ).fetchone()[0]
    assert n == 1
    assert len(ms.read_candidates(db, "mrun_1", "i1")) == 2


def test_intent_result_divergent_payload_raises(db) -> None:
    ms.write_manifest(db, _manifest())
    ms.write_intent_result(db, _intent(), [_candidate(output_hash="coh_first")], [_operand()])
    # A conflicting re-write for the same key (different candidate contract hash) MUST raise —
    # never a silent ON CONFLICT DO NOTHING on divergent telemetry.
    with pytest.raises(DivergentDuplicateError):
        ms.write_intent_result(db, _intent(), [_candidate(output_hash="coh_DIFFERENT")], [_operand()])


def test_intent_result_divergent_axis_raises(db) -> None:
    ms.write_manifest(db, _manifest())
    ms.write_intent_result(db, _intent(semantic_outcome=SemanticOutcome.resolved),
                           [_candidate()], [_operand()])
    with pytest.raises(DivergentDuplicateError):
        ms.write_intent_result(db, _intent(semantic_outcome=SemanticOutcome.no_governed_path),
                               [_candidate()], [_operand()])


# ── the four axis columns + the two-axis operability rule ──


def test_four_axis_columns_persist(db) -> None:
    ms.write_manifest(db, _manifest())
    ms.write_intent_result(
        db,
        _intent(
            semantic_outcome=SemanticOutcome.resolved,
            compile_completeness=CompileCompleteness.complete,
            technical_status=TechnicalStatus.ok,
        ),
        [_candidate()],
        [_operand()],
    )
    rows = ms.read_intent_results(db, "mrun_1")
    assert len(rows) == 1
    row = rows[0]
    assert row["semantic_outcome"] == "resolved"
    assert row["compile_completeness"] == "complete"
    assert row["technical_status"] == "ok"
    assert row["capture_status"] == "persisted"


def test_resolved_assembly_unresolved_contract_lands_compile_incomplete(db) -> None:
    """CRITICAL two-axis rule: a plan whose ASSEMBLY axis resolved but whose CONTRACT axis is
    stale/safety-gapped is NOT operationally resolved — it must land compile-INCOMPLETE on the
    contract axis, not as a clean resolve. The two axes are separate columns, never collapsed."""
    ms.write_manifest(db, _manifest())
    ms.write_intent_result(
        db,
        _intent(
            semantic_outcome=SemanticOutcome.resolved,          # assembly axis: a plan WAS assembled
            compile_completeness=CompileCompleteness.incomplete,  # contract axis: stale / safety gap
            technical_status=TechnicalStatus.ok,
        ),
        [_candidate()],
        [_operand()],
    )
    row = ms.read_intent_results(db, "mrun_1")[0]
    # Resolved on assembly, but NOT collapsed into a clean resolve — contract axis says incomplete.
    assert row["semantic_outcome"] == "resolved"
    assert row["compile_completeness"] == "incomplete"


# ── crossings (I-1): governed-crossing audit evidence + payload-hash discipline ──


def test_crossings_persist_and_exclude_confirmed_event_id_from_hash(db) -> None:
    """I-1: the operand's governed ``crossings`` round-trip (incl. the AUDIT-only confirmed_event_id),
    but the per-event confirmed_event_id is EXCLUDED from the divergent-duplicate payload_hash — hashing
    only the deterministic crossing identity. A differing event id is idempotent; a differing authority
    is a conflict."""
    ms.write_manifest(db, _manifest())
    crossing = {"kind": "governed_bridge", "catalog": "c2", "table": "public.acc",
                "bridge_fact_key": "gbfk_1", "realization_ref": None, "authority": "verified",
                "confirmed_event_id": "evt-gbfk_1"}
    base = _operand()
    ms.write_intent_result(db, _intent(), [_candidate()], [replace(base, crossings=[crossing])])

    stored = ms.read_operands(db, "mrun_1", "i1", "bp_1")[0]["crossings"]
    assert list(stored) == [crossing]   # full record persisted, incl. the audit confirmed_event_id

    # a re-write differing ONLY in the per-event confirmed_event_id is IDEMPOTENT (excluded from hash)
    status = ms.write_intent_result(
        db, _intent(), [_candidate()],
        [replace(base, crossings=[{**crossing, "confirmed_event_id": "evt-DIFFERENT"}])])
    assert status is CaptureStatus.persisted

    # ...but a re-write differing in the DETERMINISTIC crossing identity (authority) IS a conflict
    with pytest.raises(DivergentDuplicateError):
        ms.write_intent_result(
            db, _intent(), [_candidate()],
            [replace(base, crossings=[{**crossing, "authority": "unverified"}])])


# ── two-phase capture (mirror 0999 write_run_and_plans) ──


def test_two_phase_fallback_on_child_failure(db, monkeypatch) -> None:
    ms.write_manifest(db, _manifest())

    def _boom(_conn, _c):
        raise RuntimeError("candidate insert failed")

    monkeypatch.setattr(ms, "_insert_candidate", _boom)
    status = ms.write_intent_result(db, _intent(), [_candidate()], [_operand()])
    assert status is CaptureStatus.persistence_partial
    rows = ms.read_intent_results(db, "mrun_1")
    assert len(rows) == 1
    assert rows[0]["capture_status"] == "persistence_partial"
    # The atomic parent+children write rolled the children back.
    assert ms.read_candidates(db, "mrun_1", "i1") == []


def test_reconcile_complete_when_all_present(db) -> None:
    ms.write_manifest(db, _manifest(intent_ids=("i1",)))
    ms.write_intent_result(db, _intent(intent_id="i1"), [_candidate()], [_operand()])
    rec = ms.reconcile(db, "mrun_1")
    assert rec.expected == 1
    assert rec.present == 1
    assert rec.missing_intent_ids == ()
    assert rec.complete
