"""Step-3 identity contracts: the three plan layers + the seven-stage digest chain.

Pins R9 (logical identity = feature meaning only), R2 (execution context never touches logical
identity), R14 (temporal meaning is logical), the staleness-law table, the fan-out law, and the
semantic-only content hash of ``JoinValidationPolicyRevisionV1``. Golden literal pins at the bottom
prove digests are stable across process restarts (no dict-order dependence).
"""
from __future__ import annotations

import dataclasses
import re

import pytest

from featuregen.overlay.upload.bridge_realization import (
    ColumnPairV1,
    DirectionalCardinalityVerdictV1,
    FixedValueReferencePredicateV1,
)
from featuregen.overlay.upload.planner.identity_chain import (
    build_compilation_digest,
    formula_binding_digest,
    generation_configuration_digest,
    logical_digest,
    member_compile_digest,
    member_execution_input_digest,
    physical_digest,
    render_digest,
    sealed_artifact_identity,
)
from featuregen.overlay.upload.planner.logical_plan_v2 import (
    ContractDefect,
    DrivingTimeRoleV1,
    IntervalBoundaryPolicyV1,
    KnowledgeTimeBasisV2,
    LogicalFeaturePlanV2,
    LogicalOperandBindingV1,
    LogicalPlanProvenanceV1,
    LogicalRelationshipSegmentV1,
    LogicalTemporalJoinSemanticsV1,
    StaticLinkMeaningV1,
    UnmatchedRowMeaningV1,
)
from featuregen.overlay.upload.planner.physical_plan_v1 import (
    ALLOCATION_POLICY_REQUIRED,
    BlankKeyBehaviorV1,
    CaseNormalizationV1,
    CompositeKeyOrderingV1,
    CoverageDenominatorV1,
    CoverageNumeratorV1,
    FanOutControlOperatorV1,
    JoinKeyNormalizationPolicy,
    JoinOrientationV1,
    JoinValidationPolicyRevisionV1,
    LeadingZeroPolicyV1,
    NullKeyBehaviorV1,
    PhysicalExecutionPlanV1,
    PhysicalJoinSegmentV1,
    PhysicalTemporalJoinBindingV1,
    SnapshotSelectionRuleV1,
    UnmatchedRowBehaviorV1,
    WhitespaceNormalizationV1,
)
from featuregen.overlay.upload.planner.render_profile import (
    GenerationConfigurationV1,
    MemberOutputContractV1,
    NullInputBehaviorV1,
    OverflowPolicyV1,
    RenderProfileV1,
    RoundingPolicyV1,
)

CUST = "cib::public.bo_cib_customer"
TXN = "ftr::public.comp_financial_tran_repos_dly"
HEX = re.compile(r"^[0-9a-f]{64}$")


def _semantics(**over) -> LogicalTemporalJoinSemanticsV1:
    base = dict(
        effective_time_basis=KnowledgeTimeBasisV2.AS_OF_CUTOFF,
        knowledge_time_basis=KnowledgeTimeBasisV2.AS_OF_CUTOFF,
        driving_time_role=DrivingTimeRoleV1.CUTOFF_PARAMETER,
        interval_boundary_policy=IntervalBoundaryPolicyV1.CLOSED_OPEN,
        unmatched_row_meaning=UnmatchedRowMeaningV1.JOINED_ATTRIBUTES_NOT_APPLICABLE,
        static_link_meaning=StaticLinkMeaningV1.REFUSE,
    )
    base.update(over)
    return LogicalTemporalJoinSemanticsV1(**base)


def _provenance(**over) -> LogicalPlanProvenanceV1:
    base = dict(
        hypothesis_text="Find newly onboarded CIB customers whose outgoing payments increased.",
        planning_request_hash="a" * 64,
        chooser_revision_id="chooser_7",
        menu_content_hash="b" * 64,
        display_text="Outgoing payment increase (30d vs prior 30d)",
    )
    base.update(over)
    return LogicalPlanProvenanceV1(**base)


