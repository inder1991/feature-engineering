"""Phase 3C.2b-i-A · Task 12 — the PARTITIONED gold set + the assembly GATE (spec §10/§11).

The payoff of the shadow slice: the measurement that decides whether the governed multi-source
assembler is trustworthy. :func:`evaluate_assembly_gate` re-seeds the deterministic gold, drives the
Task-11 two-connection shadow harness over the CORRECTNESS gold TWICE (distinct ``run_id``s), and
evaluates the spec §10 criteria over the CLEAN population.

Covered here:
  * the gate PASSES on the current (correct) implementation — every positive resolves to its EXACT
    landing (incl. the composite grain), the ≥6-shape coverage holds, preservation/governed-endpoints/
    one-grain/aggregation-temporal hold, plan+contract identity is deterministic across the two runs,
    reconciliation is complete, and NO technical/truncation reading appears in the clean population;
  * the gate is NOT vacuous — a reject-all assembler (no-op seed) FAILS positive coverage, and a fault
    reading LEAKED into the clean population FAILS criterion (8);
  * the fault-observability controls are EXACTLY classified (``technical_failure`` / ``budget_truncated``)
    and live under their OWN run ids, excluded from the clean population.

Fixtures mirror Task 11: ``db`` is the durable telemetry connection; ``planning_conn`` is a SEPARATE
session onto the same test DB (the gold fixture is seeded there and rolled back by the harness).
"""
from __future__ import annotations

from datetime import timedelta

import psycopg
import pytest

from featuregen.contracts.envelopes import Command
from featuregen.overlay.catalog import current_catalog_adapter
from featuregen.overlay.commands import propose_fact
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.planner import multisource_shadow
from featuregen.overlay.upload.planner.contracts import MULTISOURCE_GOLD_MIN_SHAPES
from featuregen.overlay.upload.planner.multisource_gate import (
    AssemblyGateResultV1,
    evaluate_assembly_gate,
    evaluate_gate_over_runs,
)
from featuregen.overlay.upload.planner.multisource_gold import (
    CORRECTNESS_GOLD,
    FAULT_CONTROLS,
    GOLD_NOW,
    GoldCaseV1,
)
from featuregen.overlay.upload.planner.multisource_shadow import (
    run_multisource_assembly_shadow,
)
from featuregen.overlay.upload.planner.multisource_shadow_store import (
    CaptureStatus,
    CompileCompleteness,
    IntentResultRowV1,
    ManifestRecordV1,
    SemanticOutcome,
    TechnicalStatus,
    read_intent_results,
    write_intent_result,
    write_manifest,
)
from featuregen.overlay.upload.upload_catalog import (
    ensure_upload_catalog_adapter,
    table_ref,
)


# ── a SECOND connection: the gold fixture is seeded here and rolled back by the harness ──
@pytest.fixture
def planning_conn(_dsn):
    connection = psycopg.connect(_dsn)
    try:
        yield connection
        connection.rollback()
    finally:
        connection.close()


def _adapter():
    ensure_upload_catalog_adapter()
    return current_catalog_adapter()


def _noop_seed(conn, **_kw):
    """A reject-all seeder: no topology, so every positive fails to resolve (proves non-vacuity)."""
    ensure_upload_catalog_adapter()


# ── (1) the gate passes on the correct implementation over the clean population ──
def test_gate_passes_on_correct_implementation(db, planning_conn, service_actor, human_actor):
    result = evaluate_assembly_gate(
        planning_conn, db, _adapter(), service_actor=service_actor, human_actor=human_actor,
        now=GOLD_NOW, run_ids=("mgate_a", "mgate_b"))

    assert isinstance(result, AssemblyGateResultV1)
    assert result.passed, result.failures

    # criterion (1): ≥6 distinct authoritative shapes resolved in EVERY run
    assert result.positive_coverage_ok
    assert len(result.positive_shapes_covered) >= MULTISOURCE_GOLD_MIN_SHAPES
    assert set(result.positive_shapes_covered) == {
        "identity_single_measure", "ratio_take_latest_denominator", "difference", "trend",
        "count_distinct", "composite_grain_landing"}

    # criteria (2)-(8): each holds
    assert result.outcomes_match_expected           # every case hit its EXACT expected disposition
    assert result.operand_preservation_ok           # zero operand substitution/loss (incl. ordered)
    assert result.governed_endpoints_ok             # zero non-governed endpoints in a resolve
    assert result.one_grain_landing_ok              # every operand lands at ONE physical grain
    assert result.aggregation_temporal_ok           # per-path aggregation/temporal preserved
    assert result.deterministic_identity_ok         # identical plan/contract identity across runs
    assert result.reconciliation_complete           # complete manifest reconciliation both runs
    assert result.no_technical_or_truncation        # NO technical/truncation in the clean population
    assert result.failures == ()
    # resolution RATE is descriptive only (negatives resolve to a reject by design)
    assert 0.0 < result.resolution_rate < 1.0


