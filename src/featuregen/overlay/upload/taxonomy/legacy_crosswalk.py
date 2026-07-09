"""Legacy-tag crosswalk — every one of the 107 legacy ``Template.use_cases`` tags routed to its
governed home.

The legacy recipes carry a flat bag of ``use_cases`` string tags that predate the governed taxonomy:
some are genuine modelling objectives (→ the ``use_cases.py`` tree), but many are really a regulatory
framework, an output measure, a product/channel, a typology, a journey stage, a business outcome, or
plain ownership metadata. This module maps each legacy tag to a ``(dimension, target, status)`` triple
so later tasks can *derive* per-recipe applicability without re-doing the archaeology.

Authored verbatim from ``docs/superpowers/specs/2026-07-09-usecase-taxonomy-crosswalk-draft.md`` §2
(D1–D7), §3 (the tree, with its ``← tag`` annotations) and §5 (the reclassified-out / merged /
deprecated lists). Behaviour-neutral: read-only; nothing here touches ``templates.py`` or grounding.

``status`` semantics (∈ ``{"mapped", "merged", "deprecated"}``):

* ``mapped``      — a clean 1:1 route to the tag's true home (its use-case leaf, or its framework /
                    measure / context / stage / outcome dimension).
* ``merged``      — the tag folds into another tag (``authorised_push_payment`` → ``app_scam``;
                    ``sustainable_finance`` → ``esg.scoring``).
* ``deprecated``  — a generic tag that Task 4 splits per-recipe; here it points at its single best
                    representative node (or, for ``contactability``, at free-form ``metadata``).

``_validate_crosswalk()`` runs at import: a ``use_case`` target must resolve in ``USE_CASE_REGISTRY``;
any other governed-dimension target must be a member of that dimension (via ``dimensions.is_known``);
``metadata`` targets are free-form strings; ``status`` must be one of the three legal values.
"""
from __future__ import annotations

from typing import TypedDict

from featuregen.overlay.upload.taxonomy import dimensions
from featuregen.overlay.upload.taxonomy.use_cases import USE_CASE_REGISTRY


class CrosswalkEntry(TypedDict):
    dimension: str          # "use_case" | a dimensions.py key | "metadata"
    target: str             # a USE_CASE_REGISTRY id, a dimension member, or a free-form metadata id
    status: str             # "mapped" | "merged" | "deprecated"


_VALID_STATUSES: frozenset[str] = frozenset({"mapped", "merged", "deprecated"})


def _uc(target: str, status: str = "mapped") -> CrosswalkEntry:
    return {"dimension": "use_case", "target": target, "status": status}


def _dim(dimension: str, target: str, status: str = "mapped") -> CrosswalkEntry:
    return {"dimension": dimension, "target": target, "status": status}


