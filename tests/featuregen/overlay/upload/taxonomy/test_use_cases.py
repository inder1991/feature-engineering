"""Phase-0 Task 1 — governed use-case taxonomy registry."""
from __future__ import annotations

from featuregen.overlay.upload.taxonomy.use_cases import (
    USE_CASE_REGISTRY,
    ancestors,
    is_known_use_case,
    selectable_leaves,
    use_case,
)


def test_domain_parent_not_selectable():
    # D1: financial_crime is the only non-selectable node; fraud/aml_cft are its branches.
    fc = use_case("financial_crime")
    assert fc is not None and fc.selectable is False
    fraud = use_case("fraud")
    assert fraud is not None and fraud.parent == "financial_crime"


def test_financial_crime_is_the_only_non_selectable_node():
    non_selectable = [u.id for u in USE_CASE_REGISTRY.values() if not u.selectable]
    assert non_selectable == ["financial_crime"]


def test_hierarchy_resolves():
    assert ancestors("customer.relationship_attrition.primacy_loss") == (
        "customer", "customer.relationship_attrition")
    for uc in USE_CASE_REGISTRY.values():
        assert uc.parent is None or uc.parent in USE_CASE_REGISTRY


def test_every_parent_resolves_and_no_duplicate_ids():
    # The dict comprehension would silently drop a dup key; assert the authored count survives.
    from featuregen.overlay.upload.taxonomy import use_cases as m
    assert len(m._ALL) == len(USE_CASE_REGISTRY)
    for uc in USE_CASE_REGISTRY.values():
        if uc.parent is not None:
            assert is_known_use_case(uc.parent), uc.id


def test_deposit_attrition_leaf_exists_and_is_a_customer_objective():
    dep = use_case("customer.relationship_attrition.deposit_attrition")
    assert dep is not None
    assert ancestors("customer.relationship_attrition.deposit_attrition") == (
        "customer", "customer.relationship_attrition")


def test_declared_future_leaf_flagged():
    fee = use_case("pricing.fee_pricing")
    assert fee is not None and fee.intentionally_empty is True


def test_all_thirteen_star_leaves_are_intentionally_empty():
    # Every "*"-marked leaf in spec §3 — validation flags them, coverage explains their 0 recipes.
    star_leaves = {
        "wealth.client_attrition",
        "aml_cft.mule_account",
        "aml_cft.tbml",
        "treasury_alm.irrbb.basis_risk",
        "counterparty_risk.settlement_exposure",
        "pricing.credit_risk_based_pricing",
        "pricing.deposit_rate_optimisation",
        "pricing.fee_pricing",
        "pricing.relationship_pricing",
        "operations.process_cost_forecasting",
        "operations.workload_forecasting",
        "operations.manual_review_optimisation",
        "profitability.margin_forecasting",
    }
    flagged = {u.id for u in USE_CASE_REGISTRY.values() if u.intentionally_empty}
    assert flagged == star_leaves


def test_ancestors_walks_the_parent_chain_not_the_dotpath():
    # fraud carries a bare id but sits under the financial_crime domain — ancestors must report it.
    assert ancestors("fraud.transaction_fraud_detection") == ("financial_crime", "fraud")


def test_selectable_leaves_excludes_domain_and_includes_a_real_leaf():
    leaves = selectable_leaves()
    assert "financial_crime" not in leaves
    assert "fraud.transaction_fraud_detection" in leaves
    # A non-leaf (has selectable children) is not a leaf.
    assert "fraud" not in leaves
    assert "customer.relationship_attrition" not in leaves
    # Intentionally-empty leaves are still selectable, choosable leaves.
    assert "pricing.fee_pricing" in leaves
