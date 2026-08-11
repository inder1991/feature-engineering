"""BR-20 — the CIB expansion packs: grains explicit, methodologies stated or refused."""
from __future__ import annotations

from featuregen.overlay.upload.recipes.cib_client import CIB_CLIENT_RECIPES, CIB_GRAINS
from featuregen.overlay.upload.recipes.cib_risk import CIB_RISK_RECIPES
from featuregen.overlay.upload.recipes.trade_finance import TRADE_FINANCE_RECIPES
from featuregen.overlay.upload.recipes.transaction_banking import (
    TRANSACTION_BANKING_RECIPES,
)

ALL = (*CIB_CLIENT_RECIPES, *TRANSACTION_BANKING_RECIPES, *TRADE_FINANCE_RECIPES,
       *CIB_RISK_RECIPES)
BY_ID = {r.recipe_id: r for r in ALL}


def test_cib_features_distinguish_the_declared_grains():
    """The acceptance: party, group, account, facility, instrument and pool grains are the
    EXPLICIT vocabulary, and every BR-20 output grain is a member of it."""
    assert {"legal_party", "obligor", "legal_group", "account", "facility", "instrument",
            "pool", "client"} == set(CIB_GRAINS)
    assert all(r.output_grain in CIB_GRAINS for r in ALL)


def test_profitability_states_its_methodology_or_refuses():
    """The acceptance: cost carries its allocation policy as an operand; RAROC and the
    revenue wallet stay CONCEPTUAL until capital/cost policy and the external denominator
    are governed — exactly the plan's blocked list."""
    cost = BY_ID["direct_service_cost"]
    assert any(op.status_policy_ref.startswith("allocation:service-cost")
               for op in cost.operands)
    for rid in ("raroc_pattern", "revenue_wallet_share_pattern",
                "hedge_effectiveness_pattern"):
        r = BY_ID[rid]
        assert r.computation_kind == "conceptual_pattern", rid
        assert "governed" in r.conceptual_reason or "methodology" in r.conceptual_reason, rid


def test_the_kyc_and_cash_management_leaves_get_reviewed_primaries():
    assert BY_ID["kyc_periodic_review_overdue_flag"].primary_objective == "aml_cft.kyc"
    for rid in ("cash_position_volatility", "intraday_liquidity_peak_usage",
                "pool_sweep_count", "virtual_account_utilization_share"):
        assert BY_ID[rid].primary_objective == "treasury_alm.cash_management", rid


def test_intraday_liquidity_cannot_compile_from_end_of_day_data():
    """The acceptance, held at account level: the intraday recipe's source grain is the
    intraday position event with minute windows; EOD stand-ins excluded by name."""
    r = BY_ID["intraday_liquidity_peak_usage"]
    assert r.source_grain == "intraday_position_event"
    assert r.temporal.window_unit == "minutes"
    assert "standing in" in r.eligibility.excluded
    assert "not" in r.output.empty_population_policy


def test_trade_finance_follows_the_instrument_lifecycle():
    """The acceptance: lifecycle over generic exposure — the stage is an identity-bearing
    semantic parameter (five features, one body), and durations subtract named stages."""
    lc = BY_ID["lc_lifecycle_event_count"]
    stage = next(p for p in lc.parameters if p.name == "lifecycle_stage")
    assert stage.parameter_class == "semantic"
    assert stage.allowed_values == ("issuance", "amendment", "utilization", "expiry",
                                    "claim")
    assert "generic exposure" in lc.eligibility.excluded
    for rid, group in (("document_processing_days", "doc_times"),
                       ("scf_approved_to_paid_days", "scf_times")):
        legs = [op for op in BY_ID[rid].operands if op.distinct_binding_group == group]
        assert len(legs) == 2, rid


def test_the_maturity_wall_is_contractual_future_anchored():
    for rid in ("facility_maturity_wall", "refinancing_concentration_share"):
        r = BY_ID[rid]
        assert r.temporal.anchor_kind == "contractual_future", rid
        assert "knowable AT the cutoff" in r.temporal.future_horizon_policy, rid
    assert "exactly one bucket" in BY_ID["facility_maturity_wall"].output.aggregation_over_entity


def test_hedges_require_designation_and_sales_outcomes_are_fenced():
    hedge = BY_ID["fx_hedge_ratio"]
    assert any(ref.startswith("allocation:hedge-designation")
               for ref in hedge.eligibility.policy_refs)
    assert "undesignated" in hedge.eligibility.excluded
    adoption = BY_ID["treasury_product_adoption_count"]
    assert "sales_outcome_prediction" in adoption.leakage.prohibited_stages


def test_contingent_conversion_reads_both_exposure_roles():
    r = BY_ID["contingent_to_funded_share"]
    concepts = {op.concept for op in r.operands}
    assert {"contingent_exposure", "drawn_principal"} <= concepts
    assert "window START" in r.temporal.snapshot_policy
