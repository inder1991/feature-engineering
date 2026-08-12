"""SE-7 — the enforced projection: semantic verdicts become Gate-1 carriers, honestly."""
from __future__ import annotations

from featuregen.overlay.upload import semantic_eligibility_reasons as R
from featuregen.overlay.upload.candidate_assembly import assemble_candidates
from featuregen.overlay.upload.feature_planning_contracts import (
    RequiredOperandV1,
    planning_request_from_user_definition,
)
from featuregen.overlay.upload.recipe_operand_policy import OperandBindingVerdictV1
from featuregen.overlay.upload.recipe_planning_lens import V2RecipeCandidateV1
from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id
from featuregen.overlay.upload.semantic_eligibility import (
    SEMANTIC_AUTHORITY_POLICY_VERSION,
    OperandEligibilityVerdictV1,
    authority_matrix_hash,
)
from featuregen.overlay.upload.semantic_projection import project_assembled_set

EXEMPLAR = v2_recipe_by_id("customer_activity_recency")

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


def _request(definition_id="recipe:proj_probe"):
    return planning_request_from_user_definition(
        definition_id=definition_id, primary_objective=EXEMPLAR.primary_objective,
        output=EXEMPLAR.output, operands=OPERANDS,
        source_grain="transaction", output_grain="customer",
        temporal=EXEMPLAR.temporal, content_hash=f"hash:{definition_id}")


def _eligibility_verdict(role, ref, authority="human/confirmed"):
    return OperandEligibilityVerdictV1(
        operand_role=role, object_ref=ref, status="eligible", reason_codes=(),
        primary_reason_code=None, primary_family=None,
        authority_floor_required="declared", authority_observed=authority,
        missing_checks=(), resolution="",
        policy_version=SEMANTIC_AUTHORITY_POLICY_VERSION,
        policy_content_hash=authority_matrix_hash())


def _candidate(definition_id="recipe:proj_probe", verdicts=BOUND, *,
               binding_state="bound", temporal_blocker="", eligibility=None):
    request = _request(definition_id)
    if eligibility is None:
        eligibility = {(v.role, v.selected_ref): _eligibility_verdict(v.role, v.selected_ref)
                       for v in verdicts if v.selected_ref}
    return V2RecipeCandidateV1(
        recipe_id=definition_id, relationship="primary",
        planning_request=request, planning_request_hash="prh",
        recipe_revision_hash="rev", verdicts=tuple(verdicts),
        binding_state=binding_state, readiness="CONCEPTUAL_ONLY",
        temporal_pit_text="" if temporal_blocker else "pit",
        temporal_blocker=temporal_blocker,
        review_current=False, review_missing_roles=(), eligibility=eligibility)


def _project(candidates, *, target_ref=None):
    return project_assembled_set(assemble_candidates(candidates),
                                 catalog_source="bank", target_ref=target_ref)


def test_a_served_idea_carries_recipe_provenance_and_typed_requirements():
    """The projection is a translation: gauntlet requirements land through their EXACT legacy
    equivalents, with the semantic origin named in the detail text."""
    result = _project([_candidate()])
    assert not result.rejections
    idea = result.ideas[0]
    assert idea.generation_source == "recipe"
    assert idea.recipe_id == "recipe:proj_probe"
    assert idea.validation_status == "NEEDS_EXTERNAL_VALIDATION"
    by_code = {req.code: req for req in idea.requirements}
    grain = by_code["GRAIN_IS_UNIQUE"]                        # identifier uniqueness IS this check
    assert grain.operand == ("bank", "public.events.customer_id")
    assert R.IDENTIFIER_UNIQUENESS in grain.detail
    temporal = by_code["TEMPORAL_IS_POPULATED"]               # event-history depth IS this check
    assert temporal.operand == ("bank", "public.events.event_ts")
    assert R.EVENT_HISTORY_VERIFICATION in temporal.detail
    assert idea.derives_pairs == (("bank", "public.events.customer_id"),
                                  ("bank", "public.events.event_ts"))
    assert idea.operand_roles == (("public.events.customer_id", "who"),
                                  ("public.events.event_ts", "when"))
    assert result.grounded_ids == frozenset({"recipe:proj_probe"})
    assert result.binding_by_id == {"recipe:proj_probe": "exact"}


def test_floor_codes_become_confirmation_required_never_external_checks():
    """An authority floor is Gate-1 confirmation work — the RoleBinding carrier's own flag —
    not a data check somebody runs against the warehouse."""
    floored = (
        OperandBindingVerdictV1(role="who", status="bound",
                                selected_ref="public.events.customer_id",
                                reason_codes=(R.PROPOSED_METADATA_ONLY,),
                                resolution="confirm the AI-proposed concept"),
        BOUND[1],
    )
    eligibility = {
        ("who", "public.events.customer_id"): _eligibility_verdict(
            "who", "public.events.customer_id", authority="llm/proposed"),
        ("when", "public.events.event_ts"): _eligibility_verdict(
            "when", "public.events.event_ts"),
    }
    result = _project([_candidate(verdicts=floored, eligibility=eligibility)])
    idea = result.ideas[0]
    who = next(b for b in idea.input_role_bindings if b.role == "who")
    assert who.confirmation_required is True
    assert who.authority == "llm/proposed"                    # the measured pin, not a story
    when = next(b for b in idea.input_role_bindings if b.role == "when")
    assert when.confirmation_required is False
    assert "PROPOSED_METADATA_ONLY" not in {req.code for req in idea.requirements}
    assert result.binding_by_id == {"recipe:proj_probe": "acceptable"}


def test_a_bound_target_is_refused_never_served():
    result = _project([_candidate()], target_ref="public.events.customer_id")
    assert not result.ideas
    assert result.rejected_ids["recipe:proj_probe"] == (R.TARGET_LEAKAGE_BLOCKED,)
    assert result.rejections[0]["code"] == R.TARGET_LEAKAGE_BLOCKED


def test_an_uncompiled_temporal_contract_is_a_named_rejection():
    result = _project([_candidate(temporal_blocker="window parameter undeclared")])
    assert not result.ideas
    assert result.rejections[0]["reason"] == "window parameter undeclared"
    assert result.rejected_ids["recipe:proj_probe"] == (R.TEMPORAL_POLICY_UNRESOLVED,)


def test_actionable_candidates_reject_with_their_named_resolution():
    blocked = (
        OperandBindingVerdictV1(role="who", status="blocked",
                                tied_refs=("public.events.customer_id",),
                                reason_codes=(R.ECONOMIC_ROLE_UNPROVEN,),
                                resolution="a human confirms the economic role"),
        BOUND[1],
    )
    result = _project([_candidate(verdicts=blocked, binding_state="blocked")])
    assert not result.ideas
    assert result.rejections[0]["reason"] == "a human confirms the economic role"
    assert R.ECONOMIC_ROLE_UNPROVEN in result.rejected_ids["recipe:proj_probe"]
