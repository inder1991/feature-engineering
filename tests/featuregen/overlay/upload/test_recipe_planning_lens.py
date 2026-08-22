"""SE-7 (part 1) — the V2 planning lens: authored-objective applicability, assembled candidates."""
from __future__ import annotations

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.recipe_grounding_context import canonical_recipe_v2_hash
from featuregen.overlay.upload.recipe_planning_lens import (
    BINDING_STATES,
    fold_binding_state,
    v2_applicability,
    v2_recipe_candidates,
)
from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES, v2_recipe_by_id
from featuregen.overlay.upload.taxonomy.applicability import ConfirmedScope

SOURCE = "v2bank"
EXEMPLAR = v2_recipe_by_id("net_transaction_flow")   # 1 sibling at its authored objective


def _catalog(db) -> None:
    rows = [
        (CanonicalRow(SOURCE, "transactions", "acct_ref", "integer", is_grain=True,
                      entity="Account", definition="the posting account"), "account_id"),
        (CanonicalRow(SOURCE, "transactions", "amount", "numeric", additivity="additive",
                      currency="USD", definition="signed transaction amount"), "monetary_flow"),
        (CanonicalRow(SOURCE, "transactions", "dc_flag", "text",
                      definition="debit/credit indicator"), "debit_credit_indicator"),
        (CanonicalRow(SOURCE, "transactions", "booked_ts", "timestamp",
                      definition="when the transaction was booked"), "event_timestamp"),
    ]
    build_graph(db, SOURCE, [r for r, _ in rows],
                concepts={content_hash(r): c for r, c in rows})


# ── applicability: pure, over authored objectives — no tags, no crosswalk, no aliases ──────────

def test_unscoped_fails_open_to_all_primary():
    result = v2_applicability(ConfirmedScope(primary=None, unscoped=True))
    assert len(result.by_recipe) == len(V2_RECIPES)
    assert set(result.by_recipe.values()) == {"primary"}


def test_exact_scope_classifies_from_the_authored_primary():
    result = v2_applicability(ConfirmedScope(primary=EXEMPLAR.primary_objective))
    assert result.by_recipe[EXEMPLAR.recipe_id] == "primary"
    assert EXEMPLAR.recipe_id in result.eligible_ids
    # every recipe classified exactly once, and out-of-scope really means out
    assert len(result.by_recipe) == len(V2_RECIPES)
    out = [rid for rid, rel in result.by_recipe.items() if rel == "out_of_scope"]
    assert len(out) == len(V2_RECIPES) - len(result.eligible_ids)


def test_supporting_objectives_place_a_recipe_as_supporting_never_primary():
    supported = next(r for r in V2_RECIPES if r.supporting_objectives)
    scope = ConfirmedScope(primary=supported.supporting_objectives[0])
    result = v2_applicability(scope)
    assert result.by_recipe[supported.recipe_id] == "supporting"


# ── binding-state fold: fail-closed severity order over the shared verdict vocabulary ──────────

def test_fold_severity_order_is_blocked_then_ambiguous_then_missing():
    from featuregen.overlay.upload.recipe_operand_policy import OperandBindingVerdictV1

    required_roles = [op.role for op in EXEMPLAR.operands if op.required]
    a, b = required_roles[0], required_roles[1]

    def verdict(role, status):
        return OperandBindingVerdictV1(role=role, status=status)

    assert fold_binding_state(
        (verdict(a, "blocked"), verdict(b, "ambiguous")), EXEMPLAR) == "blocked"
    assert fold_binding_state(
        (verdict(a, "ambiguous"), verdict(b, "unresolved")), EXEMPLAR) == "ambiguous"
    assert fold_binding_state(
        (verdict(a, "unresolved"), verdict(b, "bound")), EXEMPLAR) == "missing"
    assert fold_binding_state(
        tuple(verdict(r, "bound") for r in required_roles), EXEMPLAR) == "bound"


def test_an_absent_optional_operand_degrades_it_never_blocks():
    from featuregen.overlay.upload.recipe_operand_policy import OperandBindingVerdictV1

    optional = next((op for op in EXEMPLAR.operands if not op.required), None)
    if optional is None:
        import pytest
        pytest.skip("exemplar has no optional operand")
    verdicts = tuple(
        OperandBindingVerdictV1(role=op.role,
                                status="unresolved" if op is optional else "bound")
        for op in EXEMPLAR.operands)
    assert fold_binding_state(verdicts, EXEMPLAR) == "bound"