def _logical(**over) -> LogicalFeaturePlanV2:
    base = dict(
        canonical_definition_content_hash="c" * 64,
        canonical_definition_revision_id="cdr_1",
        operation="sum_window_delta",
        operand_bindings=(
            LogicalOperandBindingV1(
                role="amount",
                logical_column_ref=f"{TXN}.actual_tran_amt_aed",
                governed_semantic_revision_id="sem_amount_1"),
            LogicalOperandBindingV1(
                role="direction",
                logical_column_ref=f"{TXN}.tran_dc",
                governed_semantic_revision_id="sem_dir_1"),
        ),
        output_grain_key_refs=(f"{CUST}.cust_num",),
        selected_parameters=(("window_days", 30), ("new_customer_days", 180)),
        relationship_path=(
            LogicalRelationshipSegmentV1(
                left_endpoint_refs=(f"{CUST}.cust_num",),
                right_endpoint_refs=(f"{TXN}.cif_id",),
                temporal_semantics=_semantics(),
            ),
        ),
        formula_policy_identities=(("direction_value_map", "pol_dir_1"),),
        provenance=_provenance(),
    )
    base.update(over)
    return LogicalFeaturePlanV2(**base)


def _normalization(**over) -> JoinKeyNormalizationPolicy:
    base = dict(
        whitespace=WhitespaceNormalizationV1.TRIM,
        case_handling=CaseNormalizationV1.PRESERVE,
        leading_zeros=LeadingZeroPolicyV1.PRESERVE,
        declared_type_coercions=(("varchar(150)", "string"),),
        blank_key_behavior=BlankKeyBehaviorV1.NEVER_MATCH,
        nulls_never_match=True,
        composite_key_ordering=CompositeKeyOrderingV1.DECLARED_PAIR_ORDER,
    )
    base.update(over)
    return JoinKeyNormalizationPolicy(**base)


def _binding(**over) -> PhysicalTemporalJoinBindingV1:
    base = dict(
        dataset_temporal_policy_revision_id="dtp_" + "d" * 64,
        effective_from_column_ref=None,
        effective_to_column_ref=None,
        availability_or_knowledge_time_column_ref=f"{CUST}.business_dt",
        cutoff_parameter_ref="report_cutoff",
        source_binding_revision_id="sbr_cust_1",
        tie_break_column_refs=(f"{CUST}.business_dt",),
    )
    base.update(over)
    return PhysicalTemporalJoinBindingV1(**base)


def _policy(**over) -> JoinValidationPolicyRevisionV1:
    base = dict(
        null_key_behavior=NullKeyBehaviorV1.EXCLUDE_ROW,
        unmatched_row_behavior=UnmatchedRowBehaviorV1.PRESERVE_LEFT_NULL,
        coverage_numerator=CoverageNumeratorV1.MATCHED_LEFT_ROWS,
        coverage_denominator=CoverageDenominatorV1.NON_NULL_KEY_LEFT_ROWS,
        minimum_coverage_ratio=0.95,
        orientation=JoinOrientationV1.LEFT_DRIVING,
        max_matches_per_left_row=1,
        snapshot_selection_rule=SnapshotSelectionRuleV1.LATEST_AT_OR_BEFORE_CUTOFF,
        applies_to_final_grain_aggregate=True,
        fan_out_control_operator=None,
        declared_by="user:ascoe",
        declared_at="2026-08-24T00:00:00Z",
    )
    base.update(over)
    return JoinValidationPolicyRevisionV1(**base)


def _segment(**over) -> PhysicalJoinSegmentV1:
    base = dict(
        realization_revision_id="bjr_1",
        column_pairs=(ColumnPairV1(f"{CUST}.cust_num", f"{TXN}.cif_id"),),
        predicates=(),
        directional_cardinality=DirectionalCardinalityVerdictV1.unknown(),
        realization_content_hash="e" * 64,
        realization_dependency_hash="f" * 64,
        key_normalization=_normalization(),
        temporal_binding=_binding(),
    )
    base.update(over)
    return PhysicalJoinSegmentV1(**base)


def _physical(**over) -> PhysicalExecutionPlanV1:
    base = dict(
        logical_digest_ref=logical_digest(_logical()),
        execution_context_revision_id="ecr_1",
        source_binding_revisions=((CUST, "sbr_cust_1"), (TXN, "sbr_txn_1")),
        segments=(_segment(),),
        join_validation_policy_revision_id=_policy().revision_id,
    )
    base.update(over)
    return PhysicalExecutionPlanV1(**base)


