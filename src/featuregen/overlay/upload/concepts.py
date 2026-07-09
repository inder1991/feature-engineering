"""Controlled concept vocabulary for the upload overlay — a structured, behaviour-carrying registry.

Each concept is not just a string but a small record of banking behaviour: how it may aggregate
(``additivity``), its point-in-time role (``pit_role``), its sensitivity / regulatory class
(``sensitivity``), the entity an identifier links (``entity_link``), an ``is-a`` parent for
generalisation, and whether it is a leakage anchor (a target / target-defining column that features
must never be built from). The reasoning layer uses this behaviour deterministically — e.g.
``monetary_stock`` must not be summed over time, ``currency_code`` values must not be mixed in a sum,
``geographic`` is a fair-lending proxy, ``outcome_label`` is the leakage anchor.

Authored from ``docs/superpowers/specs/2026-07-07-banking-taxonomy-reference.md`` §3 (§3.1–§3.17),
applying banking-SME judgment where a tag isn't spelled out (noted in the concept's description).

Backward-compat: ``UNCLASSIFIED``, ``CONCEPTS`` (frozenset of every name), ``is_known_concept`` and
``humanize`` keep their exact signatures. The 11 original concept strings are retained (some as legacy
aliases superseded by a richer §3 concept) so live enriched columns are never orphaned.
"""
from __future__ import annotations

from dataclasses import dataclass

UNCLASSIFIED = "unclassified"


@dataclass(frozen=True, slots=True)
class Concept:
    name: str
    group: str                      # "monetary" | "identifier" | "temporal" | "quantity_risk" |
    #                                 "categorical" | "geographic" | "flag" | "sensitive" | "text" |
    #                                 "label" | "behavioural" | "network" | "bitemporal" | "currency" |
    #                                 "eligibility" | "regulatory_capital" | "accounting" | "esg"
    additivity: str = "n/a"         # "additive" | "semi_additive" | "non_additive" | "n/a"
    pit_role: str = "none"          # "as_of"|"effective"|"event"|"maturity"|"valid_time"|"system_time"|"none"
    sensitivity: str = "public"     # "public"|"pii"|"protected_attribute"|"special_category"|"proxy"
    entity_link: str | None = None  # identifiers only: the entity it links (e.g. "customer","account")
    is_a: str | None = None         # parent concept name (is-a edge), else None
    leakage_anchor: bool = False    # True for outcome_label + the target-defining flags (§3.10/§3.7)
    description: str = ""


