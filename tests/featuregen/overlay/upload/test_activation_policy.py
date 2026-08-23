"""Remediation A1 — the activation policy: every rule row proven in both directions."""
from __future__ import annotations

import dataclasses
import inspect
from dataclasses import replace

import pytest

from featuregen.overlay.upload import semantic_eligibility_reasons as R
from featuregen.overlay.upload.activation_policy import (
    ACTIVATION_ACTIONS,
    ActivationPolicyError,
    CurrentActivationStateV1,
    FrozenOptionFactsV1,
    activation_decision,
    decide_all_actions,
)

#: A frozen option with NOTHING wrong — the maximal-green fixture every negative case breaks.
CLEAN_FROZEN = FrozenOptionFactsV1(
    binding_state="bound",
    generation_source="recipe",
    computation_kind="deterministic_formula",
    readiness="FORMULA_VALIDATED",
    review_current=True,
    recipe_revision_hash="rev1",
    confirmation_required_roles=(),
    has_reviewed_formula_expectation=True,
    plan_envelope_present=True,
    validation_status="DESIGN_CHECKED",
    formula_expectation_revision="fx1",
    snapshot_id="snap1",
)

#: The matching current state, everything re-verified green.
CLEAN_CURRENT = CurrentActivationStateV1(
    review_current=True,
    policy_revisions_current=True,
    snapshot_freshness="current",
    effective_readiness="MATERIALIZATION_READY",
    formula_expectation_revision="fx1",
    formula_schema_support="supported",
    requirements_closed=True,
    execution_authority_evaluated=True,
    execution_floor_met=True,
    authoring_floor_met=True,
    uoa_current=True,
)


def codes(decision):
    return [b.code for b in decision.blockers]


# ── the ladder itself ───────────────────────────────────────────────────────────────────────────

def test_save_idea_is_always_allowed_even_for_the_worst_candidate():
    worst = FrozenOptionFactsV1(
        binding_state="blocked", generation_source="llm_intent",
        computation_kind="conceptual_pattern", readiness="CONCEPTUAL_ONLY",
        review_current=False, confirmation_required_roles=("who", "when"))
    decision = activation_decision(worst, CurrentActivationStateV1(), "save_idea")
    assert decision.allowed and decision.blockers == ()


def test_the_clean_option_walks_the_whole_ladder():
    for action in ACTIVATION_ACTIONS:
        decision = activation_decision(CLEAN_FROZEN, CLEAN_CURRENT, action)
        assert decision.allowed, (action, codes(decision))


def test_an_unknown_action_raises_a_programmer_error():
    with pytest.raises(ActivationPolicyError):
        activation_decision(CLEAN_FROZEN, CLEAN_CURRENT, "publish")


def test_the_fold_is_pure_no_connection_in_its_signature():
    assert "conn" not in inspect.signature(activation_decision).parameters


def test_the_unassembled_current_state_blocks_everything_above_save_idea():
    """Every CurrentActivationStateV1 default is the failing side — an unmeasured state gates."""
    decision = activation_decision(CLEAN_FROZEN, CurrentActivationStateV1(), "create_contract")
    assert not decision.allowed
    assert R.SNAPSHOT_STALE_REGENERATE in codes(decision)


# ── create_contract: each rule row, negative direction ─────────────────────────────────────────

@pytest.mark.parametrize("frozen_break, expected", [
    (dict(binding_state="ambiguous"), R.BINDING_NOT_BOUND),
    (dict(confirmation_required_roles=("who",)), R.PROPOSED_METADATA_ONLY),
    (dict(review_current=False), R.RECIPE_REVIEW_NOT_CURRENT),
    (dict(computation_kind="conceptual_pattern"), R.CONCEPTUAL_PATTERN_NOT_AUTHORABLE),
    (dict(plan_envelope_present=False), R.PHYSICAL_PLAN_MISSING),
])
def test_each_frozen_defect_blocks_create_contract_with_its_code(frozen_break, expected):
    decision = activation_decision(
        replace(CLEAN_FROZEN, **frozen_break), CLEAN_CURRENT, "create_contract")
    assert not decision.allowed
    assert expected in codes(decision)
    assert all(b.next_step for b in decision.blockers)       # every blocker names the fix