def _render_profile(**over) -> RenderProfileV1:
    base = dict(
        engine="pyspark_kedro",
        compiler_version="compiler-1.0.0",
        renderer_version="renderer-1.0.0",
        template_versions=(("node", "1.0.0"), ("pipeline", "2.0.0")),
    )
    base.update(over)
    return RenderProfileV1(**base)


def _genconfig(**over) -> GenerationConfigurationV1:
    base = dict(
        population_spine_ref=CUST,
        target_mode="sandbox_preview",
        target_ref="preview_workspace",
        cadence="on_demand",
        physical_type_policy="engine_default",
        policy_realization_revision_ids=("prr_direction_1",),
        engine_settings=(("shuffle_partitions", 16),),
    )
    base.update(over)
    return GenerationConfigurationV1(**base)


def _output_contract(**over) -> MemberOutputContractV1:
    base = dict(
        output_feature_name="outgoing_increase_30d",
        output_column_name="outgoing_increase_30d",
        empty_window_value=0,
        not_applicable_value=None,
        null_input_behavior=NullInputBehaviorV1.PROPAGATE_NULL,
        physical_type="decimal(38,2)",
        decimal_scale=2,
        rounding_policy=RoundingPolicyV1.HALF_EVEN,
        overflow_policy=OverflowPolicyV1.REFUSE,
    )
    base.update(over)
    return MemberOutputContractV1(**base)


# ────────────────────────────────────────────────────────────────────────────────────────────
# R9: logical identity = feature meaning only
# ────────────────────────────────────────────────────────────────────────────────────────────
class TestR9LogicalDigest:
    @pytest.mark.parametrize("field, value", [
        ("hypothesis_text", "A completely different hypothesis about payment velocity."),
        ("planning_request_hash", "9" * 64),
        ("chooser_revision_id", "chooser_8"),
        ("menu_content_hash", "8" * 64),   # menu CONTENT change (unused allowed value added)
        ("display_text", "Renamed display title"),
    ])
    def test_provenance_never_enters_the_digest(self, field: str, value: str) -> None:
        a = _logical()
        b = _logical(provenance=_provenance(**{field: value}))
        assert logical_digest(a) == logical_digest(b)

    def test_selected_value_change_rekeys(self) -> None:
        a = _logical()
        b = _logical(selected_parameters=(("window_days", 60), ("new_customer_days", 180)))
        assert logical_digest(a) != logical_digest(b)

    def test_selected_parameter_order_is_not_identity(self) -> None:
        a = _logical(selected_parameters=(("window_days", 30), ("new_customer_days", 180)))
        b = _logical(selected_parameters=(("new_customer_days", 180), ("window_days", 30)))
        assert logical_digest(a) == logical_digest(b)

    def test_temporal_semantics_change_rekeys(self) -> None:
        segment = LogicalRelationshipSegmentV1(
            left_endpoint_refs=(f"{CUST}.cust_num",),
            right_endpoint_refs=(f"{TXN}.cif_id",),
            temporal_semantics=_semantics(
                knowledge_time_basis=KnowledgeTimeBasisV2.LATEST_AVAILABLE),
        )
        assert logical_digest(_logical()) != logical_digest(
            _logical(relationship_path=(segment,)))

    def test_operand_semantic_revision_change_rekeys(self) -> None:
        bindings = (
            LogicalOperandBindingV1(
                role="amount", logical_column_ref=f"{TXN}.actual_tran_amt_aed",
                governed_semantic_revision_id="sem_amount_2"),
            LogicalOperandBindingV1(
                role="direction", logical_column_ref=f"{TXN}.tran_dc",
                governed_semantic_revision_id="sem_dir_1"),
        )
        assert logical_digest(_logical()) != logical_digest(_logical(operand_bindings=bindings))

    def test_grain_order_is_identity(self) -> None:
        a = _logical(output_grain_key_refs=(f"{CUST}.cust_num", f"{CUST}.business_dt"))
        b = _logical(output_grain_key_refs=(f"{CUST}.business_dt", f"{CUST}.cust_num"))
        assert logical_digest(a) != logical_digest(b)

    def test_path_endpoint_order_is_identity(self) -> None:
        semantics = _semantics()
        a = _logical(relationship_path=(LogicalRelationshipSegmentV1(
            left_endpoint_refs=(f"{CUST}.cust_num", f"{CUST}.business_dt"),
            right_endpoint_refs=(f"{TXN}.cif_id", f"{TXN}.pstd_date"),
            temporal_semantics=semantics),))
        b = _logical(relationship_path=(LogicalRelationshipSegmentV1(
            left_endpoint_refs=(f"{CUST}.business_dt", f"{CUST}.cust_num"),
            right_endpoint_refs=(f"{TXN}.pstd_date", f"{TXN}.cif_id"),
            temporal_semantics=semantics),))
        assert logical_digest(a) != logical_digest(b)

    def test_operation_and_definition_changes_rekey(self) -> None:
        assert logical_digest(_logical()) != logical_digest(_logical(operation="sum_window"))
        assert logical_digest(_logical()) != logical_digest(
            _logical(canonical_definition_content_hash="0" * 64))

    def test_duplicate_operand_roles_refused(self) -> None:
        bindings = (
            LogicalOperandBindingV1(role="amount", logical_column_ref=f"{TXN}.actual_tran_amt_aed",
                                    governed_semantic_revision_id="s1"),
            LogicalOperandBindingV1(role="amount", logical_column_ref=f"{TXN}.tran_dc",
                                    governed_semantic_revision_id="s2"),
        )
        with pytest.raises(ContractDefect):
            _logical(operand_bindings=bindings)


