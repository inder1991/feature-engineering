"""Discovery-taxonomy registries (Task 1, freeze 0F-5/0F-6): feature categories + recipe-family
display names.

Two controlled, in-repo authored registries used by suggestion discovery:

* ``FEATURE_CATEGORY_REGISTRY`` — the plan-local COARSE computation-shape vocabulary (rule 1:
  ``feature_category`` is its own axis, never an overloaded "category" string). Derived from the
  real 157-template inventory; deliberately ~a dozen entries, never one-per-family.
* ``RECIPE_FAMILY_REGISTRY`` — a stable ID/display wrapper over the existing authored
  ``Template.family`` values (121 at the 0F-2 baseline). The family STRINGS stay in
  ``templates.py`` and remain the identity; this registry only adds display names, so arbitrary
  authored text is never pretended to be a display label and a display rename can never move
  identity (brief: no mutable display-name-based identity).

Identity/version rules (0F-5): registry fingerprints use the shared JCS hasher via
``contract_hash_v1`` with contract names registered here; payloads are sorted so dict/set
reordering never moves a hash (must-survive), while any content edit does.

Ownership: the taxonomy owner is THIS module (``TAXONOMY_OWNER``), with an append-only
provenance/change log (``TAXONOMY_CHANGE_LOG``). Every mapping change records whether it was
``authored``, ``llm_proposed`` or ``human_edited`` — a human review record is never fabricated
for an unreviewed proposal, so the baseline log is authored-only.

Text safety: registry text is bounded PLAIN text — NFC-normalized for identity/search, control
characters and HTML rejected, never rendered as raw HTML (the backend stores plain text only).
"""
from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from featuregen.canonical import contract_hash_v1
from featuregen.contracts.contract_versions import register_contract_version
from featuregen.overlay.upload.taxonomy.versions import (
    FEATURE_CATEGORY_REGISTRY_VERSION,
    RECIPE_FAMILY_REGISTRY_VERSION,
)
from featuregen.overlay.upload.templates import ALL_TEMPLATES

__all__ = [
    "FEATURE_CATEGORY_REGISTRY",
    "RECIPE_FAMILY_REGISTRY",
    "TAXONOMY_CHANGE_KINDS",
    "TAXONOMY_CHANGE_LOG",
    "TAXONOMY_OWNER",
    "FeatureCategory",
    "RecipeFamily",
    "TaxonomyChangeRecord",
    "TaxonomyValidationError",
    "change_log_content_hash",
    "feature_category_registry_content_hash",
    "recipe_family_registry_content_hash",
    "safe_registry_text",
    "validate_feature_categories",
    "validate_recipe_families",
    "validate_taxonomy_change_log",
]

#: The named owner of the discovery taxonomy: this registry module itself (controller decision).
TAXONOMY_OWNER = "featuregen.overlay.upload.suggestion_taxonomy"

#: Closed provenance vocabulary for taxonomy changes. ``human_edited`` is reserved for a REAL
#: recorded human edit; it is never stamped on an unreviewed LLM proposal.
TAXONOMY_CHANGE_KINDS = frozenset({"authored", "llm_proposed", "human_edited"})

_MAX_ID_LEN = 64
_MAX_DISPLAY_LEN = 80
_MAX_DESCRIPTION_LEN = 300


class TaxonomyValidationError(ValueError):
    """A discovery-taxonomy registry violated its frozen shape. Import/tests must die on this."""


def safe_registry_text(value: object, *, field: str, max_len: int) -> str:
    """Validate + canonicalize one piece of registry text: NFC-normalized (identity/search),
    non-blank, length-bounded, and free of control characters and HTML markup. Returns the
    normalized text; raises :class:`TaxonomyValidationError` otherwise. Registry text is stored
    as plain text only and is never rendered as raw HTML."""
    if not isinstance(value, str):
        raise TaxonomyValidationError(f"{field} must be a string, got {type(value).__name__}")
    normalized = unicodedata.normalize("NFC", value)
    if not normalized.strip():
        raise TaxonomyValidationError(f"{field} must not be blank")
    if len(normalized) > max_len:
        raise TaxonomyValidationError(f"{field} exceeds its {max_len}-char bound")
    for ch in normalized:
        if unicodedata.category(ch).startswith("C"):
            raise TaxonomyValidationError(
                f"{field} contains a control/format character (U+{ord(ch):04X})")
    if "<" in normalized or ">" in normalized:
        raise TaxonomyValidationError(f"{field} must be plain text without HTML markup")
    return normalized


