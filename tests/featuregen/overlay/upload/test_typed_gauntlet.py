"""SE-9 — the typed gauntlet: the tri-state from ACTUAL unmet conditions, typed per role."""
from __future__ import annotations

from featuregen.overlay.upload import semantic_eligibility_reasons as R
from featuregen.overlay.upload.feature_planning_contracts import (
    RequiredOperandV1,
    planning_request_from_user_definition,
    planning_request_hash,
)
from featuregen.overlay.upload.recipe_operand_policy import OperandBindingVerdictV1
from featuregen.overlay.upload.recipe_planning_lens import V2RecipeCandidateV1
from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id
from featuregen.overlay.upload.typed_gauntlet import (
    TYPED_GAUNTLET_VERSION,
    validate_candidate,
)

EXEMPLAR = v2_recipe_by_id("customer_activity_recency")


def _request(operands):
    return planning_request_from_user_definition(
        definition_id="user:gauntlet_probe", primary_objective=EXEMPLAR.primary_objective,
        output=EXEMPLAR.output, operands=operands,
        source_grain="transaction", output_grain="customer",
        temporal=EXEMPLAR.temporal, content_hash="probehash")


def _candidate(operands, verdicts, *, binding_state="bound", temporal_blocker=""):
    request = _request(operands)
    return V2RecipeCandidateV1(
        recipe_id="user:gauntlet_probe", relationship="primary",
        planning_request=request, planning_request_hash=planning_request_hash(request),
        recipe_revision_hash="rev", verdicts=tuple(verdicts),
        binding_state=binding_state, readiness="CONCEPTUAL_ONLY",
        temporal_pit_text="" if temporal_blocker else "pit", temporal_blocker=temporal_blocker,
        review_current=False, review_missing_roles=(), eligibility={})


OPERANDS = (
    RequiredOperandV1(role="who", concept="customer_id", operand_class="entity_key"),
    RequiredOperandV1(role="when", concept="event_timestamp",
                      operand_class="event_timestamp"),
)
BOUND = (
    OperandBindingVerdictV1(role="who", status="bound",
                            selected_ref="public.events.customer_id"),
    OperandBindingVerdictV1(role="when", status="bound",
                            selected_ref="public.events.event_ts"),
)


def test_typed_requirements_by_role_never_everything_as_a_measure():
    """The defect this task exists to kill: the key gets a UNIQUENESS check, the event anchor
    gets a HISTORY check — and neither is checked as a quantity."""
    validation = validate_candidate(_candidate(OPERANDS, BOUND))
    assert validation.status == "needs_external_validation"
    by_code = {r.code: r for r in validation.requirements}
    assert by_code[R.IDENTIFIER_UNIQUENESS].object_ref == "public.events.customer_id"
    assert by_code[R.EVENT_HISTORY_VERIFICATION].object_ref == "public.events.event_ts"
    assert by_code[R.EVENT_HISTORY_VERIFICATION].family == "needs_data_check"
    assert validation.gauntlet_version == TYPED_GAUNTLET_VERSION
    assert validation.policy_content_hash                     # reproducible evidence


def test_a_bound_target_is_refused_at_the_physical_level():
    validation = validate_candidate(
        _candidate(OPERANDS, BOUND), target_ref="public.events.customer_id")
    assert validation.status == "refused"
    assert validation.refusals[0]["code"] == R.TARGET_LEAKAGE_BLOCKED
    assert validation.refusals[0]["object_ref"] == "public.events.customer_id"


def test_floor_codes_on_bound_verdicts_become_confirmation_requirements():
    bound_with_floor = (
        OperandBindingVerdictV1(role="who", status="bound",
                                selected_ref="public.events.customer_id",
                                reason_codes=(R.PROPOSED_METADATA_ONLY,),
                                resolution="confirm the AI-proposed concept"),
        BOUND[1],
    )
    validation = validate_candidate(_candidate(OPERANDS, bound_with_floor))
    assert validation.status == "needs_external_validation"
    floor = next(r for r in validation.requirements if r.code == R.PROPOSED_METADATA_ONLY)
    assert floor.family == "undecided"
    assert "confirm" in floor.detail


