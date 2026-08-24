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
    # B4: origin is a FACT — this fixture's request has origin user_definition, so the card
    # says user_defined and carries NO recipe badge; the origin-neutral id is what survives.
    assert idea.generation_source == "user_defined"
    assert idea.recipe_id is None
    assert idea.source_definition_id == "recipe:proj_probe"
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


def test_an_unbound_required_operand_is_setup_work_never_a_card_and_never_hidden():
    """A3's law, narrowed by T2 and otherwise intact: undecided work is still VISIBLE and still
    carries its named resolution, and its codes still ride rejected_ids for the disposition lens
    — but a candidate whose REQUIRED operand never bound is no longer projected as a card,
    because a card is an offer to compute something and this one cannot be computed."""
    blocked = (
        OperandBindingVerdictV1(role="who", status="blocked",
                                tied_refs=("public.events.customer_id",),
                                reason_codes=(R.ECONOMIC_ROLE_UNPROVEN,),
                                resolution="a human confirms the economic role"),
        BOUND[1],
    )
    result = _project([_candidate(verdicts=blocked, binding_state="blocked")])
    assert not result.ideas                                   # not RECOMMENDED
    assert not result.rejections                              # and not HIDDEN either
    # T2: "who" is a REQUIRED operand that never bound, so this candidate is no longer an
    # actionable CARD — it is setup work, carrying the same named resolution it always did.
    assert not result.actionable_ideas
    (entry,) = result.needs_setup
    assert entry.source_definition_id == "recipe:proj_probe"
    assert entry.missing_concepts == ("customer_id",)
    (unbound,) = entry.unbound_operands
    assert unbound.role == "who"
    assert unbound.status == "blocked"                        # the honest state, on the entry
    assert unbound.resolution == "a human confirms the economic role"
    assert R.ECONOMIC_ROLE_UNPROVEN in result.rejected_ids["recipe:proj_probe"]


# ── T2 — serve no card whose REQUIRED operands are unbound ─────────────────────────────────────

CIB_SOURCE = "cib_probe"
#: The audit's own shape: the population key, the clock and the direction flag are all present,
#: and the MONETARY operand has nothing to bind to. Every amount-requiring recipe therefore binds
#: MOST of its operands — which is exactly why the run served 135 cards that could never compute.
FLOW = v2_recipe_by_id("net_transaction_flow")


def _cib_catalog(db) -> None:
    from featuregen.overlay.upload.canonical import CanonicalRow
    from featuregen.overlay.upload.enrich import content_hash
    from featuregen.overlay.upload.graph import build_graph

    rows = [
        (CanonicalRow(CIB_SOURCE, "transactions", "acct_ref", "integer", is_grain=True,
                      entity="Account", definition="the posting account"), "account_id"),
        (CanonicalRow(CIB_SOURCE, "transactions", "dc_flag", "text",
                      definition="debit/credit indicator"), "debit_credit_indicator"),
        (CanonicalRow(CIB_SOURCE, "transactions", "booked_ts", "timestamp",
                      definition="when the transaction was booked"), "event_timestamp"),
    ]
    build_graph(db, CIB_SOURCE, [r for r, _ in rows],
                concepts={content_hash(r): c for r, c in rows})


def _cib_projection(db):
    from featuregen.overlay.upload.recipe_planning_lens import v2_recipe_candidates
    from featuregen.overlay.upload.taxonomy.applicability import ConfirmedScope

    candidates = v2_recipe_candidates(
        db, catalog_source=CIB_SOURCE,
        scope=ConfirmedScope(primary=FLOW.primary_objective))
    return candidates, project_assembled_set(assemble_candidates(list(candidates)),
                                             catalog_source=CIB_SOURCE)


def test_the_cib_arrangement_serves_no_card_and_names_the_missing_concepts(db):
    """THE 135-noise-card failure shape, at the seam that produced it.

    The live AML run planned over a catalog with no monetary column and served 135 cards whose
    REQUIRED operands could never bind. Here the same arrangement in miniature: three of
    `net_transaction_flow`'s four operands bind, the measure does not, and the projection serves
    NOTHING — the candidate goes to the typed `needs_setup` lane naming the concept the catalog
    would have to carry."""
    _cib_catalog(db)
    candidates, result = _cib_projection(db)

    # The arrangement really is the audit's: most operands DID bind, which is what made the
    # served card look real. Without this the pin could pass on an empty catalog.
    bound_roles = {v.role for c in candidates for v in c.verdicts if v.status == "bound"}
    assert bound_roles == {"account", "direction", "event_ts"}

    assert result.ideas == []
    assert result.actionable_ideas == []
    assert result.needs_setup, "the candidates are held out BY NAME, never silently dropped"
    for entry in result.needs_setup:
        assert entry.unbound_operands, "an entry in this lane names what is unbound"
        assert entry.catalog_source == CIB_SOURCE
        assert entry.name
    by_definition = {e.source_definition_id: e for e in result.needs_setup}
    flow = by_definition[f"{FLOW.recipe_id}@window=30"]
    assert flow.recipe_id == FLOW.recipe_id
    assert flow.missing_concepts == ("monetary_flow",)
    (unbound,) = flow.unbound_operands
    assert (unbound.role, unbound.operand_class) == ("amount", "measure")
    assert unbound.status == "unresolved"
    assert "REQUIRED_OPERAND_MISSING" in unbound.reason_codes
    assert unbound.resolution                       # the binder's own named remedy, verbatim