# ── (2) NOT vacuous — a reject-all assembler fails positive coverage ──
def test_gate_reject_all_fails_positive_coverage(db, planning_conn, service_actor, human_actor):
    result = evaluate_assembly_gate(
        planning_conn, db, _adapter(), service_actor=service_actor, human_actor=human_actor,
        now=GOLD_NOW, run_ids=("mgate_rej_a", "mgate_rej_b"), seed_fn=_noop_seed)

    assert not result.passed                        # the gate must NOT pass a reject-everything run
    assert not result.positive_coverage_ok          # mandatory positive coverage is absent
    assert len(result.positive_shapes_covered) < MULTISOURCE_GOLD_MIN_SHAPES


# ── (3) NOT vacuous — a fault reading LEAKED into the clean population fails criterion (8) ──
def test_gate_fails_when_fault_control_leaks_into_clean_population(db):
    # fabricate a minimal 2-run telemetry where the clean population contains a technical_failure
    versions = {"multisource_assembly": "x", "operation_policy": "y"}
    case = GoldCaseV1(case_id="c0", is_positive=False,
                      intent=CORRECTNESS_GOLD[0].intent,          # intent unused by the pure evaluator
                      expected_outcome=SemanticOutcome.not_evaluated)
    for rid in ("leak_a", "leak_b"):
        write_manifest(db, ManifestRecordV1(
            run_id=rid, expected_intent_ids=("c0",), versions=versions, shadow_flag=True,
            producer_commit="gold-test", created_at=GOLD_NOW))
        write_intent_result(db, IntentResultRowV1(
            run_id=rid, intent_id="c0", semantic_outcome=SemanticOutcome.not_evaluated,
            compile_completeness=CompileCompleteness.not_applicable,
            technical_status=TechnicalStatus.technical_failure,   # a fault reading in the clean pop
            capture_status=CaptureStatus.persisted, normalized_intent_hash="nih_x",
            selected_plan_id=None, reason_codes=("technical_failure",), created_at=GOLD_NOW), [], [])

    result = evaluate_gate_over_runs(db, run_ids=("leak_a", "leak_b"), cases=(case,))

    assert not result.no_technical_or_truncation    # the leak is caught on criterion (8)
    assert not result.passed
    assert any("technical/truncation" in f for f in result.failures)


