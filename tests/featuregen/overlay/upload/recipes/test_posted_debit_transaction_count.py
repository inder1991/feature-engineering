"""A6b — ``posted_debit_transaction_count``, made structurally honest under ruling R13.

The recipe the D2/D5 journeys count with was the pack's least honest one (plan §V fact V9: "count
recipe bare"). It said "debit" in its NAME and its prose while declaring no row selection; it
carried no transaction identity at all, so "count of transactions" had nothing to be a count OF;
and its window rode the generic ``event_ts`` clock while the thing being counted is a POSTED
transaction, whose clock is the posting timestamp (T0 froze ``pstd_date`` = ``booking_date`` as the
journey's posting column, and the reviewed gold exemplar's own window already reads ``booking_ts``).

A6b ships three structural statements and NOT a runtime guard:

1. the DEBIT row selection, mirroring ``posted_credit_amount``'s credit selection inverted;
2. the governed canonical transaction-identity operand, DECLARING
   ``join_role="intermediate_entity_key"`` (A6's G2 ruling shape — without it this recipe would
   gate itself on the very slot R13 needs);
3. the window bound to the posting-timestamp role.

**Where R13's check lives.** R13 says a violated transaction identity produces
``TRANSACTION_IDENTITY_NOT_UNIQUE`` and NO count. The compiled uniqueness guard is step 7's
(the compiler/IR/renderer pipeline names it explicitly: "transaction uniqueness guard (R13)").
What A6b owns is the half that makes the check EXPRESSIBLE — an operand that names the governed
identity so uniqueness has a subject — plus the pins that keep the vocabulary and the refusal's
shape honest until step 7 compiles it. The one thing that must never happen in between is
COUNT_ROWS quietly consuming the identity operand as a de-duplication: that is the silent
``COUNT_DISTINCT`` substitution R13 forbids by name, and it is pinned below in both directions.
"""
from __future__ import annotations

import dataclasses

import pytest

from featuregen.formula.schema_v2 import AggregateFunctionV2
from featuregen.formula.schema_v3 import SelectionKind, SemanticRowSelectionV1
from featuregen.materialize.action_authorization import ActionV1
from featuregen.materialize.action_decision import ask
from featuregen.materialize.action_dispositions import ACTION_DISPOSITIONS, Disposition
from featuregen.materialize.action_facts import ActionFactsV1
from featuregen.overlay.upload import semantic_eligibility_reasons as R
from featuregen.overlay.upload.binding_roles import JoinRole
from featuregen.overlay.upload.contract.capability import GUARD_RENDERED_WARNINGS
from featuregen.overlay.upload.feature_planning_contracts import planning_request_from_recipe
from featuregen.overlay.upload.planner.operand_role_gate import (
    operand_role_resolutions,
    unresolved_operand_roles,
)
from featuregen.overlay.upload.recipe_formula_blueprint_derivation import derive_blueprint_v2
from featuregen.overlay.upload.recipe_formula_expectations_v2 import has_reviewed_expectation
from featuregen.overlay.upload.recipe_grounding_context import canonical_recipe_v2_hash
from featuregen.overlay.upload.recipe_temporal_v2 import compile_temporal
from featuregen.overlay.upload.recipes.transaction_foundation import (
    TRANSACTION_FOUNDATION_RECIPES,
)

_BY_ID = {d.recipe_id: d for d in TRANSACTION_FOUNDATION_RECIPES}
_COUNT = _BY_ID["posted_debit_transaction_count"]
_CREDIT_AMOUNT = _BY_ID["posted_credit_amount"]

#: The canonical hash this recipe carried BEFORE A6b, recorded in-file the way A5 and A6 recorded
#: their declared identity moves. A6b's change is STRUCTURAL — a row selection, a new operand and a
#: re-bound window all change what the feature MEANS — so unlike A6's declaration-only ruling it
#: also bumps ``revision``, and the hash must move. A silent revert would restore this value.
_PRE_A6B_HASH = "06dd22603e51b3c28f2a0efe00a66d58fafab777173badebb3364e33ff1835c5"

#: A6b's identity-slot role name and the posting clock's role name — one spelling per test module.
_IDENTITY_ROLE = "transaction"
_POSTING_ROLE = "booking_ts"


def _selection(value: str) -> SemanticRowSelectionV1:
    return SemanticRowSelectionV1(SelectionKind.TRANSACTION_DIRECTION, "direction", value)


