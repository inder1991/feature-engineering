"""Step-3 temporal contracts: R14 at contract level + the reuse law (never a second temporal
language).

The plan's six mandatory temporal cases are expressed HERE to the extent a pure contract can
express them; the dataset-level halves (actual row selection against seeded data) land with the
D6 temporal-leak fixtures and are named per test.
"""
from __future__ import annotations

import pytest
from tests.featuregen.overlay.upload.planner.test_identity_contracts import (
    CUST,
    TXN,
    _binding,
    _logical,
    _physical,
    _segment,
    _semantics,
)

from featuregen.materialize import boundary_v2 as _boundary
from featuregen.overlay.upload import bridge_realization as _bridge
from featuregen.overlay.upload import temporal_policy as _temporal_policy
from featuregen.overlay.upload.planner import logical_plan_v2, physical_plan_v1
from featuregen.overlay.upload.planner.identity_chain import logical_digest, physical_digest
from featuregen.overlay.upload.planner.logical_plan_v2 import (
    LATEST_AVAILABLE_REFUSED_FOR_PURPOSE,
    ContractDefect,
    KnowledgeTimeBasisV2,
    TemporalEvaluationPurposeV1,
    validate_temporal_semantics_for_purpose,
)
from featuregen.overlay.upload.planner.physical_plan_v1 import (
    validate_temporal_binding_for_purpose,
)
from featuregen.overlay.upload.profile_vocab import TemporalStorageModel
from featuregen.overlay.upload.source_selection import (
    TEMPORAL_HISTORICAL_CURRENT_ONLY,
    SelectionError,
)
from featuregen.overlay.upload.temporal_policy import (
    TEMPORAL_POLICY_ID_PREFIX,
    DatasetTemporalPolicyRevisionV1,
    TemporalSelectionKind,
)


def _dataset_policy(**over) -> DatasetTemporalPolicyRevisionV1:
    base = dict(
        dataset_logical_ref=CUST,
        temporal_storage_model=TemporalStorageModel.SNAPSHOT,
        current_selection=TemporalSelectionKind.LATEST_SNAPSHOT_AS_OF,
        historical_selection=TemporalSelectionKind.LATEST_SNAPSHOT_AS_OF,
        snapshot_ref=f"{CUST}.business_dt",
    )
    base.update(over)
    return DatasetTemporalPolicyRevisionV1(**base)


class TestSemanticsConstruction:
    def test_missing_field_is_a_construction_error(self) -> None:
        with pytest.raises(TypeError):
            logical_plan_v2.LogicalTemporalJoinSemanticsV1(  # type: ignore[call-arg]
                effective_time_basis=KnowledgeTimeBasisV2.AS_OF_CUTOFF)

    def test_none_is_never_fabricated_into_a_value(self) -> None:
        with pytest.raises(ContractDefect):
            _semantics(knowledge_time_basis=None)
        with pytest.raises(ContractDefect):
            _semantics(driving_time_role=None)

    def test_string_tokens_coerce_into_the_shared_vocabulary(self) -> None:
        semantics = _semantics(effective_time_basis="as_of_cutoff",
                               knowledge_time_basis="as_of_cutoff")
        assert semantics.effective_time_basis is KnowledgeTimeBasisV2.AS_OF_CUTOFF

    def test_latest_available_is_not_an_effective_time_basis(self) -> None:
        # "current state effective for a historical row" is the R14 leakage, refused at the type.
        with pytest.raises(ContractDefect):
            _semantics(effective_time_basis=KnowledgeTimeBasisV2.LATEST_AVAILABLE)


class TestBindingConstruction:
    def test_policy_revision_id_must_be_a_dtp_revision(self) -> None:
        with pytest.raises(ContractDefect):
            _binding(dataset_temporal_policy_revision_id="policy_7")

    def test_binding_must_anchor_at_least_one_temporal_column(self) -> None:
        with pytest.raises(ContractDefect):
            _binding(effective_from_column_ref=None, effective_to_column_ref=None,
                     availability_or_knowledge_time_column_ref=None)

    def test_interval_bounds_come_in_pairs(self) -> None:
        with pytest.raises(ContractDefect):
            _binding(effective_from_column_ref=f"{CUST}.business_dt",
                     effective_to_column_ref=None)

    def test_cutoff_is_a_parameter_ref_never_a_literal_date(self) -> None:
        with pytest.raises(Exception):
            _binding(cutoff_parameter_ref="2026-08-24")