# ── (4) the fault-observability controls are EXACTLY classified + excluded from the clean population ──
def _seed_main(conn, service_actor, human_actor):
    """Just the MAIN resolving topology (cb.txn -> bridge -> wl.acc -> wl.cust) so the budget control's
    first intent actually compiles (consuming the run budget) before the second is truncated."""
    ensure_upload_catalog_adapter()

    def _seed(source, rows):
        build_graph(conn, source, [r for r, _ in rows],
                    concepts={content_hash(r): c for r, c in rows})

    def _grain(source, table, cols):
        from tests.featuregen.overlay.upload.conftest import _confirm_grain
        ref = table_ref(source, table)
        res = propose_fact(conn, Command("propose_fact", "overlay_fact", None,
            {"ref": ref, "fact_type": "grain", "proposed_value": {"columns": cols, "is_unique": True}},
            service_actor, f"m-{source}-{table}"))
        assert res.accepted, res.denied_reason
        _confirm_grain(conn, source, table, cols, actor=human_actor)

    _seed("cb", [
        (CanonicalRow("cb", "txn", "transaction_id", "integer", is_grain=True), "transaction_id"),
        (CanonicalRow("cb", "txn", "account_id", "integer"), "account_id"),
        (CanonicalRow("cb", "txn", "amount", "numeric"), "monetary_flow")])
    _seed("wl", [
        (CanonicalRow("wl", "acc", "account_id", "integer", is_grain=True), "account_id"),
        (CanonicalRow("wl", "acc", "customer_id", "integer", joins_to="cust.customer_id",
                      cardinality="N:1"), "customer_id"),
        (CanonicalRow("wl", "cust", "customer_id", "integer", is_grain=True), "customer_id")])
    conn.execute(
        "INSERT INTO entity_bridge_edge (fact_key, entity_id, left_catalog_source, left_object_ref, "
        "right_catalog_source, right_object_ref, confirmed_event_id, status) "
        "VALUES ('gbfk_main','account','cb','public.txn.account_id','wl','public.acc.account_id',"
        "'evt-main','VERIFIED')")
    _grain("cb", "txn", ["transaction_id"])
    _grain("wl", "acc", ["account_id"])
    _grain("wl", "cust", ["customer_id"])
    for src in ("cb", "wl"):
        conn.execute(
            "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, "
            "head_seq) VALUES (%s,%s,'m',0) ON CONFLICT (catalog_source) DO UPDATE SET "
            "last_completed_at = EXCLUDED.last_completed_at", (src, GOLD_NOW - timedelta(minutes=5)))


def test_fault_control_injected_db_error_is_technical_failure(
        db, planning_conn, service_actor, human_actor, monkeypatch):
    """An injected DB error inside plan_multi_source is caught by the per-intent savepoint and EXACTLY
    classified ``technical_failure`` — never a semantic disposition — under its OWN run id."""
    ctrl = next(c for c in FAULT_CONTROLS if c.injection == "db_error")

    def _boom(*_a, **_k):
        raise psycopg.errors.UndefinedTable("injected fault")

    monkeypatch.setattr(multisource_shadow, "plan_multi_source", _boom)
    run_multisource_assembly_shadow(
        planning_conn=planning_conn, telemetry_conn=db, adapter=_adapter(),
        intents={ctrl.control_id: ctrl.intent}, run_id="fault_db", roles=("feature_engineer",),
        now=GOLD_NOW)

    rows = {r["intent_id"]: r for r in read_intent_results(db, "fault_db")}
    assert rows[ctrl.control_id]["technical_status"] == TechnicalStatus.technical_failure.value
    assert rows[ctrl.control_id]["semantic_outcome"] == SemanticOutcome.not_evaluated.value
    # run id is NOT one of the clean gate run ids — the control is excluded from the clean population
    assert "fault_db" not in ("mgate_a", "mgate_b")


def test_fault_control_budget_truncated_is_exactly_classified(
        db, planning_conn, service_actor, human_actor, monkeypatch):
    """With the per-run compile allowance pinned to 1, the first intent consumes it (a real compile) and
    the second is EXACTLY classified ``budget_truncated`` — a capture-incomplete reading, never a
    silent drop — under its OWN run id."""
    _seed_main(planning_conn, service_actor, human_actor)
    monkeypatch.setattr(multisource_shadow, "MAX_MULTISOURCE_COMPILES_PER_RUN", 1)
    ctrl = next(c for c in FAULT_CONTROLS if c.injection == "budget_truncation")

    run_multisource_assembly_shadow(
        planning_conn=planning_conn, telemetry_conn=db, adapter=_adapter(),
        intents={"a_first": ctrl.intent, "b_second": ctrl.intent}, run_id="fault_budget",
        roles=("feature_engineer",), now=GOLD_NOW, monotonic=lambda: 0.0)

    rows = {r["intent_id"]: r for r in read_intent_results(db, "fault_budget")}
    assert rows["a_first"]["semantic_outcome"] == SemanticOutcome.resolved.value      # compiled first
    assert rows["b_second"]["technical_status"] == TechnicalStatus.budget_truncated.value
    assert rows["b_second"]["semantic_outcome"] == SemanticOutcome.not_evaluated.value