# ── candidate assembly against a real catalog ──────────────────────────────────────────────────

def test_candidates_carry_the_complete_gate1_data(db):
    _catalog(db)
    scope = ConfirmedScope(primary=EXEMPLAR.primary_objective)
    candidates = v2_recipe_candidates(db, catalog_source=SOURCE, scope=scope)
    # The exemplar is the ONE primary at this objective and therefore ordered FIRST; the
    # supporting recipes (secondary-objective matches) follow it, never precede it. B5: each
    # recipe now yields one candidate PER authored variant — the exemplar's variants lead as
    # a block, then supporting recipes' variants.
    assert candidates[0].recipe_id == EXEMPLAR.recipe_id
    exemplar_block = [c for c in candidates if c.recipe_id == EXEMPLAR.recipe_id]
    assert list(candidates[:len(exemplar_block)]) == exemplar_block   # tuple vs list
    assert sum(1 for c in exemplar_block if c.variant_primary) == 1   # exactly one primary
    assert len({c.variant_key for c in exemplar_block}) == len(exemplar_block)
    assert all(c.relationship == "supporting"
               for c in candidates[len(exemplar_block):])
    candidate = candidates[0]
    assert candidate.relationship == "primary"
    assert candidate.recipe_revision_hash == canonical_recipe_v2_hash(EXEMPLAR)
    assert candidate.planning_request.origin == "recipe_v2"
    assert candidate.planning_request.source_content_hash == candidate.recipe_revision_hash
    assert {v.role for v in candidate.verdicts} == {op.role for op in EXEMPLAR.operands}
    assert candidate.binding_state in BINDING_STATES
    assert candidate.readiness == EXEMPLAR.readiness
    # temporal: compiled XOR blocked, never both and never neither
    assert bool(candidate.temporal_pit_text) != bool(candidate.temporal_blocker)
    # review store is empty: honestly not current, with the required roles named
    assert candidate.review_current is False
    assert "banking_sme" in candidate.review_missing_roles


def test_an_empty_catalog_yields_honest_missing_states_not_no_candidates(db):
    scope = ConfirmedScope(primary=EXEMPLAR.primary_objective)
    candidates = v2_recipe_candidates(db, catalog_source="empty_catalog", scope=scope)
    assert candidates[0].recipe_id == EXEMPLAR.recipe_id
    assert {c.binding_state for c in candidates} == {"missing"}   # honest, not absent
    required = {op.role for op in EXEMPLAR.operands if op.required}
    unresolved = {v.role for v in candidates[0].verdicts if v.status == "unresolved"}
    assert required <= unresolved


# ── SE-5 full: the capability-input binder through the lens ────────────────────────────────────

def _confirm_concept(db, object_ref: str, concept: str) -> None:
    from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
    from featuregen.overlay.upload.column_authority import logical_ref_of

    logical = logical_ref_of(db, SOURCE, object_ref)
    record_field_evidence(
        db, logical_ref=logical, field_name="concept", proposed_value=concept,
        producer="human", strength="confirmed", producer_ref="user:sme",
        source_snapshot_id="snap-test",
        input_hash=field_input_hash(logical_ref=logical, field_name="concept",
                                    material=concept))


def test_a_confirmed_concept_beats_a_proposed_twin_without_a_tie(db):
    """THE authority payoff: two columns carry the measure concept; one is human-confirmed.
    The old lexical binder saw an unadjudicatable tie — the eligibility tiers resolve it."""
    from featuregen.overlay.upload.feature_planning_contracts import (
        planning_request_from_recipe,
    )
    from featuregen.overlay.upload.generation_semantic_context import (
        build_generation_semantic_context,
    )
    from featuregen.overlay.upload.recipe_operand_policy import bind_planning_request

    _base_rows_with_twin(db)
    context = build_generation_semantic_context(db, catalog_source=SOURCE)
    request = planning_request_from_recipe(EXEMPLAR)
    verdicts, eligibility = bind_planning_request(db, request, context)
    by_role = {v.role: v for v in verdicts}
    measure_role = next(op.role for op in EXEMPLAR.operands
                        if op.operand_class == "measure")
    verdict = by_role[measure_role]
    assert verdict.status == "bound"
    assert verdict.selected_ref.endswith(".amount")           # the confirmed one
    assert verdict.tie_break_verdict_ref is None              # no adjudication needed
    # The losing twin's eligibility is in the audit: provisional, below the floor.
    twin = eligibility[(measure_role, "public.transactions.amount_twin")]
    assert twin.status == "provisional"