# ────────────────────────────────────────────────────────────────────────────────────────────
# The staleness-law table (plan §Identity & staleness law)
# ────────────────────────────────────────────────────────────────────────────────────────────
class TestStalenessLaw:
    def test_row_display_provenance_no_rekey(self) -> None:
        # proposed→confirmed / strength / display text are provenance: same logical identity.
        assert logical_digest(_logical()) == logical_digest(
            _logical(provenance=_provenance(display_text="link now CONFIRMED, strength strong")))

    def test_row_superseding_realization_new_physical_identity(self) -> None:
        # Refusing the OLD pin is the consuming layer's act; the contract fact is that a
        # superseding realization is a NEW physical identity (adoption is the path).
        a = _physical()
        b = _physical(segments=(_segment(realization_revision_id="bjr_2"),))
        assert physical_digest(a) != physical_digest(b)

    def test_row_join_columns_new_physical_identity(self) -> None:
        b = _physical(segments=(_segment(
            column_pairs=(ColumnPairV1(f"{CUST}.cust_num", f"{TXN}.tran_id"),)),))
        assert physical_digest(_physical()) != physical_digest(b)

    def test_row_predicates_new_physical_identity(self) -> None:
        predicate = FixedValueReferencePredicateV1(
            predicate_id="p1", logical_column_ref=f"{CUST}.business_dt",
            value_ref="business_dt_param")
        b = _physical(segments=(_segment(predicates=(predicate,)),))
        assert physical_digest(_physical()) != physical_digest(b)

    def test_row_cardinality_new_physical_identity(self) -> None:
        from featuregen.overlay.upload.taxonomy.entity_relationships import Cardinality
        b = _physical(segments=(_segment(
            directional_cardinality=DirectionalCardinalityVerdictV1(Cardinality.MANY_TO_ONE)),))
        assert physical_digest(_physical()) != physical_digest(b)

    def test_row_environment_new_physical_revision_logical_untouched(self) -> None:
        # R2: execution context never touches logical identity.
        base_logical = _logical()
        a = _physical(logical_digest_ref=logical_digest(base_logical))
        b = _physical(logical_digest_ref=logical_digest(base_logical),
                      execution_context_revision_id="ecr_2")
        assert physical_digest(a) != physical_digest(b)
        assert a.logical_digest_ref == b.logical_digest_ref

    def test_row_same_parameters_different_hypotheses_same_logical_identity(self) -> None:
        assert logical_digest(_logical()) == logical_digest(
            _logical(provenance=_provenance(hypothesis_text="another road to the same feature")))

    # Withdrawn/revoked → refuse: a lifecycle verdict of the consuming layer (A4c/R11's
    # assess_realization_for_preview), not a digest fact — landed with that layer, noted here.


