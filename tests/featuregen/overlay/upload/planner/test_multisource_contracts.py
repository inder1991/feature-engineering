"""Phase 3C.2b-i-A · Task 2 — the multi-source typed contracts.

Pure data module: frozen/slotted dataclasses + lowercase-snake StrEnums + the ONE constant
``PATH_AGG_TO_FUNCTION`` mapping A's ``PathAggregation`` onto the reused single-source
``AggregationFunction`` (or ``None`` for the not-yet-resolvable ``avg``/``stddev``). This test pins
the contract shape (spec §3), the aggregation mapping (spec §4), and the disposition vocabulary
(spec §9) — and the reuse-by-import invariants (``BindingPlanV1``/``PhysicalReadSetV1``, no bespoke
key fact, no ``recipe_id``, no per-plan selected id on the plan carrier)."""
from __future__ import annotations

import dataclasses

import pytest

from featuregen.overlay.upload.planner import multisource_contracts as m
from featuregen.overlay.upload.planner.contracts import (
    OPERATION_POLICY_VERSION,
    AdditivityClass,
    AggregationFunction,
    BindingPlanV1,
    BindingSafety,
    CandidateRole,
    ContractResolutionStatus,
    DeclarationStatus,
    PathResolutionStatus,
    PhysicalReadSetV1,
    PlanResolutionStatus,
    make_binding_plan,
)


def _minimal_binding_plan() -> BindingPlanV1:
    return make_binding_plan(
        recipe_id="op_num", target_entity="account", catalog_source="core",
        ingredient_bindings=(), path_segments=(),
        resolution_status=PlanResolutionStatus.resolved,
        path_resolution_status=PathResolutionStatus.source_to_target_resolved,
        primary_reason_code=None, reason_codes=(), safety=BindingSafety.safe,
        preference_rank=0, preference_reasons=(), candidate_role=CandidateRole.selected)


def _ratio_intent() -> m.MultiSourcePlannerIntentV1:
    """A RATIO intent: AVG(numerator) / take_latest(denominator), the denominator carrying the
    ordering_anchor_concept take_latest requires (spec §4/§11)."""
    num_strategy = m.PathStrategyV1(
        aggregation=m.PathAggregation.avg, output_type="numeric",
        output_additivity=AdditivityClass.additive, external_type_required=False,
        ordering_anchor_concept=None)
    den_strategy = m.PathStrategyV1(
        aggregation=m.PathAggregation.take_latest, output_type="numeric",
        output_additivity=AdditivityClass.semi_additive, external_type_required=False,
        ordering_anchor_concept="account_as_of")
    num_binding = m.GovernedSourceBindingV1(
        source_grain_entity="account", source_grain_key_refs=("core.balances.account_id",),
        grain_fact_key="gf_core_balances")
    den_binding = m.GovernedSourceBindingV1(
        source_grain_entity="account",
        source_grain_key_refs=("risk.limits.account_id", "risk.limits.as_of_date"),
        grain_fact_key="gf_risk_limits")
    num_slot = m.OperandSlotV1(
        slot_id="num", semantic_role=m.SemanticRole.numerator, catalog_source="core",
        object_ref="core.balances.balance", authoritative_concept="monetary_stock",
        path_strategy=num_strategy, source_binding=num_binding)
    den_slot = m.OperandSlotV1(
        slot_id="den", semantic_role=m.SemanticRole.denominator, catalog_source="risk",
        object_ref="risk.limits.credit_limit", authoritative_concept="monetary_stock",
        path_strategy=den_strategy, source_binding=den_binding)
    final = m.FinalExpressionV1(
        operation=m.FinalOperation.ratio, ordered_slot_ids=("num", "den"),
        time_slot_id=None, window=None, output_additivity=AdditivityClass.non_additive)
    return m.MultiSourcePlannerIntentV1(
        target_entity="account", operands=(num_slot, den_slot),
        final_expression=final, operation_policy_version=OPERATION_POLICY_VERSION)