def _base_rows_with_twin(db):
    from featuregen.overlay.upload.canonical import CanonicalRow
    from featuregen.overlay.upload.enrich import content_hash
    from featuregen.overlay.upload.graph import build_graph

    rows = [
        (CanonicalRow(SOURCE, "transactions", "acct_ref", "integer", is_grain=True,
                      entity="Account", definition="the posting account"), "account_id"),
        (CanonicalRow(SOURCE, "transactions", "amount", "numeric", additivity="additive",
                      currency="USD", definition="signed transaction amount"), "monetary_flow"),
        (CanonicalRow(SOURCE, "transactions", "amount_twin", "numeric", additivity="additive",
                      currency="USD", definition="mirrored amount"), "monetary_flow"),
        (CanonicalRow(SOURCE, "transactions", "dc_flag", "text",
                      definition="debit/credit indicator"), "debit_credit_indicator"),
        (CanonicalRow(SOURCE, "transactions", "booked_ts", "timestamp",
                      definition="when the transaction was booked"), "event_timestamp"),
    ]
    build_graph(db, SOURCE, [r for r, _ in rows],
                concepts={content_hash(r): c for r, c in rows})
    _confirm_concept(db, "public.transactions.amount", "monetary_flow")
    return rows


def test_two_equal_proposals_still_fail_closed_as_ambiguous(db):
    from featuregen.overlay.upload.canonical import CanonicalRow
    from featuregen.overlay.upload.enrich import content_hash
    from featuregen.overlay.upload.feature_planning_contracts import (
        planning_request_from_recipe,
    )
    from featuregen.overlay.upload.generation_semantic_context import (
        build_generation_semantic_context,
    )
    from featuregen.overlay.upload.graph import build_graph
    from featuregen.overlay.upload.recipe_operand_policy import bind_planning_request

    rows = [
        (CanonicalRow(SOURCE, "transactions", "acct_ref", "integer", is_grain=True,
                      entity="Account", definition="the posting account"), "account_id"),
        (CanonicalRow(SOURCE, "transactions", "amount", "numeric", additivity="additive",
                      currency="USD", definition="signed transaction amount"), "monetary_flow"),
        (CanonicalRow(SOURCE, "transactions", "amount_twin", "numeric", additivity="additive",
                      currency="USD", definition="mirrored amount"), "monetary_flow"),
        (CanonicalRow(SOURCE, "transactions", "dc_flag", "text",
                      definition="debit/credit indicator"), "debit_credit_indicator"),
        (CanonicalRow(SOURCE, "transactions", "booked_ts", "timestamp",
                      definition="when the transaction was booked"), "event_timestamp"),
    ]
    build_graph(db, SOURCE, [r for r, _ in rows],
                concepts={content_hash(r): c for r, c in rows})
    context = build_generation_semantic_context(db, catalog_source=SOURCE)
    verdicts, _ = bind_planning_request(
        db, planning_request_from_recipe(EXEMPLAR), context)
    measure_role = next(op.role for op in EXEMPLAR.operands
                        if op.operand_class == "measure")
    verdict = {v.role: v for v in verdicts}[measure_role]
    assert verdict.status == "ambiguous"
    assert verdict.selected_ref is None
    assert set(verdict.tied_refs) == {"public.transactions.amount",
                                      "public.transactions.amount_twin"}


def test_the_lens_never_reloads_graph_node_with_a_prebuilt_context(db):
    """The N+1 killer, proven: binding a whole multi-recipe scope over a prebuilt context
    issues ZERO graph_node queries — the frozen universe is the only column source."""
    from featuregen.overlay.upload.generation_semantic_context import (
        build_generation_semantic_context,
    )

    _catalog(db)
    context = build_generation_semantic_context(db, catalog_source=SOURCE)
    calls: list[str] = []
    original = db.execute

    def counting(query, *args, **kwargs):
        calls.append(str(query))
        return original(query, *args, **kwargs)

    db.execute = counting
    try:
        candidates = v2_recipe_candidates(
            db, catalog_source=SOURCE, scope=ConfirmedScope(primary=EXEMPLAR.primary_objective),
            context=context)
    finally:
        db.execute = original
    assert len(candidates) >= 7                               # the whole eligible scope bound
    assert not [q for q in calls if "FROM graph_node" in q], \
        "the frozen context is the only column source — no per-recipe reload"