# ────────────────────────────────────────────────────────────────────────────────────────────
# JoinValidationPolicyRevisionV1: semantic-only hash + the fan-out law
# ────────────────────────────────────────────────────────────────────────────────────────────
class TestJoinValidationPolicy:
    def test_semantic_only_content_hash(self) -> None:
        a = _policy()
        b = _policy(declared_by="user:someone_else", declared_at="2026-08-25T09:00:00Z")
        assert a.content_hash == b.content_hash
        assert a.revision_id == b.revision_id

    def test_semantic_change_changes_hash(self) -> None:
        assert _policy().content_hash != _policy(minimum_coverage_ratio=0.9).content_hash
        assert _policy().content_hash != _policy(
            unmatched_row_behavior=UnmatchedRowBehaviorV1.REFUSE).content_hash

    def test_fan_out_law_refuses_uncontrolled_fan_out(self) -> None:
        with pytest.raises(ContractDefect) as exc:
            _policy(max_matches_per_left_row=2, applies_to_final_grain_aggregate=True,
                    fan_out_control_operator=None)
        assert ALLOCATION_POLICY_REQUIRED in str(exc.value)

    def test_fan_out_law_with_dedup_operator_constructs(self) -> None:
        policy = _policy(max_matches_per_left_row=2, applies_to_final_grain_aggregate=True,
                         fan_out_control_operator=FanOutControlOperatorV1.DETERMINISTIC_DEDUP)
        assert policy.fan_out_control_operator is FanOutControlOperatorV1.DETERMINISTIC_DEDUP

    def test_fan_out_law_not_final_grain_constructs(self) -> None:
        policy = _policy(max_matches_per_left_row=2, applies_to_final_grain_aggregate=False,
                         fan_out_control_operator=None)
        assert policy.max_matches_per_left_row == 2

    def test_closed_enums_reject_unknown_tokens(self) -> None:
        with pytest.raises(ContractDefect):
            _policy(null_key_behavior="preserve")
        with pytest.raises(ContractDefect):
            _policy(fan_out_control_operator="hope_for_the_best")

    def test_coverage_ratio_bounds(self) -> None:
        with pytest.raises(ContractDefect):
            _policy(minimum_coverage_ratio=1.5)
        with pytest.raises(ContractDefect):
            _policy(max_matches_per_left_row=0)


class TestJoinKeyNormalizationPolicy:
    def test_nulls_never_match_is_law(self) -> None:
        with pytest.raises(ContractDefect):
            _normalization(nulls_never_match=False)

    def test_coercion_is_declared_never_assumed(self) -> None:
        a = _normalization()
        b = _normalization(declared_type_coercions=())
        assert a.identity_payload() != b.identity_payload()

    def test_every_field_is_explicit_no_hidden_defaults(self) -> None:
        for contract in (
            LogicalTemporalJoinSemanticsV1, PhysicalTemporalJoinBindingV1,
            JoinKeyNormalizationPolicy, JoinValidationPolicyRevisionV1,
            MemberOutputContractV1, LogicalFeaturePlanV2, LogicalOperandBindingV1,
            LogicalRelationshipSegmentV1, PhysicalExecutionPlanV1, PhysicalJoinSegmentV1,
            RenderProfileV1, GenerationConfigurationV1,
        ):
            for f in dataclasses.fields(contract):
                if not f.init:
                    continue  # computed identity fields, not decisions
                assert f.default is dataclasses.MISSING, f"{contract.__name__}.{f.name}"
                assert f.default_factory is dataclasses.MISSING, f"{contract.__name__}.{f.name}"


