"""A6 — the G2 OPERAND-ROLE serving gate, and the account-grain journey fixtures.

G2 (§V fact V2): ``need_metadata._derive_one`` resolves any operand whose concept links no entity
and carries no ``pit_role`` — a status, a dimension, a direction — to ``JoinRole.MEASURE``, and
``planner/requests._derived_roles`` instead honours the recipe author's ``operand_class``. The two
governed authorities agree on 1113 of the registry's 1195 operands and DISAGREE on 82. Today the
disagreement is invisible, because ``compile_aggregation`` short-circuits on ``card is None``
before the additivity matrix runs; it unmasks the moment a cardinality attaches.

A6 does NOT close G2 as a class — that ruling is chartered. A6 makes the divergence a NAMED
serving fact instead of a silent mis-classification: an option whose operand roles are not
resolved carries ``OPERAND_ROLE_UNRESOLVED``, which WARNS at AUTHOR_FORMULA (the owner's matrix
allows authoring under every link condition) and BLOCKS every act that would compute over the
crossing, while the card may still be shown under "Discoveries requiring setup".

The gate is DERIVED, never listed: it re-asks the same two governed authorities the worklist
counted, so it stays correct as the recipe registry and the concept registry move.
``test_the_gate_is_the_worklists_own_criterion_recomputed`` proves that by recomputing the
divergence independently, over the whole registry, and demanding set equality.

**The fix round took the first two rulings.** ``posted_debit_amount``'s ``transaction`` and
``original_txn`` — identifier-valued ``dimension`` slots on entity-linked concepts — now DECLARE
``join_role="intermediate_entity_key"`` in the recipe. That is what a G2 ruling looks like: a
declaration, on the platform's own first rung, not a new mechanism. It cleared 6 operands across
5 recipes (``original_txn`` is a shared operand constant), taking the census from 82/76 to 76/71,
and it did not disturb the exemplar's reviewed Formula-v2 expectation — which is keyed on the
expectation REF, never on the recipe hash.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.featuregen.overlay.upload._account_grain_fixtures import (
    ACCOUNT_BRIDGE_FACT_KEY,
    ACCOUNT_CATALOG,
    ACCOUNT_GRAIN_HYPOTHESIS,
    ACCOUNT_LINK_COLUMN,
    ACCOUNT_TABLE,
    DONOR_RECIPE_ID,
    DROPPED_DONOR_ROLES,
    RULED_DONOR_ROLES,
    TRANSACTION_CATALOG,
    TRANSACTION_LINK_COLUMN,
    TRANSACTION_TABLE,
    account_grain_operands,
    account_grain_request,
    account_grain_scope,
    donor_recipe_request,
    seed_account_grain_world,
)

from featuregen.materialize.action_authorization import ActionV1
from featuregen.materialize.action_decision import ask
from featuregen.materialize.action_dispositions import ACTION_DISPOSITIONS, Disposition
from featuregen.materialize.boundary_v2 import KnowledgeTimeBasisV2
from featuregen.overlay.upload import semantic_eligibility_reasons as R
from featuregen.overlay.upload.binding_roles import JoinRole
from featuregen.overlay.upload.feature_planning_contracts import RequiredOperandV1
from featuregen.overlay.upload.planner.contracts import PathResolutionStatus, ReasonCode
from featuregen.overlay.upload.planner.declarations import CompileBudget, build_compiler_context
from featuregen.overlay.upload.planner.logical_plan_v2 import (
    DrivingTimeRoleV1,
    IntervalBoundaryPolicyV1,
    LogicalTemporalJoinSemanticsV1,
    StaticLinkMeaningV1,
    UnmatchedRowMeaningV1,
)
from featuregen.overlay.upload.planner.logical_resolution import (
    OPERAND_ROLE_UNRESOLVED,
    resolve_logical_plan,
    select_logical_plan_candidate,
    semantic_revisions_for_plan,
    serving_action_facts,
)
from featuregen.overlay.upload.planner.operand_role_gate import (
    operand_role_resolutions,
    unresolved_operand_roles,
)
from featuregen.overlay.upload.planner.requests import plan_planning_request

_NOW = datetime(2026, 8, 24, tzinfo=UTC)


# ── helpers ───────────────────────────────────────────────────────────────────────────────────


def _plan_result(db, request):
    scope = account_grain_scope()
    return plan_planning_request(
        db, request=request, target_entity="account", scope=scope, roles=(), now=_NOW,
        compile_ctx=build_compiler_context(db, scope, (), _NOW),
        budget=CompileBudget(remaining=64, deadline_monotonic=1e9, clock=lambda: 0.0))


def _semantics() -> LogicalTemporalJoinSemanticsV1:
    """R14's first-journey selection, DECLARED — so the resolution's only remaining absence can
    be the one a test is actually about."""
    return LogicalTemporalJoinSemanticsV1(
        effective_time_basis=KnowledgeTimeBasisV2.AS_OF_CUTOFF,
        knowledge_time_basis=KnowledgeTimeBasisV2.AS_OF_CUTOFF,
        driving_time_role=DrivingTimeRoleV1.CUTOFF_PARAMETER,
        interval_boundary_policy=IntervalBoundaryPolicyV1.CLOSED_OPEN,
        unmatched_row_meaning=UnmatchedRowMeaningV1.JOINED_ATTRIBUTES_NOT_APPLICABLE,
        static_link_meaning=StaticLinkMeaningV1.APPLIES_FOR_ALL_TIME)


def _resolve(db, request):
    result = _plan_result(db, request)
    plan = select_logical_plan_candidate(result)
    assert plan is not None, "the fixture must assemble a source→target path"
    return result, plan, resolve_logical_plan(
        request=request, plan=plan,
        semantic_revisions=semantic_revisions_for_plan(db, plan),
        temporal_semantics={ACCOUNT_BRIDGE_FACT_KEY: _semantics()})


def _worklist_divergence(request) -> set[str]:
    """The 82-operand worklist's OWN criterion, recomputed here from scratch — the class-keyed
    projection against the concept-keyed ladder, exactly as
    ``test_the_class_keyed_projection_diverges_from_the_concept_ladder_only_where_g2_lives``
    measures it. Nothing in this helper imports A6's module, so set equality against the gate is
    a real cross-check rather than a tautology."""
    import dataclasses

    from featuregen.overlay.upload.need_metadata import derive_need_metadata
    from featuregen.overlay.upload.planner.requests import planning_probe

    probe = planning_probe(request)
    ladder = {m.role: m.join_role for m in derive_need_metadata(dataclasses.replace(
        probe, needs=tuple(dataclasses.replace(n, join_role=None, temporal_role=None)
                           for n in probe.needs)))}
    return {need.role for need in probe.needs if ladder[need.role] is not need.join_role}


# ── 1. THE REGISTRATION (three-part commit) ───────────────────────────────────────────────────


def test_the_code_warns_at_authoring_and_blocks_every_computing_act():
    """The owner's binding capability matrix, whose Formula column reads "Allow" in EVERY row.

    A6 first shipped this row as BLOCK × six, reasoning that an unruled slot makes the formula
    unauthorable. The controller ruled otherwise and the ruling is right: the harm is AT THE
    JOIN — the disputed slot decides hop KEY versus staged MEASURE, which is `compile_aggregation`'s
    question, not the formula text's. The reviewed gold for the exemplar reads only
    direction/amount/booking time/account and never touches a disputed slot. So authoring proceeds
    with the caller told, and every act that would COMPUTE over the crossing refuses — the same
    row shape as TEMPORAL_JOIN_POLICY_MISSING and ALLOCATION_POLICY_REQUIRED."""
    assert R.OPERAND_ROLE_UNRESOLVED == "OPERAND_ROLE_UNRESOLVED"
    assert R.reason_family(R.OPERAND_ROLE_UNRESOLVED) == "needs_setup"
    assert ACTION_DISPOSITIONS[
        (R.OPERAND_ROLE_UNRESOLVED, ActionV1.AUTHOR_FORMULA)] is Disposition.WARN
    for action in ActionV1:
        if action is ActionV1.AUTHOR_FORMULA:
            continue
        assert ACTION_DISPOSITIONS[(R.OPERAND_ROLE_UNRESOLVED, action)] is Disposition.BLOCK
    # It is the SAME row the matrix's other join facts carry — pinned by comparison, not by a
    # second hand-written expectation.
    for action in ActionV1:
        assert (ACTION_DISPOSITIONS[(R.OPERAND_ROLE_UNRESOLVED, action)]
                is ACTION_DISPOSITIONS[(R.TEMPORAL_JOIN_POLICY_MISSING, action)]), action


def test_the_code_is_under_the_matrix_law_that_a_hardcoded_list_let_it_escape():
    """The registration bug this row exposed, closed at the source. `SERVING_CAPABILITY_MATRIX_CODES`
    is the vocabulary's own grouping and `test_the_formula_column_is_ALLOW_for_every_matrix_link_condition`
    derives its set from it, so this code is now covered by the law it originally slipped past."""
    assert R.OPERAND_ROLE_UNRESOLVED in R.SERVING_CAPABILITY_MATRIX_CODES


def test_the_gate_module_reuses_the_registered_spelling_never_a_second_one():
    """One spelling, by the ``ALLOCATION_POLICY_REQUIRED`` / ``TEMPORAL_JOIN_POLICY_MISSING``
    precedent: the planner module IMPORTS the vocabulary's constant."""
    from featuregen.overlay.upload.planner import operand_role_gate

    assert operand_role_gate.OPERAND_ROLE_UNRESOLVED is R.OPERAND_ROLE_UNRESOLVED
    assert OPERAND_ROLE_UNRESOLVED is R.OPERAND_ROLE_UNRESOLVED


