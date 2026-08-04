"""Release C Task 11, scope 3 — admission: what a MEASURED crosswalk may be used for.

The bridge family's discipline, applied to three tables: no LLM, no review, no names, and the same
`SafetyStatus`/`BridgeDisplayTier`/sandbox-production split. What is new is that the two NAMED
directions are admitted INDEPENDENTLY — a 1:1 forward with an N:1 reverse admits forward only, which
is the shape a real mapping table most often has.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from tests.featuregen.overlay.upload._crosswalk_fixtures import (
    CIB_TABLE,
    FTR_TABLE,
    MAP_TABLE,
    binding,
    leg,
    mapping_observation,
)

from featuregen.data_agent.relationship_observation import RowCoverage
from featuregen.overlay.upload.bridge_admission import BridgeDisplayTier
from featuregen.overlay.upload.bridge_realization import (
    ExecutionTier,
    RealizationApplicabilityScopeV1,
    SafetyStatus,
)
from featuregen.overlay.upload.crosswalk import CrosswalkContractError, JoinLegKind, JoinLegPinV1
from featuregen.overlay.upload.crosswalk_admission import (
    CrosswalkAdmissionPolicyV1,
    CrosswalkAdmissionReason,
    admitted_crosswalk_execution,
    evaluate_crosswalk_admission,
    leg_pin_from_join_plan,
    leg_pin_from_realization,
)
from featuregen.overlay.upload.crosswalk_observation import (
    MAPPING_TO_TARGET,
    SOURCE_TO_MAPPING,
    SOURCE_TO_TARGET,
    TARGET_TO_SOURCE,
    CrosswalkObservationCaveat,
    compose_crosswalk_observation,
)
from featuregen.overlay.upload.taxonomy.entity_relationships import Cardinality

NOW = datetime(2026, 8, 4, 12, tzinfo=UTC)
DEFINITION = "cwd_" + "a" * 64
POLICY = CrosswalkAdmissionPolicyV1()

CIB = binding("cib", CIB_TABLE)
FTR = binding("ftr", FTR_TABLE, schema="dpl_ftr")
MAP = binding("cib", MAP_TABLE)

SCOPE = RealizationApplicabilityScopeV1(
    scope_id="crosswalk-sandbox-scope", execution_tier=ExecutionTier.SANDBOX,
    purposes=("crosswalk_probe",), environment="dev")


def _observation(**over):
    source = leg(which=SOURCE_TO_MAPPING, endpoint_binding=CIB, mapping_binding=MAP,
                 endpoint_column="acct_no", mapping_column="acct_no", **over.pop("source", {}))
    target = leg(which=MAPPING_TO_TARGET, endpoint_binding=FTR, mapping_binding=MAP,
                 endpoint_column="counter_party_acct_no", mapping_column="ext_acct_ref",
                 cross_catalog=True, **over.pop("target", {}))
    kwargs = dict(
        crosswalk_definition_revision_id=DEFINITION,
        source_leg=source, target_leg=target,
        mapping=mapping_observation(MAP),
        scope_id=SCOPE.scope_id,
        matched_source_distinct=3, unmatched_source_distinct=0,
        matched_target_distinct=3, unmatched_target_distinct=0,
        composed_row_count=3,
        source_to_target_max_matches=1, target_to_source_max_matches=1,
        observed_at=NOW,
        mapping_temporal_policy_revision_id="dtp_" + "b" * 64,
        mapping_row_selection_hash="c" * 64)
    kwargs.update(over)
    return compose_crosswalk_observation(**kwargs)


def _admit(observation=None, **over):
    kwargs = dict(
        crosswalk_definition_revision_id=DEFINITION, scope=SCOPE, policy=POLICY, now=NOW,
        observation=observation)
    kwargs.update(over)
    return evaluate_crosswalk_admission(**kwargs)


# ── discoverable, unmeasured ────────────────────────────────────────────────────────────────────

def test_an_unmeasured_crosswalk_is_sandbox_admissible_and_never_production():
    """"Discoverable, unmeasured" is a usable state, not a failure — and sandbox use of it is DATA
    for Task 12's exact runtime gate, never a claim that it is safe."""
    decision = _admit()
    assert decision.measured is False
    assert decision.reason_codes == (CrosswalkAdmissionReason.NOT_MEASURED.value,)
    assert decision.display_tier is BridgeDisplayTier.DISCOVERED
    for direction in (SOURCE_TO_TARGET, TARGET_TO_SOURCE):
        verdict = decision.verdict(direction)
        assert verdict.safety_status is SafetyStatus.UNASSESSED
        assert verdict.sandbox_admissible is True
        assert verdict.production_admissible is False
    assert decision.any_production_admissible is False


# ── the two directions are independent ──────────────────────────────────────────────────────────

