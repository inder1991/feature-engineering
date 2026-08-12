"""BR-15 — custody, asset-management, insurance, Islamic and ESG: specialist semantics held.

One module for the five families (the plan's per-family test files folded together — same
assertions, one harness): every legacy id replaced; every specialist output identifies its
accounting/valuation/methodology basis; the claims family is atomic; Islamic recipes carry
contract-specific semantics; ESG outputs cannot combine incompatible boundaries.
"""
from __future__ import annotations

from featuregen.overlay.upload.model_feature_registry import model_feature_by_id
from featuregen.overlay.upload.recipe_registry_v2 import v2_replaced_legacy_ids
from featuregen.overlay.upload.recipes.asset_management import AM_RECIPES
from featuregen.overlay.upload.recipes.custody import CUSTODY_RECIPES
from featuregen.overlay.upload.recipes.esg import ESG_RECIPES
from featuregen.overlay.upload.recipes.insurance import INSURANCE_RECIPES
from featuregen.overlay.upload.recipes.islamic import ISLAMIC_RECIPES
from featuregen.overlay.upload.templates import (
    ASSET_MGMT_TEMPLATES,
    CUSTODY_TEMPLATES,
    ESG_TEMPLATES,
    INSURANCE_TEMPLATES,
    ISLAMIC_TEMPLATES,
)

ALL = (*CUSTODY_RECIPES, *AM_RECIPES, *INSURANCE_RECIPES, *ISLAMIC_RECIPES, *ESG_RECIPES)
BY_ID = {r.recipe_id: r for r in ALL}


def test_every_specialist_legacy_id_is_explicitly_replaced():
    legacy = {t.id for ts in (CUSTODY_TEMPLATES, ASSET_MGMT_TEMPLATES, INSURANCE_TEMPLATES,
                              ISLAMIC_TEMPLATES, ESG_TEMPLATES) for t in ts}
    assert legacy <= v2_replaced_legacy_ids()


def test_every_specialist_output_identifies_its_methodology_basis():
    """The acceptance: every deterministic specialist recipe declares its basis — a governed
    policy reference, or a status/policy-input operand carrying the lifecycle/methodology
    (a claim count's basis is the claim lifecycle it reads)."""
    for r in ALL:
        if r.computation_kind == "deterministic_formula":
            has_basis = bool(r.eligibility.policy_refs) or any(
                op.operand_class in ("status", "policy_input") for op in r.operands)
            assert has_basis, r.recipe_id


def test_custody_fails_are_knowable_only_after_contractual_settlement():
    for rid in ("settlement_fail_count", "settlement_fail_value", "settlement_fail_rate",
                "settlement_fail_age_max"):
        r = BY_ID[rid]
        assert "knowable only after the CONTRACTUAL settlement date" in r.business_definition
        legs = [op for op in r.operands if op.distinct_binding_group == "settle_dates"]
        assert len(legs) == 2, rid          # contractual vs actual — two distinct dates
    assert {"settlement_fail_count", "settlement_fail_value",
            "settlement_fail_rate"} <= set(BY_ID)          # count/value/rate split
    assert BY_ID["matching_break_rate"].operands[1].concept == "matching_status"


def test_asset_management_separates_flows_from_performance():
    flow, perf = BY_ID["net_fund_flow"], BY_ID["performance_vs_benchmark"]
    assert "INVESTOR flows only" in flow.business_definition
    assert any(ref.startswith("risk_corridor:benchmark-identity")
               for ref in perf.eligibility.policy_refs)
    assert any(ref.startswith("business_calendar:nav-version")
               for ref in perf.eligibility.policy_refs)
    assert "superseded NAV versions" in perf.eligibility.excluded


def test_the_claims_family_is_atomic_with_correct_additivity():
    """The acceptance: claims count, severity and loss ratio are separate outputs."""
    count, paid, ratio = BY_ID["claim_count"], BY_ID["claim_paid_amount_sum"], BY_ID["loss_ratio"]
    assert count.output.additivity == "additive"
    assert paid.output.additivity == "additive"
    assert ratio.output.additivity == "non_additive"
    assert "EARNED premium" in ratio.business_definition
    # written vs collected premium are separate recipes too
    assert {"written_premium_sum", "collected_premium_sum"} <= set(BY_ID)


def test_insurance_model_outputs_are_registered_specs():
    for rid, ref in (("claims_fraud_typology", "claims_fraud_score"),
                     ("mortality_morbidity_loading", "mortality_morbidity_loading")):
        r = BY_ID[rid]
        assert r.computation_kind == "governed_model_output", rid
        assert model_feature_by_id(ref) is not None, rid


def test_the_old_insurance_admissions_are_closed_by_br10_concepts():
    assert any(op.concept == "policy_loan_balance"
               for op in BY_ID["policy_loan_utilisation"].operands)
    assert any(op.concept == "customer_income"
               for op in BY_ID["sum_assured_adequacy"].operands)
    assert any(op.concept == "product_holding"
               for op in BY_ID["bancassurance_cross_hold"].operands)


def test_islamic_recipes_carry_contract_specific_semantics():
    """The acceptance: contract type + Sharia governance declared; profit-rate words, never
    interest terminology; Murabaha reads the schedule."""
    for r in ISLAMIC_RECIPES:
        text = (r.business_definition + r.decision_context).lower()
        assert "interest" not in text, r.recipe_id
    beta = BY_ID["islamic_deposit_beta"]
    assert any(op.concept == "profit_rate" for op in beta.operands)
    murabaha = BY_ID["murabaha_installment_behaviour"]
    concepts = {op.concept for op in murabaha.operands}
    assert {"due_date", "scheduled_amount", "payment_allocation"} <= concepts
    assert any(op.status_policy_ref.startswith("active_state:islamic-contract")
               for op in murabaha.operands)


def test_esg_outputs_cannot_combine_incompatible_boundaries():
    """The acceptance: the boundary is an OPERAND, and the aggregation prose forbids
    cross-boundary sums."""
    for rid in ("absolute_emissions_by_scope", "carbon_intensity_trajectory",
                "financed_emissions_attribution", "taxonomy_alignment_share",
                "scope3_value_chain_exposure"):
        r = BY_ID[rid]
        assert any(op.status_policy_ref.startswith("risk_corridor:ghg-boundary")
                   or op.status_policy_ref.startswith("risk_corridor:pcaf")
                   for op in r.operands), rid
    absolute = BY_ID["absolute_emissions_by_scope"]
    assert "never across" in absolute.output.aggregation_over_entity
    assert "absence of data is not zero emissions" in \
        absolute.output.empty_population_policy


def test_absolute_and_intensity_are_separate_recipes():
    assert BY_ID["absolute_emissions_by_scope"].output.unit_policy == "tCO2e"
    assert "per revenue" in BY_ID["carbon_intensity_trajectory"].output.unit_policy


def test_physical_risk_names_its_scenario():
    r = BY_ID["physical_hazard_exposure"]
    assert any(op.status_policy_ref.startswith("risk_corridor:climate-scenario")
               for op in r.operands)
    assert "never scenario-free" in r.business_definition
