"""BR-12 — the credit pack: economic roles, schedules, near-label stages, knowledge time."""
from __future__ import annotations

from featuregen.overlay.upload.recipe_registry_v2 import v2_replaced_legacy_ids
from featuregen.overlay.upload.recipes.credit import CREDIT_RECIPES, NEAR_LABEL_USE
from featuregen.overlay.upload.templates import CREDIT_RISK_TEMPLATES

BY_ID = {r.recipe_id: r for r in CREDIT_RECIPES}


def test_every_legacy_credit_id_is_explicitly_replaced():
    assert {t.id for t in CREDIT_RISK_TEMPLATES} <= v2_replaced_legacy_ids()


def test_a_deposit_balance_cannot_ground_a_drawn_exposure_recipe():
    """The acceptance: every drawn/limit/EAD/collateral operand names its ECONOMIC ROLE, which
    BR-5 binds only over governed evidence — a generic monetary_stock column never satisfies."""
    util = BY_ID["utilization_level"]
    roles = {op.role: op.economic_role for op in util.operands if op.operand_class == "measure"}
    assert roles == {"drawn": "drawn_credit_exposure", "limit": "approved_credit_limit"}
    assert next(op.economic_role for op in BY_ID["ead_slope"].operands
                if op.role == "ead") == "exposure_at_default"
    assert next(op.economic_role for op in BY_ID["ltv_raw"].operands
                if op.role == "valuation") == "collateral_valuation"


def test_facility_features_run_at_facility_grain():
    for r in CREDIT_RECIPES:
        if r.recipe_id.startswith(("utilization", "ltv", "ead", "days_past_due",
                                   "delinquency", "min_payment", "missed", "ecl", "stage",
                                   "forbearance", "sicr", "dscr", "repayment")):
            assert r.output_grain == "facility", r.recipe_id


def test_utilization_level_and_trend_are_two_recipes_with_same_asof_policy():
    level, trend = BY_ID["utilization_level"], BY_ID["utilization_trend"]
    for r in (level, trend):
        assert any("same-asof" in ref for ref in r.eligibility.policy_refs)
    assert level.formula.result_class == "ratio" and trend.formula.result_class == "slope"


def test_min_payment_requires_the_contractual_minimum_and_the_approximation_is_conceptual():
    exact = BY_ID["min_payment_only_streak"]
    assert any(op.concept == "minimum_due_amount" for op in exact.operands)
    approx = BY_ID["min_payment_pct_of_limit_pattern"]
    assert approx.computation_kind == "conceptual_pattern"
    assert "CONCEPTUAL" in approx.conceptual_reason


def test_missed_payments_require_the_schedule():
    """The acceptance: a generic payment flow cannot establish a missed payment."""
    r = BY_ID["missed_partial_payment_count"]
    concepts = {op.concept for op in r.operands}
    assert {"due_date", "scheduled_amount", "payment_allocation"} <= concepts
    assert r.source_grain == "installment_schedule"


def test_near_label_stages_carry_permitted_use():
    """The acceptance: pre-default applicability refuses post-default outcomes — the near-label
    family prohibits origination and default_prediction by declaration."""
    for rid in ("days_past_due_max", "delinquency_bucket_worst", "ecl_provision_slope",
                "stage_worsened_flag", "forbearance_in_window", "sicr_onset"):
        leak = BY_ID[rid].leakage
        assert leak.classification == "near_label", rid
        assert "origination" in leak.prohibited_stages, rid
        assert "monitoring" in leak.permitted_stages, rid
    assert NEAR_LABEL_USE.prohibited_stages == ("origination", "default_prediction")


def test_ecl_and_stage_reads_carry_model_provenance():
    for rid in ("ecl_provision_slope", "stage_worsened_flag", "sicr_onset"):
        assert any(ref.startswith("model_output:")
                   for ref in BY_ID[rid].eligibility.policy_refs), rid


def test_ltv_splits_and_reads_effective_dated_valuations():
    for rid in ("ltv_raw", "ltv_indexed", "ltv_trend"):
        r = BY_ID[rid]
        assert r.temporal.anchor_kind == "effective_interval"
        assert any(ref.startswith("allocation:collateral") for ref in r.eligibility.policy_refs)
    assert "INDEXED" in BY_ID["ltv_indexed"].business_definition
    assert "unsecured is not LTV zero" in BY_ID["ltv_raw"].output.empty_population_policy


def test_bureau_reads_declare_knowledge_time():
    for rid in ("bureau_score_delta", "bureau_inquiry_velocity", "new_trade_line_count"):
        r = BY_ID[rid]
        assert r.temporal.knowledge_time_role == "knowledge_ts", rid
        assert any(op.concept == "system_time" for op in r.operands), rid