# Every registry entry, grouped by taxonomy §3 section. Defaults on the dataclass carry the common
# case (additivity "n/a", pit_role "none", sensitivity "public"); only behaviour that differs is set.
_ALL: tuple[Concept, ...] = (
    # ── §3.1 Monetary ────────────────────────────────────────────────────────────────────────────
    Concept("monetary_stock", "monetary", additivity="semi_additive",
            description="Balance / exposure / position / collateral / limit / AUM / receivable / "
                        "payable. Semi-additive: sum across entities, but take the LATEST over time "
                        "— never sum a stock across time."),
    Concept("contingent_exposure", "monetary", additivity="semi_additive", is_a="monetary_stock",
            description="Undrawn facility, LC / guarantee amount, committed line. Off-balance-sheet; "
                        "converts on drawdown via a credit-conversion-factor (see ccf)."),
    Concept("monetary_flow", "monetary", additivity="additive",
            description="Transaction amount, payment, fee, interest paid/earned, drawdown, repayment, "
                        "P&L, revenue. Fully additive across both entities and time."),
    Concept("monetary_rate", "monetary", additivity="non_additive",
            description="Interest rate, coupon, APR, yield, spread. Non-additive — never sum or "
                        "average naively across notionals."),
    Concept("price", "monetary", additivity="non_additive",
            description="Instrument price, strike, NAV. Non-additive."),
    Concept("notional", "monetary", additivity="additive",
            description="Derivative notional. Additive gross; may be netted within a netting set."),

    # ── §3.2 Identifiers → entity links (join key + grain + entity) ───────────────────────────────
    Concept("customer_id", "identifier", entity_link="customer", description="Links to the customer entity."),
    Concept("account_id", "identifier", entity_link="account", description="Links to the account entity."),
    Concept("card_id", "identifier", entity_link="card_account", description="Links to the card_account entity."),
    Concept("transaction_id", "identifier", entity_link="transaction", description="Links to the transaction entity."),
    Concept("application_id", "identifier", entity_link="application", description="Links to the application entity."),
    Concept("product_id", "identifier", entity_link="product", description="Links to the product entity."),
    Concept("facility_id", "identifier", entity_link="facility", description="Links to the facility entity."),
    Concept("instrument_id", "identifier", entity_link="instrument", description="Links to the instrument entity."),
    Concept("counterparty_id", "identifier", entity_link="counterparty", description="Links to the counterparty entity."),
    Concept("merchant_id", "identifier", entity_link="merchant", description="Links to the merchant entity."),
    Concept("lei", "identifier", entity_link="legal_entity",
            description="Legal Entity Identifier — links to the LEI-identified legal_entity."),
    Concept("branch_id", "identifier", entity_link="branch", description="Links to the branch entity."),

    # ── §3.3 Temporal (point-in-time critical) ────────────────────────────────────────────────────
    Concept("as_of_date", "temporal", pit_role="as_of",
            description="Decision reference date — the point features are computed as-of."),
    Concept("effective_date", "temporal", pit_role="effective",
            description="State start date — when a value/state became effective."),
    Concept("origination_date", "temporal", pit_role="event",
            description="When a loan/account/facility was originated (an occurrence)."),
    Concept("maturity_date", "temporal", pit_role="maturity",
            description="Contractual maturity/expiry date."),
    Concept("trade_date", "temporal", pit_role="event", description="Date a trade was struck."),
    Concept("value_date", "temporal", pit_role="effective",
            description="Date value/funds become economically effective (FX/payments)."),
    Concept("settlement_date", "temporal", pit_role="event",
            description="Date a trade/payment settles (an occurrence)."),
    Concept("event_timestamp", "temporal", pit_role="event",
            description="Timestamp an event occurred (dated at occurrence)."),
    Concept("duration_tenure", "temporal", additivity="non_additive",
            description="days_since / account_age / months_on_book. A derived duration — non-additive."),
    Concept("vintage", "temporal",
            description="Cohort label (e.g. origination quarter). Groups facts into vintages; not a date to aggregate."),

    # ── §3.4 Quantities & risk metrics ────────────────────────────────────────────────────────────
    Concept("count", "quantity_risk", additivity="additive",
            description="num_transactions, logins, etc. Fully additive."),
    Concept("quantity_units", "quantity_risk", additivity="additive",
            description="Shares, contracts, units. Additive within one instrument/unit."),
    Concept("score_probability", "quantity_risk", additivity="non_additive",
            description="credit_score, PD, risk_score. Non-additive. LEAKAGE-RISK when it is a model "
                        "output whose target overlaps the feature target — flag before use."),
    Concept("rank_percentile", "quantity_risk", additivity="non_additive",
            description="Percentile / rank. Non-additive."),
    Concept("lgd", "quantity_risk", additivity="non_additive",
            description="Loss given default (a ratio). Non-additive; aggregate exposure-weighted."),
    Concept("ead", "quantity_risk", additivity="additive",
            description="Exposure at default — a monetary amount. Additive across exposures."),
    Concept("ecl", "quantity_risk", additivity="additive",
            description="Expected credit loss (IFRS9) — a monetary amount. Additive across exposures."),
    Concept("var", "quantity_risk", additivity="non_additive",
            description="Value-at-risk. Non-additive (sub-additive with diversification) — never sum across books."),
    Concept("sensitivity_greek", "quantity_risk", additivity="non_additive",
            description="Delta/gamma/vega etc. Non-additive across underlyings (dollar-greeks are "
                        "position-additive only within a single underlying)."),
    Concept("rating", "quantity_risk", additivity="non_additive",
            description="Internal/external credit rating (ordinal). Non-additive."),
    Concept("dpd", "quantity_risk", additivity="non_additive",
            description="Days past due — a delinquency state measure. Non-additive."),
    Concept("beta", "quantity_risk", additivity="non_additive",
            description="Deposit beta (a ratio). Non-additive."),
    Concept("pd", "quantity_risk", additivity="non_additive",
            description="Basel probability of default (generalises pd_ttc/pd_pit). Non-additive. "
                        "LEAKAGE-RISK when a model output — flag before use as a feature."),

    # ── §3.5 Categorical & coded ──────────────────────────────────────────────────────────────────
    Concept("category_code", "categorical", description="Generic coded category."),
    Concept("product_type", "categorical", description="Product classification."),
    Concept("account_type", "categorical", description="Account classification (current/savings/loan/…)."),
    Concept("transaction_type", "categorical", description="Transaction classification."),
    Concept("channel", "categorical", description="Origination/servicing channel (mobile/web/branch/call-center)."),
    Concept("country_code", "categorical", description="ISO country code."),
    Concept("industry_code", "categorical", description="Industry classification (NAICS/SIC)."),
    Concept("mcc", "categorical", description="Merchant category code."),
    Concept("instrument_type", "categorical", description="Instrument classification."),
    Concept("lifecycle_state", "categorical",
            description="Lifecycle state / status (origination→active→delinquent→default→restructured→"
                        "closed/written-off). Features condition on it; transitions are often the target."),

    # ── §3.6 Geographic (fair-lending proxy) ──────────────────────────────────────────────────────
    Concept("geographic", "geographic", sensitivity="proxy",
            description="Zip/postcode/region/branch location. Fair-lending PROXY — treat as a "
                        "protected-attribute proxy; block/flag for credit & pricing."),

    # ── §3.7 Flags (boolean) — some are targets (leakage anchors) ─────────────────────────────────
    Concept("boolean_flag", "flag", description="Generic boolean flag."),
    Concept("delinquency_flag", "flag", leakage_anchor=True,
            description="Delinquency indicator. LEAKAGE ANCHOR — is the target for delinquency models."),
    Concept("default_flag", "flag", leakage_anchor=True,
            description="Default indicator. LEAKAGE ANCHOR — is the target for PD/default models."),
    Concept("fraud_flag", "flag", leakage_anchor=True,
            description="Fraud indicator. LEAKAGE ANCHOR — is the target for fraud models."),
    Concept("restructured_flag", "flag",
            description="Restructure / forbearance indicator (not itself the target here)."),
    Concept("sanctions_hit_flag", "flag", description="Sanctions-screening hit indicator."),
    Concept("pep_flag", "flag", description="Politically-exposed-person indicator."),

    # ── §3.8 Sensitive / regulatory ───────────────────────────────────────────────────────────────
    Concept("pii", "sensitive", sensitivity="pii",
            description="email, phone, ssn, address, name, DOB. Read-scoped."),
    Concept("protected_attribute", "sensitive", sensitivity="protected_attribute",
            description="age, gender, race, ethnicity, marital status, national origin, religion. "
                        "REGULATORY-BLOCKED for credit/pricing (ECOA/fair-lending)."),
    Concept("special_category", "sensitive", sensitivity="special_category",
            description="Health, biometric (GDPR special category). Read-scoped + eligibility-gated."),
    Concept("kyc_document", "sensitive", sensitivity="pii",
            description="KYC identity document — carries PII; read-scoped."),

    # ── §3.9 Text & documents ─────────────────────────────────────────────────────────────────────
    Concept("free_text", "text",
            description="Memo, notes, complaint text. May incidentally carry PII — screen on egress."),
    Concept("document_reference", "text", description="Reference/pointer to a stored document."),
    Concept("unstructured_doc", "text",
            description="Loan/KYC document body. May carry PII/special-category content — screen on egress."),

    # ── §3.10 Labels / outcomes (the leakage anchor) ──────────────────────────────────────────────
    Concept("outcome_label", "label", leakage_anchor=True,
            description="This IS a target: churned/defaulted/charged_off/prepaid/fraud/converted/"
                        "complaint/roll/recovery/mule. THE leakage anchor — features must never be "
                        "built from it or from its defining source columns."),

    # ── §3.11 Behavioural / digital ───────────────────────────────────────────────────────────────
    Concept("event_type", "behavioural", description="Digital event classification."),
    Concept("session", "behavioural", description="Session grouping of digital activity."),
    Concept("clickstream", "behavioural", description="Sequence of page/app interactions."),
    Concept("channel_usage", "behavioural", description="Usage intensity by channel."),
    Concept("device_fingerprint", "behavioural", description="Device identifier/fingerprint (fraud signal)."),
    Concept("geolocation", "behavioural",
            description="Digital geolocation (distinct from geographic §3.6). Can be sensitive — flag."),
    Concept("login_event", "behavioural", description="Login/authentication event."),
    Concept("page_app_event", "behavioural", description="Page-view / app-event."),

    # ── §3.12 Network / graph ─────────────────────────────────────────────────────────────────────
    Concept("relationship_edge", "network",
            description="Counterparty link / beneficial-ownership graph / transaction network / "
                        "shared-device/-account ring. Enables network features (degree, community, "
                        "shortest-path to a flagged node)."),

    # ── §3.13 Bi-temporal time (P0 correctness) ───────────────────────────────────────────────────
    Concept("valid_time", "bitemporal", pit_role="valid_time",
            description="The date a fact is ABOUT (as_of/effective axis)."),
    Concept("system_time", "bitemporal", pit_role="system_time",
            description="The date a fact was RECORDED/known (knowledge/transaction axis). Leakage-safe "
                        "features require both valid_time ≤ as_of AND system_time ≤ as_of — the second "
                        "drops values restated later that you didn't know at prediction time."),
    Concept("booking_date", "temporal", pit_role="system_time",
            description="Date an entry was booked to the ledger (a knowledge/system-time date)."),
    Concept("business_day_convention", "temporal",
            description="Rule for adjusting dates to business days (following/modified-following/…)."),
    Concept("reporting_period", "temporal", pit_role="as_of",
            description="The period a report covers; its end acts as an as-of reference."),

    # ── §3.14 Currency / FX consistency (P0 correctness) ──────────────────────────────────────────
    Concept("currency_code", "currency",
            description="The monetary UNIT. CANNOT mix currencies in a sum — convert to a base "
                        "currency via a point-in-time fx_rate first; mixing USD+EUR is a wrong number."),
    Concept("base_currency", "currency", description="Reporting/base currency all amounts convert to."),
    Concept("local_currency", "currency", description="Native/local currency of the amount."),
    Concept("fx_conversion_rate", "currency", additivity="non_additive",
            description="Point-in-time FX rate used to convert local→base. Non-additive."),
    Concept("cross_rate", "currency", additivity="non_additive",
            description="Currency cross-rate (via a common base). Non-additive."),

    # ── §3.15 Data eligibility (P0 compliance) ────────────────────────────────────────────────────
    Concept("data_purpose", "eligibility", description="Declared purpose the data may be used for."),
    Concept("consent_status", "eligibility", description="Whether consent covers the intended use."),
    Concept("retention_class", "eligibility", description="Retention policy class / max retention window."),
    Concept("data_residency", "eligibility", description="Jurisdiction the data must reside in."),

    # ── §3.16 Regulatory capital & accounting (the spine) ─────────────────────────────────────────
    Concept("risk_weight", "regulatory_capital", additivity="non_additive",
            description="Basel risk weight (%). Non-additive."),
    Concept("rwa", "regulatory_capital", additivity="additive",
            description="Risk-weighted assets — a monetary amount. Additive across exposures."),
    Concept("capital_ratio", "regulatory_capital", additivity="non_additive",
            description="Capital ratio (CET1/Tier-1/total). Is-a ratio — non-additive."),
    Concept("ccf", "regulatory_capital", additivity="non_additive",
            description="Credit-conversion-factor for off-balance-sheet exposure. Non-additive (a factor)."),
    Concept("pd_ttc", "regulatory_capital", additivity="non_additive", is_a="pd",
            description="Through-the-cycle probability of default. Non-additive."),
    Concept("pd_pit", "regulatory_capital", additivity="non_additive", is_a="pd",
            description="Point-in-time probability of default. Non-additive."),
    Concept("downturn_lgd", "regulatory_capital", additivity="non_additive", is_a="lgd",
            description="Downturn loss given default. Non-additive."),
    Concept("fair_value", "accounting", additivity="semi_additive",
            description="Fair-value carrying amount (a valuation stock). Semi-additive (latest over time)."),
    Concept("amortised_cost", "accounting", additivity="semi_additive",
            description="Amortised-cost carrying amount (a balance). Semi-additive (latest over time)."),
    Concept("impairment_stage", "accounting",
            description="IFRS9 stage 1/2/3 (ordinal). Not aggregatable — condition on it."),
    Concept("accrual", "accounting", additivity="additive",
            description="Accrued interest/amount over a period (flow-like). Additive over the period."),
    Concept("provision_amount", "accounting", additivity="additive",
            description="Loan-loss provision — a monetary amount. Additive across exposures."),
    Concept("benchmark_rate", "monetary", additivity="non_additive", is_a="monetary_rate",
            description="Reference rate (SOFR/SONIA/€STR). Non-additive."),
    Concept("tenor", "temporal", additivity="non_additive",
            description="Time-to-maturity / term (a duration). Non-additive."),
    Concept("discount_factor", "accounting", additivity="non_additive",
            description="Present-value discount factor. Non-additive."),
    Concept("haircut", "accounting", additivity="non_additive",
            description="Collateral valuation haircut (%). Non-additive."),
    Concept("advance_rate", "accounting", additivity="non_additive",
            description="Advance rate against collateral (%). Non-additive."),

    # ── §3.17 ESG & compliance flags ──────────────────────────────────────────────────────────────
    Concept("esg_score", "esg", additivity="non_additive", description="ESG rating/score. Non-additive."),
    Concept("carbon_intensity", "esg", additivity="non_additive",
            description="Emissions per unit of activity/revenue. Non-additive (a ratio)."),
    Concept("green_flag", "esg", description="Green/sustainable-finance eligibility flag."),
    Concept("sharia_compliant_flag", "esg", description="Sharia-compliance flag (Islamic banking)."),

    # ── Legacy aliases — the original 11 vocabulary strings retained so live enriched columns and
    #    the current classifier are never orphaned. Superseded names carry a "# legacy alias" note. ──
    # legacy alias — superseded by monetary_stock / monetary_flow (generic; additivity unknown)
    Concept("monetary_amount", "monetary",
            description="Legacy alias — generic monetary amount; superseded by monetary_stock / "
                        "monetary_flow (which carry the correct additivity)."),
    # legacy alias — superseded by account_id
    Concept("account_identifier", "identifier", entity_link="account",
            description="Legacy alias — superseded by account_id."),
    # legacy alias — superseded by customer_id
    Concept("customer_identifier", "identifier", entity_link="customer",
            description="Legacy alias — superseded by customer_id."),
    # legacy alias — superseded by event_timestamp
    Concept("timestamp", "temporal", pit_role="event",
            description="Legacy alias — superseded by event_timestamp."),
    # legacy alias — superseded by monetary_rate / rank_percentile
    Concept("rate_or_ratio", "monetary", additivity="non_additive",
            description="Legacy alias — generic rate/ratio; superseded by monetary_rate / rank_percentile."),
)