def _operand(definition, role: str):
    return next((op for op in definition.operands if op.role == role), None)


# ── 1. the DEBIT selection is structural, not a name ───────────────────────────────────────────


def test_the_debit_selection_is_structural_and_mirrors_the_credit_siblings_shape():
    """The C-A3b law applied to the count recipe: "debit" is a DECLARATION, in exactly the shape
    ``posted_credit_amount`` already carries, with the value inverted. Same kind, same role — only
    the semantic token differs, which is the whole point of the axis."""
    assert _COUNT.row_selections == (_selection("debit"),)

    credit, = _CREDIT_AMOUNT.row_selections
    debit, = _COUNT.row_selections
    assert (debit.kind, debit.role) == (credit.kind, credit.role)
    assert (debit.semantic_value, credit.semantic_value) == ("debit", "credit")


def test_the_selection_carries_the_governed_direction_convention_that_resolves_it():
    """A selection declares INTENT; without the ``direction_sign:`` policy nothing resolves the
    token to a source's literal. The recipe contract refuses the pair apart — proved by removing
    the policy ref rather than by re-reading the declaration."""
    assert any(ref.startswith("direction_sign:") for ref in _COUNT.eligibility.policy_refs)
    assert "one direction" in _COUNT.eligibility.included
    assert "the opposite direction" in _COUNT.eligibility.excluded

    from featuregen.overlay.upload.recipe_contract_v2 import (
        EligibilitySpecV2,
        RecipeContractError,
    )
    with pytest.raises(RecipeContractError, match="direction_sign"):
        dataclasses.replace(_COUNT, eligibility=EligibilitySpecV2(
            policy_refs=("eligible_status:foundation-posted-events",)))


# ── 2. the governed transaction identity, declared a KEY ───────────────────────────────────────


def test_the_governed_transaction_identity_operand_exists_and_is_declared_a_key():
    """R13's precondition: "COUNT_ROWS requires governed transaction identity". The slot names the
    concept ``transaction_id`` and DECLARES ``intermediate_entity_key`` — A6's ruling for
    identifier-valued ``dimension`` slots, taken here for the same reason: an identifier staged as
    a MEASURE is handed to the additivity matrix as something to aggregate."""
    identity = _operand(_COUNT, _IDENTITY_ROLE)
    assert identity is not None, "the count recipe must name the identity it counts"
    assert (identity.concept, identity.operand_class) == ("transaction_id", "dimension")
    assert identity.join_role == str(JoinRole.INTERMEDIATE_ENTITY_KEY)
    assert identity.required, "an identity a guard must check cannot be optional"
    assert identity.allowed_source_grains == ("transaction",)


def test_the_recipe_is_UNGATED_by_the_g2_serving_gate_and_the_declaration_is_what_does_it():
    """A6's gate, run over this recipe's own registry projection. The new slot is EXACTLY the shape
    A6 measured as divergent (``dimension``-classed on an entity-linked concept), so without the
    declaration A6b would have gated its own journey recipe. Both directions are asserted, so the
    declaration is proved load-bearing rather than decorative."""
    request = planning_request_from_recipe(_COUNT)
    assert unresolved_operand_roles(request) == ()

    resolution = {r.role: r for r in operand_role_resolutions(request)}[_IDENTITY_ROLE]
    assert resolution.projected_join_role is JoinRole.INTERMEDIATE_ENTITY_KEY
    assert "DECLARES" in resolution.detail

    stripped = planning_request_from_recipe(dataclasses.replace(_COUNT, operands=tuple(
        dataclasses.replace(op, join_role="") if op.role == _IDENTITY_ROLE else op
        for op in _COUNT.operands)))
    reverted = {r.role: r for r in unresolved_operand_roles(stripped)}
    assert set(reverted) == {_IDENTITY_ROLE}
    assert reverted[_IDENTITY_ROLE].projected_join_role is JoinRole.MEASURE
    assert reverted[_IDENTITY_ROLE].concept_ladder_join_role is JoinRole.INTERMEDIATE_ENTITY_KEY


# ── 3. R13: the operand exists to be CHECKED — COUNT_ROWS never consumes it ─────────────────────