# ── 2. THE GATE'S DERIVATION ──────────────────────────────────────────────────────────────────


def test_the_gate_is_the_worklists_own_criterion_recomputed():
    """OVER THE WHOLE REGISTRY, in BOTH directions: the operands the gate calls unresolved are
    exactly the operands the worklist's own divergence criterion finds. Not a subset, not a
    superset — so the gate cannot rot into a stale list as either registry moves."""
    from featuregen.overlay.upload.feature_planning_contracts import planning_request_from_recipe
    from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES

    gated_total, recipes_gated = 0, 0
    for recipe in V2_RECIPES:
        request = planning_request_from_recipe(recipe)
        gated = {r.role for r in unresolved_operand_roles(request)}
        assert gated == _worklist_divergence(request), recipe.recipe_id
        gated_total += len(gated)
        recipes_gated += 1 if gated else 0

    # ▲ The census is pinned in BOTH directions, and the shrink direction is the one that
    # matters. A one-sided floor (`>= 76`) would let a legitimate G2 ruling — the very act this
    # gate exists to provoke — land silently; a one-sided ceiling would hide a new divergence.
    # 76 operands across 71 recipes, measured after A6's ruling on `transaction`/`original_txn`
    # cleared 6 operands across 5 recipes (pre-ruling: 82 across 76). Moving either number is a
    # deliberate edit that says which ruling moved it.
    assert (gated_total, recipes_gated) == (76, 71), (
        f"the G2 census moved to {gated_total} operands across {recipes_gated} recipes. A "
        f"DECREASE means a ruling landed — record which, and update these numbers. An INCREASE "
        f"means a new divergence entered the registry — that needs a ruling, not a new number.")
    assert recipes_gated * 4 < len(V2_RECIPES), (
        f"{recipes_gated}/{len(V2_RECIPES)} recipes gated — the gate has widened into the "
        f"healthy population")