# Public registry: name -> full Concept record.
CONCEPT_REGISTRY: dict[str, Concept] = {c.name: c for c in _ALL}

# Backward-compat: the flat set of every known concept name (is_known_concept works on the full set).
CONCEPTS: frozenset[str] = frozenset(CONCEPT_REGISTRY)


def is_known_concept(c: str) -> bool:
    return c in CONCEPTS


def humanize(c: str) -> str:
    return c.replace("_", " ")


def concept(name: str) -> Concept | None:
    """The full behaviour record for a concept name, or None if it isn't in the registry."""
    return CONCEPT_REGISTRY.get(name)


# The 5 legacy aliases are retained so already-enriched data + the pre-B1b classifier are never orphaned,
# but they are NOT classification targets — the classifier should choose the richer §3 concept instead.
_LEGACY_ALIASES: frozenset[str] = frozenset({
    "monetary_amount", "account_identifier", "customer_identifier", "timestamp", "rate_or_ratio",
})


def classification_vocabulary() -> tuple[dict, ...]:
    """The vocabulary the enrichment classifier chooses from — each concept's ``name``, ``group`` and a
    short ``hint`` (first clause of its description), EXCLUDING the legacy aliases. Passed to the LLM
    (B1b) so it classifies into the full structured vocabulary rather than a hardcoded subset; an
    unrecognised answer still falls back to ``unclassified`` at the caller."""
    return tuple(
        {"name": c.name, "group": c.group, "hint": c.description.split(".")[0].strip()[:120]}
        for c in _ALL if c.name not in _LEGACY_ALIASES)
