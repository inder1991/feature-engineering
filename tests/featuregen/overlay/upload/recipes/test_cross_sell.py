"""BR-11 — the cross-sell pack: models separated from formulas, denominators owned or refused."""
from __future__ import annotations

from featuregen.overlay.upload.model_feature_registry import MODEL_FEATURES, model_feature_by_id
from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES, v2_replaced_legacy_ids
from featuregen.overlay.upload.recipes.cross_sell import (
    CROSS_SELL_MODEL_FEATURES,
    CROSS_SELL_RECIPES,
)
from featuregen.overlay.upload.templates import CROSS_SELL_TEMPLATES

BY_ID = {r.recipe_id: r for r in CROSS_SELL_RECIPES}


def test_every_legacy_cross_sell_id_is_explicitly_replaced():
    legacy = {t.id for t in CROSS_SELL_TEMPLATES}
    assert legacy <= v2_replaced_legacy_ids()
    assert set(BY_ID) <= {r.recipe_id for r in V2_RECIPES}


def test_model_outputs_are_visibly_separated_from_deterministic_features():
    """The acceptance sentence: NBP propensity and CLV projection are governed_model_output
    recipes referencing REGISTERED ModelFeatureSpecs — and those specs are honestly
    MODEL_SPEC_BLOCKED (no model version registered), never claiming readiness."""
    for rid, ref in (("next_best_product_propensity", "nbp_propensity"),
                     ("clv_projected_value", "clv_projection")):
        r = BY_ID[rid]
        assert r.computation_kind == "governed_model_output"
        assert r.formula is None
        spec = model_feature_by_id(ref)
        assert spec is not None and spec.model_version == ""
    assert {m.model_feature_id for m in CROSS_SELL_MODEL_FEATURES} <= {
        m.model_feature_id for m in MODEL_FEATURES}

    from featuregen.overlay.upload.model_feature_readiness import (
        ModelReadinessInputsV1,
        fold_model_readiness,
    )
    readiness = fold_model_readiness(model_feature_by_id("nbp_propensity"),
                                     ModelReadinessInputsV1())
    assert readiness.state == "MODEL_SPEC_BLOCKED"
    assert "model_version_absent" in readiness.blockers


def test_whitespace_requires_the_effective_dated_eligible_universe():
    r = BY_ID["whitespace_product_gap"]
    universe = next(op for op in r.operands if op.role == "eligible_universe")
    assert universe.operand_class == "policy_input"
    assert universe.status_policy_ref.startswith("active_state:")
    assert "no universe" in r.output.empty_population_policy


def test_no_output_claims_wallet_share_without_its_denominator():
    """The acceptance: internal penetration is NAMED internal penetration; share of wallet is
    conceptual until an external or modelled total-wallet denominator exists."""
    internal = BY_ID["internal_penetration_share"]
    assert internal.computation_kind == "deterministic_formula"
    assert "INTERNAL" in internal.business_definition
    wallet = BY_ID["share_of_wallet"]
    assert wallet.computation_kind == "conceptual_pattern"
    assert "denominator" in wallet.conceptual_reason
    assert "internal_penetration_share" in wallet.conceptual_reason


def test_campaign_response_is_descriptive_and_requires_treatment():
    rate = BY_ID["campaign_response_rate"]
    assert "never predictive uplift" in rate.business_definition
    assert "no prior recorded treatment" in rate.eligibility.excluded
    assert rate.output.zero_denominator_policy
    recency = BY_ID["campaign_response_recency_days"]
    assert recency.formula.result_class == "recency"


def test_household_rollups_require_verified_membership_and_allocation():
    r = BY_ID["household_relationship_value"]
    member = next(op for op in r.operands if op.concept == "household_id")
    assert "VERIFIED" in member.relationship_requirement
    assert any(ref.startswith("allocation:") for ref in r.eligibility.policy_refs)
    assert r.output_grain == "household"


def test_tenure_only_upsell_scoring_is_conceptual_without_a_suitability_policy():
    r = BY_ID["tenure_upsell_readiness"]
    assert r.computation_kind == "conceptual_pattern"
    assert "suitability" in r.conceptual_reason


def test_clv_keeps_its_deterministic_history_beside_the_model():
    hist = BY_ID["historical_product_revenue"]
    assert hist.computation_kind == "deterministic_formula"
    assert hist.formula.result_class == "sum"
    revenue = next(op for op in hist.operands if op.role == "revenue")
    assert revenue.economic_role == "recognized_customer_revenue"
    assert BY_ID["clv_projected_value"].replaces_legacy_ids == ("clv_revenue_trajectory",)
    assert hist.replaces_legacy_ids == ("clv_revenue_trajectory",)