def test_ratio_intent_constructs_and_is_frozen_slotted():
    intent = _ratio_intent()
    assert intent.final_expression.operation is m.FinalOperation.ratio
    den = intent.operands[1]
    assert den.path_strategy.aggregation is m.PathAggregation.take_latest
    assert den.path_strategy.ordering_anchor_concept == "account_as_of"
    # frozen
    with pytest.raises(dataclasses.FrozenInstanceError):
        intent.target_entity = "other"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        den.path_strategy.aggregation = m.PathAggregation.sum  # type: ignore[misc]
    # slotted — no per-instance __dict__ (so no stray attributes can be attached)
    assert not hasattr(intent, "__dict__")
    assert not hasattr(intent.operands[0], "__dict__")


def test_physical_landing_composite_grain_keys():
    landing = m.PhysicalLandingV1(
        catalog="core", table_ref="core.positions",
        grain_key_refs=("core.positions.account_id", "core.positions.as_of_date"))
    assert len(landing.grain_key_refs) == 2
    assert not hasattr(landing, "__dict__")


def test_path_agg_to_function_mapping():
    F = AggregationFunction
    A = m.PathAggregation
    assert m.PATH_AGG_TO_FUNCTION == {
        A.sum: F.sum, A.min: F.min, A.max: F.max, A.take_latest: F.take_latest,
        A.count: F.count, A.count_distinct: F.count, A.avg: None, A.stddev: None,
    }
    # the load-bearing identities from the brief
    assert m.PATH_AGG_TO_FUNCTION[A.sum] is F.sum
    assert m.PATH_AGG_TO_FUNCTION[A.stddev] is None
    assert m.PATH_AGG_TO_FUNCTION[A.avg] is None
    # count_distinct maps to the ORDER-SAFE count, never a distinct member (spec §4)
    assert m.PATH_AGG_TO_FUNCTION[A.count_distinct] is F.count
    # total over PathAggregation — every member has an entry
    assert set(m.PATH_AGG_TO_FUNCTION) == set(A)


def test_enums_lowercase_snake():
    for enum in (m.SemanticRole, m.PathAggregation, m.FinalOperation, m.MultiSourceReason):
        for member in enum:
            assert member.value == member.value.lower()
            assert " " not in member.value
    assert {r.value for r in m.SemanticRole} == {
        "measure", "counted", "time", "numerator", "denominator", "minuend", "subtrahend"}
    assert {a.value for a in m.PathAggregation} == {
        "sum", "min", "max", "take_latest", "count", "count_distinct", "avg", "stddev"}
    assert {o.value for o in m.FinalOperation} == {
        "identity", "count", "count_distinct", "recency", "trend", "ratio", "difference"}


def test_multisource_reason_enumerates_every_disposition():
    expected = {
        "resolved",
        # semantic (§9)
        "operand_shape_invalid", "unsupported_path_aggregation", "ordering_anchor_missing",
        "no_governed_path", "realization_endpoint_ungoverned", "no_common_physical_grain",
        "ambiguous_physical_grain", "aggregation_unsafe_on_path", "temporal_paths_incompatible",
        "source_binding_ungoverned",
        # technical (§9)
        "operand_or_slot_not_preserved", "technical_failure",
        # capture-incomplete (§9)
        "budget_truncated",
    }
    assert {r.value for r in m.MultiSourceReason} == expected


def test_governed_source_binding_has_no_key_fact():
    names = {f.name for f in dataclasses.fields(m.GovernedSourceBindingV1)}
    assert names == {"source_grain_entity", "source_grain_key_refs", "grain_fact_key"}
    # explicitly: no bespoke per-event key fact
    assert "key_fact_event_id" not in names
    assert not any("key_fact" in n and n != "grain_fact_key" for n in names)


def test_path_strategy_carries_anchor_and_external_type_flag():
    names = {f.name for f in dataclasses.fields(m.PathStrategyV1)}
    assert "ordering_anchor_concept" in names
    assert "external_type_required" in names