# ────────────────────────────────────────────────────────────────────────────────────────────
# GenerationConfigurationV1 / MemberOutputContractV1 single-owner split
# ────────────────────────────────────────────────────────────────────────────────────────────
class TestGenerationConfiguration:
    def test_no_per_feature_output_fields(self) -> None:
        config_fields = {f.name for f in dataclasses.fields(GenerationConfigurationV1)}
        output_fields = {f.name for f in dataclasses.fields(MemberOutputContractV1)}
        assert not config_fields & output_fields

    def test_digest_changes_on_semantic_change(self) -> None:
        assert generation_configuration_digest(_genconfig()) != generation_configuration_digest(
            _genconfig(cadence="daily"))
        assert generation_configuration_digest(_genconfig()) != generation_configuration_digest(
            _genconfig(target_ref="another_target"))

    def test_duplicate_engine_setting_keys_refused(self) -> None:
        with pytest.raises(ContractDefect):
            _genconfig(engine_settings=(("shuffle_partitions", 16), ("shuffle_partitions", 8)))


class TestRenderProfile:
    def test_digest_changes_on_renderer_version(self) -> None:
        assert render_digest(_render_profile()) != render_digest(
            _render_profile(renderer_version="renderer-1.1.0"))

    def test_template_order_is_not_identity(self) -> None:
        a = _render_profile(template_versions=(("node", "1.0.0"), ("pipeline", "2.0.0")))
        b = _render_profile(template_versions=(("pipeline", "2.0.0"), ("node", "1.0.0")))
        assert render_digest(a) == render_digest(b)

    def test_duplicate_template_names_refused(self) -> None:
        with pytest.raises(ContractDefect):
            _render_profile(template_versions=(("node", "1.0.0"), ("node", "2.0.0")))


# ────────────────────────────────────────────────────────────────────────────────────────────
# The seven-stage chain
# ────────────────────────────────────────────────────────────────────────────────────────────
class TestDigestChain:
    def test_every_stage_is_sha256_hex(self) -> None:
        ld = logical_digest(_logical())
        pd = physical_digest(_physical())
        fbd = formula_binding_digest(ld, "1" * 64, "method:llm_v4@7")
        meid = member_execution_input_digest(fbd, pd, _output_contract())
        mcd = member_compile_digest(meid, "2" * 64, (("occ:amount", "prr_direction_1"),))
        bcd = build_compilation_digest(
            "tsr_1", (mcd,), generation_configuration_digest(_genconfig()))
        sai = sealed_artifact_identity(bcd, render_digest(_render_profile()), "3" * 64)
        for digest in (ld, pd, fbd, meid, mcd, bcd, sai):
            assert HEX.match(digest), digest

    def test_formula_binding_digest_changes_iff_inputs_change(self) -> None:
        ld = logical_digest(_logical())
        base = formula_binding_digest(ld, "1" * 64, "method:llm_v4@7")
        assert base == formula_binding_digest(ld, "1" * 64, "method:llm_v4@7")
        assert base != formula_binding_digest("0" * 64, "1" * 64, "method:llm_v4@7")
        assert base != formula_binding_digest(ld, "4" * 64, "method:llm_v4@7")
        assert base != formula_binding_digest(ld, "1" * 64, "method:llm_v4@8")

    def test_member_execution_input_digest_sees_output_contract(self) -> None:
        ld, pd = logical_digest(_logical()), physical_digest(_physical())
        fbd = formula_binding_digest(ld, "1" * 64, "method:llm_v4@7")
        assert member_execution_input_digest(fbd, pd, _output_contract()) != \
            member_execution_input_digest(fbd, pd, _output_contract(empty_window_value=None))

    def test_member_compile_digest_sees_policy_occurrences(self) -> None:
        fbd = formula_binding_digest(logical_digest(_logical()), "1" * 64, "m@1")
        meid = member_execution_input_digest(fbd, physical_digest(_physical()), _output_contract())
        a = member_compile_digest(meid, "2" * 64, (("occ:amount", "prr_1"),))
        b = member_compile_digest(meid, "2" * 64, (("occ:amount", "prr_2"),))
        c = member_compile_digest(meid, "5" * 64, (("occ:amount", "prr_1"),))
        assert a != b and a != c

    def test_two_member_order_reversal_changes_build_digest(self) -> None:
        gcd = generation_configuration_digest(_genconfig())
        m1, m2 = "6" * 64, "7" * 64
        assert build_compilation_digest("tsr_1", (m1, m2), gcd) != \
            build_compilation_digest("tsr_1", (m2, m1), gcd)

    def test_sealed_artifact_identity_changes_with_each_input(self) -> None:
        rd = render_digest(_render_profile())
        base = sealed_artifact_identity("8" * 64, rd, "3" * 64)
        assert base != sealed_artifact_identity("9" * 64, rd, "3" * 64)
        assert base != sealed_artifact_identity("8" * 64, render_digest(
            _render_profile(engine="pyspark_raw")), "3" * 64)
        assert base != sealed_artifact_identity("8" * 64, rd, "4" * 64)

    def test_chain_inputs_validated(self) -> None:
        with pytest.raises(ContractDefect):
            formula_binding_digest("not-a-digest", "1" * 64, "m@1")
        with pytest.raises(ContractDefect):
            formula_binding_digest("0" * 64, "", "m@1")
        with pytest.raises(ContractDefect):
            build_compilation_digest("tsr_1", (), generation_configuration_digest(_genconfig()))

    def test_physical_plan_requires_join_validation_policy_revision(self) -> None:
        with pytest.raises(ContractDefect):
            _physical(join_validation_policy_revision_id="not_a_jvp_revision")
        with pytest.raises(ContractDefect):
            _physical(logical_digest_ref="not-hex")