# ── SE-8 steps 2+3: the feature-level dataset story ─────────────────────────────────────────────

def test_the_dataset_story_names_an_explicit_population_from_the_declared_grain(db):
    """The population is the table whose DECLARED grain column the entity key bound — a governed
    upload fact, never a table-name or primary-entity inference (step 3's rule, verbatim)."""
    from featuregen.overlay.upload import semantic_eligibility_reasons as R

    _catalog(db)
    candidates = v2_recipe_candidates(
        db, catalog_source=SOURCE,
        scope=ConfirmedScope(primary=EXEMPLAR.primary_objective))
    bound = next(c for c in candidates
                 if c.recipe_id == EXEMPLAR.recipe_id and c.binding_state == "bound")
    story = bound.dataset_story
    assert story is not None
    assert story.population_ref == "transactions"             # acct_ref is DECLARED is_grain
    assert story.population_basis == "declared_grain"
    assert story.dataset_tables == ("transactions",)
    assert story.cross_dataset is False
    assert R.POPULATION_DATASET_UNDECLARED not in story.codes

    # ▲ AND THE GRAIN COLUMN, WHICH THIS FOLD USED TO DISCARD. `population_ref` answers "whose
    # rows?"; authoring needs "keyed by WHICH column?" — `formula_draft_worker._frozen_facts`
    # refuses a draft at REQUESTED, before any provider call, when `grain_refs` is empty. The ref
    # here is the one the governed binder actually selected, not a second lookup.
    assert story.population_key_ref == "public.transactions.acct_ref"

    # It must survive into the BINDING PLAN, which is what the serving projection reads (the
    # projection holds no connection and can only carry what the plan established).
    assert bound.binding_plan["grain_refs"] == [[SOURCE, "public.transactions.acct_ref"]]


def test_an_undeclared_grain_makes_the_population_named_setup_work(db):
    """Same catalog, grain flag withheld: the entity key still BINDS (meaning matched), but the
    population is honestly UNDECLARED — named setup work on the candidate, never an inference."""
    from featuregen.overlay.upload import semantic_eligibility_reasons as R
    from featuregen.overlay.upload.typed_gauntlet import validate_candidate

    rows = [
        (CanonicalRow(SOURCE, "transactions", "acct_ref", "integer",
                      entity="Account", definition="the posting account"), "account_id"),
        (CanonicalRow(SOURCE, "transactions", "amount", "numeric", additivity="additive",
                      currency="USD", definition="signed transaction amount"), "monetary_flow"),
        (CanonicalRow(SOURCE, "transactions", "dc_flag", "text",
                      definition="debit/credit indicator"), "debit_credit_indicator"),
        (CanonicalRow(SOURCE, "transactions", "booked_ts", "timestamp",
                      definition="when the transaction was booked"), "event_timestamp"),
    ]
    build_graph(db, SOURCE, [r for r, _ in rows],
                concepts={content_hash(r): c for r, c in rows})
    candidates = v2_recipe_candidates(
        db, catalog_source=SOURCE,
        scope=ConfirmedScope(primary=EXEMPLAR.primary_objective))
    candidate = next(c for c in candidates if c.recipe_id == EXEMPLAR.recipe_id)
    story = candidate.dataset_story
    assert story is not None
    assert story.population_ref is None
    assert R.POPULATION_DATASET_UNDECLARED in story.codes
    if candidate.binding_state == "bound" and not candidate.temporal_blocker:
        validation = validate_candidate(candidate)
        assert validation.status == "needs_external_validation"
        population = next(r for r in validation.requirements
                          if r.code == R.POPULATION_DATASET_UNDECLARED)
        assert population.family == "needs_setup"
        assert "grain" in population.detail