def test_a_fully_resolved_registry_recipe_is_not_gated():
    """NO FALSE POSITIVES, proved on a REAL shipped recipe rather than a hand-built request: a
    recipe whose every operand's two governed authorities agree carries no gate at all."""
    from featuregen.overlay.upload.feature_planning_contracts import planning_request_from_recipe
    from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES

    clean = [r for r in V2_RECIPES
             if not _worklist_divergence(planning_request_from_recipe(r))]
    assert len(clean) > 200, f"only {len(clean)} clean recipes — the healthy population moved"

    for recipe in clean[:25]:
        request = planning_request_from_recipe(recipe)
        assert unresolved_operand_roles(request) == (), recipe.recipe_id
        assert all(r.resolved for r in operand_role_resolutions(request)), recipe.recipe_id


def test_the_donor_recipes_two_divergent_slots_are_now_RULED_not_dropped():
    """``posted_debit_amount`` — the plan's own §V9 exemplar — carried TWO divergent operands,
    both ``dimension``-classed on an ENTITY-LINKED concept (`transaction_id` /
    `original_transaction_id`). A6's first cut dropped them from the fixture; the fix round RULES
    on them instead, in the recipe, the way G2's ruling is meant to be expressed.

    The exemplar is therefore no longer gated at all, and the cure is the platform's own first
    rung rather than a new mechanism."""
    assert unresolved_operand_roles(donor_recipe_request()) == ()

    by_role = {r.role: r for r in operand_role_resolutions(donor_recipe_request())}
    for role in RULED_DONOR_ROLES:
        resolution = by_role[role]
        assert resolution.operand_class == "dimension"
        assert resolution.declared_join_role == str(JoinRole.INTERMEDIATE_ENTITY_KEY)
        assert resolution.projected_join_role is JoinRole.INTERMEDIATE_ENTITY_KEY
        assert resolution.resolved and "DECLARES" in resolution.detail
    # And the ruling is load-bearing: strip the declaration and the divergence returns, with the
    # ladder and the class saying exactly what they said before.
    import dataclasses

    stripped = account_grain_request(operands=tuple(
        dataclasses.replace(op, join_role="") if op.role in RULED_DONOR_ROLES else op
        for op in donor_recipe_request().operands))
    reverted = {r.role: r for r in unresolved_operand_roles(stripped)}
    assert set(reverted) == set(RULED_DONOR_ROLES)
    for role in RULED_DONOR_ROLES:
        assert reverted[role].projected_join_role is JoinRole.MEASURE
        assert reverted[role].concept_ladder_join_role is JoinRole.INTERMEDIATE_ENTITY_KEY
        assert "authorities" in reverted[role].detail.lower()


