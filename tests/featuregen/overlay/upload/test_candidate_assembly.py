"""SE-10 — assembly: one meaning one card, primacy by authorship+review, a designed order."""
from __future__ import annotations

import random
from dataclasses import replace

from featuregen.overlay.upload import semantic_eligibility_reasons as R
from featuregen.overlay.upload.candidate_assembly import (
    ASSEMBLY_VERSION,
    assemble_candidates,
    semantic_signature,
)
from featuregen.overlay.upload.feature_planning_contracts import (
    RequiredOperandV1,
    planning_request_from_user_definition,
)
from featuregen.overlay.upload.recipe_operand_policy import OperandBindingVerdictV1
from featuregen.overlay.upload.recipe_planning_lens import V2RecipeCandidateV1
from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id

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


def _request(definition_id="recipe:twin_probe", *, origin="recipe_v2", objective=None):
    request = planning_request_from_user_definition(
        definition_id=definition_id,
        primary_objective=objective or EXEMPLAR.primary_objective,
        output=EXEMPLAR.output, operands=OPERANDS,
        source_grain="transaction", output_grain="customer",
        temporal=EXEMPLAR.temporal, content_hash=f"hash:{definition_id}")
    return replace(request, origin=origin)


def _candidate(request, verdicts=BOUND, *, binding_state="bound", relationship="primary",
               readiness="CONCEPTUAL_ONLY", review_current=False):
    return V2RecipeCandidateV1(
        recipe_id=request.source_definition_id, relationship=relationship,
        planning_request=request, planning_request_hash="prh",
        recipe_revision_hash="rev", verdicts=tuple(verdicts),
        binding_state=binding_state, readiness=readiness,
        temporal_pit_text="pit", temporal_blocker="",
        review_current=review_current, review_missing_roles=(), eligibility={})


# ── the signature: meaning + binding, never origin or names ─────────────────────────────────────

def test_a_recipe_and_an_llm_twin_share_a_signature_and_merge_into_one_card():
    recipe = _candidate(_request("recipe:activity", origin="recipe_v2"))
    llm = _candidate(_request("intent:activity_twin", origin="llm_intent"))
    assert semantic_signature(recipe) == semantic_signature(llm)

    result = assemble_candidates([llm, recipe])
    assert len(result.ranked) == 1 and not result.actionable
    card = result.ranked[0]
    assert card.candidate.planning_request.origin == "recipe_v2"  # authored policies front
    assert [c.origin for c in card.corroborations] == ["llm_intent"]


def test_matching_physical_refs_with_different_meaning_never_merge():
    """Step 3's law: refs and names are not identity — the objective/output meaning is."""
    from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES

    different = next(r.primary_objective for r in V2_RECIPES
                     if r.primary_objective != EXEMPLAR.primary_objective)
    activity = _candidate(_request("recipe:recency"))
    other = _candidate(_request("recipe:other_meaning", objective=different))
    assert semantic_signature(activity) != semantic_signature(other)
    assert len(assemble_candidates([activity, other]).ranked) == 2


def test_a_different_bound_column_is_a_different_candidate():
    a = _candidate(_request("recipe:a"))
    b = _candidate(_request("recipe:b"), verdicts=(
        BOUND[0],
        OperandBindingVerdictV1(role="when", status="bound",
                                selected_ref="public.events.settled_ts")))
    assert semantic_signature(a) != semantic_signature(b)


def test_parameter_values_are_identity_one_request_is_one_variant():
    base = _request("recipe:w90")
    ninety = _candidate(replace(base, parameter_values=(("window_days", 90),)))
    thirty = _candidate(replace(base, parameter_values=(("window_days", 30),)))
    assert semantic_signature(ninety) != semantic_signature(thirty)


# ── primacy inside a merged group ───────────────────────────────────────────────────────────────

def test_current_review_approval_outranks_an_unreviewed_recipe_twin():
    reviewed = _candidate(_request("recipe:reviewed"), review_current=True)
    unreviewed = _candidate(_request("recipe:unreviewed"), review_current=False)
    card = assemble_candidates([unreviewed, reviewed]).ranked[0]
    assert card.candidate.recipe_id == "recipe:reviewed"
    twin = card.corroborations[0]
    assert twin.source_definition_id == "recipe:unreviewed"
    assert twin.review_current is False                       # honestly labeled, kept


# ── the order: composite key, explainable position by position ──────────────────────────────────

def test_the_order_is_the_designed_composite_key_not_a_score():
    reviewed_ready = _candidate(_request("recipe:rr"), review_current=True,
                                readiness="FORMULA_VALIDATED")
    reviewed_conceptual = _candidate(
        replace(_request("recipe:rc"), parameter_values=(("window_days", 30),)),
        review_current=True)
    unreviewed_supporting = _candidate(
        replace(_request("recipe:us"), parameter_values=(("window_days", 60),)),
        relationship="supporting")
    result = assemble_candidates(
        [unreviewed_supporting, reviewed_conceptual, reviewed_ready])
    assert [a.candidate.recipe_id for a in result.ranked] == [
        "recipe:rr", "recipe:rc", "recipe:us"]
    assert "review_validity" in result.order_basis            # the basis is STATED
    assert result.assembly_version == ASSEMBLY_VERSION