def test_count_rows_consumes_no_operand_the_identity_slot_exists_to_be_CHECKED():
    """R13's sharpest line, pinned where the aggregate is actually chosen. The blueprint derivation
    maps ``count`` to ``(COUNT_ROWS, None)`` — no operand class — so adding the identity operand
    changes nothing about what the aggregate reads. The slot is there so a guard can CHECK
    uniqueness (step 7 compiles that guard); it is not a de-duplication argument."""
    from featuregen.overlay.upload.recipe_formula_blueprint_derivation import (
        BlueprintDerivationRefusal,
    )
    blueprint = derive_blueprint_v2(_COUNT)
    assert not isinstance(blueprint, BlueprintDerivationRefusal), blueprint
    expression, = blueprint.expressions
    assert expression.aggregation is AggregateFunctionV2.COUNT_ROWS
    assert expression.operand_role is None
    # and the identity role appears NOWHERE in the expression: not as the operand, not as the
    # relation anchor. A count that read it would be counting something else.
    assert expression.source_relation_role != _IDENTITY_ROLE
    assert _IDENTITY_ROLE not in (expression.operand_role, expression.source_relation_role)


def test_a_count_distinct_substitution_WOULD_consume_the_identity_which_is_why_r13_forbids_it():
    """The negative control that gives the rule above its teeth. Re-declare the same recipe as
    ``distinct_count`` and the derivation immediately binds the identity operand as the
    COUNT_DISTINCT argument — silently turning "refuse duplicates" into "count them once". R13
    names that substitution and forbids it; deduplication is chartered as an immutable
    ``TransactionDeduplicationPolicyRevision`` plus a typed survivor operator, never this."""
    substituted = dataclasses.replace(
        _COUNT, formula=dataclasses.replace(_COUNT.formula, result_class="distinct_count"))
    expression, = derive_blueprint_v2(substituted).expressions
    assert expression.aggregation is AggregateFunctionV2.COUNT_DISTINCT
    assert expression.operand_role == _IDENTITY_ROLE

    # the shipped recipe is NOT that recipe
    assert _COUNT.formula.result_class == "count"
    assert canonical_recipe_v2_hash(substituted) != canonical_recipe_v2_hash(_COUNT)


def test_the_refusal_code_carries_the_plans_matrix_row_through_the_canonical_service(db):
    """``TRANSACTION_IDENTITY_NOT_UNIQUE`` through ``action_decision.ask`` — the only verdict.

    The plan's matrix row reads *"Formula: Allow · Preview: Render guard; fixture/run REFUSES"*, so
    this code is NOT the "block from preview" shape its neighbours carry: preview PROCEEDS with the
    guard compiled in and the caller told, and the first act that would EXECUTE over the duplicates
    refuses. ``GUARD_RENDERED_WARNINGS`` is what makes "warns at preview" mean "renders WITH the
    guard" rather than "shrugs"."""
    assert R.reason_family(R.TRANSACTION_IDENTITY_NOT_UNIQUE) == "needs_setup"
    assert R.TRANSACTION_IDENTITY_NOT_UNIQUE in R.SERVING_CAPABILITY_MATRIX_CODES
    assert R.TRANSACTION_IDENTITY_NOT_UNIQUE in GUARD_RENDERED_WARNINGS

    facts = ActionFactsV1(member_names=("m",),
                          member_blockers={"m": (R.TRANSACTION_IDENTITY_NOT_UNIQUE,)})
    warned, blocked = set(), set()
    for action in ActionV1:
        decision = ask(db, facts.request(action=action, resource_identity_hash="a6b-r13"))
        member = decision.per_member[0]
        if R.TRANSACTION_IDENTITY_NOT_UNIQUE in member.warnings:
            warned.add(action)
        if R.TRANSACTION_IDENTITY_NOT_UNIQUE in member.blockers:
            blocked.add(action)

    assert warned == {ActionV1.AUTHOR_FORMULA, ActionV1.GENERATE_PREVIEW}
    assert blocked == set(ActionV1) - warned
    # the disposition table says the same thing, and it is the row the plan's matrix specifies
    assert ACTION_DISPOSITIONS[
        (R.TRANSACTION_IDENTITY_NOT_UNIQUE, ActionV1.GENERATE_PREVIEW)] is Disposition.WARN
    assert ACTION_DISPOSITIONS[
        (R.TEMPORAL_JOIN_POLICY_MISSING, ActionV1.GENERATE_PREVIEW)] is Disposition.BLOCK


