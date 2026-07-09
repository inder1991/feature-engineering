"""Phase-0 Task 4 — per-recipe applicability (derive + overrides).

The headline guarantee is that **every** one of the 153 recipes resolves to a *selectable-leaf*
primary use-case (never a domain parent, never a non-use_case dimension). The rest pin the D3/D6/D7
hard cases: the 22 ``transaction_monitoring`` recipes split fraud/aml by family, ``crypto_offramp_exposure``
carries its crypto context + typology, ``external_own_transfer_trend`` carries both the primacy-loss and
wealth secondaries, and the concentration / counterparty overrides land on their promoted homes.
"""
from __future__ import annotations

from featuregen.overlay.upload.taxonomy.recipe_applicability import (
    ApplicabilitySpec,
    recipe_applicability,
)
from featuregen.overlay.upload.taxonomy.use_cases import selectable_leaves, use_case
from featuregen.overlay.upload.templates import ALL_TEMPLATES

_BY_ID = {t.id: t for t in ALL_TEMPLATES}


def _template(recipe_id: str):
    """Fetch a Template by id from ALL_TEMPLATES (KeyError if the id is unknown)."""
    return _BY_ID[recipe_id]


def _spec(recipe_id: str) -> ApplicabilitySpec:
    return recipe_applicability(_template(recipe_id))


def test_every_recipe_has_a_selectable_leaf_primary():
    leaves = set(selectable_leaves())
    for t in ALL_TEMPLATES:
        spec = recipe_applicability(t)
        assert use_case(spec.primary) is not None, (t.id, spec.primary)
        assert spec.primary in leaves, (t.id, spec.primary)


def test_transaction_monitoring_split_by_family():
    assert _spec("txn_velocity_spike").primary == "fraud.transaction_fraud_detection"
    assert _spec("cross_border_burst").primary == "fraud.transaction_fraud_detection"
    assert _spec("structuring_smurfing").primary == "aml_cft.suspicious_transaction_monitoring"
    assert _spec("cash_intensity_ratio").primary == "aml_cft.suspicious_transaction_monitoring"


def test_crypto_offramp_carries_context_and_typology():
    spec = _spec("crypto_offramp_exposure")
    assert spec.primary == "aml_cft.suspicious_transaction_monitoring"
    assert "crypto_assets" in spec.product_context
    assert "crypto_asset_laundering" in spec.typology


def test_external_own_transfer_trend_secondaries():
    secondary = _spec("external_own_transfer_trend").secondary
    assert "customer.relationship_attrition.primacy_loss" in secondary
    assert "wealth.asset_outflow" in secondary


def test_salary_signal_carries_primacy_loss_secondary():
    assert "customer.relationship_attrition.primacy_loss" in _spec("salary_signal").secondary


def test_concentration_recipes_land_on_portfolio_risk():
    assert _spec("book_desk_concentration").primary == "portfolio_risk.concentration"
    assert _spec("rate_sensitive_concentration").primary == "portfolio_risk.concentration"
    assert _spec("guarantor_reliance").primary == "portfolio_risk.concentration"


def test_counterparty_overrides():
    assert _spec("notional_netting_exposure").primary == "counterparty_risk.exposure_monitoring"
    assert _spec("margin_call_intensity").primary == "counterparty_risk.margin_call_risk"
    assert _spec("benchmark_basis_dislocation").primary == "markets.market_risk.basis_risk"


def test_journey_stage_and_business_outcome_additions():
    assert "unbundling" in _spec("dd_cancellation_rate").journey_stage
    assert "cost_efficiency" in _spec("cost_to_collect_ratio").business_outcome


def test_primary_is_never_in_secondary():
    for t in ALL_TEMPLATES:
        spec = recipe_applicability(t)
        assert spec.primary not in spec.secondary, t.id


# ── taxonomy patch: three recipes remapped onto precise leaves; the old closest-fit is gone ─────────
def test_three_recipes_remapped_to_precise_primaries():
    assert _spec("claims_frequency_severity").primary == "insurance.actuarial.claims_cost_modelling"
    assert (_spec("mortality_morbidity_loading").primary
            == "insurance.underwriting.mortality_morbidity_risk_assessment")
    assert _spec("custody_holding_dynamics").primary == "securities_services.custody.holdings_dynamics"


def test_remapped_recipes_carry_insurance_product_context():
    assert _spec("claims_frequency_severity").product_context == ("insurance",)
    mm = _spec("mortality_morbidity_loading").product_context
    assert "life_insurance" in mm and "health_insurance" in mm


def test_old_closest_fit_is_gone_not_a_secondary():
    # The owner's ruling: the old closest-fit is an audit record only — never primary AND never secondary.
    claims = _spec("claims_frequency_severity")
    assert "insurance.claims.claims_fraud" not in (claims.primary, *claims.secondary)
    mm = _spec("mortality_morbidity_loading")
    assert "insurance.reinsurance" not in (mm.primary, *mm.secondary)
    holdings = _spec("custody_holding_dynamics")
    for old in ("securities_services.custody.settlement",
                "securities_services.custody.settlement_failure_risk"):
        assert old not in (holdings.primary, *holdings.secondary)


def test_four_settlement_recipes_map_to_settlement_failure_risk():
    for rid in ("matching_break_rate", "pre_settlement_aging",
                "settlement_fail_rate", "fail_ageing_buckets"):
        assert (_spec(rid).primary
                == "securities_services.custody.settlement_failure_risk"), rid


# ── adversarial-review fix: family-aware derivation (a foreign generic-tag leaf must NOT be primary) ──
def test_cross_domain_review_corrections():
    # Each of these previously landed on a FOREIGN family's leaf (or the wrong same-family leaf).
    assert _spec("policy_loan_utilisation").primary == "insurance.lapse.surrender"        # was credit.collections.hardship
    assert _spec("trading_limit_utilisation").primary == "markets.market_risk.portfolio"  # was credit.monitoring.limit_management
    assert _spec("dscr_covenant_headroom").primary == "credit.early_warning"              # was credit.underwriting.affordability
    assert _spec("rail_scheme_diversity").primary == "payments.behaviour"                 # was customer.segmentation
    assert _spec("purpose_code_diversity").primary == "payments.behaviour"                # was customer.segmentation


def test_no_unexpected_cross_domain_primary():
    # Domain-consistency guard: a derived/fallback primary MUST live under the recipe's own family root.
    # Only the audited _PRIMARY_OVERRIDE / _ORPHAN_PRIMARY tables may home a recipe cross-domain
    # (the counterparty recipes authored in the markets tuple; the cross-cutting concentration recipes).
    from featuregen.overlay.upload.taxonomy.recipe_applicability import (
        _FAMILY_ROOT,
        _ID_TO_FAMILY,
        _ORPHAN_PRIMARY,
        _PRIMARY_OVERRIDE,
    )
    from featuregen.overlay.upload.taxonomy.use_cases import ancestors

    violations: list[tuple[str, str | None, str]] = []
    for t in ALL_TEMPLATES:
        if t.id in _PRIMARY_OVERRIDE or t.id in _ORPHAN_PRIMARY:
            continue                                              # intentional, audited cross-domain homes
        primary = recipe_applicability(t).primary
        family_root = _FAMILY_ROOT.get(_ID_TO_FAMILY.get(t.id, ""))
        in_family = family_root is not None and (
            primary == family_root or family_root in ancestors(primary))
        if not in_family:
            violations.append((t.id, family_root, primary))
    assert not violations, f"cross-domain primaries outside the override/orphan tables: {violations}"
