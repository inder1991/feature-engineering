"""BR-21 — the final coverage packs: every selectable leaf covered or intentionally empty."""
from __future__ import annotations

from featuregen.overlay.upload.recipes.fincrime_expansion import (
    FINCRIME_EXPANSION_RECIPES,
)
from featuregen.overlay.upload.recipes.lending_lifecycle import LENDING_LIFECYCLE_RECIPES
from featuregen.overlay.upload.taxonomy.coverage import coverage_report

ALL = (*LENDING_LIFECYCLE_RECIPES, *FINCRIME_EXPANSION_RECIPES)
BY_ID = {r.recipe_id: r for r in ALL}


def test_the_coverage_program_is_complete():
    """The milestone the whole tier machinery was built to reach: ZERO gaps — every
    selectable leaf carries a reviewed V2 primary or is intentionally empty with its owner
    and rationale. A future regression reopens a gap and fails HERE."""
    report = coverage_report()
    tiers = report["coverage_tier_by_leaf"]
    assert {t for t in tiers.values()} == {"AUTHORED_PRIMARY", "INTENTIONALLY_EMPTY"}
    assert sum(1 for t in tiers.values() if t == "AUTHORED_PRIMARY") == 75
    assert sum(1 for t in tiers.values() if t == "INTENTIONALLY_EMPTY") == 13


def test_every_recipe_answers_a_named_decision():
    """The admission rule's structural half: an end-user decision, atomic output, exact
    grains, a formula path and declared temporal semantics — no pack added merely to turn a
    leaf green."""
    for r in ALL:
        assert r.decision_context, r.recipe_id
        assert r.source_grain and r.output_grain, r.recipe_id
        assert r.formula is not None, r.recipe_id
        assert r.temporal.anchor_kind, r.recipe_id


def test_affordability_reads_governed_income_never_a_guess():
    r = BY_ID["disposable_income_share"]
    assert any(op.concept == "customer_income" for op in r.operands)
    assert "one salary-like credit" in r.eligibility.excluded
    assert "never defaulted" in r.output.null_input_policy


def test_mitigation_counts_only_enforceable_mitigants():
    r = BY_ID["mitigation_coverage_share"]
    assert any(ref.startswith("allocation:mitigant-enforceability")
               for ref in r.eligibility.policy_refs)
    assert "face values without haircut" in r.eligibility.excluded


def test_recovery_cash_is_post_default_only():
    r = BY_ID["recovery_cash_collected"]
    assert r.leakage.classification == "outcome"
    assert "default_prediction" in r.leakage.prohibited_stages


def test_the_ato_shape_is_a_sequence_not_a_score():
    r = BY_ID["credential_change_then_payment_flag"]
    assert "sequence IS the signal" in r.eligibility.excluded
    assert r.output.output_type == "boolean"


def test_synthetic_identity_reads_the_bureau_through_knowledge_time():
    r = BY_ID["thin_file_rapid_acquisition_flag"]
    assert r.temporal.knowledge_time_role == "knowledge_ts"
    assert any(op.concept == "thin_file_flag" for op in r.operands)
    assert "never" in r.output.null_input_policy and "thick" in r.output.null_input_policy


def test_sanctions_and_screening_are_control_state_facts():
    hits = BY_ID["sanctions_hit_pending_count"]
    assert hits.leakage.classification == "near_label"
    assert "absence of feed" in hits.output.empty_population_policy
    coverage = BY_ID["screening_coverage_share"]
    assert "UNSCREENED, never dropped" in coverage.output.null_input_policy


def test_structuring_persistence_reads_the_governed_threshold():
    r = BY_ID["sub_threshold_cash_day_count"]
    assert any(op.status_policy_ref.startswith("threshold:aml-reporting")
               for op in r.operands)
    assert r.primary_objective == "aml_cft.structuring"


def test_the_intentionally_empty_leaves_stay_untouched():
    for r in ALL:
        assert r.primary_objective not in ("aml_cft.mule_account", "aml_cft.tbml"), r.recipe_id