def test_the_ruling_reached_every_recipe_that_shares_the_ruled_operand():
    """A disclosed consequence, pinned rather than left as a surprise: `original_txn` is a SHARED
    module-level operand constant (`_CORRECTION_LINK`), so ruling on it cleared four sibling
    recipes in the same pack as well as the exemplar. That is correct — one slot, one concept,
    one ruling — but it must be visible, because it is why the census moved by 6 and not 2."""
    from featuregen.overlay.upload.feature_planning_contracts import planning_request_from_recipe
    from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id

    for recipe_id in ("posted_debit_amount", "refund_amount", "refund_count",
                      "reversal_amount", "reversal_count"):
        recipe = v2_recipe_by_id(recipe_id)
        assert recipe is not None, recipe_id
        assert unresolved_operand_roles(planning_request_from_recipe(recipe)) == (), recipe_id

    # NOT ruled: `chargeback_rate` declares its own `original_txn` operand in `recipes/payments.py`
    # rather than reusing the foundation constant, so it is still gated. The ruling is per
    # DECLARATION, and A6's brief scoped it to the transaction-foundation pack.
    chargeback = v2_recipe_by_id("chargeback_rate")
    assert chargeback is not None
    assert {r.role for r in unresolved_operand_roles(
        planning_request_from_recipe(chargeback))} == {"original_txn"}


def test_a_declared_join_role_resolves_the_operand_and_a_nonsense_one_does_not():
    """The gate's escape hatch is the platform's OWN first rung (``_projected_roles``: a
    non-empty declaration wins outright). Declaring the role is what G2's ruling will do, per
    operand — so a declared role clears the gate, and a declared string naming no member of the
    planner's five-value vocabulary does NOT (the planner leaves that field unset, and an
    operand with no role at all is exactly what the gate exists to catch)."""
    divergent = RequiredOperandV1(role="txn_ref", concept="transaction_id",
                                  operand_class="dimension",
                                  allowed_source_grains=("transaction",))
    request = account_grain_request(operands=(*account_grain_operands(), divergent))
    assert {r.role for r in unresolved_operand_roles(request)} == {"txn_ref"}

    import dataclasses

    declared = account_grain_request(operands=(
        *account_grain_operands(),
        dataclasses.replace(divergent, join_role=str(JoinRole.INTERMEDIATE_ENTITY_KEY))))
    assert unresolved_operand_roles(declared) == ()

    nonsense = account_grain_request(operands=(
        *account_grain_operands(), dataclasses.replace(divergent, join_role="dimension")))
    assert {r.role for r in unresolved_operand_roles(nonsense)} == {"txn_ref"}