@pytest.mark.parametrize("current_break, expected", [
    (dict(review_current=False), R.RECIPE_REVIEW_NOT_CURRENT),        # revoked AFTER generation
    (dict(snapshot_freshness="drifted"), R.SNAPSHOT_STALE_REGENERATE),
    (dict(snapshot_freshness="unverifiable"), R.SNAPSHOT_STALE_REGENERATE),  # fails CLOSED
    (dict(policy_revisions_current=False), R.ACTIVATION_STATE_DRIFTED),
])
def test_each_current_state_drift_blocks_create_contract(current_break, expected):
    decision = activation_decision(
        CLEAN_FROZEN, replace(CLEAN_CURRENT, **current_break), "create_contract")
    assert not decision.allowed
    assert expected in codes(decision)


def test_formula_blocked_readiness_does_NOT_block_contract_creation():
    """Parent-plan SE-12 rule 3: a bound deterministic candidate may be contracted while its
    formula readiness is blocked — the contract CARRIES the state; execution stays refused.
    (Requiring a reviewed formula here would cap E2E at the 3 recipes that have one.)"""
    frozen = replace(CLEAN_FROZEN, readiness="FORMULA_BLOCKED",
                     has_reviewed_formula_expectation=False, formula_expectation_revision="")
    current = replace(CLEAN_CURRENT, effective_readiness="FORMULA_BLOCKED",
                      formula_schema_support="unsupported", formula_expectation_revision="")
    assert activation_decision(frozen, current, "create_contract").allowed
    materialize = activation_decision(frozen, current, "execute_materialization")
    assert not materialize.allowed
    assert R.FORMULA_NOT_REVIEWED in codes(materialize)
    assert R.READINESS_NOT_MATERIALIZATION_READY in codes(materialize)


def test_llm_intent_origin_needs_no_recipe_review_but_conceptual_stays_unauthorable():
    """An intent has no recipe review to demand — but a deterministic intent projects as
    conceptual_pattern (the structural ceiling), so it saves as an idea, never a contract."""
    intent = replace(CLEAN_FROZEN, generation_source="llm_intent",
                     computation_kind="conceptual_pattern", review_current=False)
    decision = activation_decision(intent, CLEAN_CURRENT, "create_contract")
    assert R.RECIPE_REVIEW_NOT_CURRENT not in codes(decision)
    assert R.CONCEPTUAL_PATTERN_NOT_AUTHORABLE in codes(decision)


# ── author_formula: never gated on the readiness it improves ───────────────────────────────────

def test_authoring_is_allowed_while_formula_readiness_is_blocked():
    frozen = replace(CLEAN_FROZEN, readiness="FORMULA_BLOCKED",
                     has_reviewed_formula_expectation=False, formula_expectation_revision="")
    current = replace(CLEAN_CURRENT, effective_readiness="FORMULA_BLOCKED",
                      formula_schema_support="unsupported", formula_expectation_revision="")
    assert activation_decision(frozen, current, "author_formula").allowed


# ── materialization: the strong end of the ladder ──────────────────────────────────────────────

@pytest.mark.parametrize("current_break, expected", [
    (dict(effective_readiness="FORMULA_VALIDATED"), R.READINESS_NOT_MATERIALIZATION_READY),
    (dict(formula_schema_support="unsupported"), R.FORMULA_SCHEMA_UNSUPPORTED),
    (dict(formula_expectation_revision="fx2"), R.ACTIVATION_STATE_DRIFTED),
    (dict(execution_authority_evaluated=False), R.EXECUTION_AUTHORITY_UNEVALUATED),
    (dict(execution_floor_met=False), R.EXECUTION_AUTHORITY_UNMET),
])
def test_each_materialization_gate_blocks_with_its_code(current_break, expected):
    decision = activation_decision(
        CLEAN_FROZEN, replace(CLEAN_CURRENT, **current_break), "execute_materialization")
    assert not decision.allowed
    assert expected in codes(decision)


def test_outstanding_validation_blocks_materialize_unless_requirements_closed():
    frozen = replace(CLEAN_FROZEN, validation_status="NEEDS_EXTERNAL_VALIDATION",
                     outstanding_requirement_codes=(R.IDENTIFIER_UNIQUENESS,))
    blocked = activation_decision(
        frozen, replace(CLEAN_CURRENT, requirements_closed=False), "request_materialization")
    assert R.EXTERNAL_VALIDATION_OUTSTANDING in codes(blocked)
    closed = activation_decision(
        frozen, replace(CLEAN_CURRENT, requirements_closed=True), "request_materialization")
    assert R.EXTERNAL_VALIDATION_OUTSTANDING not in codes(closed)