def test_a_clean_measurement_admits_both_directions():
    decision = _admit(_observation())
    assert decision.forward.safety_status is SafetyStatus.DETERMINISTICALLY_VALIDATED
    assert decision.reverse.safety_status is SafetyStatus.DETERMINISTICALLY_VALIDATED
    assert decision.forward.cardinality.value is Cardinality.ONE_TO_ONE
    assert decision.display_tier is BridgeDisplayTier.STRONG_PROPOSED
    assert decision.combined_safety_status is SafetyStatus.DETERMINISTICALLY_VALIDATED
    assert decision.forward.reason_codes == (
        CrosswalkAdmissionReason.POLICY_SATISFIED.value,)


def test_a_1to1_forward_with_an_N1_reverse_admits_FORWARD_ONLY():
    """THE headline case. Admitting the pair as one verdict would either refuse a usable direction
    or license an unusable one."""
    observation = _observation(
        mapping=mapping_observation(MAP, duplicate_source_tuple_count=1,
                                    max_rows_per_source_tuple=2,
                                    distinct_source_tuple_count=2),
        target_to_source_max_matches=2)
    decision = _admit(observation)
    assert decision.forward.safety_status is SafetyStatus.DETERMINISTICALLY_VALIDATED
    assert decision.forward.production_admissible is True
    assert decision.reverse.safety_status is SafetyStatus.UNSAFE
    assert decision.reverse.production_admissible is False
    assert decision.reverse.sandbox_admissible is False
    assert (CrosswalkAdmissionReason.DIRECTIONAL_FANOUT.value
            in decision.reverse.reason_codes)
    # The crosswalk AS A WHOLE cannot claim deterministic safety — the usable direction survives on
    # its own verdict, which is where a compiler must read it.
    assert decision.combined_safety_status is SafetyStatus.UNSAFE
    assert decision.any_production_admissible is True


def test_the_forward_cardinality_records_the_reverse_multiplicity():
    observation = _observation(
        mapping=mapping_observation(MAP, duplicate_source_tuple_count=1,
                                    max_rows_per_source_tuple=2),
        target_to_source_max_matches=2)
    decision = _admit(observation)
    assert decision.forward.cardinality.value is Cardinality.MANY_TO_ONE


# ── evidence quality disqualifies both ──────────────────────────────────────────────────────────

def test_a_sampled_measurement_admits_sandbox_and_refuses_production():
    decision = _admit(_observation(source={"row_coverage": RowCoverage.SAMPLED}))
    for verdict in (decision.forward, decision.reverse):
        assert verdict.safety_status is SafetyStatus.UNASSESSED
        assert verdict.sandbox_admissible is True
        assert verdict.production_admissible is False
    assert (CrosswalkAdmissionReason.OBSERVATION_NOT_FULL_COVERAGE.value
            in decision.reason_codes)


def test_stale_evidence_refuses_production():
    decision = _admit(_observation(), now=NOW + timedelta(days=60))
    assert CrosswalkAdmissionReason.OBSERVATION_TOO_OLD.value in decision.reason_codes
    assert decision.any_production_admissible is False


def test_an_unfiltered_mapping_measurement_cannot_reach_production():
    """A mapping table nobody declared a row policy for may be full SCD history, and uniqueness over
    history is a different claim from uniqueness now."""
    decision = _admit(_observation(mapping_temporal_policy_revision_id=None,
                                   mapping_row_selection_hash=None))
    assert (CrosswalkAdmissionReason.MAPPING_ROWS_NOT_TEMPORALLY_FILTERED.value
            in decision.reason_codes)
    assert decision.any_production_admissible is False
    assert decision.forward.sandbox_admissible is True


def test_an_unresolved_mapping_policy_is_reported_as_its_own_reason():
    decision = _admit(_observation(
        mapping_temporal_policy_revision_id=None, mapping_row_selection_hash=None,
        caveats=(CrosswalkObservationCaveat.MAPPING_TEMPORAL_POLICY_UNRESOLVED.value,)))
    assert (CrosswalkAdmissionReason.MAPPING_TEMPORAL_POLICY_UNRESOLVED.value
            in decision.reason_codes)


def test_a_deployment_may_accept_an_unfiltered_mapping_by_policy():
    """The rule is a POLICY, not a law of nature — but it has to be declared, not defaulted."""
    lenient = CrosswalkAdmissionPolicyV1(require_governed_mapping_rows_for_production=False)
    decision = _admit(_observation(mapping_temporal_policy_revision_id=None,
                                   mapping_row_selection_hash=None), policy=lenient)
    assert decision.any_production_admissible is True
    assert lenient.policy_version != POLICY.policy_version


