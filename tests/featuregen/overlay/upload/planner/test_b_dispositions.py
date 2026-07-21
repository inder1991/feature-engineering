"""Phase 3C.2b-i-B · Task 0 — the B disposition enum + the A-outcome mapping.

Pins: (1) ``BDisposition`` is a lowercase-snake ``StrEnum`` with exactly the 18 named members;
(2) the three policy-version constants exist and are non-empty strings; (3) ``map_a_outcome``
honours the TWO-AXIS rule (assembly axis = ``MultiSourcePlanningResultV1.result_status``, contract
axis = ``.contract_result_status``) — ``governed`` is reachable ONLY when both axes are resolved AND
both selected ids are set; an assembly-resolved-but-contract-unresolved result NEVER reaches
``governed`` and instead maps to ``contract_unresolved``; every non-resolved
``MultiSourceReason`` (A's semantic/technical/capture-incomplete outcomes) maps to some
non-``governed`` ``BDisposition`` — i.e. the mapping is TOTAL and never raises."""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.planner import b_dispositions as b
from featuregen.overlay.upload.planner.contracts import ContractResolutionStatus
from featuregen.overlay.upload.planner.multisource_contracts import (
    MultiSourceBoundingMetricsV1,
    MultiSourcePlanningResultV1,
    MultiSourceReason,
    MultiSourceReplayEnvelopeV1,
)

_EXPECTED_MEMBERS = {
    "governed",
    "proposal_lossy",
    "gauntlet_rejected",
    "concept_authority_missing",
    "concept_authority_conflict",
    "concept_authority_stale",
    "concept_not_in_registry",
    "source_entity_ungoverned",
    "structural_need_ungoverned",
    "role_not_aggregatable",
    "operation_unrecognized",
    "operation_deferred",
    "operand_order_authority_missing",
    "contract_unresolved",
    "technical_failure",
    "budget_truncated",
    "unresolved_operand",
    "ambiguous_column_identity",
}


def _bounding() -> MultiSourceBoundingMetricsV1:
    return MultiSourceBoundingMetricsV1(
        paths_per_operand_truncated=False, operand_combinations_truncated=False,
        states_truncated=False, landing_ambiguous=False, total_states_expanded=1)


def _replay_envelope() -> MultiSourceReplayEnvelopeV1:
    return MultiSourceReplayEnvelopeV1(
        target_entity="account", operand_pins=(), source_grain_key_refs=(),
        governed_endpoint_fact_keys=(), bridge_fact_keys=(), input_hash="ih")


def _result(
    *,
    result_status: MultiSourceReason,
    contract_result_status: ContractResolutionStatus = ContractResolutionStatus.not_compiled,
    selected_plan_id: str | None = None,
    selected_contract_id: str | None = None,
    primary_reason_code: MultiSourceReason | None = None,
) -> MultiSourcePlanningResultV1:
    """A minimal, otherwise-empty ``MultiSourcePlanningResultV1`` — only the fields
    ``map_a_outcome`` actually inspects are varied per-test."""
    return MultiSourcePlanningResultV1(
        run_id="run_1", target_entity="account", candidate_plans=(),
        selected_plan_id=selected_plan_id, result_status=result_status,
        primary_reason_code=primary_reason_code, reason_codes=(),
        bounding=_bounding(), replay_envelope=_replay_envelope(),
        contract_result_status=contract_result_status,
        selected_contract_plan_id=None, selected_contract_id=selected_contract_id)


# ---------------------------------------------------------------------------
# Enum shape
# ---------------------------------------------------------------------------


def test_b_disposition_is_lowercase_snake_strenum():
    for member in b.BDisposition:
        assert isinstance(member, str)
        assert member.value == member.value.lower()
        assert " " not in member.value


def test_b_disposition_has_exactly_the_named_members():
    assert {m.value for m in b.BDisposition} == _EXPECTED_MEMBERS


# ---------------------------------------------------------------------------
# Policy version constants
# ---------------------------------------------------------------------------


def test_policy_version_constants_present_and_nonempty():
    assert isinstance(b.B_DISPOSITION_VERSION, str) and b.B_DISPOSITION_VERSION
    assert isinstance(b.ROLE_POLICY_VERSION, str) and b.ROLE_POLICY_VERSION
    assert isinstance(b.OPERATION_ALIAS_VERSION, str) and b.OPERATION_ALIAS_VERSION
    assert b.B_DISPOSITION_VERSION == "3c2bib.disp.1.0.0"
    assert b.ROLE_POLICY_VERSION == "3c2bib.role.1.0.0"
    assert b.OPERATION_ALIAS_VERSION == "3c2bib.op.1.0.0"


# ---------------------------------------------------------------------------
# Two-axis rule — the load-bearing invariant.
# ---------------------------------------------------------------------------


