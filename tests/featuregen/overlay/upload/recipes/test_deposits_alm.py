"""BR-14 — the Deposits/ALM pack: the HQLA retirement and regulatory classification factors."""
from __future__ import annotations

from featuregen.overlay.upload.recipe_registry_v2 import v2_replaced_legacy_ids
from featuregen.overlay.upload.recipes.deposits_alm import DEPOSITS_ALM_RECIPES
from featuregen.overlay.upload.templates import DEPOSITS_TEMPLATES

BY_ID = {r.recipe_id: r for r in DEPOSITS_ALM_RECIPES}


def test_every_legacy_deposits_id_is_explicitly_replaced():
    assert {t.id for t in DEPOSITS_TEMPLATES} <= v2_replaced_legacy_ids()


def test_no_deposit_recipe_claims_the_liability_is_hqla():
    """The acceptance: hqla_eligibility_contribution is RETIRED into the liability's outflow
    contribution and a separate ASSET-side buffer — no V2 recipe reads deposit balances as
    HQLA, and the retirement is written into the replacement's own exclusions."""
    assert "hqla_eligibility_contribution" not in BY_ID
    outflow = BY_ID["liability_cash_outflow_contribution"]
    assert "hqla_eligibility_contribution" in outflow.replaces_legacy_ids
    assert "never a high-quality liquid asset" in outflow.eligibility.excluded
    buffer = BY_ID["asset_hqla_buffer"]
    assert buffer.source_grain == "asset_snapshot"
    assert "liabilities of any kind" in buffer.eligibility.excluded


def test_lcr_nsfr_contributions_use_regulatory_classification_factors():
    for rid, kind in (("liability_cash_outflow_contribution", "risk_corridor:lcr"),
                      ("nsfr_asf_contribution", "risk_corridor:nsfr"),
                      ("asset_hqla_buffer", "risk_corridor:hqla")):
        r = BY_ID[rid]
        assert any(ref.startswith(kind) for ref in r.eligibility.policy_refs), rid
        assert any(op.operand_class == "policy_input" for op in r.operands), rid


def test_deposit_beta_cannot_compute_without_the_paid_customer_rate():
    """The acceptance sentence: two DISTINCT rate operands, the paid rate carrying its
    economic role — a benchmark-only source cannot bind."""
    r = BY_ID["deposit_beta"]
    legs = [op for op in r.operands if op.distinct_binding_group == "beta_rates"]
    assert len(legs) == 2
    paid = next(op for op in legs if op.role == "customer_rate")
    assert paid.economic_role == "paid_deposit_rate"
    assert "benchmark-only" in r.eligibility.excluded


def test_repricing_gap_runs_at_book_bucket_grain():
    r = BY_ID["repricing_gap_exposure"]
    assert r.output_grain == "book_bucket"
    assert "customer-grain" in r.eligibility.excluded


def test_the_ladders_are_contractual_future_anchored():
    for rid in ("maturity_ladder_runoff", "contractual_deposit_maturity_profile"):
        r = BY_ID[rid]
        assert r.temporal.anchor_kind == "contractual_future", rid
        assert "knowable AT the cutoff" in r.temporal.future_horizon_policy, rid
    ladder = BY_ID["maturity_ladder_runoff"]
    assert "exactly ONE bucket" in ladder.output.aggregation_over_entity
    assert any(op.status_policy_ref.startswith("risk_corridor:runoff-scenario")
               for op in ladder.operands)


def test_early_withdrawal_reads_the_break_event_against_the_contract():
    r = BY_ID["early_withdrawal_break"]
    concepts = {op.concept for op in r.operands}
    assert {"origination_date", "maturity_date", "notice_period"} <= concepts
    assert r.source_grain == "closure_event"
    assert "matured deposit is not a break" in r.eligibility.excluded


def test_net_interest_flow_carries_its_sign_authority():
    r = BY_ID["lagged_net_interest_flow"]
    legs = [op for op in r.operands if op.distinct_binding_group == "interest_legs"]
    assert {op.concept for op in legs} == {"interest_income", "interest_expense"}
    assert any(ref.startswith("direction_sign:") for ref in r.eligibility.policy_refs)