# ── 4. the window binds the POSTING timestamp ──────────────────────────────────────────────────


def test_the_window_binds_the_posting_timestamp_role_not_a_generic_clock():
    """"Posted debit transactions over the window" is a claim about POSTING time. The window now
    names ``booking_ts``/``booking_date`` — the platform's existing vocabulary, and the column T0
    froze for the journey (``pstd_date``, governed concept ``booking_date``)."""
    assert _COUNT.temporal.event_time_role == _POSTING_ROLE
    posting = _operand(_COUNT, _POSTING_ROLE)
    assert posting is not None
    assert (posting.concept, posting.operand_class) == ("booking_date", "event_timestamp")

    # ONE clock, and it is the posting one: no generic `event_timestamp` slot survives to be
    # required, bound and never read.
    assert [op.role for op in _COUNT.operands if op.operand_class == "event_timestamp"] \
        == [_POSTING_ROLE]
    assert not any(op.concept == "event_timestamp" for op in _COUNT.operands)


def test_the_posting_window_compiles_and_reaches_the_blueprint():
    """The binding is real at both layers the platform reads it through: ``compile_temporal``
    resolves the role to a declared timestamp operand (an unbound role is
    ``event_time_role_unbound``), and the derived blueprint's window and relation anchor both name
    it — COUNT_ROWS binds no operand but always binds a time."""
    compiled = compile_temporal(_COUNT)
    assert compiled.status == "compiled", compiled.blockers
    assert _POSTING_ROLE in compiled.pit_text

    expression, = derive_blueprint_v2(_COUNT).expressions
    assert expression.window.event_time_role == _POSTING_ROLE
    assert expression.source_relation_role == _POSTING_ROLE
    assert expression.row_selections == (_selection("debit"),)
    assert expression.authority_refs.direction_policy_ref.startswith("direction_sign:")


# ── 5. the revision bump, and what it invalidates ──────────────────────────────────────────────


def test_the_revision_bump_moved_the_canonical_hash():
    """A6 ruled that DECLARING a ``join_role`` warrants no revision bump — the canonical hash is
    field-exhaustive and moves on the declaration alone, so bumping too would falsely imply a
    semantic revision. A6b is the OTHER case: a row selection, a new operand and a re-bound window
    change what this feature MEANS, which is exactly what a revision says. So it bumps, and the
    hash moves for both reasons at once."""
    assert _COUNT.revision == 2
    assert canonical_recipe_v2_hash(_COUNT) != _PRE_A6B_HASH
    # the bump is itself hash-bearing — the canonical form is field-exhaustive
    assert canonical_recipe_v2_hash(dataclasses.replace(_COUNT, revision=1)) \
        != canonical_recipe_v2_hash(_COUNT)
    # and the sibling that A6b did NOT change keeps revision 1 (the bump is per-definition)
    assert _CREDIT_AMOUNT.revision == 1


def test_the_bump_strips_no_reviewed_expectation_because_this_recipe_never_had_one():
    """What a bump invalidates is REVIEW EVIDENCE keyed on the recipe revision hash. This recipe
    holds none to lose: the pack's only reviewed Formula-v2 expectation is ``posted_debit_amount``'s
    (plan §V9), which A6b does not touch, and readiness is still granted by the registry."""
    assert not has_reviewed_expectation(_COUNT.formula.expectation_ref)
    assert _COUNT.readiness == "FORMULA_BLOCKED"
    assert has_reviewed_expectation(_BY_ID["posted_debit_amount"].formula.expectation_ref)
    assert _BY_ID["posted_debit_amount"].revision == 1


def test_the_credit_count_sibling_is_still_prose_only_and_that_asymmetry_is_deliberate():
    """A disclosed consequence, pinned rather than left to be discovered. A6b's scope is the DEBIT
    count recipe — the one D2/D5 count with. ``posted_credit_transaction_count`` is its mirror and
    carries the SAME three defects; ruling on it is the same edit and needs the same authorisation
    (and its own revision bump, which moves its hash for every live review row). Making the
    asymmetry visible is what stops it being an accident."""
    credit_count = _BY_ID["posted_credit_transaction_count"]
    assert credit_count.row_selections == ()
    assert credit_count.temporal.event_time_role == "event_ts"
    assert _operand(credit_count, _IDENTITY_ROLE) is None
    assert credit_count.revision == 1
