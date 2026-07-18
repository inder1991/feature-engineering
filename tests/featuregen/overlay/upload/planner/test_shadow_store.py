from __future__ import annotations

from datetime import UTC, datetime

import pytest

from featuregen.overlay.upload.planner import shadow_store as ss
from featuregen.overlay.upload.planner.shadow_store import (
    CaptureStatus,
    CompileStatus,
    DispatchRecordV1,
    DivergentDuplicateError,
    PlannerOutcome,
    PlanObservationRowV1,
    RunResultRowV1,
)

_NOW = datetime(2026, 7, 17, tzinfo=UTC)


def _dispatch(run_id="grun_1", recipe_ids=("r1", "r2"), recipe_hash="rh") -> DispatchRecordV1:
    return DispatchRecordV1(
        generation_run_id=run_id, eligible_recipe_ids=tuple(recipe_ids), recipe_hash=recipe_hash,
        expected_count=len(recipe_ids), invocation_predicate="entity_scoped", compile_flag=True,
        telemetry_flag=True, scoped_applicability_flag=False, ranking_flag=False,
        applicability_version="1.0.0", producer_commit="abc",
        compiler_versions={"planner": "1.0.0"}, created_at=_NOW)


def _run_result(recipe_id="r1") -> RunResultRowV1:
    return RunResultRowV1(
        generation_run_id="grun_1", recipe_id=recipe_id, catalog_scope_id="cs_1",
        planner_input_hash="pih", planner_outcome=PlannerOutcome.compiled,
        compile_status=CompileStatus.complete, incomplete_reason=None, path_resolved_eligible=1,
        compiled_count=1, skipped_count=0, capture_status=CaptureStatus.persisted,
        selected_contract_physical_plan_id="bp_1", selected_contract_id="cc_1",
        contract_result_status="resolved", bounding={"plans_truncated": False}, created_at=_NOW)


def _observation(pid="bp_1") -> PlanObservationRowV1:
    return PlanObservationRowV1(
        generation_run_id="grun_1", recipe_id="r1", physical_plan_id=pid,
        path_resolution_status="source_to_target_resolved", is_compiled=True, contract_id="cc_1",
        contract_input_hash="cih", contract_resolution_status="resolved", declaration_status="resolved",
        contract_primary_reason_code=None, contract_reason_codes=(), bridge_count=1,
        tier="tier_2_one_bridge", preference_rank=0, declarations={"k": 1},
        declarations_output_hash="oh", replay_stamp={"s": 1}, created_at=_NOW)


def test_dispatch_roundtrip(db) -> None:
    ss.write_dispatch(db, _dispatch())
    rec = ss.reconcile(db, "grun_1")
    assert rec.expected == 2 and rec.present == 0 and rec.missing_recipe_ids == ("r1", "r2")


def test_dispatch_idempotent(db) -> None:
    ss.write_dispatch(db, _dispatch())
    ss.write_dispatch(db, _dispatch())   # no error, single row
    n = db.execute("SELECT count(*) FROM planner_shadow_dispatch WHERE generation_run_id='grun_1'").fetchone()[0]
    assert n == 1


def test_dispatch_divergent_duplicate_conflicts(db) -> None:
    ss.write_dispatch(db, _dispatch())
    with pytest.raises(DivergentDuplicateError):
        ss.write_dispatch(db, _dispatch(recipe_hash="DIFFERENT"))


def test_run_and_plans_roundtrip_persisted(db) -> None:
    ss.write_dispatch(db, _dispatch())
    status = ss.write_run_and_plans(db, _run_result(), [_observation()])
    assert status is CaptureStatus.persisted
    rows = ss.read_run_results(db, "grun_1")
    assert len(rows) == 1 and rows[0]["capture_status"] == "persisted"
    obs = ss.read_observations(db, "grun_1")
    assert len(obs) == 1 and obs[0]["is_compiled"] is True
    assert ss.reconcile(db, "grun_1").missing_recipe_ids == ("r2",)


def test_two_phase_fallback_on_child_failure(db, monkeypatch) -> None:
    ss.write_dispatch(db, _dispatch())

    def _boom(_conn, _o):
        raise RuntimeError("child insert failed")

    monkeypatch.setattr(ss, "_insert_observation", _boom)
    status = ss.write_run_and_plans(db, _run_result(), [_observation()])
    assert status is CaptureStatus.persistence_partial
    rows = ss.read_run_results(db, "grun_1")
    assert len(rows) == 1 and rows[0]["capture_status"] == "persistence_partial"
    assert ss.read_observations(db, "grun_1") == []   # the atomic write rolled the children back


def test_reconcile_detects_missing_recipe(db) -> None:
    ss.write_dispatch(db, _dispatch(recipe_ids=("r1", "r2", "r3")))
    ss.write_run_and_plans(db, _run_result("r1"), [])
    ss.write_run_and_plans(db, _run_result("r2"), [])
    rec = ss.reconcile(db, "grun_1")
    assert rec.expected == 3 and rec.present == 2 and rec.missing_recipe_ids == ("r3",)
    assert not rec.complete
