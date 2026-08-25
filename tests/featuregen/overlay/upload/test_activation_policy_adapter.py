"""R1 (2026-08-24 plan) — the ladder's ``author_formula`` rung is an ADAPTER over the canonical
six-action authority, and the two materialization rungs are RETIRED IN PLACE.

The disagreement-inventory regressions here pin T0's verified §V8 rows: for each fact the two
live authorities answered DIFFERENTLY at AUTHOR_FORMULA, the ladder now serves the CANONICAL
answer — and each behavior change is documented as the deliberate R1 consequence, not an
accident a future reader should "fix".
"""
from __future__ import annotations

from dataclasses import replace

from featuregen.materialize.action_authorization import ActionV1
from featuregen.materialize.action_decision import ActionRequestV1, ask
from featuregen.overlay.upload import semantic_eligibility_reasons as R
from featuregen.overlay.upload.activation_policy import (
    CurrentActivationStateV1,
    FrozenOptionFactsV1,
    activation_decision,
)

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


def codes(blockers):
    return [b.code for b in blockers]


# ══ THE MANDATORY ROW — T0 concern 3, disagreement inventory §V8 ═════════════════════════════════
def test_STALE_RECIPE_REVIEW_previously_BLOCKED_authoring_and_now_WARNS():
    """▲ THE DELIBERATE R1 BEHAVIOR CHANGE, pinned so it is a documented ruling rather than a
    surprise. T0's verified disagreement row (§V8): the legacy ladder BLOCKED author_formula on
    RECIPE_REVIEW_NOT_CURRENT (activation_policy.py's create_contract rule set) while the
    canonical table says WARN at AUTHOR_FORMULA (action_dispositions: review facts warn;
    production gates by the same code's own production cells, which stay BLOCK). R1 rules
    canonical-wins: authoring proceeds, the caller MUST be told (D3), and the review fact still
    blocks create_contract (the legacy rung the ladder keeps) and both production acts."""
    stale = replace(CLEAN_FROZEN, review_current=False)

    authoring = activation_decision(stale, CLEAN_CURRENT, "author_formula")
    assert authoring.allowed                                     # previously: blocked
    assert R.RECIPE_REVIEW_NOT_CURRENT in codes(authoring.warnings)
    assert R.RECIPE_REVIEW_NOT_CURRENT not in codes(authoring.blockers)
    # The warning keeps its human next step — WARN is "proceeds AND the caller is told".
    warning = next(w for w in authoring.warnings if w.code == R.RECIPE_REVIEW_NOT_CURRENT)
    assert warning.next_step

    contracting = activation_decision(stale, CLEAN_CURRENT, "create_contract")
    assert not contracting.allowed                               # the funnel still gates HERE
    assert R.RECIPE_REVIEW_NOT_CURRENT in codes(contracting.blockers)


def test_UNCONFIRMED_SEMANTICS_previously_BLOCKED_authoring_and_now_DROP():
    """Disagreement rows 2 and 3: PROPOSED_METADATA_ONLY and SEMANTIC_AUTHORITY_INSUFFICIENT are
    semantic-confirmation facts, and the canonical table rules them not any of the six actions'
    gate (DROP — recorded, never discarded). The ladder served them as author_formula blockers;
    canonical wins. They still block create_contract, where the funnel actually lives."""
    unconfirmed = replace(CLEAN_FROZEN, confirmation_required_roles=("customer",))
    weakened = replace(CLEAN_CURRENT, authoring_floor_met=False)

    authoring = activation_decision(unconfirmed, weakened, "author_formula")
    assert authoring.allowed
    assert R.PROPOSED_METADATA_ONLY in authoring.dropped         # recorded, not discarded
    assert R.PROPOSED_METADATA_ONLY not in codes(authoring.blockers)

    contracting = activation_decision(unconfirmed, weakened, "create_contract")
    assert R.PROPOSED_METADATA_ONLY in codes(contracting.blockers)

    served_clean = activation_decision(CLEAN_FROZEN, weakened, "author_formula")
    assert served_clean.allowed
    assert R.SEMANTIC_AUTHORITY_INSUFFICIENT in served_clean.dropped
    assert R.SEMANTIC_AUTHORITY_INSUFFICIENT in codes(
        activation_decision(CLEAN_FROZEN, weakened, "create_contract").blockers)