def _safe_stable_id(value: object, *, field: str) -> str:
    text = safe_registry_text(value, field=field, max_len=_MAX_ID_LEN)
    if text != text.lower() or any(ch.isspace() for ch in text):
        raise TaxonomyValidationError(
            f"{field} must be a stable lowercase token (no spaces), got {text!r}")
    return text


# ──────────────────────────────────────────────────────────────────────────────────────────────
# Feature categories — the coarse computation-shape browse axis (plan-local, controlled)
# ──────────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class FeatureCategory:
    """One coarse computation-shape category. ``id`` is the controlled facet key; the display
    name and description are presentation-only plain text."""

    id: str
    display_name: str
    description: str


_FEATURE_CATEGORIES: tuple[FeatureCategory, ...] = (
    FeatureCategory("ratio", "Ratio & Share",
                    "One quantity relative to another — rates, shares, splits and mix "
                    "proportions over a window."),
    FeatureCategory("trend", "Trend & Trajectory",
                    "Directional change over time — growth, decline, deltas and stage/bucket "
                    "migration dynamics."),
    FeatureCategory("recency", "Recency & Timing",
                    "Time since (or until) an event — dormancy, lags, timeliness and elapsed "
                    "time to a milestone."),
    FeatureCategory("frequency", "Frequency & Velocity",
                    "How often events occur in a window — counts, velocities and incidence of "
                    "discrete events."),
    FeatureCategory("concentration", "Concentration & Diversity",
                    "How activity or exposure distributes across a dimension — HHI-style "
                    "concentration, breadth and distinct-value diversity."),
    FeatureCategory("volatility", "Volatility & Stability",
                    "Dispersion or steadiness of a quantity over time — variability, "
                    "stickiness and regularity."),
    FeatureCategory("utilisation", "Utilisation & Headroom",
                    "Usage against a limit, capacity or threshold — utilisation rates, "
                    "headroom, coverage and breach proximity."),
    FeatureCategory("duration", "Duration & Streak",
                    "Elapsed-state measures — tenure, days-in-state, ageing buckets, streaks "
                    "and cycle lengths."),
    FeatureCategory("deviation", "Deviation & Anomaly",
                    "Departure from a baseline, benchmark or expected pattern — spikes, "
                    "z-scores, gaps and anomaly flags."),
    FeatureCategory("composite", "Composite Score",
                    "A multi-factor composite combining several signals into one score or "
                    "typology assessment."),
    FeatureCategory("exposure", "Exposure & Level",
                    "The magnitude of a position, exposure or aggregated value at a point in "
                    "time or over a window."),
)

FEATURE_CATEGORY_REGISTRY: dict[str, FeatureCategory] = {c.id: c for c in _FEATURE_CATEGORIES}


def validate_feature_categories(registry: Mapping[str, FeatureCategory]) -> None:
    """Frozen shape of the feature-category registry: key==id, stable lowercase ids, bounded
    safe display/description text. Raises :class:`TaxonomyValidationError` on any violation."""
    if not registry:
        raise TaxonomyValidationError("feature-category registry must not be empty")
    for key, cat in registry.items():
        if not isinstance(cat, FeatureCategory):
            raise TaxonomyValidationError(f"feature-category {key!r} is not a FeatureCategory")
        cid = _safe_stable_id(cat.id, field="feature-category id")
        if key != cid:
            raise TaxonomyValidationError(
                f"feature-category key {key!r} != id {cat.id!r}: identity is the stable id, "
                f"never a display name")
        safe_registry_text(cat.display_name, field=f"feature-category {cid} display_name",
                           max_len=_MAX_DISPLAY_LEN)
        safe_registry_text(cat.description, field=f"feature-category {cid} description",
                           max_len=_MAX_DESCRIPTION_LEN)