def test_a_null_in_the_mapping_refuses_both_directions():
    decision = _admit(_observation(
        mapping=mapping_observation(MAP, non_null_row_count=2)))
    assert CrosswalkAdmissionReason.MAPPING_NULL_ROWS.value in decision.reason_codes
    assert decision.any_production_admissible is False


def test_a_free_form_normalization_can_never_pass_the_closed_vocabulary():
    decision = _admit(_observation(source={"normalization_ids": ("strip_leading_zeros_v9",)}))
    assert (CrosswalkAdmissionReason.NORMALIZATION_NOT_ADMITTED.value
            in decision.reason_codes)
    assert decision.any_production_admissible is False


# ── hard refusals ───────────────────────────────────────────────────────────────────────────────

def test_multiple_active_mappings_for_one_endpoint_pair_refuse():
    """§6.6: V1 conflict behaviour is only `refuse_on_multiple`. No dedupe, no first-row pick."""
    decision = _admit(_observation(), active_mapping_count=2)
    assert (CrosswalkAdmissionReason.MULTIPLE_ACTIVE_MAPPINGS.value
            in decision.reason_codes)
    assert decision.display_tier is BridgeDisplayTier.REJECTED
    assert decision.forward.safety_status is SafetyStatus.UNSAFE
    assert decision.forward.sandbox_admissible is False


def test_an_observation_of_another_definition_revision_is_refused():
    decision = _admit(_observation(), crosswalk_definition_revision_id="cwd_" + "9" * 64)
    assert (CrosswalkAdmissionReason.OBSERVATION_NOT_FOR_DEFINITION_REVISION.value
            in decision.reason_codes)


def test_an_observation_from_another_scope_is_refused():
    other = replace(SCOPE, scope_id="crosswalk-production-scope")
    decision = _admit(_observation(), scope=other)
    assert CrosswalkAdmissionReason.OBSERVATION_SCOPE_MISMATCH.value in decision.reason_codes


def test_a_leg_that_is_no_longer_executable_refuses():
    decision = _admit(_observation(), legs_currently_executable=False)
    assert (CrosswalkAdmissionReason.LEG_NOT_CURRENTLY_EXECUTABLE.value
            in decision.reason_codes)
    assert decision.any_production_admissible is False


def test_a_superseded_definition_revision_refuses():
    decision = _admit(_observation(), definition_revision_current=False)
    assert (CrosswalkAdmissionReason.DEFINITION_REVISION_NOT_CURRENT.value
            in decision.reason_codes)


def test_a_measurement_of_another_mapping_binding_refuses():
    decision = _admit(_observation(), mapping_binding_revision_id="pbr_" + "7" * 64)
    assert CrosswalkAdmissionReason.MAPPING_BINDING_MISMATCH.value in decision.reason_codes


# ── leg pins, through their real owners ─────────────────────────────────────────────────────────

def _realization():
    from tests.featuregen.overlay.upload.test_bridge_assessment_contracts import (
        _executable_pair,
    )
    from tests.featuregen.overlay.upload.test_bridge_assessment_contracts import (
        _realization as build,
    )

    left, right = _executable_pair()
    return build(left, right)


def _current(revision, *, safety=SafetyStatus.DETERMINISTICALLY_VALIDATED):
    from featuregen.overlay.upload.bridge_assessment import LinkReviewStatus
    from featuregen.overlay.upload.bridge_realization import (
        BridgeRealizationCurrentV1,
        RealizationLifecycle,
    )

    return BridgeRealizationCurrentV1(
        revision.realization_id, revision.realization_revision_id, safety,
        LinkReviewStatus.UNREVIEWED, RealizationLifecycle.ACTIVE, 1)


def test_a_cross_catalog_leg_pins_its_governed_realization():
    revision = _realization()
    pin = leg_pin_from_realization(revision, _current(revision))
    assert pin.kind is JoinLegKind.CROSS_CATALOG
    assert pin.fact_keys == (revision.bridge_fact_key,)
    assert pin.realization_revision_ids == (revision.realization_revision_id,)
    assert pin.dependency_snapshot_ids == (revision.dependency_snapshot_id,)
    assert pin.from_binding_revision_id in pin.binding_revision_ids
    assert pin.read_set_hash


def test_a_demoted_realization_cannot_be_pinned():
    revision = _realization()
    with pytest.raises(ValueError, match="not currently"):
        leg_pin_from_realization(revision, _current(revision, safety=SafetyStatus.UNSAFE))


def test_a_production_leg_pin_demands_production_eligibility():
    revision = _realization()
    with pytest.raises(ValueError, match="production"):
        leg_pin_from_realization(
            revision, _current(revision, safety=SafetyStatus.UNASSESSED),
            require_production=True)


class _Step:
    def __init__(self, from_table, to_table, cardinality):
        self.from_table, self.to_table, self.cardinality = from_table, to_table, cardinality