# ── the wire-shape source ───────────────────────────────────────────────────────────────────────

def test_decide_all_actions_is_the_same_fold_per_rung():
    everything = decide_all_actions(CLEAN_FROZEN, CurrentActivationStateV1())
    assert set(everything) == set(ACTIVATION_ACTIONS)
    assert everything["save_idea"].allowed
    assert not everything["create_contract"].allowed
    assert not everything["execute_materialization"].allowed


def test_every_blocker_code_has_a_reason_family():
    """Activation codes join the closed vocabulary — the family pin covers them."""
    worst = FrozenOptionFactsV1(
        binding_state="blocked", generation_source="recipe",
        computation_kind="conceptual_pattern", readiness="CONCEPTUAL_ONLY",
        review_current=False, confirmation_required_roles=("who",))
    decision = activation_decision(worst, CurrentActivationStateV1(),
                                   "execute_materialization")
    for blocker in decision.blockers:
        assert R.reason_family(blocker.code)


def test_a_uoa_reconfirmed_differently_after_serving_is_activation_drift():
    """B10 item 4: the human changed the confirmed unit of analysis AFTER this card was
    served — the card answers a stale question; every rung above save_idea blocks with the
    regenerate blocker."""
    from dataclasses import replace

    moved = replace(CLEAN_CURRENT, uoa_current=False)
    decision = activation_decision(CLEAN_FROZEN, moved, "create_contract")
    assert not decision.allowed
    assert "ACTIVATION_STATE_DRIFTED" in codes(decision)
    assert activation_decision(CLEAN_FROZEN, moved, "save_idea").allowed


def test_an_authority_that_weakened_since_serving_blocks_authoring_as_drift():
    """C2: the card was served clean (no confirmation-required roles), but a bound operand's
    CURRENT resolved authority no longer clears the authoring floor — the durable write
    refuses with the named re-confirm step. The serve-time story is untouched: a card whose
    floors failed AT SERVING carries PROPOSED_METADATA_ONLY instead (no double-naming)."""
    from dataclasses import replace

    weakened = replace(CLEAN_CURRENT, authoring_floor_met=False)
    decision = activation_decision(CLEAN_FROZEN, weakened, "create_contract")
    assert not decision.allowed
    assert "SEMANTIC_AUTHORITY_INSUFFICIENT" in codes(decision)

    served_blocked = replace(CLEAN_FROZEN, confirmation_required_roles=("customer",))
    decision = activation_decision(served_blocked, weakened, "create_contract")
    assert "PROPOSED_METADATA_ONLY" in codes(decision)
    assert "SEMANTIC_AUTHORITY_INSUFFICIENT" not in codes(decision)


def test_materialization_is_floor_driven_not_unconditionally_blocked():
    """C2 closes A1's placeholder: with the floor EVALUATED and met (plus readiness, formula,
    schema, checks), materialization is allowed; an unmet floor names the raise-authority
    step; an unevaluated floor still fails closed."""
    from dataclasses import replace

    assert activation_decision(CLEAN_FROZEN, CLEAN_CURRENT,
                               "request_materialization").allowed

    unmet = replace(CLEAN_CURRENT, execution_floor_met=False)
    decision = activation_decision(CLEAN_FROZEN, unmet, "request_materialization")
    assert "EXECUTION_AUTHORITY_UNMET" in codes(decision)

    unevaluated = replace(CLEAN_CURRENT, execution_authority_evaluated=False,
                          execution_floor_met=False)
    decision = activation_decision(CLEAN_FROZEN, unevaluated, "request_materialization")
    assert "EXECUTION_AUTHORITY_UNEVALUATED" in codes(decision)


def test_unlicensed_personal_data_refuses_the_governed_contract():
    """C4: the frozen decision carried PERSONAL_DATA_POLICY_REQUIRED among its outstanding
    requirements — read-allowed is not use-allowed, and no governed contract forms over it.
    The blocker names the governance action; save_idea stays open."""
    from dataclasses import replace

    frozen = replace(CLEAN_FROZEN,
                     outstanding_requirement_codes=("PERSONAL_DATA_POLICY_REQUIRED",))
    decision = activation_decision(frozen, CLEAN_CURRENT, "create_contract")
    assert not decision.allowed
    assert "PERSONAL_DATA_POLICY_REQUIRED" in codes(decision)
    assert activation_decision(frozen, CLEAN_CURRENT, "save_idea").allowed