def test_an_ambiguous_source_anchor_fails_CLOSED_not_open():
    """The concept-keyed ladder REFUSES a template with two distinct entity keys and no anchor.
    The gate may not swallow that: every operand is unresolved, under the registered code, with
    the ambiguity named — a request the platform cannot resolve roles for must never serve.

    Two keys on DIFFERENT entities, neither of which is the request's own output grain, so
    ``_source_anchor``'s two declared tie-breaks cannot settle it either."""
    request = account_grain_request(operands=(
        RequiredOperandV1(role="customer", concept="customer_id", operand_class="entity_key",
                          allowed_source_grains=("transaction",)),
        RequiredOperandV1(role="txn", concept="transaction_id", operand_class="entity_key",
                          allowed_source_grains=("transaction",)),
        RequiredOperandV1(role="amount", concept="monetary_flow", operand_class="measure",
                          allowed_source_grains=("transaction",),
                          join_role=str(JoinRole.MEASURE)),
    ))
    with pytest.raises(ValueError, match="source_entity_need_role"):
        _worklist_divergence(request)           # the ladder itself refuses this shape

    unresolved = unresolved_operand_roles(request)
    assert {r.role for r in unresolved} == {"customer", "txn", "amount"}
    assert all("anchor" in r.detail.lower() for r in unresolved)


# ── 3. THE SERVING GATE, THROUGH THE CANONICAL DECISION SERVICE ───────────────────────────────


def test_an_unresolved_operand_role_warns_at_authoring_and_blocks_every_computing_act(db):
    """The whole point, asserted where the platform actually decides: the resolution's facts go
    to ``action_decision.ask`` and the §5 disposition table decides. Nothing here re-implements
    the policy — the codes are FACTS and ``ask`` is the only verdict.

    AUTHOR_FORMULA proceeds with the code carried as a WARNING (the owner's matrix); every act
    that would compute over the crossing refuses with it as a BLOCKER."""
    seed_account_grain_world(db)
    request = account_grain_request(operands=(
        *account_grain_operands(),
        RequiredOperandV1(role="txn_ref", concept="transaction_id", operand_class="dimension",
                          allowed_source_grains=("transaction",))))
    _result, _plan, resolution = _resolve(db, request)

    absences = {a.code: a for a in resolution.absences}
    assert OPERAND_ROLE_UNRESOLVED in absences
    assert absences[OPERAND_ROLE_UNRESOLVED].subject == "txn_ref"
    assert not resolution.is_complete

    facts = serving_action_facts(resolution)
    for action in ActionV1:
        decision = ask(db, facts.request(
            action=action, resource_identity_hash=resolution.plan_variant_address))
        member = decision.per_member[0]
        if action is ActionV1.AUTHOR_FORMULA:
            # Allowed, and the caller is TOLD (D3) — never silently.
            assert decision.allowed, decision
            assert OPERAND_ROLE_UNRESOLVED in member.warnings, member
            assert OPERAND_ROLE_UNRESOLVED not in member.blockers, member
        else:
            assert not decision.allowed, action
            assert OPERAND_ROLE_UNRESOLVED in member.blockers, (action, member)


def test_a_resolved_option_carries_no_operand_role_gate(db):
    """The other half of "no false positives", measured on the SERVING seam rather than on the
    derivation: the account-grain fixture's own option is not gated, and with its temporal
    semantics declared it resolves COMPLETE."""
    seed_account_grain_world(db)
    _result, _plan, resolution = _resolve(db, account_grain_request())

    assert [a.code for a in resolution.absences] == []
    assert resolution.is_complete

    facts = serving_action_facts(resolution)
    assert ask(db, facts.request(action=ActionV1.AUTHOR_FORMULA,
                                 resource_identity_hash=resolution.plan_variant_address)).allowed


def test_the_gate_covers_only_the_operands_the_option_actually_binds(db):
    """An operand the plan never bound is not part of the served option, so it is not gated —
    the check is about THIS option's operands, not about every slot the request declares."""
    seed_account_grain_world(db)
    # `customer_id` is entity-linked (so a `dimension` on it IS divergent) and NO column in
    # either fixture catalog carries that concept, so nothing binds it.
    unbindable = RequiredOperandV1(
        role="cust_ref", concept="customer_id", operand_class="dimension",
        required=False, allowed_source_grains=("transaction",))
    request = account_grain_request(operands=(*account_grain_operands(), unbindable))

    assert {r.role for r in unresolved_operand_roles(request)} == {"cust_ref"}  # it IS divergent
    _result, plan, resolution = _resolve(db, request)
    assert "cust_ref" not in {b.need_role for b in plan.ingredient_bindings}    # …but unbound
    assert [a.code for a in resolution.absences] == []