def feature_category_registry_content_hash(
        registry: Mapping[str, FeatureCategory] = FEATURE_CATEGORY_REGISTRY) -> str:
    """Order-independent version fingerprint of the feature-category registry."""
    payload = {
        "version": FEATURE_CATEGORY_REGISTRY_VERSION,
        "categories": [
            {"id": c.id, "display_name": c.display_name, "description": c.description}
            for c in sorted(registry.values(), key=lambda c: c.id)
        ],
    }
    return contract_hash_v1("feature-category-registry", "1", payload)


# ──────────────────────────────────────────────────────────────────────────────────────────────
# Recipe families — stable display wrapper over the authored Template.family values
# ──────────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class RecipeFamily:
    """Display record for one authored ``Template.family`` value. ``id`` IS the authored family
    string (identity never moves with a display rename)."""

    id: str
    display_name: str


_RECIPE_FAMILY_DISPLAY_NAMES: dict[str, str] = {
    "access_takeover": "Access Takeover",
    "activity_trend": "Activity Trend",
    "affordability": "Affordability",
    "arrears_dpd": "Arrears & Days Past Due",
    "aum_stability": "AUM Stability",
    "balance_stock": "Balance Stock",
    "bancassurance": "Bancassurance",
    "benchmark_tracking": "Benchmark Tracking",
    "break_behaviour": "Break Behaviour",
    "bureau_external": "External Bureau",
    "campaign_response": "Campaign Response",
    "cash_out": "Cash-out",
    "cash_pooling": "Cash Pooling",
    "cashflow_ratio": "Cashflow Ratio",
    "channel_engagement": "Channel Engagement",
    "charge_off": "Charge-off",
    "claims": "Claims",
    "claims_fraud": "Claims Fraud",
    "clv_revenue": "CLV & Revenue",
    "collateral": "Collateral",
    "collection_tenure": "Collection Tenure",
    "concentration": "Concentration",
    "contactability": "Contactability",
    "corporate_actions": "Corporate Actions",
    "corridor_mix": "Corridor Mix",
    "cost_to_collect": "Cost to Collect",
    "counterparty_deterioration": "Counterparty Deterioration",
    "counterparty_exposure": "Counterparty Exposure",
    "covenant": "Covenant",
    "cross_product_stress": "Cross-product Stress",
    "cure_reage": "Cure & Re-age",
    "custody_holdings": "Custody Holdings",
    "data_quality": "Data Quality",
    "distribution_mix": "Distribution Mix",
    "economics": "Economics",
    "emissions": "Emissions",
    "exposure_provisioning": "Exposure & Provisioning",
    "facility_utilisation": "Facility Utilisation",
    "fail_ageing": "Fail Ageing",
    "fee_competitiveness": "Fee Competitiveness",
    "financed_emissions": "Financed Emissions",
    "forbearance": "Forbearance",
    "forbearance_sicr": "Forbearance & SICR",
    "fund_admin_nav": "Fund Administration NAV",
    "funding_concentration": "Funding Concentration",
    "group_exposure": "Group Exposure",
    "guarantor_support": "Guarantor Support",
    "hot_money": "Hot Money",
    "income_signal": "Income Signal",
    "integration": "Integration",
    "invoice_finance": "Invoice Finance",
    "irrbb_repricing": "IRRBB Repricing",
    "layering": "Layering",
    "lcr_liquidity": "LCR Liquidity",
    "liquidity_coverage": "Liquidity Coverage",
    "mandate_compliance": "Mandate Compliance",
    "margin_collateral": "Margin & Collateral",
    "market_risk_measures": "Market Risk Measures",
    "matching": "Matching",
    "merchant_behaviour": "Merchant Behaviour",
    "murabaha": "Murabaha",
    "net_interest_margin": "Net Interest Margin",
    "next_best_product": "Next Best Product",
    "nmd_stability": "NMD Stability",
    "notional_exposure": "Notional Exposure",
    "nsfr_funding": "NSFR Funding",
    "obligor_monitoring": "Obligor Monitoring",
    "payment_plan": "Payment Plan",
    "peer_penetration": "Peer Penetration",
    "persistency": "Persistency",
    "physical_risk": "Physical Risk",
    "placement": "Placement",
    "pre_settlement": "Pre-settlement",
    "primacy_outflow": "Primacy Outflow",
    "product_holding": "Product Holding",
    "promise_to_pay": "Promise to Pay",
    "purpose_mix": "Purpose Mix",
    "rail_mix": "Rail Mix",
    "rate_sensitivity": "Rate Sensitivity",
    "recency": "Recency",
    "recon_targeting": "Recon Targeting",
    "recovery": "Recovery",
    "redemption_flow": "Redemption Flow",
    "reinsurance": "Reinsurance",
    "relationship_aggregation": "Relationship Aggregation",
    "relationship_deepening": "Relationship Deepening",
    "relative_performance": "Relative Performance",
    "repayment_behaviour": "Repayment Behaviour",
    "rfm": "RFM",
    "roll_rate": "Roll Rate",
    "runoff_forecasting": "Run-off Forecasting",
    "runoff_ladder": "Run-off Ladder",
    "securities_lending": "Securities Lending",
    "sensitivities_greeks": "Sensitivities & Greeks",
    "settlement_fail": "Settlement Fail",
    "settlement_quality": "Settlement Quality",
    "settlement_timing": "Settlement Timing",
    "setup_staging": "Setup & Staging",
    "share_of_wallet": "Share of Wallet",
    "sharia_profit_rate": "Sharia Profit Rate",
    "sharia_profit_sharing": "Sharia Profit Sharing",
    "sharia_purification": "Sharia Purification",
    "sharia_screening": "Sharia Screening",
    "sukuk": "Sukuk",
    "supply_chain_finance": "Supply Chain Finance",
    "surrender_pressure": "Surrender Pressure",
    "sustainability_linked": "Sustainability-linked",
    "syndication": "Syndication",
    "takaful": "Takaful",
    "taxonomy": "Taxonomy Alignment",
    "tenure": "Tenure",
    "tracking_error": "Tracking Error",
    "trade_finance_contingent": "Trade Finance Contingent",
    "trading_limits": "Trading Limits",
    "transition": "Transition Risk",
    "unbundling": "Unbundling",
    "underwriting": "Underwriting",
    "upsell_readiness": "Upsell Readiness",
    "utilisation_exposure": "Utilisation & Exposure",
    "whitespace": "Whitespace",
    "working_capital": "Working Capital",
}

