"""Phase-3B.4 — map a shadow planner result to the durable store rows.

Keeps ``shadow.py`` the orchestrator: this module owns the (result -> RunResultRowV1 + observations)
mapping, the TOTAL ``PlanResolutionStatus -> PlannerOutcome`` map (an unmapped status would hit the DB
CHECK and become silent loss), the ``compile_status`` computation (relative to PATH-RESOLVED
candidates), and the calls into the D3 replay fingerprints. Pure over its inputs (no DB).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from typing import Any

from featuregen.overlay.upload.planner.contracts import (
    APPLICABILITY_MAPPING_VERSION,
    CONCEPT_REGISTRY_VERSION,
    PLAN_CONTRACT_VERSION,
    PLANNER_VERSION,
    ROLE_RESOLUTION_VERSION,
    BindingPlanningResultV1,
    BindingPlanV1,
    ContractResolutionStatus,
    PathResolutionStatus,
    PlanResolutionStatus,
    ReasonCode,
)
from featuregen.overlay.upload.planner.fingerprint import (
    contract_input_hash,
    declarations_output_hash,
    planner_input_hash,
)
from featuregen.overlay.upload.planner.shadow_store import (
    CaptureStatus,
    CompileStatus,
    DispatchRecordV1,
    IncompleteReason,
    PlannerOutcome,
    PlanObservationRowV1,
    RunResultRowV1,
)

# A build-time producer identity would come from the deployment; a static placeholder is fine for
# shadow telemetry (D8's artifact-integrity gate is where a real commit is required).
PRODUCER_COMMIT = "dev"
INVOCATION_PREDICATE = "entity_scoped_no_catalog"

_COMPILER_VERSIONS = {
    "planner": PLANNER_VERSION, "plan_contract": PLAN_CONTRACT_VERSION,
    "applicability": APPLICABILITY_MAPPING_VERSION, "concept_registry": CONCEPT_REGISTRY_VERSION,
    "role_resolution": ROLE_RESOLUTION_VERSION,
}

# TOTAL over PlanResolutionStatus (a planned recipe -> 'compiled'; only not_applicable/internal_error
# map elsewhere). not_applicable is disambiguated by whether the scope was empty (below).
_PLANNED = frozenset({
    PlanResolutionStatus.resolved, PlanResolutionStatus.resolved_with_ambiguity,
    PlanResolutionStatus.partially_resolved, PlanResolutionStatus.unresolved,
    PlanResolutionStatus.bounded_out, PlanResolutionStatus.safety_rejected,
})


def _jsonencode(o):
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return dataclasses.asdict(o)   # recurses into nested dataclasses; leaves enums for the str fallback
    return str(o)


def _jsonable(obj) -> Any:
    """A JSON-safe dict/list (dataclasses -> dicts, enums -> str) for a jsonb payload."""
    return json.loads(json.dumps(obj, sort_keys=True, default=_jsonencode))


def build_dispatch(*, run_id: str | None, eligible_recipe_ids: frozenset[str], compile_flag: bool,
                   telemetry_flag: bool, now) -> DispatchRecordV1:
    eligible = tuple(sorted(eligible_recipe_ids))
    recipe_hash = hashlib.sha256("|".join(eligible).encode()).hexdigest()
    return DispatchRecordV1(
        generation_run_id=run_id, eligible_recipe_ids=eligible, recipe_hash=recipe_hash,
        expected_count=len(eligible), invocation_predicate=INVOCATION_PREDICATE,
        compile_flag=compile_flag, telemetry_flag=telemetry_flag,
        applicability_version=APPLICABILITY_MAPPING_VERSION, producer_commit=PRODUCER_COMMIT,
        compiler_versions=_COMPILER_VERSIONS, created_at=now)


def _planner_outcome(result: BindingPlanningResultV1) -> PlannerOutcome:
    st = result.result_status
    if st is PlanResolutionStatus.internal_error:
        return PlannerOutcome.internal_error
    if st is PlanResolutionStatus.not_applicable:
        # empty authorized scope (no_authorized_catalog reason) vs a catalog that yielded no candidate
        if result.primary_reason_code is ReasonCode.no_authorized_catalog:
            return PlannerOutcome.no_authorized_catalog
        return PlannerOutcome.no_physical_plan
    if st in _PLANNED:
        return PlannerOutcome.compiled
    return PlannerOutcome.no_physical_plan   # defensive total fallback


def is_identity_comparable(compile_status: CompileStatus) -> bool:
    """A run whose compile pass was TRUNCATED by the operational budget (``incomplete``) must be
    EXCLUDED from deterministic double-compile verdict comparisons (D6/F17): the set of compiled
    contracts then depends on wall-time/ordering, not the inputs alone. complete / not_applicable /
    compile_disabled are all determined by the inputs, so they ARE comparable."""
    return compile_status is not CompileStatus.incomplete


def _compile_axes(result: BindingPlanningResultV1, compile_contracts: bool,
                  budget_stopped_by_time: bool | None
                  ) -> tuple[CompileStatus, IncompleteReason | None, int, int, int]:
    eligible = [p for p in result.candidate_plans
                if p.path_resolution_status is PathResolutionStatus.source_to_target_resolved]
    path_resolved_eligible = len(eligible)
    compiled = sum(1 for p in eligible
                   if p.contract_resolution_status is not ContractResolutionStatus.not_compiled)
    skipped = path_resolved_eligible - compiled
    if path_resolved_eligible == 0:
        return CompileStatus.not_applicable, None, 0, 0, 0
    if not compile_contracts:
        return CompileStatus.compile_disabled, None, path_resolved_eligible, 0, path_resolved_eligible
    if compiled == path_resolved_eligible:
        return CompileStatus.complete, None, path_resolved_eligible, compiled, skipped
    # some eligible candidate did not compile under an active compile pass -> operationally incomplete.
    # D6/F17: the budget records which bound truncated the run (budget_time when the elapsed-time
    # deadline fired, else the plan-count bound).
    reason = IncompleteReason.budget_time if budget_stopped_by_time else IncompleteReason.budget_count
    return CompileStatus.incomplete, reason, path_resolved_eligible, compiled, skipped


def _declarations_json(plan: BindingPlanV1) -> dict:
    return {
        "hop_aggregations": _jsonable(plan.hop_aggregations),
        "temporal_declaration": _jsonable(plan.temporal_declaration)
        if plan.temporal_declaration is not None else None,
        "physical_read_set": _jsonable(plan.physical_read_set)
        if plan.physical_read_set is not None else None,
    }


def _replay_stamp_json(plan: BindingPlanV1) -> dict | None:
    env = plan.audit_envelope
    if env is None:
        return None
    return {"catalog_state_stamps": _jsonable(env.catalog_state_stamps),
            "stamp_consistency": str(env.stamp_consistency),
            "replay_strength": str(env.replay_strength)}


def _observation(plan: BindingPlanV1, *, run_id: str | None, recipe_id: str, ctx, template, now
                 ) -> PlanObservationRowV1:
    is_compiled = (plan.path_resolution_status is PathResolutionStatus.source_to_target_resolved
                   and plan.contract_resolution_status is not ContractResolutionStatus.not_compiled)
    return PlanObservationRowV1(
        generation_run_id=run_id, recipe_id=recipe_id, physical_plan_id=plan.physical_plan_id,
        path_resolution_status=str(plan.path_resolution_status), is_compiled=is_compiled,
        contract_id=plan.contract_id if is_compiled else None,
        contract_input_hash=contract_input_hash(ctx, plan, template) if is_compiled else None,
        contract_resolution_status=str(plan.contract_resolution_status) if is_compiled else None,
        declaration_status=str(plan.declaration_status) if is_compiled else None,
        contract_primary_reason_code=str(plan.contract_primary_reason_code)
        if is_compiled and plan.contract_primary_reason_code is not None else None,
        contract_reason_codes=tuple(str(r) for r in plan.contract_reason_codes) if is_compiled else (),
        bridge_count=plan.bridge_count, tier=str(plan.tier), preference_rank=plan.preference_rank,
        declarations=_declarations_json(plan) if is_compiled else None,
        declarations_output_hash=declarations_output_hash(plan) if is_compiled else None,
        replay_stamp=_replay_stamp_json(plan) if is_compiled else None, created_at=now)


def map_result(result: BindingPlanningResultV1, *, template, scope, compile_ctx, compile_contracts,
               now, budget_stopped_by_time: bool | None = None
               ) -> tuple[RunResultRowV1, list[PlanObservationRowV1]]:
    """Map one recipe's planner result to its store rows. ``compile_ctx`` is the batched context (needed
    for the fingerprints); None when not compiling. ``budget_stopped_by_time`` (from the run's shared
    budget) labels an incomplete run's cause as budget_time vs budget_count (D6/F17)."""
    compile_status, incomplete_reason, eligible, compiled, skipped = _compile_axes(
        result, compile_contracts, budget_stopped_by_time)
    p_hash = planner_input_hash(compile_ctx, template, scope) if compile_ctx is not None else None
    run_row = RunResultRowV1(
        generation_run_id=result.run_id, recipe_id=result.recipe_id,
        catalog_scope_id=result.catalog_scope_id, planner_input_hash=p_hash,
        planner_outcome=_planner_outcome(result), compile_status=compile_status,
        incomplete_reason=incomplete_reason, path_resolved_eligible=eligible, compiled_count=compiled,
        skipped_count=skipped, capture_status=CaptureStatus.persisted,  # vestigial — the writer's `capture` arg drives the stored value
        selected_contract_physical_plan_id=result.selected_contract_physical_plan_id,
        selected_contract_id=result.selected_contract_id,
        contract_result_status=str(result.contract_result_status)
        if result.contract_result_status is not None else None,
        bounding=_jsonable(result.bounding), created_at=now)
    observations = [_observation(p, run_id=result.run_id, recipe_id=result.recipe_id,
                                 ctx=compile_ctx, template=template, now=now)
                    for p in result.candidate_plans]
    return run_row, observations


def preloop_failure_row(*, run_id: str | None, recipe_id: str, now) -> RunResultRowV1:
    return RunResultRowV1(
        generation_run_id=run_id, recipe_id=recipe_id, catalog_scope_id=None, planner_input_hash=None,
        planner_outcome=PlannerOutcome.preloop_failure, compile_status=CompileStatus.not_applicable,
        incomplete_reason=None, path_resolved_eligible=0, compiled_count=0, skipped_count=0,
        capture_status=CaptureStatus.persisted, selected_contract_physical_plan_id=None, selected_contract_id=None,
        contract_result_status=None, bounding={}, created_at=now)


def template_not_found_row(*, run_id: str | None, recipe_id: str, now) -> RunResultRowV1:
    return RunResultRowV1(
        generation_run_id=run_id, recipe_id=recipe_id, catalog_scope_id=None, planner_input_hash=None,
        planner_outcome=PlannerOutcome.template_not_found, compile_status=CompileStatus.not_applicable,
        incomplete_reason=None, path_resolved_eligible=0, compiled_count=0, skipped_count=0,
        capture_status=CaptureStatus.persisted, selected_contract_physical_plan_id=None, selected_contract_id=None,
        contract_result_status=None, bounding={}, created_at=now)