def test_the_gate_does_not_fix_g2_the_physical_refusal_is_untouched(db):
    """Requirement 2, pinned: A6 makes the divergence VISIBLE, it does not re-classify anything.
    The same run that mints the gate still refuses physically for G3's own reason, and the
    divergent operand is still staged as a MEASURE by the planner."""
    seed_account_grain_world(db)
    request = account_grain_request(operands=(
        *account_grain_operands(),
        RequiredOperandV1(role="txn_ref", concept="transaction_id", operand_class="dimension",
                          allowed_source_grains=("transaction",))))
    _result, plan, resolution = _resolve(db, request)

    assert ReasonCode.physical_cardinality_unavailable in plan.contract_reason_codes
    bound = {b.need_role: b.join_role for b in plan.ingredient_bindings}
    assert bound["txn_ref"] == str(JoinRole.MEASURE)      # unchanged: G2's ruling is chartered
    assert OPERAND_ROLE_UNRESOLVED in {a.code for a in resolution.absences}


# ── 4. THE ACCOUNT-GRAIN JOURNEY FIXTURES ─────────────────────────────────────────────────────


def test_the_account_grain_fixture_plans_clean_end_to_end_at_account_grain(db):
    """D1-D4's substrate: two catalogs, one AI-PROPOSED link, and a plan that lands on the
    ACCOUNT grain through exactly that link — with a complete R9 logical identity."""
    seed_account_grain_world(db)
    result, plan, resolution = _resolve(db, account_grain_request())

    assert plan.path_resolution_status is PathResolutionStatus.source_to_target_resolved
    assert plan.output_grain_ref == (ACCOUNT_CATALOG, f"public.{ACCOUNT_TABLE}.{ACCOUNT_LINK_COLUMN}")
    assert plan.participating_catalogs == (TRANSACTION_CATALOG, ACCOUNT_CATALOG)
    assert plan.audit_envelope.active_bridge_fact_keys == (ACCOUNT_BRIDGE_FACT_KEY,)
    assert result.candidate_plans

    crossings = resolution.path
    assert len(crossings) == 1
    assert crossings[0].bridge_fact_key == ACCOUNT_BRIDGE_FACT_KEY
    assert crossings[0].left_endpoint_refs == (
        f"{TRANSACTION_CATALOG}::public.{TRANSACTION_TABLE}.{TRANSACTION_LINK_COLUMN}",)
    assert crossings[0].right_endpoint_refs == (
        f"{ACCOUNT_CATALOG}::public.{ACCOUNT_TABLE}.{ACCOUNT_LINK_COLUMN}",)
    assert resolution.is_complete and len(resolution.logical_digest) == 64


def test_the_account_grain_fixture_reads_a_POPULATED_governed_semantic_revision(db):
    """A5's concern 5, closed: its planner fixtures seeded ``graph_node`` but no
    ``field_evidence``, so ``semantic_revisions_for_plan`` was exercised only for its EMPTY case.
    These fixtures seed the governed concept evidence, so every bound operand carries a real
    ``fev_`` revision and R9's identity stands on a fact rather than on a blank."""
    seed_account_grain_world(db)
    _result, plan, resolution = _resolve(db, account_grain_request())

    revisions = semantic_revisions_for_plan(db, plan)
    assert revisions and all(v.startswith("fev_") for v in revisions.values())
    assert {b.role for b in resolution.plan.operand_bindings} == {
        b.need_role for b in plan.ingredient_bindings}
    assert all(b.governed_semantic_revision_id.startswith("fev_")
               for b in resolution.plan.operand_bindings)