# ────────────────────────────────────────────────────────────────────────────────────────────
# The six mandatory temporal cases (plan §Temporal contracts), at contract level
# ────────────────────────────────────────────────────────────────────────────────────────────
class TestSixMandatoryCases:
    def test_case1_customer_state_changes_after_cutoff(self) -> None:
        # Effective-time basis IS logical meaning: as_of_cutoff and event_time features are
        # different features (R14). Dataset half (a state row changing after cutoff must not be
        # read) lands with D6.
        a = _logical()
        segment = logical_plan_v2.LogicalRelationshipSegmentV1(
            left_endpoint_refs=(f"{CUST}.cust_num",),
            right_endpoint_refs=(f"{TXN}.cif_id",),
            temporal_semantics=_semantics(effective_time_basis=KnowledgeTimeBasisV2.EVENT_TIME))
        b = _logical(relationship_path=(segment,))
        assert logical_digest(a) != logical_digest(b)

    def test_case2_backdated_record_inserted_after_cutoff(self) -> None:
        # Knowledge-time basis IS logical meaning, and the physical binding must anchor a
        # knowledge/availability column — a binding that names nothing temporal refuses
        # (never fabricates). Dataset half (the backdated row excluded) lands with D6.
        a = _logical()
        segment = logical_plan_v2.LogicalRelationshipSegmentV1(
            left_endpoint_refs=(f"{CUST}.cust_num",),
            right_endpoint_refs=(f"{TXN}.cif_id",),
            temporal_semantics=_semantics(
                knowledge_time_basis=KnowledgeTimeBasisV2.LATEST_AVAILABLE))
        assert logical_digest(a) != logical_digest(_logical(relationship_path=(segment,)))
        with pytest.raises(ContractDefect):
            _binding(availability_or_knowledge_time_column_ref=None)

    def test_case3_overlapping_validity_intervals_need_declared_tie_break(self) -> None:
        # Deterministic selection over overlapping/tied rows requires a DECLARED total order;
        # an empty tie-break tuple is a construction defect, never a silent "first row wins".
        with pytest.raises(ContractDefect):
            _binding(tie_break_column_refs=())

    def test_case4_two_records_valid_at_one_cutoff_tie_break_is_identity(self) -> None:
        a = _physical()
        b = _physical(segments=(_segment(temporal_binding=_binding(
            tie_break_column_refs=(f"{CUST}.business_dt", f"{CUST}.cust_num"))),))
        assert physical_digest(a) != physical_digest(b)

    def test_case5_current_only_source_refused_for_historical_generation(self) -> None:
        # (a) The EXISTING authority already refuses a historical selection on a current-only
        # model — pinned here because our binding validation leans on it.
        with pytest.raises(SelectionError):
            _dataset_policy(
                temporal_storage_model=TemporalStorageModel.CURRENT_ONLY,
                current_selection=TemporalSelectionKind.CURRENT_RECORD,
                historical_selection=TemporalSelectionKind.VALID_AT_REPORT_CUTOFF,
                snapshot_ref=None)
        # (b) An honest current-only policy (historical = explicit_only) still cannot serve a
        # TRAINING/BACKTESTING purpose.
        policy = _dataset_policy(
            temporal_storage_model=TemporalStorageModel.CURRENT_ONLY,
            current_selection=TemporalSelectionKind.CURRENT_RECORD,
            historical_selection=TemporalSelectionKind.EXPLICIT_ONLY,
            snapshot_ref=None)
        binding = _binding(dataset_temporal_policy_revision_id=policy.revision_id)
        with pytest.raises(ContractDefect) as exc:
            validate_temporal_binding_for_purpose(
                binding, policy, TemporalEvaluationPurposeV1.TRAINING)
        assert TEMPORAL_HISTORICAL_CURRENT_ONLY in str(exc.value)

    def test_case5b_binding_must_name_the_policy_it_claims(self) -> None:
        policy = _dataset_policy()
        binding = _binding(dataset_temporal_policy_revision_id=TEMPORAL_POLICY_ID_PREFIX + "0" * 64)
        with pytest.raises(ContractDefect):
            validate_temporal_binding_for_purpose(
                binding, policy, TemporalEvaluationPurposeV1.TRAINING)

    def test_case6_latest_available_refused_for_training_and_backtesting(self) -> None:
        semantics = _semantics(knowledge_time_basis=KnowledgeTimeBasisV2.LATEST_AVAILABLE)
        for purpose in (TemporalEvaluationPurposeV1.TRAINING,
                        TemporalEvaluationPurposeV1.BACKTESTING):
            with pytest.raises(ContractDefect) as exc:
                validate_temporal_semantics_for_purpose(semantics, purpose)
            assert LATEST_AVAILABLE_REFUSED_FOR_PURPOSE in str(exc.value)
        # Current scoring may read latest_available: that IS its meaning, no leakage.
        validate_temporal_semantics_for_purpose(
            semantics, TemporalEvaluationPurposeV1.CURRENT_SCORING)
        validate_temporal_semantics_for_purpose(
            _semantics(), TemporalEvaluationPurposeV1.TRAINING)