def test_crossing_datasets_is_a_feature_level_relationship_need():
    """Step 2: dataset needs are ONE decision over the whole candidate. Two bound operands on
    two tables make the relationship the candidate's own named setup work — not a per-column
    afterthought — until governed facts prove the hop."""
    from types import SimpleNamespace

    from featuregen.overlay.upload import semantic_eligibility_reasons as R
    from featuregen.overlay.upload.feature_planning_contracts import (
        RequiredOperandV1,
        planning_request_from_user_definition,
    )
    from featuregen.overlay.upload.recipe_operand_policy import OperandBindingVerdictV1
    from featuregen.overlay.upload.recipe_planning_lens import fold_dataset_story

    exemplar = v2_recipe_by_id("customer_activity_recency")
    request = planning_request_from_user_definition(
        definition_id="user:cross_probe", primary_objective=exemplar.primary_objective,
        output=exemplar.output,
        operands=(RequiredOperandV1(role="who", concept="customer_id",
                                    operand_class="entity_key"),
                  RequiredOperandV1(role="when", concept="event_timestamp",
                                    operand_class="event_timestamp")),
        source_grain="transaction", output_grain="customer",
        temporal=exemplar.temporal, content_hash="crosshash")
    verdicts = (
        OperandBindingVerdictV1(role="who", status="bound",
                                selected_ref="public.customers.cust_id"),
        OperandBindingVerdictV1(role="when", status="bound",
                                selected_ref="public.events.event_ts"),
    )
    context = SimpleNamespace(columns=(
        SimpleNamespace(object_ref="public.customers.cust_id", table="customers",
                        is_grain=True),
        SimpleNamespace(object_ref="public.events.event_ts", table="events",
                        is_grain=False),
    ))
    story = fold_dataset_story(request, verdicts, context)
    assert story.population_ref == "customers"
    assert story.cross_dataset is True
    assert story.dataset_tables == ("customers", "events")
    assert R.RELATIONSHIP_REQUIRED in story.codes
    assert R.POPULATION_DATASET_UNDECLARED not in story.codes


# ── B6: the run's DB work is O(fact families), never O(recipes) ────────────────────────────────

def test_a_full_unscoped_run_is_two_queries_with_a_prebuilt_context(db):
    """The PLAN-13 pin: binding ALL 317 recipes over a prebuilt context costs exactly ONE
    batched capability read + ONE review-event read — a 318th recipe changes the fold count,
    never the query count. (Tie-break consultation would add reads only on GENUINE same-tier
    ties, which this fixture has none of.)"""
    from featuregen.overlay.upload.generation_semantic_context import (
        build_generation_semantic_context,
    )

    _catalog(db)
    context = build_generation_semantic_context(db, catalog_source=SOURCE)
    calls: list[str] = []
    original = db.execute

    def counting(query, *args, **kwargs):
        calls.append(str(query))
        return original(query, *args, **kwargs)

    db.execute = counting
    try:
        candidates = v2_recipe_candidates(
            db, catalog_source=SOURCE, scope=ConfirmedScope(primary=None, unscoped=True),
            context=context)
    finally:
        db.execute = original
    # B5: one candidate per authored VARIANT (~940) — and the query count is STILL a
    # constant 3 (capability evidence + C1's revalidation read + review_events_all):
    # variant expansion multiplies folds, never reads (the whole point of B6-before-B5).
    assert len(candidates) > len(V2_RECIPES)
    assert len({c.recipe_id for c in candidates}) == len(V2_RECIPES)
    assert len(calls) == 3, calls


def test_the_batched_fold_is_byte_identical_to_the_per_request_path(db):
    """Golden equality: every candidate's verdicts + eligibility from the batched run match
    the single-request wrapper exactly — the batch changed WHERE the read happens, never a
    single decision."""
    from featuregen.overlay.upload.feature_planning_contracts import (
        planning_request_from_recipe,
    )
    from featuregen.overlay.upload.generation_semantic_context import (
        build_generation_semantic_context,
    )
    from featuregen.overlay.upload.recipe_operand_policy import bind_planning_request

    _catalog(db)
    context = build_generation_semantic_context(db, catalog_source=SOURCE)
    candidates = v2_recipe_candidates(
        db, catalog_source=SOURCE,
        scope=ConfirmedScope(primary=EXEMPLAR.primary_objective), context=context)
    assert candidates
    for candidate in candidates:
        request = planning_request_from_recipe(v2_recipe_by_id(candidate.recipe_id))
        verdicts, eligibility = bind_planning_request(db, request, context)
        assert verdicts == candidate.verdicts, candidate.recipe_id
        assert eligibility == candidate.eligibility, candidate.recipe_id