def test_undecided_work_is_actionable_never_ranked_low():
    bound = _candidate(_request("recipe:bound"))
    blocked = _candidate(
        replace(_request("recipe:blocked"), parameter_values=(("window_days", 7),)),
        verdicts=(OperandBindingVerdictV1(
            role="who", status="blocked", tied_refs=("public.events.customer_id",),
            reason_codes=(R.ECONOMIC_ROLE_UNPROVEN,),
            resolution="a human confirms the economic role"), BOUND[1]),
        binding_state="blocked")
    result = assemble_candidates([blocked, bound])
    assert [a.candidate.recipe_id for a in result.ranked] == ["recipe:bound"]
    assert [a.candidate.recipe_id for a in result.actionable] == ["recipe:blocked"]
    kept = result.actionable[0].candidate.verdicts[0]
    assert kept.resolution                                    # the named action survives


def test_assembly_is_deterministic_under_input_shuffle():
    pool = [
        _candidate(_request("recipe:reviewed"), review_current=True),
        _candidate(_request("intent:twin", origin="llm_intent")),
        _candidate(replace(_request("recipe:other"),
                           parameter_values=(("window_days", 30),))),
        _candidate(replace(_request("recipe:gap"),
                           parameter_values=(("window_days", 7),)),
                   binding_state="missing",
                   verdicts=(BOUND[0], OperandBindingVerdictV1(
                       role="when", status="unresolved"))),
    ]
    baseline = assemble_candidates(list(pool))
    for seed in (3, 11, 42):
        random.Random(seed).shuffle(pool)
        again = assemble_candidates(list(pool))
        assert [a.signature for a in again.ranked] == [a.signature for a in baseline.ranked]
        assert [a.signature for a in again.actionable] \
            == [a.signature for a in baseline.actionable]
        assert [c.source_definition_id for a in again.ranked for c in a.corroborations] \
            == [c.source_definition_id for a in baseline.ranked for c in a.corroborations]


# ── D2: the candidate seen is the candidate governed — mechanism IN the identity ───────────────

def test_two_formulas_are_two_cards_and_identical_semantics_still_merge():
    """D2's acceptance: candidates differing ONLY in formula expectation render as TWO
    cards. The cross-origin merge stays possible exactly where executable semantics are
    IDENTICAL — which the planning contract makes well-defined (deterministic always carries
    its formula, conceptual never does), so pinned-vs-unpinned "twins" cannot exist."""
    from dataclasses import replace

    from featuregen.overlay.upload.feature_planning_contracts import FormulaReferenceV2

    def _pinned(definition_id, ref):
        # ONE replace: the dataclass validates each construction, and a deterministic
        # request without its formula (or vice versa) refuses — atomicity is the contract.
        return _candidate(replace(
            _request(definition_id, origin="recipe_v2"),
            computation_kind="deterministic_formula", conceptual_reason="",
            formula=FormulaReferenceV2(expectation_ref=ref,
                                       formula_schema_version="formula-v2",
                                       result_class="sum")))

    recipe_a = _pinned("recipe:alpha", "retail:alpha")
    recipe_b = _pinned("recipe:beta", "retail:beta")
    intent = _candidate(_request("intent:twin", origin="llm_intent"))   # formula=None

    # Two pinned mechanisms: TWO visibly distinct cards even though everything else is
    # identical — no shared card ever hides different formulas behind secret option ids.
    assembled = assemble_candidates([recipe_a, recipe_b])
    assert len(assembled.ranked) + len(assembled.actionable) == 2

    # A conceptual candidate is a DIFFERENT computation from a deterministic one — even the
    # same meaning+binding never merges across that line (three cards, not two).
    assembled = assemble_candidates([recipe_a, recipe_b, intent])
    assert len(assembled.ranked) + len(assembled.actionable) == 3

    # And the cross-origin merge the assembly exists for is untouched: identical conceptual
    # twins (both formula-less by contract) still fold into one corroborated card.
    conceptual_recipe = _candidate(_request("recipe:twin", origin="recipe_v2"))
    conceptual_intent = _candidate(_request("intent:twin2", origin="llm_intent"))
    assembled = assemble_candidates([conceptual_recipe, conceptual_intent])
    total = list(assembled.ranked) + list(assembled.actionable)
    assert len(total) == 1
    assert total[0].candidate.planning_request.origin == "recipe_v2"   # primacy holds
    assert any(c.origin == "llm_intent" for c in total[0].corroborations)