# The full 107-tag crosswalk. Keys are exactly the tags found across ``ALL_TEMPLATES[*].use_cases``
# (the coverage test enforces completeness). Grouped by governed home for review.
LEGACY_TAG_CROSSWALK: dict[str, CrosswalkEntry] = {
    # ── customer ────────────────────────────────────────────────────────────────────────────────
    "retail_churn": _uc("customer.relationship_attrition.churn"),
    "deposit_attrition": _uc("customer.relationship_attrition.deposit_attrition"),
    "primacy_loss": _uc("customer.relationship_attrition.primacy_loss"),
    "cross_sell": _uc("customer.cross_sell"),
    "next_best_action": _uc("customer.cross_sell.next_best_action"),
    "share_of_wallet": _uc("customer.cross_sell.share_of_wallet"),
    "whitespace": _uc("customer.cross_sell.whitespace"),
    "clv": _uc("customer.clv"),
    "engagement": _uc("customer.engagement"),
    "segmentation": _uc("customer.segmentation"),
    "campaign_analytics": _uc("customer.campaign"),
    "overdraft_propensity": _uc("customer.overdraft_propensity"),

    # ── wealth ──────────────────────────────────────────────────────────────────────────────────
    "wealth_outflow": _uc("wealth.asset_outflow"),

    # ── credit ──────────────────────────────────────────────────────────────────────────────────
    "credit_risk": _uc("credit"),                                   # broad domain → family parent
    "underwriting": _uc("credit.underwriting"),
    "affordability": _uc("credit.underwriting.affordability"),
    "credit_seasoning": _uc("credit.underwriting.seasoning"),
    "sme_credit": _uc("credit.underwriting.sme"),
    "early_warning": _uc("credit.early_warning"),
    "limit_management": _uc("credit.monitoring.limit_management"),
    "credit_mitigation": _uc("credit.monitoring.credit_mitigation"),
    "collections": _uc("credit.collections"),
    "recoveries": _uc("credit.collections.recoveries"),
    "hardship": _uc("credit.collections.hardship"),
    "self_cure": _uc("credit.collections.self_cure"),
    "workout": _uc("credit.collections.workout"),

    # ── financial_crime (domain + fraud / aml branches) ───────────────────────────────────────────
    "financial_crime": _uc("financial_crime"),                      # non-selectable domain hint
    "transaction_monitoring": _uc("financial_crime"),               # split by family in Task 4
    "fraud": _uc("fraud"),                                          # broad family → parent
    "card_fraud": _uc("fraud.card_fraud"),
    "account_takeover": _uc("fraud.account_takeover"),
    "app_scam": _uc("fraud.app_scam"),
    "synthetic_id": _uc("fraud.synthetic_id"),
    "aml": _uc("aml_cft"),                                          # broad family → parent
    "kyc": _uc("aml_cft.kyc"),
    "sanctions": _uc("aml_cft.sanctions"),
    "screening": _uc("aml_cft.screening"),
    "structuring": _uc("aml_cft.structuring"),
    "correspondent_banking": _uc("aml_cft.correspondent"),

    # ── treasury_alm ──────────────────────────────────────────────────────────────────────────────
    "alm": _uc("treasury_alm"),                                     # broad family → parent
    "deposit_stability": _uc("treasury_alm.deposit_stability"),
    "liquidity_risk": _uc("treasury_alm.liquidity"),
    "cash_management": _uc("treasury_alm.cash_management"),

    # ── portfolio_risk / counterparty_risk / markets ──────────────────────────────────────────────
    "portfolio_risk": _uc("portfolio_risk"),                        # broad family → parent
    "concentration_risk": _uc("portfolio_risk.concentration"),
    "counterparty_risk": _uc("counterparty_risk"),
    "market_risk": _uc("markets.market_risk"),
    "trading_risk": _uc("markets.market_risk"),                     # synonym of trading-book market risk

    # ── payments ──────────────────────────────────────────────────────────────────────────────────
    "payments": _uc("payments"),                                    # broad family → parent
    "payments_ops": _uc("payments.operations"),
    "cross_border": _uc("payments.cross_border"),
    "merchant_analytics": _uc("payments.merchant"),
    "interchange_optimisation": _uc("payments.merchant.interchange"),

    # ── securities_services ───────────────────────────────────────────────────────────────────────
    "securities_services": _uc("securities_services"),              # broad family → parent
    "custody": _uc("securities_services.custody"),
    "settlement_risk": _uc("securities_services.custody.settlement_failure_risk"),
    "corporate_actions": _uc("securities_services.custody.corporate_actions"),
    "securities_lending": _uc("securities_services.securities_lending"),
    "fund_administration": _uc("securities_services.fund_administration"),

    # ── insurance ─────────────────────────────────────────────────────────────────────────────────
    "insurance": _uc("insurance"),                                  # broad family → parent
    "lapse_risk": _uc("insurance.lapse"),
    "surrender": _uc("insurance.lapse.surrender"),
    "persistency": _uc("insurance.lapse.persistency"),
    "claims": _uc("insurance.claims"),
    "claims_fraud": _uc("insurance.claims.claims_fraud"),
    "reinsurance": _uc("insurance.reinsurance"),
    "bancassurance": _uc("insurance.bancassurance"),

    # ── asset_management ──────────────────────────────────────────────────────────────────────────
    "asset_management": _uc("asset_management"),                    # broad family → parent
    "redemption_risk": _uc("asset_management.redemption"),
    "fund_flows": _uc("asset_management.redemption.fund_flows"),
    "fund_liquidity": _uc("asset_management.redemption.fund_liquidity"),
    "aum_stability": _uc("asset_management.redemption.aum_stability"),
    "mandate_compliance": _uc("asset_management.mandate_compliance"),
    "fund_performance": _uc("asset_management.performance"),
    "distribution": _uc("asset_management"),                        # fund-distribution theme, no leaf

    # ── islamic ───────────────────────────────────────────────────────────────────────────────────
    "islamic_banking": _uc("islamic.banking"),
    "sharia_compliance": _uc("islamic.sharia_compliance"),

    # ── esg ───────────────────────────────────────────────────────────────────────────────────────
    "esg_scoring": _uc("esg.scoring"),
    "climate_risk": _uc("esg.climate"),
    "transition_risk": _uc("esg.climate.transition"),
    "physical_risk": _uc("esg.climate.physical"),

    # ── corporate_trade ───────────────────────────────────────────────────────────────────────────
    "trade_finance": _uc("corporate_trade.trade_finance"),
    "supply_chain_finance": _uc("corporate_trade.supply_chain_finance"),
    "working_capital": _uc("corporate_trade.working_capital"),
    "receivables_finance": _uc("corporate_trade.receivables_finance"),

    # ── merged into another tag (status "merged") ─────────────────────────────────────────────────
    "authorised_push_payment": _dim("typology", "app_scam", "merged"),   # → fraud app-scam typology
    "sustainable_finance": _uc("esg.scoring", "merged"),                 # folded into ESG scoring

    # ── reclassified OUT of use_case — regulatory frameworks (modelling_context) ───────────────────
    "ifrs9_staging": _dim("modelling_context", "ifrs9"),
    "frtb": _dim("modelling_context", "frtb"),
    "irrbb": _dim("modelling_context", "irrbb"),
    "lcr": _dim("modelling_context", "lcr"),
    "nsfr": _dim("modelling_context", "nsfr"),
    "ftp": _dim("modelling_context", "ftp"),
    "xva": _dim("modelling_context", "xva"),
    "lgd": _dim("modelling_context", "lgd"),

    # ── reclassified OUT — output measures (measure) ──────────────────────────────────────────────
    "tracking_error": _dim("measure", "tracking_error"),
    "data_quality": _dim("measure", "data_quality"),

    # ── reclassified OUT — product / journey / outcome ────────────────────────────────────────────
    "crypto": _dim("product_context", "crypto_assets"),
    "unbundling": _dim("journey_stage", "unbundling"),
    "cost_efficiency": _dim("business_outcome", "cost_efficiency"),

    # ── reclassified OUT — folded to state/ownership metadata (status "deprecated") ────────────────
    "contactability": _dim("metadata", "customer_state", "deprecated"),

    # ── deprecated generic tags — best representative node, split per-recipe in Task 4 ─────────────
    "exposure_management": _uc("counterparty_risk.exposure_monitoring", "deprecated"),
    "basis_risk": _uc("markets.market_risk.basis_risk", "deprecated"),
    "margin": _uc("counterparty_risk.margin_call_risk", "deprecated"),
    "cashflow": _uc("customer.relationship_attrition.churn", "deprecated"),
    "cashflow_risk": _uc("customer.relationship_attrition.churn", "deprecated"),
    "pricing": _uc("pricing", "deprecated"),                        # parent; leaves split in Task 4
}