class _Plan:
    steps = (_Step("bo_cib_account", "acct_xref", "N:1"),)
    outcome_kind = "operational"
    roles_used = ("data_scientist",)


def test_a_same_catalog_leg_pins_its_join_plan_and_never_a_realization():
    """`JoinLegPinV1` refuses fact keys on a same-catalog leg structurally; this is the producer
    that honours it."""
    pin = leg_pin_from_join_plan(
        _Plan(), from_dataset_ref="cib::public.bo_cib_account",
        to_dataset_ref="cib::public.acct_xref",
        from_binding_revision_id=CIB.binding_revision_id,
        to_binding_revision_id=MAP.binding_revision_id)
    assert pin.kind is JoinLegKind.SAME_CATALOG
    assert pin.fact_keys == () and pin.realization_revision_ids == ()
    assert pin.plan_hash and pin.read_set_hash and pin.plan_hash != pin.read_set_hash


def test_a_same_catalog_leg_cannot_be_pinned_from_a_bare_hash():
    with pytest.raises(ValueError, match="bare hash"):
        leg_pin_from_join_plan(
            object(), from_dataset_ref="cib::public.a", to_dataset_ref="cib::public.b",
            from_binding_revision_id="pbr_1", to_binding_revision_id="pbr_2")


# ── the first producer of the Task-10 execution shape ───────────────────────────────────────────

def _pins():
    source = leg_pin_from_join_plan(
        _Plan(), from_dataset_ref="cib::public.bo_cib_account",
        to_dataset_ref="cib::public.acct_xref",
        from_binding_revision_id=CIB.binding_revision_id,
        to_binding_revision_id=MAP.binding_revision_id)
    revision = _realization()
    target = leg_pin_from_realization(revision, _current(revision))
    return source, target


def test_admission_produces_a_pinned_execution_revision():
    observation = _observation()
    decision = _admit(observation)
    source_pin, target_pin = _pins()
    execution = admitted_crosswalk_execution(
        decision, source_leg=source_pin, target_leg=target_pin,
        mapping_binding_revision_id=MAP.binding_revision_id, scope=SCOPE,
        observation=observation, mapping_temporal_policy_revision_id="dtp_" + "b" * 64)
    assert execution.execution_revision_id.startswith("cwx_")
    assert execution.crosswalk_definition_revision_id == DEFINITION
    assert execution.composition_observation_revision_id == observation.observation_revision_id
    assert len(execution.leg_observation_revision_ids) == 2
    assert execution.safety_status is SafetyStatus.DETERMINISTICALLY_VALIDATED
    assert execution.execution_tier is ExecutionTier.SANDBOX


def test_an_unmeasured_crosswalk_cannot_produce_a_validated_execution_revision():
    """The Task-10 contract's own rule: two safe legs do not compose into a safe crosswalk."""
    decision = _admit()
    source_pin, target_pin = _pins()
    execution = admitted_crosswalk_execution(
        decision, source_leg=source_pin, target_leg=target_pin,
        mapping_binding_revision_id=MAP.binding_revision_id, scope=SCOPE, observation=None)
    assert execution.safety_status is SafetyStatus.UNASSESSED
    assert execution.composition_observation_revision_id is None
    assert execution.leg_observation_revision_ids == ()


def test_the_execution_revision_must_pin_the_observation_the_decision_was_taken_on():
    decision = _admit(_observation())
    source_pin, target_pin = _pins()
    with pytest.raises(ValueError, match="SAME composed observation"):
        admitted_crosswalk_execution(
            decision, source_leg=source_pin, target_leg=target_pin,
            mapping_binding_revision_id=MAP.binding_revision_id, scope=SCOPE,
            observation=_observation(composed_row_count=99))


def test_two_identical_leg_pins_are_still_refused_by_the_contract():
    decision = _admit(_observation())
    source_pin, _target = _pins()
    with pytest.raises(CrosswalkContractError):
        admitted_crosswalk_execution(
            decision, source_leg=source_pin, target_leg=source_pin,
            mapping_binding_revision_id=MAP.binding_revision_id, scope=SCOPE,
            observation=_observation())


def test_a_leg_pin_carries_the_full_eleven_field_shape():
    """The §6.6 amended form: a bare plan hash is not enough for replay or explainability."""
    source_pin, target_pin = _pins()
    for pin in (source_pin, target_pin):
        payload = pin.identity_payload()
        assert set(payload) == {
            "kind", "plan_hash", "from_dataset_ref", "to_dataset_ref",
            "from_binding_revision_id", "to_binding_revision_id", "read_set_hash",
            "binding_revision_ids", "fact_keys", "realization_revision_ids",
            "dependency_snapshot_ids", "predicate_content_hashes"}
        assert isinstance(pin, JoinLegPinV1)
