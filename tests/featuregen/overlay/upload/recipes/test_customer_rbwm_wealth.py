"""BR-19 — customer, RBWM and wealth packs: rollups that explain themselves, gated facts."""
from __future__ import annotations

from featuregen.overlay.upload.model_feature_registry import model_feature_by_id
from featuregen.overlay.upload.recipes.customer import (
    CUSTOMER_MODEL_FEATURES,
    CUSTOMER_RECIPES,
)
from featuregen.overlay.upload.recipes.rbwm import RBWM_RECIPES
from featuregen.overlay.upload.recipes.wealth import WEALTH_RECIPES

ALL = (*CUSTOMER_RECIPES, *RBWM_RECIPES, *WEALTH_RECIPES)
BY_ID = {r.recipe_id: r for r in ALL}


def test_customer_rollups_explain_their_accounts_and_allocation():
    """The acceptance: a customer feature can explain exactly which accounts and allocation
    rules contributed — the account is an operand and the allocation rule a policy input."""
    for rid in ("relationship_balance", "relationship_revenue"):
        r = BY_ID[rid]
        assert any(op.role == "account" for op in r.operands), rid
        assert any(op.operand_class == "policy_input"
                   and op.status_policy_ref.startswith("allocation:")
                   for op in r.operands), rid
        assert "allocation rule" in r.output.aggregation_over_entity, rid


def test_predictions_are_separate_from_history_with_registered_specs():
    for rid, ref in (("churn_probability", "churn_probability"),
                     ("campaign_uplift_score", "campaign_uplift")):
        r = BY_ID[rid]
        assert r.computation_kind == "governed_model_output"
        spec = model_feature_by_id(ref)
        assert spec is not None and spec.model_version == ""
    assert len(CUSTOMER_MODEL_FEATURES) == 2
    uplift = model_feature_by_id("campaign_uplift")
    assert "control" in uplift.fallback_policy


def test_the_clv_leaf_gets_its_deterministic_primary():
    r = BY_ID["relationship_revenue"]
    assert r.primary_objective == "customer.clv"
    assert "REALIZED" in r.business_definition


def test_primacy_loss_gets_reviewed_primaries():
    for rid in ("salary_anchoring_ceased_flag", "operating_balance_share_trend"):
        assert BY_ID[rid].primary_objective == "customer.relationship_attrition.primacy_loss"
    anchor = BY_ID["salary_anchoring_ceased_flag"]
    assert any(op.status_policy_ref.startswith("active_state:primary-salary")
               for op in anchor.operands)
    assert "one missed cadence period" in anchor.eligibility.excluded


def test_gated_facts_compute_only_under_their_policies():
    """Privacy/suitability gates: contactability, vulnerability and wealth suitability carry
    their privacy_purpose policy as an operand — no permitted purpose, no number."""
    for rid, prefix in (("contactability_quality_share", "privacy_purpose:contactability"),
                        ("vulnerability_indicator_flag", "privacy_purpose:vulnerability"),
                        ("risk_profile_mismatch_flag", "privacy_purpose:wealth-suitability")):
        r = BY_ID[rid]
        assert any(op.status_policy_ref.startswith(prefix) for op in r.operands), rid
    vuln = BY_ID["vulnerability_indicator_flag"]
    assert "never derived from behaviour" in vuln.business_definition


def test_wealth_asset_outflow_gets_its_primaries_and_split_flows():
    for rid in ("wealth_contribution_flow", "wealth_asset_outflow", "cash_drag_share"):
        assert BY_ID[rid].primary_objective == "wealth.asset_outflow", rid
    assert "never netted" in BY_ID["wealth_contribution_flow"].business_definition
    outflow = BY_ID["wealth_asset_outflow"]
    assert "market value movement" in outflow.eligibility.excluded


def test_nothing_touches_the_intentionally_empty_client_attrition_leaf():
    for r in ALL:
        assert r.primary_objective != "wealth.client_attrition", r.recipe_id
        assert "wealth.client_attrition" not in r.supporting_objectives, r.recipe_id


def test_household_membership_is_verified_and_effective_dated():
    r = BY_ID["verified_household_member_count"]
    assert r.temporal.anchor_kind == "effective_interval"
    assert "unverified or lapsed" in r.eligibility.excluded


def test_wealth_valuation_facts_carry_the_valuation_policy():
    """The acceptance: wealth performance carries valuation/benchmark/fee/currency basis —
    the portfolio facts here carry valuation + currency; performance itself lives in the
    asset-management pack with its benchmark and fee policies."""
    for rid in ("cash_drag_share", "portfolio_concentration_hhi"):
        r = BY_ID[rid]
        assert any(ref.startswith("business_calendar:wealth-valuation")
                   for ref in r.eligibility.policy_refs), rid