def _validate_crosswalk() -> None:
    """Fail fast at import if a target is ungoverned: a ``use_case`` target must resolve in the
    registry; a governed-dimension target must be a member of that dimension; ``metadata`` targets
    are free-form (but must be a non-empty string); ``status`` must be one of the three legal values."""
    for tag, entry in LEGACY_TAG_CROSSWALK.items():
        dimension = entry["dimension"]
        target = entry["target"]
        status = entry["status"]
        if status not in _VALID_STATUSES:
            raise ValueError(f"crosswalk tag {tag!r} has invalid status {status!r}")
        if not target:
            raise ValueError(f"crosswalk tag {tag!r} has an empty target")
        if dimension == "use_case":
            if target not in USE_CASE_REGISTRY:
                raise ValueError(
                    f"crosswalk tag {tag!r} use_case target {target!r} is not a registered use-case")
        elif dimension == "metadata":
            continue                                                # free-form ownership/state id
        elif not dimensions.is_known(dimension, target):
            raise ValueError(
                f"crosswalk tag {tag!r} target {target!r} is not a member of dimension {dimension!r}")


_validate_crosswalk()


def crosswalk(tag: str) -> CrosswalkEntry | None:
    """The crosswalk entry for a legacy ``use_cases`` tag, or ``None`` if the tag is unknown."""
    return LEGACY_TAG_CROSSWALK.get(tag)