RECIPE_FAMILY_REGISTRY: dict[str, RecipeFamily] = {
    fid: RecipeFamily(id=fid, display_name=name)
    for fid, name in _RECIPE_FAMILY_DISPLAY_NAMES.items()
}


def validate_recipe_families(registry: Mapping[str, RecipeFamily],
                             templates=ALL_TEMPLATES) -> None:
    """EXACT coverage of the authored family ids, both directions: every ``Template.family`` has
    a display entry, and no entry names a family that no template authors (orphan). Identity is
    the authored family string; display names are bounded safe text."""
    authored = {t.family for t in templates}
    missing = authored - set(registry)
    if missing:
        raise TaxonomyValidationError(
            f"recipe-family registry is missing authored families: {sorted(missing)}")
    orphans = set(registry) - authored
    if orphans:
        raise TaxonomyValidationError(
            f"recipe-family registry has orphan families no template authors: {sorted(orphans)}")
    for key, fam in registry.items():
        if not isinstance(fam, RecipeFamily):
            raise TaxonomyValidationError(f"recipe-family {key!r} is not a RecipeFamily")
        if fam.id != key:
            raise TaxonomyValidationError(f"recipe-family key {key!r} != id {fam.id!r}")
        safe_registry_text(fam.display_name, field=f"recipe-family {key} display_name",
                           max_len=_MAX_DISPLAY_LEN)


def recipe_family_registry_content_hash(
        registry: Mapping[str, RecipeFamily] = RECIPE_FAMILY_REGISTRY) -> str:
    """Order-independent version fingerprint of the recipe-family display registry."""
    payload = {
        "version": RECIPE_FAMILY_REGISTRY_VERSION,
        "families": [
            {"id": f.id, "display_name": f.display_name}
            for f in sorted(registry.values(), key=lambda f: f.id)
        ],
    }
    return contract_hash_v1("recipe-family-registry", "1", payload)