def test_the_link_is_ai_proposed_and_usable_before_any_human_confirmation(db):
    """The plan's world: the link is DRAFT — nobody confirmed it — and it is CONSUMED anyway
    (``cross_catalog_links.AVAILABLE_STATUSES`` includes DRAFT). A fixture that seeded a VERIFIED
    link would be testing a world the journeys are not about."""
    from featuregen.overlay.state import fold_overlay_state
    from featuregen.overlay.store import load_fact

    seed_account_grain_world(db)
    assert fold_overlay_state(load_fact(db, ACCOUNT_BRIDGE_FACT_KEY)).status == "DRAFT"

    _result, plan, _resolution = _resolve(db, account_grain_request())
    assert ACCOUNT_BRIDGE_FACT_KEY in {s.bridge_fact_key for s in plan.path_segments}


def test_a_customer_uoa_over_the_account_grain_recipe_refuses_UOA_MISMATCH():
    """THE B10 PIN — why the journeys are account-grain at all (§V9). ``posted_debit_amount``
    computes PER ACCOUNT, so a confirmed CUSTOMER unit of analysis over it is honest setup work,
    never a silently-served mismatch. The decisive customer-grain CIB/FTR journey needs a
    customer-grain feature and Formula V4's joined-attribute predicate (plan step 6) — this
    refusal is what keeps A6 from pretending otherwise."""
    from featuregen.overlay.upload.recipe_operand_policy import OperandBindingVerdictV1
    from featuregen.overlay.upload.recipe_planning_lens import (
        DatasetStoryV1,
        fold_frozen_binding_plan,
    )

    request = account_grain_request()
    verdicts = (OperandBindingVerdictV1(
        role="account", status="bound",
        selected_ref=f"public.{TRANSACTION_TABLE}.{TRANSACTION_LINK_COLUMN}"),)
    story = DatasetStoryV1(population_ref=TRANSACTION_TABLE, population_basis="declared_grain",
                           dataset_tables=(TRANSACTION_TABLE,), cross_dataset=False, codes=())

    plan, refusals = fold_frozen_binding_plan(
        request, verdicts, story, "pit", "", TRANSACTION_CATALOG, uoa_entity="customer")
    assert plan is None and refusals == (R.UOA_MISMATCH,)

    plan, refusals = fold_frozen_binding_plan(
        request, verdicts, story, "pit", "", TRANSACTION_CATALOG, uoa_entity="Account")
    assert refusals == () and plan is not None


def test_the_fixture_drops_exactly_what_this_constant_says():
    """The guard A6's first cut lacked. `DROPPED_DONOR_ROLES` claimed two roles while the fixture
    actually dropped FOUR — including `booking_ts` and `value_ts`, which are ungated (both
    authorities say TIME) and were simply missing columns. A constant describing the fixture must
    be checked AGAINST the fixture, in both directions, or it drifts into decoration."""
    donor_roles = [op.role for op in donor_recipe_request().operands]
    fixture_roles = [op.role for op in account_grain_request().operands]

    assert set(DROPPED_DONOR_ROLES) == set(donor_roles) - set(fixture_roles)
    assert DROPPED_DONOR_ROLES == ()          # nothing is dropped any more
    assert fixture_roles == donor_roles       # …because the operand set is the donor's, verbatim
    assert len(donor_roles) == 9


def test_every_fixture_operand_has_a_column_to_bind(db):
    """The other half of "drops nothing": carrying all nine operands is only honest if the
    catalog can BIND all nine. The three columns the fix round seeded (`booking_ts`, `value_ts`,
    `original_txn_id`) are exactly what makes that true."""
    seed_account_grain_world(db)
    _result, plan, _resolution = _resolve(db, account_grain_request())

    bound = {b.need_role for b in plan.ingredient_bindings}
    assert {op.role for op in account_grain_request().operands} <= bound
    assert {"booking_ts", "value_ts", "original_txn", "transaction"} <= bound


def test_the_account_grain_hypothesis_is_a_level_never_a_spike():
    """``posted_debit_amount`` is a windowed SUM: it measures how much flowed out, not how much
    the outflow MOVED. The hypothesis wording may therefore never promise a change."""
    text = ACCOUNT_GRAIN_HYPOTHESIS.lower()
    assert "high posted debit outflows" in text
    for change_word in ("spike", "increase", "increased", "surge", "sharply", "jump", "trend"):
        assert change_word not in text, change_word
    assert DONOR_RECIPE_ID == "posted_debit_amount"
