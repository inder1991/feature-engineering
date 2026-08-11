"""BR-18 — the transaction foundation pack: executable primitives, the exemplar authorable."""
from __future__ import annotations

import json
from pathlib import Path

from featuregen.overlay.upload.recipe_formula_expectations_v2 import (
    RECIPE_FORMULA_V2_EXPECTATIONS,
    has_reviewed_expectation,
)
from featuregen.overlay.upload.recipes.transaction_foundation import (
    TRANSACTION_FOUNDATION_RECIPES,
)

BY_ID = {r.recipe_id: r for r in TRANSACTION_FOUNDATION_RECIPES}
_GOLD_V2 = Path(__file__).resolve().parents[3] / "formula" / "gold_v2"


def test_the_v2_expectation_registry_pins_reviewed_fixtures():
    """The fixture-side proof of the v2 expectation seam: every registered ref names a real
    gold_v2 fixture that parses under the declared-version rule, declares formula-v2, and
    hashes to EXACTLY the pin — editing either side without the other fails here (the same
    no-self-refresh freeze the v1 manifest carries)."""
    from featuregen.formula.parse_v2 import parse_versioned

    for ref, (fixture_name, pinned_hash) in RECIPE_FORMULA_V2_EXPECTATIONS.items():
        doc = json.loads((_GOLD_V2 / fixture_name).read_text())
        proposal = parse_versioned(doc["proposal"])
        assert proposal.formula_schema_version == 2, ref
        assert doc["expected_proposal_hash"] == pinned_hash, ref


def test_the_canonical_exemplar_demonstrates_the_complete_contract():
    """The plan's exemplar checklist, structural: account grain over the transaction event
    grain; the full operand set; all four governed policies; trailing window; additive within
    one currency; empty window zero; null policy reviewed — and honestly FORMULA_AUTHORABLE,
    because its reviewed Formula-v2 expectation exists (the BR-6 gold exemplar)."""
    r = BY_ID["posted_debit_amount"]
    assert (r.output_grain, r.source_grain) == ("account", "transaction")
    concepts = {op.concept for op in r.operands}
    assert {"transaction_id", "account_id", "monetary_flow", "debit_credit_indicator",
            "booking_status", "booking_date", "original_transaction_id"} <= concepts
    kinds = {ref.split(":")[0] for ref in r.eligibility.policy_refs}
    assert kinds == {"eligible_status", "reversal_correction", "direction_sign",
                     "currency_conversion"}
    assert r.temporal.window_parameter == "window"
    assert r.output.additivity == "additive"
    assert "within one currency" in r.output.aggregation_over_entity
    assert "returns zero" in r.output.empty_population_policy
    assert "reviewed source policy" in r.output.null_input_policy
    assert r.readiness == "FORMULA_AUTHORABLE"
    assert has_reviewed_expectation(r.formula.expectation_ref)


def test_only_the_exemplar_claims_authorable_in_tranche_a():
    """Readiness is granted by the expectation registry, one review at a time."""
    authorable = [r.recipe_id for r in TRANSACTION_FOUNDATION_RECIPES
                  if r.readiness == "FORMULA_AUTHORABLE"]
    assert authorable == ["posted_debit_amount"]
    for r in TRANSACTION_FOUNDATION_RECIPES:
        if r.recipe_id != "posted_debit_amount":
            assert r.readiness == "FORMULA_BLOCKED", r.recipe_id
            assert not has_reviewed_expectation(r.formula.expectation_ref), r.recipe_id


def test_reversals_and_failures_cannot_inflate_posted_activity():
    """The acceptance: the posted recipes EXCLUDE failures/reversals by name and policy; the
    failure/correction family counts them in its own population, with the correction link."""
    for rid in ("posted_debit_amount", "posted_credit_amount",
                "posted_debit_transaction_count", "net_posted_transaction_flow"):
        r = BY_ID[rid]
        assert "reversed" in r.eligibility.excluded, rid
        assert any(ref.startswith("reversal_correction:")
                   for ref in r.eligibility.policy_refs), rid
    rev = BY_ID["reversal_count"]
    assert "never double-counted" in rev.eligibility.excluded
    assert any(op.concept == "reversal_indicator" for op in rev.operands)
    assert any(op.concept == "original_transaction_id" for op in rev.operands)


def test_mixed_currencies_cannot_sum_without_the_governed_conversion():
    for r in TRANSACTION_FOUNDATION_RECIPES:
        if r.output.unit_kind == "monetary" and r.output.additivity == "additive":
            assert r.output.currency_policy.startswith("currency_conversion:"), r.recipe_id


def test_account_and_customer_grains_are_not_conflated():
    """Every foundation output grain is the account — customer rollups are separate recipes
    with an allocation policy (the plan's rule), and none exist in this pack."""
    assert all(r.output_grain == "account" for r in TRANSACTION_FOUNDATION_RECIPES)


def test_signed_versus_unsigned_is_a_declared_policy_choice():
    amount = next(op for op in BY_ID["posted_debit_amount"].operands if op.role == "amount")
    assert "never inferred" in amount.sign_direction_expectation


def test_every_primitive_is_atomic_with_its_own_expectation_ref():
    refs = [r.formula.expectation_ref for r in TRANSACTION_FOUNDATION_RECIPES]
    assert len(refs) == len(set(refs)) == 24


# ── tranche B: the analytical primitives ────────────────────────────────────────────────────────
def test_tranche_b_is_atomic_blocked_and_variant_correct():
    """19 analytical primitives, every one FORMULA_BLOCKED (no expectation reviewed yet),
    every grammar operation they need already in the BR-6 vocabulary — and the semantic
    parameters are identity-bearing: p95 and p99 are different features, each payment
    category its own."""
    from featuregen.overlay.upload.recipe_variants import enumerate_variant_identities
    from featuregen.overlay.upload.recipes.transaction_analytics import (
        TRANSACTION_ANALYTICS_RECIPES,
    )

    assert len(TRANSACTION_ANALYTICS_RECIPES) == 19
    by_id = {r.recipe_id: r for r in TRANSACTION_ANALYTICS_RECIPES}
    assert all(r.readiness == "FORMULA_BLOCKED" for r in TRANSACTION_ANALYTICS_RECIPES)
    assert all(r.output_grain == "account" for r in TRANSACTION_ANALYTICS_RECIPES)

    pct = by_id["transaction_amount_percentile"]
    pct_param = next(p for p in pct.parameters if p.name == "percentile")
    assert pct_param.parameter_class == "semantic"
    ids = enumerate_variant_identities(pct)
    assert len(ids) == len(set(ids)) == 9          # 3 windows × 3 percentiles, all distinct

    cat = by_id["categorized_payment_regularity"]
    cat_param = next(p for p in cat.parameters if p.name == "payment_category")
    assert cat_param.allowed_values == ("subscription", "bill", "rent", "loan_payment")

    spread = by_id["fx_conversion_spread"]
    assert "no authority, no spread" in spread.business_definition
    assert any(ref.startswith("currency_conversion:foundation-fx-rate")
               for ref in spread.eligibility.policy_refs)

    fan_in, fan_out = by_id["fan_in_counterparty_count"], by_id["fan_out_counterparty_count"]
    assert any(op.role == "payer" for op in fan_in.operands)
    assert any(op.role == "payee" for op in fan_out.operands)