# ──────────────────────────────────────────────────────────────────────────────────────────────
# Owner provenance — append-only change log
# ──────────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class TaxonomyChangeRecord:
    """One append-only provenance entry for a taxonomy/mapping change. ``change_kind`` records
    HOW the change was made (``authored`` | ``llm_proposed`` | ``human_edited``); a human review
    record is never fabricated for an unreviewed proposal."""

    seq: int
    change_kind: str
    subject: str
    summary: str


#: Append-only. New changes APPEND with the next ``seq``; existing entries are immutable —
#: the golden prefix-hash test dies on any rewrite of history.
TAXONOMY_CHANGE_LOG: tuple[TaxonomyChangeRecord, ...] = (
    TaxonomyChangeRecord(1, "authored", "feature-category-registry",
                         "Authored the coarse computation-shape category registry (11 entries) "
                         "derived from the 157-template inventory."),
    TaxonomyChangeRecord(2, "authored", "recipe-family-registry",
                         "Authored display names for all 121 authored Template.family values."),
    TaxonomyChangeRecord(3, "authored", "family-feature-category-mapping",
                         "Authored the deterministic family-to-category mapping; genuinely "
                         "ambiguous families stay unclassified."),
    TaxonomyChangeRecord(4, "authored", "template-discovery-metadata",
                         "Authored canonical use-case assignments for the 14 templates with "
                         "authored objectives; recorded the 107 legacy use-case tags as an "
                         "unresolved audit manifest (no silent conversion)."),
    TaxonomyChangeRecord(5, "authored", "family-feature-category-mapping",
                         "Review fix: a family-derived feature_category now cites the mapping "
                         "content hash and family rule that produced it, not the recipe "
                         "revision alone, so it never reads as an SME-authored objective."),
    TaxonomyChangeRecord(6, "authored", "template-discovery-metadata",
                         "Review fix: needs_sme is inadmissible in v1. The disposition is "
                         "excluded from the revision hash because it is derived; an authored "
                         "escalation there would move no hash and could serve stale forever."),
)


def validate_taxonomy_change_log(log: Sequence[TaxonomyChangeRecord]) -> None:
    """Closed kinds, strictly-increasing ``seq`` from 1, bounded safe text."""
    for position, record in enumerate(log, start=1):
        if not isinstance(record, TaxonomyChangeRecord):
            raise TaxonomyValidationError(f"change-log entry {position} is not a record")
        if record.change_kind not in TAXONOMY_CHANGE_KINDS:
            raise TaxonomyValidationError(
                f"change-log entry {position} has unknown kind {record.change_kind!r}; the "
                f"closed vocabulary is {sorted(TAXONOMY_CHANGE_KINDS)}")
        if record.seq != position:
            raise TaxonomyValidationError(
                f"change-log seq must be strictly increasing from 1 (append-only); entry at "
                f"position {position} carries seq {record.seq}")
        safe_registry_text(record.subject, field=f"change-log {position} subject",
                           max_len=_MAX_ID_LEN)
        safe_registry_text(record.summary, field=f"change-log {position} summary",
                           max_len=_MAX_DESCRIPTION_LEN)


def change_log_content_hash(log: Sequence[TaxonomyChangeRecord]) -> str:
    """Hash of a change-log PREFIX. Order is meaning here (append-only history), so the payload
    keeps authored order; the golden prefix pin makes history rewrites die."""
    payload = {
        "entries": [
            {"seq": r.seq, "change_kind": r.change_kind, "subject": r.subject,
             "summary": r.summary}
            for r in log
        ],
    }
    return contract_hash_v1("suggestion-taxonomy-change-log", "1", payload)


# ── contract registrations (one owner; contract_hash_v1 refuses unregistered pairs) ────────────
register_contract_version("feature-category-registry", "1", owner=TAXONOMY_OWNER)
register_contract_version("recipe-family-registry", "1", owner=TAXONOMY_OWNER)
register_contract_version("suggestion-taxonomy-change-log", "1", owner=TAXONOMY_OWNER)

# ── import-time validation: a malformed registry dies at import, never at first read ───────────
validate_feature_categories(FEATURE_CATEGORY_REGISTRY)
validate_recipe_families(RECIPE_FAMILY_REGISTRY)
validate_taxonomy_change_log(TAXONOMY_CHANGE_LOG)