# ── B5: parameter variants — enumerated, hypothesis-matched, honestly labeled ───────────────────

def test_a_90_day_hypothesis_leads_with_the_90_day_variant(db):
    """GEN-03 closed: the exemplar authors window ∈ (30, 90, 180). A hypothesis naming
    "90 days" makes the 90-day variant PRIMARY (deterministic token match, no LLM); all three
    variants exist as distinct candidates with distinct keys and hashes; the card names its
    alternatives with the chosen value bracketed."""
    _catalog(db)
    candidates = v2_recipe_candidates(
        db, catalog_source=SOURCE, scope=ConfirmedScope(primary=EXEMPLAR.primary_objective),
        redacted_hypothesis="balances decline over 90 days before churn")
    block = [c for c in candidates if c.recipe_id == EXEMPLAR.recipe_id]
    assert len(block) == 3
    primary = next(c for c in block if c.variant_primary)
    assert dict(primary.planning_request.parameter_values)["window"] == 90
    assert primary.variant_key == f"{EXEMPLAR.recipe_id}@window=90"
    assert "30/[90]/180" in primary.param_alternatives
    assert len({c.planning_request_hash for c in block}) == 3   # variants are IDENTITY


def test_no_window_token_leads_with_the_authored_first_value(db):
    _catalog(db)
    candidates = v2_recipe_candidates(
        db, catalog_source=SOURCE, scope=ConfirmedScope(primary=EXEMPLAR.primary_objective),
        redacted_hypothesis="customers with declining balances churn")
    block = [c for c in candidates if c.recipe_id == EXEMPLAR.recipe_id]
    primary = next(c for c in block if c.variant_primary)
    assert dict(primary.planning_request.parameter_values)["window"] == 30   # authored-first


def test_month_tokens_convert_to_banking_days():
    from featuregen.overlay.upload.recipe_planning_lens import _hypothesis_window_tokens

    assert _hypothesis_window_tokens("churn within 3 months") == {90}
    assert _hypothesis_window_tokens("a 90-day window and 2 weeks") == {90, 14}
    assert _hypothesis_window_tokens("no windows here") == set()


# ── B7: the plan validates the frozen bindings — it never chooses ───────────────────────────────

def test_a_bound_single_source_candidate_carries_a_real_plan(db):
    """The read set IS the bound refs — the exact columns the user saw — with population,
    PIT, grain, and the chosen window. plan_envelope_present becomes a passable gate."""
    _catalog(db)
    candidates = v2_recipe_candidates(
        db, catalog_source=SOURCE, scope=ConfirmedScope(primary=EXEMPLAR.primary_objective),
        redacted_hypothesis="a 90 day window")
    bound = next(c for c in candidates
                 if c.recipe_id == EXEMPLAR.recipe_id and c.binding_state == "bound"
                 and c.variant_primary and not c.temporal_blocker)
    plan = bound.binding_plan
    assert plan is not None and plan["plan_kind"] == "single_source"
    expected_reads = sorted(v.selected_ref for v in bound.verdicts
                            if v.status == "bound" and v.selected_ref)
    assert plan["read_set"] == expected_reads                  # verdicts ARE the read set
    assert plan["role_bindings"] == {v.role: v.selected_ref for v in bound.verdicts
                                     if v.status == "bound" and v.selected_ref}
    assert plan["population_ref"] == "transactions"
    assert plan["pit"] == bound.temporal_pit_text
    assert plan["window"] == 90