# ────────────────────────────────────────────────────────────────────────────────────────────
# Golden literal pins: stable across process restarts (no dict-order dependence).
# Regenerate ONLY on a declared contract-version bump.
# ────────────────────────────────────────────────────────────────────────────────────────────
class TestGoldenPins:
    def test_stage_digests_are_pinned(self) -> None:
        ld = logical_digest(_logical())
        pd = physical_digest(_physical())
        gcd = generation_configuration_digest(_genconfig())
        rd = render_digest(_render_profile())
        fbd = formula_binding_digest(ld, "1" * 64, "method:llm_v4@7")
        meid = member_execution_input_digest(fbd, pd, _output_contract())
        mcd = member_compile_digest(meid, "2" * 64, (("occ:amount", "prr_direction_1"),))
        bcd = build_compilation_digest("tsr_1", (mcd,), gcd)
        sai = sealed_artifact_identity(bcd, rd, "3" * 64)
        assert ld == GOLDEN["logical_digest"]
        assert pd == GOLDEN["physical_digest"]
        assert gcd == GOLDEN["generation_configuration_digest"]
        assert rd == GOLDEN["render_digest"]
        assert fbd == GOLDEN["formula_binding_digest"]
        assert meid == GOLDEN["member_execution_input_digest"]
        assert mcd == GOLDEN["member_compile_digest"]
        assert bcd == GOLDEN["build_compilation_digest"]
        assert sai == GOLDEN["sealed_artifact_identity"]

    def test_join_validation_policy_revision_pinned(self) -> None:
        assert _policy().revision_id == GOLDEN["join_validation_policy_revision_id"]


GOLDEN = {
    "logical_digest": "359ee47a35067cc47536d1441c3cf960b3a402734462523870652c2d6b537100",
    "physical_digest": "88214c7558498798aeec17481510362c5cf04a7172eb34f50267efd1ad7d415d",
    "generation_configuration_digest":
        "c22b94411bcb21b666c7855e210c6c3993a7e2e35e0d9e2b819c0180437b7c70",
    "render_digest": "cde01d5e4e298fdc669299b647bee3b6f8668afabcb5a404cb7210fe0ac63150",
    "formula_binding_digest":
        "3869c3d8522d2b8628afa85be49f3b86cf9af85e2278042e698415d50fe82561",
    "member_execution_input_digest":
        "0f53b47570cada4bd4686bf2e9e14269dfaf40f8d929ab72e9d5ae5c5badd537",
    "member_compile_digest":
        "fcfcd2c218880805cd6f0feedbbe6217d47e6d9904803b77f7c9aba8e309d9df",
    "build_compilation_digest":
        "5ccbb1248cb54938ed03d01b5e1703d647d534aa7dde27149b1786f7e2d9510c",
    "sealed_artifact_identity":
        "003527951252a3f44161ce34281ee13d615d5f4a104bb69da375549f878389c8",
    "join_validation_policy_revision_id":
        "jvp_e1d1ae637b2a3f2d977ab7843861686c04af07d755580ca2b67884ecf0a1fdb4",
}