# ── §6 — the schema-support tri-state: each failure surfaces under its own name ────────────────

def test_unmeasured_because_unreviewed_is_named_a_GOVERNANCE_gap_not_an_engine_one():
    """The old bool told an unreviewed recipe "engine support not implemented" — a FALSE
    statement that sent an operator to the engine team for a governance problem. §6: missing
    review must never wear an engine-capability code."""
    decision = activation_decision(
        CLEAN_FROZEN,
        replace(CLEAN_CURRENT, formula_schema_support="unmeasured_unreviewed"),
        "execute_materialization")
    assert R.FORMULA_REVIEW_UNMEASURED in codes(decision)
    assert R.FORMULA_SCHEMA_UNSUPPORTED not in codes(decision)
    assert R.ENGINE_CAPABILITY_UNMEASURED not in codes(decision)


def test_unmeasured_because_the_engine_is_unknown_gets_its_OWN_code():
    decision = activation_decision(
        CLEAN_FROZEN,
        replace(CLEAN_CURRENT, formula_schema_support="unmeasured_engine"),
        "execute_materialization")
    assert R.ENGINE_CAPABILITY_UNMEASURED in codes(decision)
    assert R.FORMULA_SCHEMA_UNSUPPORTED not in codes(decision)


def test_an_UNRECOGNIZED_support_value_is_an_unmeasured_one_fail_closed():
    """An unrecognized measurement is an unmeasured one — never a pass, never a crash."""
    decision = activation_decision(
        CLEAN_FROZEN, replace(CLEAN_CURRENT, formula_schema_support="banana"),
        "execute_materialization")
    assert R.ENGINE_CAPABILITY_UNMEASURED in codes(decision)


def test_FORMULA_SCHEMA_UNSUPPORTED_is_reserved_for_a_measured_no():
    """§6: the code is entitled to speak only when classify_demands_for_engine actually ran and
    actually said no — and then none of the unmeasured codes ride along."""
    decision = activation_decision(
        CLEAN_FROZEN, replace(CLEAN_CURRENT, formula_schema_support="unsupported"),
        "execute_materialization")
    assert R.FORMULA_SCHEMA_UNSUPPORTED in codes(decision)
    assert R.FORMULA_REVIEW_UNMEASURED not in codes(decision)
    assert R.ENGINE_CAPABILITY_UNMEASURED not in codes(decision)


# ── Task S1A-5a — the two appended facts, and the compatibility they must not break ────────────

def test_the_cross_catalog_facts_are_APPENDED_and_DEFAULTED():
    """``FrozenOptionFactsV1`` is constructed positionally in production code and across the
    fixtures. The two new facts therefore go at the END with defaults whose meaning is "nobody
    said" — an empty plan kind and no pairs, which every fold reads as "measure nothing here"."""
    frozen = FrozenOptionFactsV1(
        binding_state="bound", generation_source="recipe",
        computation_kind="deterministic_formula", readiness="FORMULA_VALIDATED",
        review_current=True)

    assert frozen.plan_kind == ""
    assert frozen.read_set_pairs == ()
    fields = [f.name for f in dataclasses.fields(FrozenOptionFactsV1)]
    assert fields[-2:] == ["plan_kind", "read_set_pairs"], fields


def test_the_positional_construction_every_existing_caller_uses_still_works():
    """The five required facts, positionally, exactly as ``CLEAN_FROZEN`` and the production
    assembler spell them — the compatibility this task promised to verify rather than assume."""
    frozen = FrozenOptionFactsV1("bound", "recipe", "deterministic_formula",
                                 "FORMULA_VALIDATED", True)

    assert (frozen.binding_state, frozen.readiness) == ("bound", "FORMULA_VALIDATED")
    assert (frozen.plan_kind, frozen.read_set_pairs) == ("", ())


def test_the_pairs_are_carried_verbatim_and_stay_hashable():
    """The facts record is frozen and used as a value — the pairs must be a tuple of tuples, not
    a list of lists that would make the whole record unhashable the moment one is present."""
    pairs = (("bank", "public.accounts.acct_id"), ("crm", "sales.customers.cust_id"))
    governed = replace(CLEAN_FROZEN, plan_kind="governed_cross_catalog", read_set_pairs=pairs)

    assert governed.read_set_pairs == pairs
    assert len({governed, replace(CLEAN_FROZEN, plan_kind="governed_cross_catalog",
                                  read_set_pairs=pairs)}) == 1