def test_the_needs_setup_lane_names_no_catalog_it_cannot_see(db):
    """Honest absence, deliberately: the projection is handed one assembled set and one catalog
    name — it holds no cross-catalog concept inventory and takes no connection — so the lane says
    WHICH concepts are missing and never guesses which other catalog carries them. Naming the
    other catalog is T5's refusal, which plans with the inventory in hand."""
    _cib_catalog(db)
    _candidates, result = _cib_projection(db)
    entry = result.needs_setup[0]
    assert entry.catalog_source == CIB_SOURCE       # the catalog it was PLANNED over, no other
    assert not hasattr(entry, "satisfying_catalog_source")
    assert not hasattr(entry, "available_in")


# ── T3 — the badge tells the corpus's truth ────────────────────────────────────────────────────

def _clean_candidate(*, readiness: str, recipe_id: str = "recipe:proj_probe",
                     origin_recipe=None):
    """A candidate the typed gauntlet passes with NOTHING outstanding — one measure operand, a
    folded dataset story, a non-ratio output — so `verification` is decided by READINESS alone
    and not by an incidental requirement."""
    from featuregen.overlay.upload.recipe_contract_v2 import OutputSpecV2
    from featuregen.overlay.upload.recipe_planning_lens import DatasetStoryV1

    operands = (RequiredOperandV1(role="amount", concept="monetary_flow",
                                  operand_class="measure"),)
    output = OutputSpecV2(
        output_id="probe", display_label="Probe", output_type="integer",
        additivity="additive", unit_kind="count",
        null_input_policy="nulls are zero", empty_population_policy="empty is zero")
    if origin_recipe is not None:
        from featuregen.overlay.upload.feature_planning_contracts import (
            planning_request_from_recipe,
        )

        request = planning_request_from_recipe(origin_recipe)
    else:
        request = planning_request_from_user_definition(
            definition_id=recipe_id, primary_objective=EXEMPLAR.primary_objective,
            output=output, operands=operands, source_grain="transaction",
            output_grain="customer", temporal=EXEMPLAR.temporal,
            content_hash=f"hash:{recipe_id}")
    verdicts = tuple(
        OperandBindingVerdictV1(role=op.role, status="bound",
                                selected_ref=f"public.txns.{op.role}")
        for op in request.operands)
    return V2RecipeCandidateV1(
        recipe_id=recipe_id, relationship="primary", planning_request=request,
        planning_request_hash="prh", recipe_revision_hash="rev", verdicts=verdicts,
        binding_state="bound", readiness=readiness, temporal_pit_text="pit",
        temporal_blocker="", review_current=False, review_missing_roles=(),
        eligibility={}, dataset_story=DatasetStoryV1(
            population_ref="txns", population_basis="declared_grain",
            dataset_tables=("txns",), cross_dataset=False, codes=()))


def test_a_gauntlet_pass_over_a_blocked_readiness_is_never_design_checked():
    """The 132-card defect, isolated: the gauntlet says design_checked and the recipe says
    FORMULA_BLOCKED. The badge is the WEAKER of the two, always."""
    blocked = _project([_clean_candidate(readiness="FORMULA_BLOCKED")]).ideas[0]
    assert blocked.validation_status == "DESIGN_CHECKED"      # the gauntlet's own axis, unmoved
    assert blocked.verification == "UNVERIFIED"

    authorable = _project([_clean_candidate(readiness="FORMULA_AUTHORABLE")]).ideas[0]
    assert authorable.verification == "DESIGN-CHECKED"        # still REACHABLE, honestly earned


def test_the_readiness_to_verification_map_is_total_and_never_promotes():
    """The class-killer the run needed: every rung of the corpus's OWN ladder, against every
    gauntlet status. A rung that cannot execute cannot wear the stamp, whatever the gauntlet
    said; and no gauntlet status below design_checked earns it either."""
    from featuregen.overlay.upload.recipe_readiness import READINESS_LADDER
    from featuregen.overlay.upload.semantic_projection import (
        EXECUTABLE_READINESS,
        card_verification,
    )

    assert set(EXECUTABLE_READINESS) <= set(READINESS_LADDER)
    stamped = {
        rung for rung in READINESS_LADDER
        if card_verification("design_checked", (rung,)) == "DESIGN-CHECKED"}
    assert stamped == set(EXECUTABLE_READINESS)
    assert "FORMULA_BLOCKED" not in stamped
    assert "CONCEPTUAL_ONLY" not in stamped
    assert "RETIRED" not in stamped
    for status in ("refused", "not_bindable", "needs_external_validation"):
        for rung in READINESS_LADDER:
            assert card_verification(status, (rung,)) == "UNVERIFIED", (status, rung)
    # Two statements, the weaker wins — this is what makes a spoofed candidate rung unable to
    # promote a blocked recipe, and a blocked candidate unable to be rescued by a clean recipe.
    assert card_verification(
        "design_checked", ("MATERIALIZATION_READY", "FORMULA_BLOCKED")) == "UNVERIFIED"


