"""BR-11 — the retail pack: every plan correction held as a STRUCTURAL fact.

These tests read the definitions, not prose: the splits exist as separate atomic recipes with
different contracts; eligible activity and cutoffs are declared; direction comes from a governed
sign policy; the conceptual survivors say WHY they cannot execute; and every legacy retail id
has an explicit replacement, which is what moves the audit's migration counter.
"""
from __future__ import annotations

from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES, v2_replaced_legacy_ids
from featuregen.overlay.upload.recipes.retail import RETAIL_RECIPES
from featuregen.overlay.upload.templates import RETAIL_CHURN_TEMPLATES

BY_ID = {r.recipe_id: r for r in RETAIL_RECIPES}


def test_every_legacy_retail_id_is_explicitly_replaced():
    legacy = {t.id for t in RETAIL_CHURN_TEMPLATES}
    assert legacy <= v2_replaced_legacy_ids()
    assert all(r in {rid for rec in RETAIL_RECIPES for rid in rec.replaces_legacy_ids}
               for r in legacy)
    # ...and the pack is IN the production registry, so the audit counter actually moved.
    assert set(BY_ID) <= {r.recipe_id for r in V2_RECIPES}


def test_every_recipe_is_atomic_and_honestly_ready():
    """One output each (the contract enforces construction; this pins the population), and no
    recipe CLAIMS authorable — FORMULA_BLOCKED until its expectation is reviewed, or
    CONCEPTUAL_ONLY with a stated reason."""
    for r in RETAIL_RECIPES:
        assert r.readiness in ("FORMULA_BLOCKED", "CONCEPTUAL_ONLY"), r.recipe_id
        if r.computation_kind == "deterministic_formula":
            assert r.formula is not None and r.formula.expectation_ref.startswith("retail:")
        else:
            assert r.conceptual_reason, r.recipe_id


def test_balance_trend_splits_into_raw_and_normalized_slopes():
    raw, norm = BY_ID["balance_slope"], BY_ID["normalized_balance_slope"]
    assert raw.output.unit_kind == "rate" and norm.output.unit_kind == "ratio"
    assert norm.output.zero_denominator_policy            # a normalized slope can divide by zero
    for r in (raw, norm):
        assert "latest-known" in r.temporal.snapshot_policy
        assert any(ref.startswith("currency_conversion:") for ref in r.eligibility.policy_refs)
        assert r.replaces_legacy_ids == ("balance_trend",)


def test_net_flow_and_inflow_outflow_ratio_have_different_contracts():
    """The acceptance sentence, literally: different unit kinds, different additivity, different
    empty-window answers — a ratio and a signed sum are not variants of one thing."""
    ratio, net = BY_ID["inflow_outflow_ratio"], BY_ID["net_transaction_flow"]
    assert ratio.output.unit_kind == "ratio" and net.output.unit_kind == "monetary"
    assert ratio.output.additivity == "non_additive" and net.output.additivity == "additive"
    assert "null" in ratio.output.empty_population_policy
    assert "zero" in net.output.empty_population_policy
    for r in (ratio, net):
        assert any(ref.startswith("direction_sign:") for ref in r.eligibility.policy_refs), (
            "direction must come from the governed sign policy, never inferred from amounts")


def test_no_retail_recipe_counts_unspecified_events_as_activity():
    """The acceptance: every transaction-fed deterministic recipe declares its eligible-status
    policy and excludes the ineligible event classes by name."""
    for r in RETAIL_RECIPES:
        if r.computation_kind != "deterministic_formula" or r.source_grain != "transaction":
            continue
        assert any(ref.startswith("eligible_status:") for ref in r.eligibility.policy_refs), (
            r.recipe_id)
        assert "reversed" in r.eligibility.excluded or "unverified" in r.eligibility.excluded, (
            r.recipe_id)


def test_dormancy_excludes_the_five_ineligible_event_classes():
    d = BY_ID["dormancy_recency_days"]
    for word in ("failed", "reversed", "technical", "closure", "system-only"):
        assert word in d.eligibility.excluded
    assert "never active" in d.output.empty_population_policy.lower() or \
        "null" in d.output.empty_population_policy


def test_salary_splits_four_ways_and_confidence_is_conceptual():
    for rid in ("salary_credit_count", "salary_credit_amount", "salary_regularity"):
        r = BY_ID[rid]
        assert r.computation_kind == "deterministic_formula"
        assert any(op.relationship_requirement for op in r.operands), (
            "stable payer identity is a declared operand requirement, not prose")
        assert "lookalikes" in r.eligibility.excluded
    conf = BY_ID["salary_confidence"]
    assert conf.computation_kind == "conceptual_pattern"
    assert "unreviewed heuristic" in conf.conceptual_reason


def test_product_breadth_reads_effective_dated_active_holdings():
    r = BY_ID["product_breadth_active"]
    assert r.temporal.anchor_kind == "effective_interval"
    assert any(op.concept == "product_holding" for op in r.operands)
    assert any(ref.startswith("active_state:") for ref in r.eligibility.policy_refs)


def test_rfm_is_three_atoms_and_an_honest_composite():
    kinds = {rid: BY_ID[rid].computation_kind
             for rid in ("rfm_recency_days", "rfm_frequency_count", "rfm_monetary_amount",
                         "rfm_composite_score")}
    assert kinds.pop("rfm_composite_score") == "conceptual_pattern"
    assert set(kinds.values()) == {"deterministic_formula"}
    assert "ModelFeatureSpec" in BY_ID["rfm_composite_score"].conceptual_reason


def test_mandate_cancellation_and_collection_returns_are_two_events():
    cancel, bounce = BY_ID["dd_mandate_cancellation_count"], BY_ID["dd_collection_return_count"]
    assert cancel.source_grain == "mandate_event"
    assert bounce.source_grain == "payment_return_event"
    assert any(op.concept == "payment_return_status" for op in bounce.operands)
    assert any(op.concept == "mandate" for op in cancel.operands)
    assert "not a cancelled" in cancel.eligibility.excluded.lower() \
        or "collections" in cancel.eligibility.excluded.lower()


def test_own_transfer_executes_only_over_a_verified_relationship():
    verified, fuzzy = BY_ID["own_transfer_outflow_amount"], BY_ID["external_own_transfer_pattern"]
    payee = next(op for op in verified.operands if op.concept == "beneficiary_id")
    assert "VERIFIED" in payee.relationship_requirement
    assert fuzzy.computation_kind == "conceptual_pattern"
    assert "false-match" in fuzzy.conceptual_reason
    assert "PII" in fuzzy.conceptual_reason


def test_customer_grain_rollups_declare_the_joint_account_allocation():
    for r in RETAIL_RECIPES:
        if (r.computation_kind == "deterministic_formula" and r.output_grain == "customer"
                and r.output.unit_kind == "monetary"):
            assert any(ref.startswith("allocation:") for ref in r.eligibility.policy_refs), (
                r.recipe_id)