def test_divergence_and_cross_dataset_refuse_the_plan_never_substitute():

    from featuregen.overlay.upload import semantic_eligibility_reasons as R
    from featuregen.overlay.upload.feature_planning_contracts import (
        RequiredOperandV1,
        planning_request_from_user_definition,
    )
    from featuregen.overlay.upload.recipe_operand_policy import OperandBindingVerdictV1
    from featuregen.overlay.upload.recipe_planning_lens import (
        DatasetStoryV1,
        fold_frozen_binding_plan,
    )

    exemplar = v2_recipe_by_id("customer_activity_recency")
    request = planning_request_from_user_definition(
        definition_id="user:plan_probe", primary_objective=exemplar.primary_objective,
        output=exemplar.output,
        operands=(RequiredOperandV1(role="who", concept="customer_id",
                                    operand_class="entity_key"),),
        source_grain="transaction", output_grain="customer",
        temporal=exemplar.temporal, content_hash="planhash")
    verdicts = (OperandBindingVerdictV1(role="who", status="bound",
                                        selected_ref="public.OTHER_TABLE.cust_id"),)
    story = DatasetStoryV1(population_ref="customers", population_basis="declared_grain",
                           dataset_tables=("customers",), cross_dataset=False, codes=())
    plan, refusals = fold_frozen_binding_plan(request, verdicts, story, "pit", "", "bank")
    assert plan is None
    assert refusals == (R.BINDING_PLAN_DIVERGENCE,)            # a bound ref OUTSIDE the story

    cross = DatasetStoryV1(population_ref="customers", population_basis="declared_grain",
                           dataset_tables=("customers", "events"), cross_dataset=True,
                           codes=(R.RELATIONSHIP_REQUIRED,))
    plan, refusals = fold_frozen_binding_plan(request, verdicts, cross, "pit", "", "bank")
    assert plan is None and refusals == (R.RELATIONSHIP_REQUIRED,)


def test_a_capitalized_uoa_matches_the_lowercase_grain():
    """B10: the catalog declares 'Customer'; the planning grain says 'customer' — the SAME
    unit of analysis, never a refusal. A genuinely different UOA still refuses."""
    from featuregen.overlay.upload import semantic_eligibility_reasons as R
    from featuregen.overlay.upload.feature_planning_contracts import (
        RequiredOperandV1,
        planning_request_from_user_definition,
    )
    from featuregen.overlay.upload.recipe_operand_policy import OperandBindingVerdictV1
    from featuregen.overlay.upload.recipe_planning_lens import (
        DatasetStoryV1,
        fold_frozen_binding_plan,
    )

    exemplar = v2_recipe_by_id("customer_activity_recency")
    request = planning_request_from_user_definition(
        definition_id="user:uoa_case_probe", primary_objective=exemplar.primary_objective,
        output=exemplar.output,
        operands=(RequiredOperandV1(role="who", concept="customer_id",
                                    operand_class="entity_key"),),
        source_grain="transaction", output_grain="customer",
        temporal=exemplar.temporal, content_hash="uoacasehash")
    verdicts = (OperandBindingVerdictV1(role="who", status="bound",
                                        selected_ref="public.customers.cust_id"),)
    story = DatasetStoryV1(population_ref="customers", population_basis="declared_grain",
                           dataset_tables=("customers",), cross_dataset=False, codes=())
    plan, refusals = fold_frozen_binding_plan(
        request, verdicts, story, "pit", "", "bank", uoa_entity="Customer")
    assert refusals == () and plan is not None

    plan, refusals = fold_frozen_binding_plan(
        request, verdicts, story, "pit", "", "bank", uoa_entity="Account")
    assert plan is None and refusals == (R.UOA_MISMATCH,)


def test_the_GRAIN_REACHES_THE_PROJECTED_FEATURE_IDEA(db):
    """▲ THE END OF THE CHAIN, on the real candidate rather than a hand-built one.

    Two earlier tests here constructed a `DatasetStoryV1` themselves and asserted against their own
    fixtures — they would have passed unchanged if production stopped assigning or projecting the
    grain entirely, which is precisely the regression they were meant to catch. This drives the real
    fold and the real projection and asserts on what authoring would actually receive.

    `FeatureIdea.grain_refs` is the field `_frozen_facts` reads. Empty here means no formula is ever
    authored for this candidate, whatever its origin.
    """
    from featuregen.overlay.upload.semantic_projection import _served_idea
    from featuregen.overlay.upload.typed_gauntlet import validate_candidate

    _catalog(db)
    candidates = v2_recipe_candidates(
        db, catalog_source=SOURCE,
        scope=ConfirmedScope(primary=EXEMPLAR.primary_objective))
    bound = next(c for c in candidates
                 if c.recipe_id == EXEMPLAR.recipe_id and c.binding_state == "bound")

    idea = _served_idea(
        type("A", (), {"candidate": bound})(), validate_candidate(bound),
        catalog_source=SOURCE)

    assert idea.grain_refs == ((SOURCE, "public.transactions.acct_ref"),), (
        "the projected idea carries no grain, so the draft worker would refuse this candidate at "
        "REQUESTED with GRAIN_NOT_RESOLVED before any provider call")
    # The table half must still be there too — they answer different questions.
    assert idea.grain_table == "transactions"
