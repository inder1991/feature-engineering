"""BR-13 — the payments pack: stage-correct feeds, lifecycle-bearing disputes and returns."""
from __future__ import annotations

from featuregen.overlay.upload.recipe_registry_v2 import v2_replaced_legacy_ids
from featuregen.overlay.upload.recipes.payments import PAYMENTS_RECIPES
from featuregen.overlay.upload.templates import PAYMENTS_TEMPLATES

BY_ID = {r.recipe_id: r for r in PAYMENTS_RECIPES}


def test_every_legacy_payments_id_is_explicitly_replaced():
    assert {t.id for t in PAYMENTS_TEMPLATES} <= v2_replaced_legacy_ids()


def test_decline_rate_reads_authorization_never_settlement():
    """The acceptance: authorization and settlement are never interchangeable operands."""
    r = BY_ID["authorisation_decline_rate"]
    assert r.source_grain == "authorization_event"
    concepts = {op.concept for op in r.operands}
    assert "authorization_status" in concepts and "settlement_status" not in concepts
    assert "never interchangeable" in r.business_definition


def test_chargebacks_carry_the_dispute_lifecycle_and_original_link():
    r = BY_ID["chargeback_rate"]
    concepts = {op.concept for op in r.operands}
    assert {"chargeback_status", "dispute_reason_code", "original_transaction_id"} <= concepts
    assert "raised after the cutoff" in r.eligibility.excluded


def test_returns_carry_status_and_reason_and_exclude_mandate_state():
    r = BY_ID["return_payment_rate"]
    concepts = {op.concept for op in r.operands}
    assert {"payment_return_status", "return_reason_code"} <= concepts
    assert "mandate" in r.eligibility.excluded.lower()


def test_settlement_lag_subtracts_two_named_stages():
    r = BY_ID["settlement_lag_avg_days"]
    stages = [op for op in r.operands if op.distinct_binding_group == "stage_timestamps"]
    assert {op.concept for op in stages} == {"booking_date", "settlement_date"}
    assert "one physical column" in r.eligibility.excluded


def test_merchant_economics_run_at_merchant_grain_with_fee_basis():
    for rid in ("interchange_revenue_sum", "merchant_discount_rate"):
        r = BY_ID[rid]
        assert r.output_grain == "merchant", rid
        assert r.source_grain == "acquiring_settlement", rid
        assert any(ref.startswith("threshold:scheme-fee-basis")
                   for ref in r.eligibility.policy_refs), rid


def test_counts_amounts_and_rates_are_atomic_outputs():
    assert BY_ID["rail_txn_count"].output.unit_kind == "count"
    assert BY_ID["rail_txn_amount"].output.unit_kind == "monetary"
    assert BY_ID["merchant_discount_rate"].output.unit_kind == "rate"
    assert {"rail_volume_value"} == set(BY_ID["rail_txn_count"].replaces_legacy_ids)