def test_a_blocked_binding_is_refused_with_its_own_codes():
    blocked = (
        OperandBindingVerdictV1(role="who", status="blocked",
                                tied_refs=("public.events.customer_id",),
                                reason_codes=(R.IDENTIFIER_NOT_A_MEASURE,)),
        BOUND[1],
    )
    validation = validate_candidate(
        _candidate(OPERANDS, blocked, binding_state="blocked"))
    assert validation.status == "refused"
    assert validation.refusals[0]["code"] == R.IDENTIFIER_NOT_A_MEASURE


def test_unbindable_states_are_not_overwritten_by_validation():
    validation = validate_candidate(
        _candidate(OPERANDS, BOUND, binding_state="ambiguous"))
    assert validation.status == "not_bindable"
    assert validation.requirements == () and validation.refusals == ()


def test_a_temporal_blocker_is_named_setup_work():
    validation = validate_candidate(
        _candidate(OPERANDS, BOUND, temporal_blocker="window parameter undeclared"))
    blocker = next(r for r in validation.requirements
                   if r.code == R.TEMPORAL_POLICY_UNRESOLVED)
    assert blocker.family == "needs_setup"
    assert "window parameter undeclared" in blocker.detail


def test_design_checked_is_reachable_and_still_not_proof_of_usefulness():
    """A bound as-of dimension candidate with nothing outstanding — the honest green, which
    the module docstring is careful to say is NOT predictive evidence."""
    operands = (RequiredOperandV1(role="seg", concept="segment", operand_class="dimension"),)
    verdicts = (OperandBindingVerdictV1(role="seg", status="bound",
                                        selected_ref="public.customers.segment"),)
    validation = validate_candidate(_candidate(operands, verdicts))
    assert validation.status == "design_checked"
    assert validation.requirements == ()


def test_a_ratio_shaped_output_without_its_zero_denominator_policy_is_named_setup(monkeypatch):
    """C3: a ratio DIVIDES — what a zero denominator returns is part of the authored design,
    and its absence is a named requirement, never a silent gap."""
    from dataclasses import replace

    bare_ratio = replace(EXEMPLAR.output, unit_kind="rate", zero_denominator_policy="")
    request = planning_request_from_user_definition(
        definition_id="user:ratio_probe", primary_objective=EXEMPLAR.primary_objective,
        output=bare_ratio, operands=OPERANDS,
        source_grain="transaction", output_grain="customer",
        temporal=EXEMPLAR.temporal, content_hash="ratiohash")
    from featuregen.overlay.upload.feature_planning_contracts import planning_request_hash
    candidate = V2RecipeCandidateV1(
        recipe_id="user:ratio_probe", relationship="primary",
        planning_request=request, planning_request_hash=planning_request_hash(request),
        recipe_revision_hash="rev", verdicts=BOUND,
        binding_state="bound", readiness="CONCEPTUAL_ONLY",
        temporal_pit_text="pit", temporal_blocker="",
        review_current=False, review_missing_roles=(), eligibility={})
    validation = validate_candidate(candidate)
    codes = {r.code for r in validation.requirements}
    assert R.OUTPUT_POLICY_INCOMPLETE in codes

    authored = replace(bare_ratio, zero_denominator_policy="a zero denominator returns null")
    request2 = planning_request_from_user_definition(
        definition_id="user:ratio_probe2", primary_objective=EXEMPLAR.primary_objective,
        output=authored, operands=OPERANDS,
        source_grain="transaction", output_grain="customer",
        temporal=EXEMPLAR.temporal, content_hash="ratiohash2")
    candidate2 = V2RecipeCandidateV1(
        recipe_id="user:ratio_probe2", relationship="primary",
        planning_request=request2, planning_request_hash=planning_request_hash(request2),
        recipe_revision_hash="rev", verdicts=BOUND,
        binding_state="bound", readiness="CONCEPTUAL_ONLY",
        temporal_pit_text="pit", temporal_blocker="",
        review_current=False, review_missing_roles=(), eligibility={})
    codes2 = {r.code for r in validate_candidate(candidate2).requirements}
    assert R.OUTPUT_POLICY_INCOMPLETE not in codes2