def test_governed_endpoint_shape():
    ep = m.GovernedEndpointV1(
        catalog="core", table_ref="core.positions",
        grain_key_refs=("core.positions.account_id",), grain_fact_key="gf_core_positions")
    assert ep.grain_fact_key == "gf_core_positions"
    names = {f.name for f in dataclasses.fields(m.GovernedEndpointV1)}
    assert names == {"catalog", "table_ref", "grain_key_refs", "grain_fact_key"}


def test_operand_path_reuses_binding_plan_and_read_set_by_import():
    bp = _minimal_binding_plan()
    ep = m.GovernedEndpointV1(
        catalog="core", table_ref="core.balances",
        grain_key_refs=("core.balances.account_id",), grain_fact_key="gf")
    strategy = m.PathStrategyV1(
        aggregation=m.PathAggregation.sum, output_type="numeric",
        output_additivity=AdditivityClass.additive, external_type_required=False,
        ordering_anchor_concept=None)
    op = m.OperandPathV1(
        slot_id="num", semantic_role=m.SemanticRole.numerator, catalog_source="core",
        object_ref="core.balances.balance", binding_plan=bp, governed_endpoints=(ep,),
        path_strategy=strategy, pit_treatment="as_of")
    assert isinstance(op.binding_plan, BindingPlanV1)
    assert op.binding_plan is bp


def test_multisource_binding_plan_carries_own_compile_result_not_selected_id():
    plan = m.MultiSourceBindingPlanV1(
        plan_id="ms_1",
        physical_landing=m.PhysicalLandingV1(
            catalog="core", table_ref="core.positions",
            grain_key_refs=("core.positions.account_id",)),
        operand_paths=(),
        final_expression=_ratio_intent().final_expression,
        physical_read_set=PhysicalReadSetV1(columns=()),
        resolution_status=m.MultiSourceReason.resolved,
        reason_codes=(),
        contract_result_status=ContractResolutionStatus.resolved,
        contract_id="cc_x",
        declaration_evidence=m.MultiSourceDeclarationEvidenceV1(
            per_path=(), final_verdict=DeclarationStatus.resolved),
        contract_input_hash="ih", contract_output_hash="oh")
    assert isinstance(plan.physical_read_set, PhysicalReadSetV1)
    assert plan.contract_result_status is ContractResolutionStatus.resolved
    names = {f.name for f in dataclasses.fields(m.MultiSourceBindingPlanV1)}
    # the plan carries its OWN compile result; NOT a selected id (that lives on the result)
    assert "selected_plan_id" not in names
    assert "selected_contract_plan_id" not in names


def test_multisource_planning_result_mirrors_binding_planning_result():
    names = {f.name for f in dataclasses.fields(m.MultiSourcePlanningResultV1)}
    for expected in ("run_id", "target_entity", "candidate_plans", "selected_plan_id",
                     "result_status", "primary_reason_code", "reason_codes", "bounding",
                     "replay_envelope", "contract_result_status", "selected_contract_plan_id",
                     "selected_contract_id"):
        assert expected in names


def test_replay_envelope_over_fact_keys_no_recipe_id_no_per_event_id():
    names = {f.name for f in dataclasses.fields(m.MultiSourceReplayEnvelopeV1)}
    assert "recipe_id" not in names
    # fingerprinted over deterministic fact_keys, never per-event ids
    assert "governed_endpoint_fact_keys" in names
    assert "bridge_fact_keys" in names
    assert not any("event_id" in n for n in names)


def test_bounding_metrics_include_landing_ambiguous():
    names = {f.name for f in dataclasses.fields(m.MultiSourceBoundingMetricsV1)}
    assert "landing_ambiguous" in names
    metrics = m.MultiSourceBoundingMetricsV1(
        paths_per_operand_truncated=False, operand_combinations_truncated=False,
        states_truncated=False, landing_ambiguous=True, total_states_expanded=3)
    assert metrics.landing_ambiguous is True