# ────────────────────────────────────────────────────────────────────────────────────────────
# Reuse law: never a second temporal language
# ────────────────────────────────────────────────────────────────────────────────────────────
class TestReuseLaw:
    def test_knowledge_time_vocabulary_is_the_boundary_v2_one(self) -> None:
        assert logical_plan_v2.KnowledgeTimeBasisV2 is _boundary.KnowledgeTimeBasisV2

    def test_physical_types_are_the_bridge_realization_ones(self) -> None:
        assert physical_plan_v1.ColumnPairV1 is _bridge.ColumnPairV1
        assert physical_plan_v1.AsOfIntervalRequirementV1 is _bridge.AsOfIntervalRequirementV1
        assert (physical_plan_v1.DirectionalCardinalityVerdictV1
                is _bridge.DirectionalCardinalityVerdictV1)

    def test_temporal_policy_prefix_is_shared(self) -> None:
        assert physical_plan_v1.TEMPORAL_POLICY_ID_PREFIX == TEMPORAL_POLICY_ID_PREFIX

    def test_interval_law_matches_the_existing_half_open_spelling(self) -> None:
        requirement = _bridge.AsOfIntervalRequirementV1(
            predicate_id="p", effective_from_ref=f"{CUST}.cust_reln_start_dt",
            effective_to_ref=f"{CUST}.business_dt", as_of_value_ref="cutoff")
        assert (logical_plan_v2.IntervalBoundaryPolicyV1.CLOSED_OPEN.value
                == requirement.identity_payload()["interval"])

    def test_no_duplicated_temporal_enums_in_the_new_modules(self) -> None:
        from pathlib import Path
        for module in (logical_plan_v2, physical_plan_v1):
            source = Path(module.__file__).read_text()
            for forbidden in ("class KnowledgeTimeBasis", "class TemporalSelectionKind",
                              "class TemporalStorageModel", "class Cardinality(",
                              "class ColumnPair", "class AsOfIntervalRequirement"):
                assert forbidden not in source, f"{module.__name__} redefines {forbidden}"

    def test_purpose_vocabulary_is_closed(self) -> None:
        assert {m.value for m in TemporalEvaluationPurposeV1} == {
            "training", "backtesting", "current_scoring"}
        # DatasetRowSelectionV1 stays the row-selection record; pin its import path so a future
        # module cannot quietly fork it.
        assert _temporal_policy.DatasetRowSelectionV1.__module__ == \
            "featuregen.overlay.upload.temporal_policy"
