"""Per-recipe applicability — the governed use-case a recipe scopes on, *derived* (not stamped).

Every one of the 153 legacy recipes still carries only its flat ``Template.use_cases`` tag bag; this
module turns that bag into a governed :class:`ApplicabilitySpec` — a single selectable-leaf ``primary``
objective plus the ordered ``secondary`` objectives and the routed non-use_case dimension fields
(``product_context`` / ``typology`` / ``journey_stage`` / ``business_outcome``). Behaviour-neutral:
read-only, derived through the Task-3 crosswalk + a small per-recipe override table — ``templates.py``
and grounding are untouched.

Resolution order for the ``primary`` (first hit wins):

1. **Explicit override** (:data:`_PRIMARY_OVERRIDE`) — the spec's D3/D7 hard cases that cannot be read
   off the tag alone (the ``transaction_monitoring`` fraud/aml split, the concentration recipes, the
   promoted counterparty / basis homes). These win even when a tag *would* derive a different leaf.
2. **Derivation** — the first ``use_case``-dimension tag whose crosswalk target is a **selectable leaf**
   (``selectable_leaves()``). A tag whose target is a non-leaf *parent* (``credit``, ``fraud``,
   ``financial_crime``, ``credit.collections`` …) is a domain, not an objective — skipped.
3. **Orphan home** (:data:`_ORPHAN_PRIMARY`) — a per-recipe default for the handful of recipes whose
   family default would be the *wrong* leaf (a counterparty recipe in the markets family; the two
   insurance actuarial/underwriting recipes and the custody holdings recipe, each now pinned to its
   precise governed leaf — see :data:`_LEAF_MIGRATIONS` for the old→new closest-fit corrections).
4. **Family fallback** (:data:`_FAMILY_FALLBACK_LEAF`) — the family's default leaf, keyed by which
   ``templates.*_TEMPLATES`` tuple the recipe belongs to, for recipes whose tags only ever hit non-leaf
   parents (the IFRS9 deterioration recipes, ``chargeback_dispute_rate``, VaR/greek book-risk, …).

Steps 3–4 close the gap the plan flags ("any recipe you could not give a selectable-leaf primary … +
how you fixed it"): the plan spells out family fallback only for fraud/aml, but a further 10 recipes
across the credit / payments / markets / custody / asset-management families reach fallback because
their tags resolve solely to non-leaf parents. Each is given a domain-defensible selectable leaf here.

Authored from ``docs/superpowers/specs/2026-07-09-usecase-taxonomy-crosswalk-draft.md`` §2 (D3/D6/D7)
and the Task-4 override table in ``docs/superpowers/plans/2026-07-09-phase0-taxonomy-foundation.md``.
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.overlay.upload import templates
from featuregen.overlay.upload.taxonomy.legacy_crosswalk import crosswalk
from featuregen.overlay.upload.taxonomy.use_cases import ancestors, selectable_leaves


@dataclass(frozen=True, slots=True)
class ApplicabilitySpec:
    """The governed applicability of one recipe.

    ``primary`` is always a **selectable leaf** in ``USE_CASE_REGISTRY`` (validated by the Task-4 test);
    ``secondary`` are the other distinct selectable-leaf objectives the recipe also serves, in tag order.
    The remaining tuples route the recipe's non-use_case tags to their governed dimension (spec §1);
    ``supporting`` is declared for future use and is currently always empty.
    """
    primary: str
    secondary: tuple[str, ...] = ()
    supporting: tuple[str, ...] = ()
    product_context: tuple[str, ...] = ()
    typology: tuple[str, ...] = ()
    journey_stage: tuple[str, ...] = ()
    business_outcome: tuple[str, ...] = ()


# ── Step 1: explicit primary overrides (win over derivation — spec D3/D7, by recipe id) ─────────────
# transaction_monitoring split by governed objective — bare "transaction_monitoring" is only a
# capability tag, so the primary is the family's monitoring leaf even when a more specific typology
# tag exists (D3). The other derived use-case leaves fall through to `secondary`.
_FRAUD_TXN_MONITORING: tuple[str, ...] = (
    "card_testing_velocity", "device_sharing_velocity", "new_device_flag", "geo_velocity_impossible",
    "first_time_payee_high_value", "merchant_risk_anomaly", "txn_velocity_spike", "amount_zscore_spike",
    "cross_channel_rail_anomaly", "cross_border_burst", "amount_just_under_limit",
)
_AML_TXN_MONITORING: tuple[str, ...] = (
    "structuring_smurfing", "cash_intensity_ratio", "rapid_movement_passthrough", "round_amount_ratio",
    "fan_in_fan_out", "high_risk_corridor_exposure", "nested_correspondent_flow", "crypto_offramp_exposure",
    "dormant_reactivation", "screening_exposure", "prior_alert_recidivism",
)
# concentration_risk is cross-cutting — all six recipes land on the promoted concentration leaf (D7).
_CONCENTRATION: tuple[str, ...] = (
    "rate_sensitive_concentration", "book_desk_concentration", "sukuk_concentration",
    "syndication_concentration", "group_exposure_aggregation", "guarantor_reliance",
)

_PRIMARY_OVERRIDE: dict[str, str] = {
    **{r: "fraud.transaction_fraud_detection" for r in _FRAUD_TXN_MONITORING},
    **{r: "aml_cft.suspicious_transaction_monitoring" for r in _AML_TXN_MONITORING},
    **{r: "portfolio_risk.concentration" for r in _CONCENTRATION},
    # counterparty / basis promotions (D7): pin the generic exposure/margin/basis recipes to their homes.
    "notional_netting_exposure": "counterparty_risk.exposure_monitoring",
    "margin_call_intensity": "counterparty_risk.margin_call_risk",
    "benchmark_basis_dislocation": "markets.market_risk.basis_risk",
    # Adversarial-review corrections: a FOREIGN generic-tag leaf (hardship/limit_management -> credit)
    # was beating the recipe's own family, or the wrong SAME-family leaf won on tag order. Pin each to
    # its true objective (the family-aware rule below also prevents the foreign-leaf class in general).
    "policy_loan_utilisation": "insurance.lapse.surrender",         # insurance pre-lapse, NOT credit hardship
    "trading_limit_utilisation": "markets.market_risk.portfolio",   # markets book limit, NOT credit facility limit
    "dscr_covenant_headroom": "credit.early_warning",               # covenant-headroom EWI, NOT origination affordability
    "rail_scheme_diversity": "payments.behaviour",                  # payment-behaviour mix, NOT customer segmentation
    "purpose_code_diversity": "payments.behaviour",                 # payment-behaviour mix, NOT customer segmentation
}

# ── Step 3: per-recipe orphan homes (derivation yields no leaf; the family default is the wrong leaf) ─
_ORPHAN_PRIMARY: dict[str, str] = {
    # EPE/PFE counterparty exposure sitting in the markets family — sibling of notional_netting_exposure.
    "counterparty_exposure_trend": "counterparty_risk.exposure_monitoring",
    # Three recipes now on their PRECISE governed home (taxonomy patch — see _LEAF_MIGRATIONS). Each is a
    # distinct objective the tree previously lacked a leaf for; the old closest-fit is an audit record
    # only (never a secondary), per the owner's ruling.
    "claims_frequency_severity": "insurance.actuarial.claims_cost_modelling",
    "mortality_morbidity_loading": "insurance.underwriting.mortality_morbidity_risk_assessment",
    "custody_holding_dynamics": "securities_services.custody.holdings_dynamics",
}

# ── Leaf-migration audit record (owner-approved MINOR corrections) ───────────────────────────────────
# Three recipes previously sat on a *documented closest-fit* leaf because the tree lacked a precise home;
# the taxonomy owner promoted three precise leaves (+ renamed the custody settlement leaf) and remapped
# each recipe onto its true objective. This dict keeps the old→new correction ONLY as a legacy audit
# record — the old id is NEVER carried as a secondary applicability (owner's ruling).
_LEAF_MIGRATIONS: dict[str, tuple[str, str, str]] = {
    # recipe id: (old closest-fit leaf, new precise leaf, reason)
    "claims_frequency_severity": (
        "insurance.claims.claims_fraud", "insurance.actuarial.claims_cost_modelling",
        "expected claims cost (frequency × severity) is an actuarial objective, not fraud detection"),
    "mortality_morbidity_loading": (
        "insurance.reinsurance", "insurance.underwriting.mortality_morbidity_risk_assessment",
        "applicant mortality/morbidity risk assessment is underwriting, not ceded-risk reinsurance"),
    "custody_holding_dynamics": (
        "securities_services.custody.settlement", "securities_services.custody.holdings_dynamics",
        "assets-under-custody holdings dynamics is a stock objective, not settlement-fail risk"),
}

# ── Step 4: family fallback — the family's default leaf when a recipe has no IN-FAMILY derivable leaf ─
# Every family maps to a real selectable leaf, so a recipe whose own-domain tags only hit parents (or
# whose only derivable leaf is FOREIGN) lands on a domain-appropriate default instead of leaking to
# another family's leaf. All targets are verified selectable leaves by the Task-4 test.
_FAMILY_FALLBACK_LEAF: dict[str, str] = {
    "RETAIL_CHURN_TEMPLATES": "customer.relationship_attrition.churn",
    "CREDIT_RISK_TEMPLATES": "credit.early_warning",              # IFRS9 SICR/ECL/delinquency deterioration
    "FRAUD_TEMPLATES": "fraud.transaction_fraud_detection",
    "AML_TEMPLATES": "aml_cft.suspicious_transaction_monitoring",
    "COLLECTIONS_TEMPLATES": "credit.collections.recoveries",
    "DEPOSITS_TEMPLATES": "treasury_alm.deposit_stability",
    "PAYMENTS_TEMPLATES": "payments.operations",                  # chargeback / dispute processing quality
    "MARKETS_TEMPLATES": "markets.market_risk.portfolio",         # VaR/ES & book-level greek sensitivities
    "CUSTODY_TEMPLATES": "securities_services.custody.settlement_failure_risk",  # custody-book fail anchor
    "ASSET_MGMT_TEMPLATES": "asset_management.redemption.fund_flows",  # share-class flow mix / TER-driven flows
    "INSURANCE_TEMPLATES": "insurance.lapse.surrender",
    "ISLAMIC_TEMPLATES": "islamic.sharia_compliance",
    "ESG_TEMPLATES": "esg.scoring",
    "CROSS_SELL_TEMPLATES": "customer.cross_sell.next_best_action",
    "CORPORATE_TRADE_TEMPLATES": "corporate_trade.trade_finance",
}

# Each family's top-level use-case root (the subtree a recipe's primary should live in). A derived or
# fallback primary MUST sit under its family root; only the audited _PRIMARY_OVERRIDE / _ORPHAN_PRIMARY
# tables may home a recipe cross-domain (e.g. a counterparty recipe authored in the markets tuple, or a
# cross-cutting concentration recipe -> portfolio_risk). Enforced by the domain-consistency test.
_FAMILY_ROOT: dict[str, str] = {
    "RETAIL_CHURN_TEMPLATES": "customer",
    "CREDIT_RISK_TEMPLATES": "credit",
    "FRAUD_TEMPLATES": "fraud",
    "AML_TEMPLATES": "aml_cft",
    "COLLECTIONS_TEMPLATES": "credit",
    "DEPOSITS_TEMPLATES": "treasury_alm",
    "PAYMENTS_TEMPLATES": "payments",
    "MARKETS_TEMPLATES": "markets",
    "CUSTODY_TEMPLATES": "securities_services",
    "ASSET_MGMT_TEMPLATES": "asset_management",
    "INSURANCE_TEMPLATES": "insurance",
    "ISLAMIC_TEMPLATES": "islamic",
    "ESG_TEMPLATES": "esg",
    "CROSS_SELL_TEMPLATES": "customer",
    "CORPORATE_TRADE_TEMPLATES": "corporate_trade",
}

# Ordered (name, tuple) family membership — used to resolve a recipe id to its family fallback leaf.
_FAMILIES: tuple[tuple[str, tuple[templates.Template, ...]], ...] = (
    ("RETAIL_CHURN_TEMPLATES", templates.RETAIL_CHURN_TEMPLATES),
    ("CREDIT_RISK_TEMPLATES", templates.CREDIT_RISK_TEMPLATES),
    ("FRAUD_TEMPLATES", templates.FRAUD_TEMPLATES),
    ("AML_TEMPLATES", templates.AML_TEMPLATES),
    ("COLLECTIONS_TEMPLATES", templates.COLLECTIONS_TEMPLATES),
    ("DEPOSITS_TEMPLATES", templates.DEPOSITS_TEMPLATES),
    ("PAYMENTS_TEMPLATES", templates.PAYMENTS_TEMPLATES),
    ("MARKETS_TEMPLATES", templates.MARKETS_TEMPLATES),
    ("CUSTODY_TEMPLATES", templates.CUSTODY_TEMPLATES),
    ("ASSET_MGMT_TEMPLATES", templates.ASSET_MGMT_TEMPLATES),
    ("INSURANCE_TEMPLATES", templates.INSURANCE_TEMPLATES),
    ("ISLAMIC_TEMPLATES", templates.ISLAMIC_TEMPLATES),
    ("ESG_TEMPLATES", templates.ESG_TEMPLATES),
    ("CROSS_SELL_TEMPLATES", templates.CROSS_SELL_TEMPLATES),
    ("CORPORATE_TRADE_TEMPLATES", templates.CORPORATE_TRADE_TEMPLATES),
)
_ID_TO_FAMILY: dict[str, str] = {t.id: name for name, fam in _FAMILIES for t in fam}

# ── Per-recipe field additions (idempotent) — spec D4/D6, and journey/outcome tags the plan pins ─────
# Most of these are ALSO derivable from the recipe's own tags; they are declared explicitly so the spec
# guarantee holds regardless of tag drift, and applied as order-preserving *additions* (never dropping a
# derived value). `crypto_asset_laundering` is the one non-derivable addition (no legacy tag maps to it).
_SECONDARY_ADD: dict[str, tuple[str, ...]] = {
    "salary_signal": ("customer.relationship_attrition.primacy_loss",),
    "external_own_transfer_trend": (
        "customer.relationship_attrition.primacy_loss", "wealth.asset_outflow"),
}
_PRODUCT_CONTEXT_ADD: dict[str, tuple[str, ...]] = {
    "crypto_offramp_exposure": ("crypto_assets",),
    # Insurance product-line context for the two remapped actuarial/underwriting recipes.
    "claims_frequency_severity": ("insurance",),
    "mortality_morbidity_loading": ("life_insurance", "health_insurance"),
}
_TYPOLOGY_ADD: dict[str, tuple[str, ...]] = {
    "crypto_offramp_exposure": ("crypto_asset_laundering",),
}
_JOURNEY_STAGE_ADD: dict[str, tuple[str, ...]] = {
    "dd_cancellation_rate": ("unbundling",),
}
_BUSINESS_OUTCOME_ADD: dict[str, tuple[str, ...]] = {
    "cost_to_collect_ratio": ("cost_efficiency",),
}

# Dimension name -> the ApplicabilitySpec field it routes to (non-use_case, non-ignored dimensions).
# modelling_context / measure / metadata are deliberately absent — they are not applicability fields.
_DIMENSION_FIELDS: frozenset[str] = frozenset(
    {"product_context", "typology", "journey_stage", "business_outcome"})


def _dedup(values: list[str]) -> tuple[str, ...]:
    """Order-preserving de-duplication."""
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return tuple(out)


def recipe_applicability(template: templates.Template) -> ApplicabilitySpec:
    """Derive the governed :class:`ApplicabilitySpec` for one recipe.

    Walks the recipe's ``use_cases`` tags in order through the Task-3 crosswalk: use_case-dimension
    tags that hit a selectable leaf are candidate objectives (parents are skipped as domains), and the
    routed dimensions (product_context / typology / journey_stage / business_outcome) accumulate. The
    ``primary`` is chosen by the four-step resolution documented in the module header; the remaining
    selectable-leaf objectives become ``secondary``; finally the per-recipe overrides add any objective
    or dimension value the tag bag cannot express (e.g. the ``crypto_asset_laundering`` typology).
    """
    leaves = frozenset(selectable_leaves())

    leaf_targets: list[str] = []
    dim_values: dict[str, list[str]] = {name: [] for name in _DIMENSION_FIELDS}
    for tag in template.use_cases:
        entry = crosswalk(tag)
        if entry is None:
            continue
        dimension = entry["dimension"]
        target = entry["target"]
        if dimension == "use_case":
            if target in leaves:                      # a selectable objective; parents are domains -> skip
                leaf_targets.append(target)
        elif dimension in dim_values:                 # product_context / typology / journey / outcome
            dim_values[dimension].append(target)
        # modelling_context / measure / metadata -> ignored (not applicability fields)

    # ── primary: override > IN-FAMILY derived leaf > orphan home > family fallback ──
    # Family-aware (adversarial-review fix): a foreign-family generic-tag leaf (e.g. a markets recipe's
    # `limit_management` tag hitting the CREDIT limit leaf) must NEVER become the primary — the recipe's
    # objective belongs to its own domain. So derivation only accepts a leaf inside the family's subtree;
    # a recipe with no in-family leaf falls to its orphan home / family fallback, not a foreign leaf.
    family = _ID_TO_FAMILY.get(template.id)
    family_root = _FAMILY_ROOT.get(family) if family is not None else None

    def _in_family(leaf: str) -> bool:
        return family_root is not None and (leaf == family_root or family_root in ancestors(leaf))

    in_family_leaves = [leaf for leaf in leaf_targets if _in_family(leaf)]
    if template.id in _PRIMARY_OVERRIDE:
        primary = _PRIMARY_OVERRIDE[template.id]
    elif in_family_leaves:
        primary = in_family_leaves[0]
    elif template.id in _ORPHAN_PRIMARY:
        primary = _ORPHAN_PRIMARY[template.id]
    else:
        fallback = _FAMILY_FALLBACK_LEAF.get(family) if family is not None else None
        if fallback is not None:
            primary = fallback
        elif leaf_targets:                       # last resort only (every family has a fallback today)
            primary = leaf_targets[0]
        else:
            raise ValueError(
                f"recipe {template.id!r} (family {family!r}) has no selectable-leaf primary: "
                "no override, no in-family leaf, no orphan home, and no family fallback")

    # secondary: the other distinct selectable-leaf objectives (excluding primary), in tag order,
    # then any explicitly-added secondaries — deduped, and never containing the primary.
    secondary = tuple(
        s for s in _dedup([*leaf_targets, *_SECONDARY_ADD.get(template.id, ())]) if s != primary)

    return ApplicabilitySpec(
        primary=primary,
        secondary=secondary,
        product_context=_dedup(
            [*dim_values["product_context"], *_PRODUCT_CONTEXT_ADD.get(template.id, ())]),
        typology=_dedup([*dim_values["typology"], *_TYPOLOGY_ADD.get(template.id, ())]),
        journey_stage=_dedup(
            [*dim_values["journey_stage"], *_JOURNEY_STAGE_ADD.get(template.id, ())]),
        business_outcome=_dedup(
            [*dim_values["business_outcome"], *_BUSINESS_OUTCOME_ADD.get(template.id, ())]),
    )