def test_the_corpus_row_is_read_not_just_the_candidate_s_own_rung():
    """A candidate may carry any rung its lens folded; the REGISTRY's word about the recipe is
    read too, so a fold that lost the blocker cannot re-mint the badge."""
    from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES

    corpus_blocked = next(r for r in V2_RECIPES if r.readiness == "FORMULA_BLOCKED")
    spoofed = _clean_candidate(readiness="MATERIALIZATION_READY",
                               recipe_id=corpus_blocked.recipe_id,
                               origin_recipe=corpus_blocked)
    idea = _project([spoofed]).ideas[0]
    assert idea.recipe_id == corpus_blocked.recipe_id
    assert idea.verification == "UNVERIFIED"


def test_every_blocked_recipe_in_the_corpus_maps_to_unverified():
    """The run's 132 blocked cards were 132 instances of ONE rule. Pinned over the whole corpus
    rather than over that run: no FORMULA_BLOCKED recipe can wear DESIGN-CHECKED, and the three
    that CAN are the three the registry actually marks authorable."""
    from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES
    from featuregen.overlay.upload.semantic_projection import card_verification

    stamped = {r.recipe_id for r in V2_RECIPES
               if card_verification("design_checked", (r.readiness,)) == "DESIGN-CHECKED"}
    assert stamped == {r.recipe_id for r in V2_RECIPES
                       if r.readiness == "FORMULA_AUTHORABLE"}
    assert len(stamped) == 3
    assert not any(r.readiness == "FORMULA_BLOCKED" for r in V2_RECIPES
                   if r.recipe_id in stamped)


# ── T4 — the projection serves the corpus's riches ─────────────────────────────────────────────

def _executable_recipes():
    from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES

    return [r for r in V2_RECIPES if r.computation_kind == "deterministic_formula"]


def test_every_executable_recipe_serves_a_rationale_byte_derived_from_its_definition():
    """Total over the corpus, because the defect was total: every served card's rationale used
    to be `conceptual_reason`, which the contract FORBIDS on an executable recipe (0 populated).
    The recipe's own business_definition — populated on all of them — is what the card says."""
    recipes = _executable_recipes()
    assert len(recipes) == 298                       # the corpus's executable half, measured
    empty, unrooted = [], []
    for recipe in recipes:
        candidate = _clean_candidate(readiness=recipe.readiness,
                                     recipe_id=recipe.recipe_id, origin_recipe=recipe)
        idea = _project([candidate]).ideas[0]
        if not idea.rationale.strip():
            empty.append(recipe.recipe_id)
        elif not idea.rationale.startswith(recipe.business_definition):
            unrooted.append(recipe.recipe_id)
        assert recipe.decision_context in idea.rationale, recipe.recipe_id
        assert not recipe.conceptual_reason           # the contract's own rule, restated
    assert empty == []
    assert unrooted == []


def test_a_served_card_carries_the_declared_aggregation_and_the_typed_operands():
    """`aggregation` was hardcoded None and the typed operand fields fell to their defaults,
    even though the request's operands and temporal contract were in scope. Each is now the
    recipe's own declaration, projected — never a guess from a column name."""
    from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id

    recipe = v2_recipe_by_id("net_transaction_flow")
    idea = _project([_clean_candidate(readiness=recipe.readiness,
                                      recipe_id=recipe.recipe_id,
                                      origin_recipe=recipe)]).ideas[0]
    assert idea.aggregation == recipe.formula.result_class == "sum"
    assert idea.operation_kind == "sum"
    assert idea.operation_class == "sum"
    assert idea.measure_refs == (("bank", "public.txns.amount"),)   # the MEASURE operand alone
    assert idea.time_ref == ("bank", "public.txns.event_ts")        # the temporal anchor's role
    assert idea.grouping_refs == ()                                  # no dimension operand
    assert idea.critic_note == ""                                    # no critic ran — say nothing


def test_aggregation_is_non_none_for_every_recipe_that_declares_one():
    """Total, and it is the recipe's declaration verbatim: 298 executable recipes declare a
    result class, and not one card may answer None where the corpus answered."""
    missing = []
    for recipe in _executable_recipes():
        idea = _project([_clean_candidate(readiness=recipe.readiness,
                                          recipe_id=recipe.recipe_id,
                                          origin_recipe=recipe)]).ideas[0]
        if idea.aggregation != recipe.formula.result_class:
            missing.append((recipe.recipe_id, idea.aggregation))
    assert missing == []