def test_the_facts_whose_canonical_cell_says_BLOCK_still_block_authoring():
    """The adapter downgrades nothing on its own authority — a fact blocks author_formula
    exactly when its canonical cell says BLOCK, next steps preserved."""
    for frozen_break, expected in [
        (dict(binding_state="ambiguous"), R.BINDING_NOT_BOUND),
        (dict(computation_kind="conceptual_pattern"), R.CONCEPTUAL_PATTERN_NOT_AUTHORABLE),
        (dict(plan_envelope_present=False), R.PHYSICAL_PLAN_MISSING),
    ]:
        decision = activation_decision(
            replace(CLEAN_FROZEN, **frozen_break), CLEAN_CURRENT, "author_formula")
        assert not decision.allowed, expected
        assert expected in codes(decision.blockers)
        assert all(b.next_step for b in decision.blockers)
    drifted = activation_decision(
        CLEAN_FROZEN, replace(CLEAN_CURRENT, snapshot_freshness="drifted"), "author_formula")
    assert not drifted.allowed
    assert R.SNAPSHOT_STALE_REGENERATE in codes(drifted.blockers)


# ══ the adapter's answer IS the canonical service's ══════════════════════════════════════════════
def test_the_adapters_answer_is_BYTE_EQUAL_to_ask_over_the_same_facts():
    """The one-authority proof: for a grid of frozen/current states, feeding the ladder's own
    fact codes (the create_contract rule set — the same builder the rung adapts) through the
    canonical service's ``ask`` yields exactly the adapter's verdict, blockers and warnings.
    ``ask`` with ``authorization_id=None`` reads nothing, so no database is needed."""
    grid = [
        (CLEAN_FROZEN, CLEAN_CURRENT),
        (replace(CLEAN_FROZEN, review_current=False), CLEAN_CURRENT),
        (replace(CLEAN_FROZEN, binding_state="missing"), CLEAN_CURRENT),
        (replace(CLEAN_FROZEN, confirmation_required_roles=("who",)),
         replace(CLEAN_CURRENT, authoring_floor_met=False)),
        (replace(CLEAN_FROZEN, computation_kind="conceptual_pattern", review_current=False),
         replace(CLEAN_CURRENT, snapshot_freshness="unverifiable",
                 policy_revisions_current=False)),
    ]
    for frozen, current in grid:
        ladder_facts = codes(
            activation_decision(frozen, current, "create_contract").blockers)
        service = ask(None, ActionRequestV1(
            action=ActionV1.AUTHOR_FORMULA, resource_identity_hash="subject-key",
            member_names=("option",), member_blockers={"option": tuple(ladder_facts)}))
        adapter = activation_decision(frozen, current, "author_formula")

        assert adapter.allowed == service.allowed, (frozen, current)
        [verdict] = service.per_member
        assert tuple(codes(adapter.blockers)) == verdict.blockers
        assert tuple(codes(adapter.warnings)) == verdict.warnings
        assert adapter.dropped == verdict.dropped


# ══ the two materialization rungs: RETIRED IN PLACE, deliberately NOT adapted ════════════════════
def test_the_materialization_rungs_keep_the_legacy_fold_UNTIL_step8():
    """The documented deferral, pinned so it reads as a decision: the V1-lane rungs still block
    on READINESS_NOT_MATERIALIZATION_READY — a code the canonical table DROPS everywhere (§6:
    readiness authorizes nothing). Folding the V1 lane's stand-in facts through the canonical
    table would turn "no executable formula" into an ALLOW, because the lane cannot supply the
    canonical FORMULA_NOT_AUTHORED fact. The rungs therefore keep the legacy fold, frozen, and
    their canonical successor is EXECUTE_SANDBOX over the sealed-artifact subject (step 8/B3)."""
    not_ready = replace(CLEAN_CURRENT, effective_readiness="FORMULA_VALIDATED")
    for rung in ("request_materialization", "execute_materialization"):
        decision = activation_decision(CLEAN_FROZEN, not_ready, rung)
        assert not decision.allowed, rung
        assert R.READINESS_NOT_MATERIALIZATION_READY in codes(decision.blockers), rung
        # The legacy fold carries no adapter channels — appended fields stay defaulted.
        assert decision.warnings == () and decision.dropped == ()
