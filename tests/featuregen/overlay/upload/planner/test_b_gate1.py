"""Phase 3C.2b-i-B · Task 10 — Gate 1 (component qualification) over ``govern_llm_idea`` (T9).

DB-backed. All authority is seeded through the REAL governance commands (the proven T9/spike chain);
NEVER a direct concept/grain/bridge table insert. The suite proves:

  1. **Gate passes** — ``run_b_gate1`` returns a PASS report (every criterion True) over the immutable
     partitioned gold: two distinct positive shapes two-axis-govern, every negative rejects with its
     exact ``BDisposition``, determinism holds, zero false resolves, no fault leak, and the fault
     controls classify exactly.
  2. **Exact per-case dispositions** — driving each clean case's RAW proposal through the REAL
     ``govern_llm_idea`` yields exactly the authored ``BDisposition`` (positives a ``GovernedResult``,
     negatives never one) — independent, per-case evidence the gate is not merely self-consistent.
  3. **Fault controls** — the injected DB error classifies ``technical_failure`` (and the outer
     transaction survives the contained savepoint), the spent budget classifies ``budget_truncated``.
  4. **Non-vacuity** — a reject-all ``govern_llm_idea`` collapses positive coverage, failing the gate
     (mirrors A's ``test_gate_reject_all_fails_positive_coverage``).
  5. **Fault-leak guard** — a technical/truncation reading in the clean population fails the gate, and
     the fault-control partition is disjoint from the clean population by construction.
"""
from __future__ import annotations

import pytest

from featuregen.contracts import DbConn
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.catalog import current_catalog_adapter
from featuregen.overlay.upload.planner.b_dispositions import BDisposition
from featuregen.overlay.upload.planner.b_gate1 import (
    evaluate_b_gate1,
    run_b_gate1,
    run_fault_controls,
)
from featuregen.overlay.upload.planner.b_gate1_gold import (
    CORRECTNESS_GOLD,
    FAULT_CONTROLS,
    FRESH_WITHIN,
    GOLD_NOW,
    RUN_ID,
    seed_correctness_gold,
)
from featuregen.overlay.upload.planner.b_service import (
    FEATUREGEN_LLM_XCAT_SHADOW,
    GovernedResult,
    govern_llm_idea,
)
from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter


def _feature_engineer() -> IdentityEnvelope:
    return IdentityEnvelope(subject="fe", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("feature_engineer",))


def _adapter():
    ensure_upload_catalog_adapter()
    return current_catalog_adapter()


# ── 1) the gate passes over the full gold ────────────────────────────────────────────────────────
def test_gate_passes(db: DbConn, service_actor: IdentityEnvelope, human_actor: IdentityEnvelope,
                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FEATUREGEN_LLM_XCAT_SHADOW, "1")
    report = run_b_gate1(db, _adapter(), service_actor=service_actor, human_actor=human_actor)

    assert report.passed, report.failures
    assert report.positive_coverage_ok
    assert report.outcomes_match_expected
    assert report.operand_operation_preservation_ok
    assert report.zero_false_resolves
    assert report.deterministic_ok
    assert report.no_fault_leak
    assert report.fault_controls_ok
    # both authored positive shapes two-axis-governed (non-vacuity is REAL, not a floor accident).
    assert set(report.positive_shapes_covered) == {
        "identity_single_measure", "composite_grain_landing"}


# ── 2) exact per-case dispositions through the REAL govern_llm_idea ───────────────────────────────
def test_exact_dispositions_per_case(
        db: DbConn, service_actor: IdentityEnvelope, human_actor: IdentityEnvelope,
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FEATUREGEN_LLM_XCAT_SHADOW, "1")
    seed_correctness_gold(db, service_actor=service_actor, human_actor=human_actor)
    adapter = _adapter()
    actor = _feature_engineer()

    for case in CORRECTNESS_GOLD:
        res = govern_llm_idea(db, adapter, actor=actor, proposal=case.proposal,
                              generation_run_id=RUN_ID, now=GOLD_NOW, fresh_within=FRESH_WITHIN)
        if case.is_positive:
            assert isinstance(res, GovernedResult), (case.case_id, res)
            assert res.disposition is BDisposition.governed, case.case_id
            assert res.planning_result.selected_plan_id is not None, case.case_id
            assert res.planning_result.selected_contract_id is not None, case.case_id
            op = res.intent.operands[0]
            assert tuple(op.source_binding.source_grain_key_refs) == case.expected_grain_key_refs, (
                case.case_id, op.source_binding.source_grain_key_refs)
        else:
            assert not isinstance(res, GovernedResult), (case.case_id, "leaked a GovernedResult")
            assert res is case.expected, (case.case_id, res)


# ── 3) fault controls classify exactly + the savepoint contains the DB error ─────────────────────
def test_fault_controls_classified(
        db: DbConn, service_actor: IdentityEnvelope, human_actor: IdentityEnvelope,
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FEATUREGEN_LLM_XCAT_SHADOW, "1")
    seed_correctness_gold(db, service_actor=service_actor, human_actor=human_actor)
    adapter = _adapter()

    fault_runs = run_fault_controls(db, adapter, actor=_feature_engineer(), now=GOLD_NOW,
                                    fresh_within=FRESH_WITHIN)
    by_id = {ctrl.control_id: outcome for ctrl, outcome in fault_runs}
    assert by_id["fault_injected_db_error"][0] is BDisposition.technical_failure
    assert by_id["fault_budget_truncated"][0] is BDisposition.budget_truncated

    # the DB-error fault was CONTAINED by the T9 savepoint: the outer transaction is still usable.
    row = db.execute("SELECT 1").fetchone()
    assert row is not None and row[0] == 1


# ── 4) non-vacuity: a reject-all govern_llm_idea collapses positive coverage ──────────────────────
def test_non_vacuity_reject_all_fails_coverage(
        db: DbConn, service_actor: IdentityEnvelope, human_actor: IdentityEnvelope,
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FEATUREGEN_LLM_XCAT_SHADOW, "1")

    def _reject_all(conn, adapter, **kwargs):
        # a fixed reject for EVERY proposal — the reject-all implementation the gate must catch.
        return BDisposition.structural_need_ungoverned

    report = run_b_gate1(db, _adapter(), service_actor=service_actor, human_actor=human_actor,
                         govern_fn=_reject_all)
    assert not report.positive_coverage_ok
    assert report.positive_shapes_covered == ()
    assert not report.passed


# ── 5) fault-leak guard: a technical/truncation reading in the clean population fails the gate ────
def test_fault_leak_fails_gate() -> None:
    # the fault-control partition is DISJOINT from the clean population by construction.
    assert {c.control_id for c in FAULT_CONTROLS}.isdisjoint({c.case_id for c in CORRECTNESS_GOLD})

    # a clean case reading technical_failure (a fault disposition) must fail the gate — pure evaluator,
    # so the poke needs no DB. Every other case is set to its authored expectation.
    case_runs: dict[str, tuple[tuple[object | None, str | None], tuple[object | None, str | None]]] = {}
    poisoned = next(c for c in CORRECTNESS_GOLD if not c.is_positive)
    for case in CORRECTNESS_GOLD:
        disp = BDisposition.technical_failure if case.case_id == poisoned.case_id else case.expected
        case_runs[case.case_id] = ((disp, None), (disp, None))

    report = evaluate_b_gate1(case_runs, [], cases=CORRECTNESS_GOLD)
    assert not report.no_fault_leak
    assert not report.passed