def test_governed_requires_both_axes_resolved_and_both_ids_set():
    result = _result(
        result_status=MultiSourceReason.resolved,
        contract_result_status=ContractResolutionStatus.resolved,
        selected_plan_id="ms_1", selected_contract_id="cc_1")
    assert b.map_a_outcome(result) is b.BDisposition.governed


@pytest.mark.parametrize("contract_status", [
    ContractResolutionStatus.not_compiled,
    ContractResolutionStatus.unresolved_ingredient_connectivity,
    ContractResolutionStatus.unresolved_aggregation_declaration,
    ContractResolutionStatus.unresolved_temporal_declaration,
    ContractResolutionStatus.unresolved_safety_evaluation,
    ContractResolutionStatus.safety_rejected,
    ContractResolutionStatus.unresolved_freshness,
])
def test_assembly_resolved_but_contract_unresolved_never_governed(contract_status):
    result = _result(
        result_status=MultiSourceReason.resolved,
        contract_result_status=contract_status,
        selected_plan_id="ms_1", selected_contract_id=None)
    disposition = b.map_a_outcome(result)
    assert disposition is b.BDisposition.contract_unresolved
    assert disposition is not b.BDisposition.governed


def test_both_axes_resolved_but_missing_selected_plan_id_never_governed():
    result = _result(
        result_status=MultiSourceReason.resolved,
        contract_result_status=ContractResolutionStatus.resolved,
        selected_plan_id=None, selected_contract_id="cc_1")
    assert b.map_a_outcome(result) is not b.BDisposition.governed


def test_both_axes_resolved_but_missing_selected_contract_id_never_governed():
    result = _result(
        result_status=MultiSourceReason.resolved,
        contract_result_status=ContractResolutionStatus.resolved,
        selected_plan_id="ms_1", selected_contract_id=None)
    assert b.map_a_outcome(result) is not b.BDisposition.governed


def test_assembly_not_resolved_never_governed_regardless_of_contract_axis():
    result = _result(
        result_status=MultiSourceReason.technical_failure,
        contract_result_status=ContractResolutionStatus.resolved,
        selected_plan_id="ms_1", selected_contract_id="cc_1")
    assert b.map_a_outcome(result) is not b.BDisposition.governed


# ---------------------------------------------------------------------------
# Explicit A-reason -> B-disposition mapping (assembly axis NOT resolved).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reason,expected", [
    (MultiSourceReason.technical_failure, b.BDisposition.technical_failure),
    (MultiSourceReason.operand_or_slot_not_preserved, b.BDisposition.technical_failure),
    (MultiSourceReason.budget_truncated, b.BDisposition.budget_truncated),
    (MultiSourceReason.source_binding_ungoverned, b.BDisposition.structural_need_ungoverned),
    (MultiSourceReason.no_governed_path, b.BDisposition.structural_need_ungoverned),
    (MultiSourceReason.no_common_physical_grain, b.BDisposition.structural_need_ungoverned),
    (MultiSourceReason.realization_endpoint_ungoverned, b.BDisposition.source_entity_ungoverned),
    (MultiSourceReason.ambiguous_physical_grain, b.BDisposition.ambiguous_column_identity),
    (MultiSourceReason.ordering_anchor_missing, b.BDisposition.operand_order_authority_missing),
    (MultiSourceReason.unsupported_path_aggregation, b.BDisposition.role_not_aggregatable),
    (MultiSourceReason.aggregation_unsafe_on_path, b.BDisposition.role_not_aggregatable),
    (MultiSourceReason.operand_shape_invalid, b.BDisposition.unresolved_operand),
    # no closer B member exists for this one — the safe non-governed fallback.
    (MultiSourceReason.temporal_paths_incompatible, b.BDisposition.technical_failure),
])
def test_explicit_a_reason_mapping(reason, expected):
    result = _result(result_status=reason)
    disposition = b.map_a_outcome(result)
    assert disposition is expected
    assert disposition is not b.BDisposition.governed


def test_mapping_is_total_over_every_multisource_reason():
    """Every A ``MultiSourceReason`` member (fed as the run-level ``result_status``, paired with a
    non-resolved contract axis so `resolved` can't accidentally read as governed) must resolve to
    SOME ``BDisposition`` without raising."""
    for reason in MultiSourceReason:
        result = _result(result_status=reason)
        disposition = b.map_a_outcome(result)
        assert isinstance(disposition, b.BDisposition)
        if reason is not MultiSourceReason.resolved:
            assert disposition is not b.BDisposition.governed


def test_a_reason_to_b_mapping_table_is_total_over_non_resolved_reasons():
    non_resolved = {r for r in MultiSourceReason if r is not MultiSourceReason.resolved}
    assert non_resolved <= set(b._A_REASON_TO_B_DISPOSITION)
    assert b.BDisposition.governed not in b._A_REASON_TO_B_DISPOSITION.values()
