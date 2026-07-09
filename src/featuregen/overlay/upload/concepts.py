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
    #                                 "eligibility" | "regulatory_capital" | "accounting" | "esg" |
    #                                 "crypto"
    additivity: str = "n/a"         # "additive" | "semi_additive" | "non_additive" | "n/a"
    pit_role: str = "none"          # "as_of"|"effective"|"event"|"maturity"|"valid_time"|"system_time"|"none"
    sensitivity: str = "public"     # "public"|"pii"|"protected_attribute"|"special_category"|"proxy"
    entity_link: str | None = None  # identifiers only: the entity it links (e.g. "customer","account")
    is_a: str | None = None         # parent concept name (is-a edge), else None
    leakage_anchor: bool = False    # True for outcome_label + the target-defining flags (§3.10/§3.7)
    near_label: bool = False        # True for funnel-tail signals that BORDER the label (forbearance,
    #                                 stage-3 impairment, 90+ DPD, CASS switch, filed SAR) — the 3-part
    #                                 leakage control must FLAG these (softer than leakage_anchor).
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
    Concept("notional", "monetary", additivity="semi_additive", is_a="monetary_stock",
            description="Derivative notional — a position attribute. Semi-additive: additive GROSS "
                        "across positions (netted within a netting set), latest over time — never sum a "
                        "notional across snapshots."),

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
    Concept("quantity_units", "quantity_risk", additivity="semi_additive",
            description="Shares, contracts, units — a position quantity. Semi-additive (latest over "
                        "time); NEVER sum across different instruments/units (a unit-mixing error, "
                        "like mixing currencies — needs a unit guard)."),
    Concept("score_probability", "quantity_risk", additivity="non_additive",
            description="credit_score, PD, risk_score. Non-additive. LEAKAGE-RISK when it is a model "
                        "output whose target overlaps the feature target — flag before use."),
    Concept("rank_percentile", "quantity_risk", additivity="non_additive",
            description="Percentile / rank. Non-additive."),
    Concept("lgd", "quantity_risk", additivity="non_additive",
            description="Loss given default (a ratio). Non-additive; aggregate exposure-weighted."),
    Concept("ead", "quantity_risk", additivity="semi_additive", is_a="monetary_stock",
            description="Exposure at default — a monetary STOCK. Semi-additive: sum across exposures, "
                        "but take the latest over time — never sum across reporting dates."),
    Concept("ecl", "quantity_risk", additivity="semi_additive", is_a="monetary_stock",
            description="Expected credit loss (IFRS9) — a provision STOCK. Semi-additive: sum across "
                        "exposures, latest over time — never sum across reporting dates."),
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
    Concept("direct_debit", "categorical",
            description="Direct-debit mandate + its lifecycle events (setup / amend / cancel). Distinct "
                        "from a one-off transaction — cancellation is a Stage-4 churn signal "
                        "(§B1 / PART F dd_cancellation_rate)."),
    Concept("standing_order", "categorical",
            description="Standing-order mandate + events (setup / redirect / cancel). Redirection to an "
                        "external bank is a primacy-loss signal (§A9)."),
    Concept("debit_credit_indicator", "categorical",
            description="Flow DIRECTION on a transaction (debit vs credit / dr-cr / sign). Required by "
                        "every cash-flow feature (inflow_outflow_ratio §A4) — distinct from boolean_flag."),
    Concept("beneficiary_bank", "categorical",
            description="The payee's destination bank / sort-code / scheme, with an internal-vs-EXTERNAL "
                        "flag. Powers the own-money-to-a-competitor primacy signal (§A9)."),
    Concept("channel", "categorical", description="Origination/servicing channel (mobile/web/branch/call-center)."),
    Concept("country_code", "categorical", sensitivity="proxy",
            description="ISO country code. When it encodes nationality/residence it is a national-"
                        "origin PROXY (ECOA/fair-lending) — proxy-flagged; use-case-gate for credit."),
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
    Concept("restructured_flag", "flag", near_label=True,
            description="Restructure / forbearance indicator. NEAR-LABEL: forbearance ≈ the default "
                        "label (§B2 Stage-4) — the 3-part leakage control must flag it."),
    Concept("sanctions_hit_flag", "flag", sensitivity="pii", near_label=True,
            description="Sanctions-screening hit — sensitive (read-scoped, AML-lawful-basis; not fair-"
                        "lending-blocked). NEAR-LABEL: a filed hit ≈ the sanctions-model target."),
    Concept("pep_flag", "flag", sensitivity="pii",
            description="Politically-exposed-person indicator — GDPR-sensitive (political); read-scoped "
                        "and AML-lawful-basis. Tagged pii (usable for AML), NOT special_category-blocked."),

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
    Concept("beneficiary_name", "sensitive", sensitivity="pii", entity_link="beneficiary",
            description="Payee name on a transfer — PII, read-scoped. Name-matched against the customer "
                        "name to DERIVE the own-account flag downstream (§A9 external_own_transfer_trend; "
                        "§D.8 derived intermediate — probabilistic PII entity-resolution)."),

    # ── §3.9 Text & documents ─────────────────────────────────────────────────────────────────────
    Concept("free_text", "text", sensitivity="pii",
            description="Memo, notes, complaint text. Tagged pii: may carry PII — read-scoped + screen "
                        "on egress (a deterministic gate, not just a prose warning)."),
    Concept("document_reference", "text", description="Reference/pointer to a stored document."),
    Concept("unstructured_doc", "text", sensitivity="pii",
            description="Loan/KYC document body. Tagged pii: may carry PII/special-category content — "
                        "read-scoped + screen on egress."),

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
    Concept("device_fingerprint", "behavioural", sensitivity="pii",
            description="Device identifier/fingerprint (fraud signal) — an online identifier = GDPR "
                        "personal data; read-scoped (a deterministic gate, not just a fraud note)."),
    Concept("geolocation", "behavioural", sensitivity="pii",
            description="Digital geolocation (distinct from geographic §3.6) — precise location is "
                        "personal data; read-scoped. (Also a protected-class proxy for credit/pricing.)"),
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
    Concept("rwa", "regulatory_capital", additivity="semi_additive", is_a="monetary_stock",
            description="Risk-weighted assets — a monetary STOCK. Semi-additive: sum across exposures, "
                        "latest over time — never sum monthly RWA snapshots."),
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
    Concept("impairment_stage", "accounting", near_label=True,
            description="IFRS9 stage 1/2/3 (ordinal). Not aggregatable — condition on it. NEAR-LABEL: "
                        "stage 3 (credit-impaired) ≈ the default label — the 3-part leakage control "
                        "must flag it."),
    Concept("accrual", "accounting", additivity="additive",
            description="Accrued interest/amount over a period (flow-like). Additive over the period."),
    Concept("provision_amount", "accounting", additivity="semi_additive", is_a="monetary_stock",
            description="Loan-loss provision — a provision STOCK. Semi-additive: sum across exposures, "
                        "latest over time — never sum across reporting dates."),
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

    # ══════════════════════════════════════════════════════════════════════════════════════════
    # Phase-2 additive expansion — closes the SME gap-review's missing-concept findings (§B) plus
    # the still-missing banking areas. ADDITIVE ONLY: nothing above this line is retagged. Behaviour
    # is set only where it differs from the dataclass defaults; each is_a points at an existing
    # concrete concept (validated at import by _validate_registry).
    # ══════════════════════════════════════════════════════════════════════════════════════════

    # ── Wholesale / markets (gap-review §B) ──────────────────────────────────────────────────────
    Concept("limit", "monetary", additivity="semi_additive",
            description="A credit/exposure CEILING (facility/counterparty/country/sector) — NOT a "
                        "balance, so NOT is_a monetary_stock. Non-fungible and NESTS (sub-limits under a "
                        "master limit): semi-additive at most (latest over time); never naively sum "
                        "nested limits — double-counts. Contrast a drawn balance (§E limit-vs-balance)."),
    Concept("limit_type", "categorical",
            description="Kind of limit (facility / counterparty / country / sector / settlement / "
                        "single-name). Disambiguates a limit's scope."),
    Concept("covenant", "quantity_risk", additivity="non_additive", near_label=True,
            description="Loan covenant threshold / actual / headroom (leverage, DSCR, ICR). Non-additive. "
                        "NEAR-LABEL: a breach borders the default/forbearance label — the leakage control "
                        "must flag headroom/breach features."),
    Concept("collateral_type", "categorical",
            description="Kind of collateral (cash / real-estate / securities / receivables / guarantee). "
                        "Drives haircut + advance_rate."),
    Concept("lien_seniority", "categorical",
            description="Priority of the security interest (first / second lien, senior / subordinated) "
                        "— ordinal; drives recovery/LGD. Loan-level (contrast tranche)."),
    Concept("netting_set_id", "identifier", entity_link="netting_set",
            description="Links to the ISDA netting_set — the grain at which MtM/exposure NETS. Summing "
                        "trade MtMs across netting sets without netting overstates exposure (§D)."),
    Concept("margin", "monetary", additivity="semi_additive", is_a="monetary_stock",
            description="Posted/received margin (initial IM / variation VM) — a collateral STOCK. "
                        "Semi-additive: sum across counterparties, latest over time."),
    Concept("syndication_share", "quantity_risk", additivity="non_additive",
            description="A lender's share (%) of a syndicated facility. Non-additive (a proportion; "
                        "shares sum to 100% within a deal — a constraint, not an aggregation)."),
    Concept("lcr", "regulatory_capital", additivity="non_additive",
            description="Liquidity Coverage Ratio (Basel III) = HQLA / 30-day net outflows. A ratio — "
                        "non-additive."),
    Concept("nsfr", "regulatory_capital", additivity="non_additive",
            description="Net Stable Funding Ratio (Basel III) — a structural funding ratio. Non-additive."),
    Concept("hqla", "regulatory_capital", additivity="semi_additive", is_a="monetary_stock",
            description="High-Quality Liquid Assets — the LCR buffer, a monetary STOCK. Semi-additive: "
                        "sum across the buffer, latest over time (never sum daily HQLA snapshots)."),
    Concept("pv01", "quantity_risk", additivity="non_additive", is_a="sensitivity_greek",
            description="Price value of a basis point (PV01) — interest-rate sensitivity. Non-additive "
                        "across curves/tenors (position-additive only within one risk factor)."),
    Concept("dv01", "quantity_risk", additivity="non_additive", is_a="sensitivity_greek",
            description="Dollar value of a basis point (DV01) — the dollar-sensitivity twin of pv01. "
                        "Non-additive across risk factors."),
    Concept("repricing_gap", "quantity_risk", additivity="non_additive",
            description="IRRBB repricing/maturity gap — net assets less liabilities repricing in a time "
                        "bucket (signed). Non-additive: nets within a snapshot; never sum across dates."),
    Concept("ftp_rate", "monetary", additivity="non_additive", is_a="monetary_rate",
            description="Funds-transfer-pricing rate — the internal cost/credit of funds. Non-additive."),
    Concept("invoice_id", "identifier", entity_link="invoice",
            description="Links to the invoice entity (trade finance / receivables / supply-chain)."),
    Concept("pooling_structure_id", "identifier", entity_link="pooling_structure",
            description="Links to a cash-pooling structure (notional/zero-balancing) — the grain for "
                        "group cash management."),
    Concept("implied_volatility", "quantity_risk", additivity="non_additive",
            description="Option-implied volatility (a market observable / surface point). Non-additive "
                        "across strikes/expiries."),
    Concept("position_direction", "categorical",
            description="Market position DIRECTION (long / short / buy / sell). Required for netting and "
                        "signed exposure — distinct from boolean_flag (cf. debit_credit_indicator)."),
    Concept("expected_exposure", "quantity_risk", additivity="semi_additive", is_a="monetary_stock",
            description="Expected (positive) exposure — EPE, counterparty credit risk. A monetary "
                        "exposure STOCK: sum across netting sets, latest over time."),
    Concept("potential_future_exposure", "quantity_risk", additivity="non_additive",
            description="PFE — a high-quantile future exposure. Non-additive (a quantile; sub-additive "
                        "with diversification — never sum across netting sets, like var)."),
    Concept("expected_shortfall", "quantity_risk", additivity="non_additive",
            description="Expected shortfall / CVaR (FRTB market-risk measure). Non-additive "
                        "(coherent but sub-additive — never sum across books)."),

    # ── Risk & credit (gap-review §B) ────────────────────────────────────────────────────────────
    Concept("macro_variable", "quantity_risk", additivity="non_additive",
            description="Macro-economic driver (GDP, unemployment, HPI, rates) for IFRS9 forward-looking "
                        "ECL / CCAR scenarios. Non-additive (an economic level/rate)."),
    Concept("scenario_id", "identifier", entity_link="scenario",
            description="Links to a macro scenario (base / adverse / severely-adverse) — the grain for "
                        "scenario-conditioned features."),
    Concept("scenario_weight", "quantity_risk", additivity="non_additive",
            description="Probability weight of a scenario (IFRS9 probability-weighting). Non-additive "
                        "(weights sum to 1 across scenarios — a constraint, not an aggregation)."),
    Concept("recovery_amount", "monetary", additivity="additive", is_a="monetary_flow", near_label=True,
            description="Post-default workout recovery cashflow (the LGD numerator). A flow — additive. "
                        "NEAR-LABEL: post-default + the LGD target — leaks default; flag before use."),
    Concept("write_off_amount", "monetary", additivity="additive", is_a="monetary_flow", near_label=True,
            description="Amount charged-off / written-off. A flow — additive. NEAR-LABEL: the charge-off "
                        "IS an outcome (see outcome_label) — features from it leak the label."),
    Concept("cost_to_collect", "monetary", additivity="additive", is_a="monetary_flow",
            description="Collections/workout cost. A flow — additive. Only exists for delinquent/defaulted "
                        "accounts (survivorship + leakage-risk — flag)."),
    Concept("bureau_score", "quantity_risk", additivity="non_additive", is_a="score_probability",
            description="EXTERNAL credit-bureau score (FICO/VantageScore) — FCRA-regulated, its own "
                        "regime; a model output. Non-additive. Distinguish from an internal score (§E)."),
    Concept("bureau_inquiry", "quantity_risk", additivity="additive",
            description="Credit-bureau inquiry event (hard vs soft) — FCRA-regulated external data. "
                        "Count of recent hard inquiries is the feature (additive)."),
    Concept("trade_line", "categorical",
            description="A credit-bureau tradeline — one account's history (limit/balance/status) on the "
                        "file. External / FCRA-regulated reference data."),
    Concept("sicr_flag", "flag", near_label=True,
            description="IFRS9 Significant-Increase-in-Credit-Risk trigger (Stage 1→2). NEAR-LABEL: the "
                        "staging trigger borders the default label — flag."),
    Concept("delinquency_bucket", "quantity_risk", additivity="non_additive", near_label=True,
            description="Ordinal delinquency bucket (current / 1-29 / 30-59 / 60-89 / 90+ DPD). "
                        "Non-additive. NEAR-LABEL: the 90+ bucket is a default backstop — flag."),
    Concept("exposure_class", "categorical",
            description="Basel exposure class / regulatory segment (sovereign / bank / corporate / "
                        "retail / equity). Drives the risk_weight; the standardised/IRB segment."),
    Concept("customer_risk_rating", "quantity_risk", additivity="non_additive",
            description="AML/KYC customer risk rating (low / medium / high) — ordinal. Non-additive. "
                        "Distinct from the credit rating (different lineage)."),
    Concept("expected_loss", "quantity_risk", additivity="semi_additive", is_a="monetary_stock",
            description="Basel expected loss EL = PD×LGD×EAD — a loss-amount STOCK. Semi-additive: sum "
                        "across exposures, latest over time. Distinct from IFRS9 ecl."),
    Concept("lifetime_pd", "quantity_risk", additivity="non_additive", is_a="pd",
            description="IFRS9 lifetime probability of default (Stage 2/3), vs 12-month pd. Non-additive. "
                        "A model output — leakage-risk when its target overlaps the feature target."),
    Concept("effective_maturity", "temporal", additivity="non_additive", is_a="tenor",
            description="Basel effective maturity (M), floored/capped 1–5y — a regulatory duration. "
                        "Non-additive."),
    Concept("npe_flag", "flag", near_label=True,
            description="Non-performing-exposure flag (EBA NPE: 90+ DPD / unlikely-to-pay). NEAR-LABEL: "
                        "NPE overlaps the default definition — flag (a distinct-but-adjacent target)."),
    Concept("watchlist_hit_flag", "flag", near_label=True,
            description="Internal credit watchlist / early-warning hit. NEAR-LABEL: watchlisting borders "
                        "the default/forbearance funnel — flag."),
    Concept("adverse_media_flag", "flag", sensitivity="pii", near_label=True,
            description="Negative-news (adverse-media) screening hit — AML, read-scoped (may carry "
                        "special-category/criminal data). NEAR-LABEL: borders the financial-crime label."),
    Concept("collateral_value", "monetary", additivity="semi_additive", is_a="monetary_stock",
            description="Appraised/market value of collateral — a valuation STOCK. Semi-additive: latest "
                        "over time. haircut/advance_rate apply; distinct from collateral_type."),
    Concept("ownership_percentage", "quantity_risk", additivity="non_additive",
            description="Beneficial/parent ownership stake (%) — the consolidation weight on a group "
                        "edge. Non-additive (a proportion)."),
    Concept("model_tier", "categorical",
            description="Model-risk materiality tier (SR 11-7 / model governance). Governance metadata — "
                        "gates validation rigour; not aggregatable."),

    # ── Specialist · insurance (gap-review §B) ───────────────────────────────────────────────────
    Concept("premium", "monetary", additivity="additive", is_a="monetary_flow",
            description="Insurance premium — a flow. WRITTEN-vs-EARNED trap: do NOT sum WRITTEN and "
                        "EARNED for one period (double-counts); written books at inception, earned "
                        "accrues over cover (UPR bridges them). Cf. takaful_contribution."),
    Concept("claim_reserve", "accounting", additivity="semi_additive", is_a="monetary_stock",
            description="Claims reserve incl. IBNR (incurred-but-not-reported) — an actuarial liability "
                        "STOCK (ESTIMATED). Semi-additive: sum across policies, latest over time."),
    Concept("sum_assured", "monetary", additivity="semi_additive", is_a="monetary_stock",
            description="Sum assured / face amount — the maximum benefit (an exposure ceiling). "
                        "Semi-additive: sum across policies (gross), latest over time."),
    Concept("surrender_value", "monetary", additivity="semi_additive", is_a="monetary_stock",
            description="Cash surrender value of a policy — a policyholder-value STOCK. Semi-additive: "
                        "latest over time. (The surrender EVENT is a near-label — see surrendered.)"),
    Concept("reinsurance_recoverable", "monetary", additivity="semi_additive", is_a="monetary_stock",
            description="Amount recoverable from reinsurers on ceded reserves — a reinsurance-asset "
                        "STOCK (ESTIMATED). Semi-additive: latest over time."),
    Concept("mortality_morbidity", "quantity_risk", additivity="non_additive",
            description="Actuarial mortality/morbidity RATE assumption (from a table). Non-additive. "
                        "(An individual's health STATUS is special_category — this is the rate.)"),

    # ── Specialist · custody & securities services (gap-review §B) ───────────────────────────────
    Concept("nav", "monetary", additivity="non_additive", is_a="price",
            description="Net asset value per unit — a PRICE. Non-additive. (Fund-level total NAV is a "
                        "stock — see monetary_stock.)"),
    Concept("settlement_status", "categorical",
            description="Settlement lifecycle status (pending / settled / failed / partial). Distinct "
                        "from settlement_date; a fail is the settlement_fail outcome."),
    Concept("settlement_cycle", "temporal",
            description="Settlement convention (T+1 / T+2 / T+0). PIT-critical: a fail is not KNOWABLE "
                        "until T+n — features must respect that lag (system_time)."),
    Concept("corporate_action", "categorical",
            description="Corporate-action event (dividend / split / merger / rights). Entitlement is "
                        "fixed at record_date, priced at ex_date, paid at pay_date."),
    Concept("record_date", "temporal", pit_role="effective",
            description="Corporate-action record date — entitlement is FIXED (effective) as-of this date."),
    Concept("ex_date", "temporal", pit_role="as_of",
            description="Ex-dividend/ex-entitlement date — entitlement is read AS-OF here (the price "
                        "drops by the entitlement on this date)."),
    Concept("pay_date", "temporal", pit_role="event",
            description="Corporate-action payment date — the cash/stock pays (an occurrence)."),
    Concept("securities_loan", "monetary", additivity="semi_additive", is_a="monetary_stock",
            description="Securities lending/borrowing (SFT) position — a STOCK. Semi-additive: sum "
                        "across positions, latest over time."),
    Concept("custody_holding", "monetary", additivity="semi_additive", is_a="monetary_stock",
            description="Assets-under-custody holding — a position STOCK. Semi-additive: sum across "
                        "accounts, latest over time."),

    # ── Specialist · asset & wealth management (gap-review §B) ──────────────────────────────────
    Concept("fund", "identifier", entity_link="fund",
            description="Links to the fund entity (the pooled vehicle) — the grain above share_class."),
    Concept("share_class", "identifier", entity_link="share_class",
            description="Links to a fund share-class (fee/currency/accumulation variants of one fund) — "
                        "the sub-fund grain."),
    Concept("fund_flow", "monetary", additivity="additive", is_a="monetary_flow",
            description="Net fund flow = subscriptions − redemptions (net new money). A flow — additive. "
                        "The asset-management attrition/growth signal."),
    Concept("mandate", "categorical",
            description="Investment mandate (IMA) — benchmark + constraints an account is managed to. "
                        "Distinct from a PAYMENT mandate (direct_debit / standing_order)."),
    Concept("benchmark", "categorical",
            description="Performance benchmark INDEX a portfolio is measured against (e.g. S&P 500). "
                        "Distinct from benchmark_rate (a reference INTEREST rate)."),
    Concept("tracking_error", "quantity_risk", additivity="non_additive",
            description="Std-dev of active return vs benchmark (active risk). Non-additive."),
    Concept("expense_ratio", "monetary", additivity="non_additive",
            description="Fund expense ratio (TER / OCF) — annual cost as a % of assets. Non-additive "
                        "(a ratio)."),

    # ── Specialist · Islamic finance (gap-review §B) ─────────────────────────────────────────────
    Concept("profit_rate", "monetary", additivity="non_additive",
            description="Islamic PROFIT rate (Murabaha mark-up / Mudaraba expected profit) — NOT "
                        "interest (riba); do NOT model as a guaranteed conventional rate. Non-additive. "
                        "Deliberately NOT is_a monetary_rate (a Sharia compliance + modelling distinction)."),
    Concept("profit_share_ratio", "monetary", additivity="non_additive",
            description="Mudaraba/Musharaka profit-sharing ratio (PSR) — the pre-agreed profit split, "
                        "not a guaranteed return. Non-additive (a ratio)."),
    Concept("purification_amount", "monetary", additivity="additive", is_a="monetary_flow",
            description="Income-purification amount — non-compliant income donated to charity (Sharia). "
                        "A flow — additive."),
    Concept("prohibited_activity_exposure", "monetary", additivity="semi_additive", is_a="monetary_stock",
            description="Exposure to Sharia-prohibited activities (alcohol/gambling/conventional finance) "
                        "— a screening STOCK; threshold-gated (5%/33% screens). Semi-additive."),
    Concept("sukuk", "categorical", is_a="instrument_type",
            description="Sukuk — a Sharia-compliant asset-backed certificate (NOT a conventional "
                        "interest-bearing bond). An instrument classification."),
    Concept("takaful_contribution", "monetary", additivity="additive", is_a="monetary_flow",
            description="Takaful contribution (tabarru' — a cooperative donation), the Islamic analogue "
                        "of a premium (NOT interest/premium). A flow — additive."),

    # ── Specialist · ESG & climate (gap-review §B) ───────────────────────────────────────────────
    Concept("scope_1_emissions", "esg", additivity="additive",
            description="Direct GHG emissions (tCO2e). Additive within scope; do NOT sum ACROSS scopes "
                        "or the value chain — double-counts (one firm's Scope 1 is another's Scope 3)."),
    Concept("scope_2_emissions", "esg", additivity="additive",
            description="Indirect purchased-energy emissions (tCO2e, location/market-based). Additive "
                        "within scope; never sum across scopes — double-counts."),
    Concept("scope_3_emissions", "esg", additivity="additive",
            description="Value-chain emissions (tCO2e) — 15 categories, ESTIMATED (low data quality). "
                        "Additive within one firm; NOT summable across a portfolio (cross-entity "
                        "double-count). See emissions_data_quality."),
    Concept("financed_emissions", "esg", additivity="additive",
            description="PCAF financed emissions — emissions ATTRIBUTED to loans/investments. Additive "
                        "across the book (attribution avoids double-count); heavily ESTIMATED."),
    Concept("taxonomy_alignment", "esg", additivity="non_additive",
            description="EU-Taxonomy alignment (% of revenue/capex/opex eligible & aligned). "
                        "Non-additive (a ratio)."),
    Concept("emissions_data_quality", "esg", additivity="non_additive",
            description="PCAF data-quality score (1 measured → 5 estimated). Provenance: flags how "
                        "estimated an emissions figure is. Non-additive (ordinal)."),
    Concept("physical_hazard_score", "esg", additivity="non_additive",
            description="Physical climate-risk hazard score (flood/heat/wildfire, location-based, "
                        "scenario-dependent). Non-additive."),
    Concept("transition_alignment", "esg", additivity="non_additive",
            description="Transition / net-zero alignment (implied temperature rise, SBTi). Non-additive."),
    Concept("sll_kpi", "esg", additivity="non_additive",
            description="Sustainability-linked-loan/bond KPI (the SPT the margin ratchet keys off). "
                        "Non-additive."),

    # ── Specialist · payments (gap-review §B) ────────────────────────────────────────────────────
    Concept("payment_rail", "categorical",
            description="Payment rail (FPS / BACS / CHAPS / SEPA / ACH / Fedwire / RTGS / card). Drives "
                        "speed, cost and settlement finality."),
    Concept("scheme", "categorical",
            description="Card/payment SCHEME (Visa / Mastercard / Amex). Distinct from the rail."),
    Concept("interchange", "monetary", additivity="additive", is_a="monetary_flow",
            description="Interchange fee (issuer revenue on a card transaction). A flow — additive."),
    Concept("merchant_discount_rate", "monetary", additivity="non_additive",
            description="Merchant discount rate (MDR) — the acquiring fee % charged to a merchant. "
                        "Non-additive (a rate)."),
    Concept("corridor", "categorical", sensitivity="proxy",
            description="Remittance/payment corridor — the send→receive country pair (cross-border). "
                        "PROXY: correlates with national origin — use-case-gate for credit."),
    Concept("settlement_finality", "categorical",
            description="The irrevocability point of a payment. PIT-critical: real-time (APP-scam) "
                        "scoring must DECIDE BEFORE finality — a batch trailing-window model cannot."),
    Concept("nostro_vostro", "categorical",
            description="Correspondent-account type (nostro = our account abroad / vostro = their "
                        "account here). Reconciliation + liquidity grain."),
    Concept("iso20022_purpose_code", "categorical",
            description="ISO 20022 payment purpose code (SALA / SUPP / …) — structured payment context "
                        "for AML/analytics."),

    # ── Cross-cutting · provenance, metadata & guards (gap-review §B/§E) ──────────────────────────
    Concept("reference_data", "categorical",
            description="Reference / master data (slowly-changing) vs transactional facts — different "
                        "PIT semantics: join AS-OF and watch restatement (system_time), don't event-date it."),
    Concept("model_output", "flag",
            description="Provenance marker: this column is a MODEL OUTPUT (score/PD/ESG derived), not "
                        "observed. Leakage-risk when its target overlaps the feature target; also a "
                        "model-monitoring input."),
    Concept("data_quality_flag", "flag",
            description="Data-quality marker (missing / imputed / stale / reconciliation-break). Gate "
                        "features on it; not a target."),
    Concept("source_system", "categorical",
            description="Provenance: the originating system-of-record. Lineage / reconciliation / "
                        "join disambiguation."),
    Concept("segment", "categorical",
            description="Customer/portfolio segment (mass / affluent / HNW; value/behaviour tiers). "
                        "Audit for proxy leakage if derived from protected attributes."),
    Concept("peer_group", "categorical",
            description="Comparison cohort for benchmarking / outlier features (a peer set). "
                        "Non-aggregatable."),
    Concept("scheduled_amount", "monetary", additivity="additive", is_a="monetary_flow",
            description="Contractual amount DUE (installment / EMI / scheduled repayment). A flow — "
                        "additive. Distinct from actual paid; arrears = scheduled − paid."),
    Concept("unit_of_measure", "categorical",
            description="The non-monetary UNIT (shares / oz / MWh / tonnes / bbl) — the unit-mixing "
                        "guard for quantity_units. Mixing units in a sum is a wrong number (cf. currency_code)."),
    Concept("vulnerability_flag", "sensitive", sensitivity="special_category",
            description="FCA Consumer-Duty vulnerable-customer indicator — highly sensitive (may derive "
                        "from health/capacity): read-scoped + eligibility-gated. MUST support fair "
                        "treatment, never disadvantage."),
    Concept("household_id", "identifier", entity_link="household",
            description="Links to the household entity (relationship/primacy aggregation grain)."),
    Concept("portfolio_id", "identifier", entity_link="portfolio",
            description="Links to the portfolio entity — a markets/AM aggregation grain."),
    Concept("book_id", "identifier", entity_link="book",
            description="Links to the trading book entity — a markets grain (netting/PnL)."),
    Concept("desk_id", "identifier", entity_link="desk",
            description="Links to the trading desk entity — a markets grain."),
    Concept("bureau_provenance", "flag",
            description="Provenance marker: EXTERNAL bureau/third-party data — FCRA-regulated and heavily "
                        "lagged/restated (use system_time to avoid restated-data leakage)."),
    Concept("collateral_id", "identifier", entity_link="collateral",
            description="Links to the collateral entity."),
    Concept("policy_id", "identifier", entity_link="policy",
            description="Links to the insurance policy entity."),
    Concept("claim_id", "identifier", entity_link="claim",
            description="Links to the insurance claim entity."),
    Concept("case_id", "identifier", entity_link="case",
            description="Links to an investigation/case entity (AML/fraud/complaint case management)."),
    Concept("alert_id", "identifier", entity_link="alert",
            description="Links to a monitoring alert entity (transaction-monitoring / screening)."),
    Concept("campaign_id", "identifier", entity_link="campaign",
            description="Links to a marketing campaign entity."),
    Concept("relationship_manager_id", "identifier", entity_link="relationship_manager",
            description="Links to the relationship-manager (banker) entity — book/advisor-attrition grain."),
    Concept("gl_account", "identifier", entity_link="gl_account",
            description="Links to the general-ledger account entity (finance/reconciliation)."),
    Concept("obligor_id", "identifier", entity_link="obligor",
            description="Links to the obligor entity (the party obliged to repay — the credit grain)."),
    Concept("guarantor_id", "identifier", entity_link="guarantor",
            description="Links to the guarantor entity (credit-risk mitigation / support)."),

    # ── Specialist near-labels (§3.10) — outcome states that ARE targets ─────────────────────────
    Concept("lapsed", "label", is_a="outcome_label", leakage_anchor=True,
            description="Policy LAPSE outcome (non-payment) — a target (persistency). LEAKAGE ANCHOR; a "
                        "competing risk vs surrender/death/maturity (right-censored)."),
    Concept("surrendered", "label", is_a="outcome_label", leakage_anchor=True,
            description="Policy SURRENDER outcome (voluntary cash-out) — a target. LEAKAGE ANCHOR; a "
                        "competing risk vs lapse/death/maturity."),
    Concept("settlement_fail", "label", is_a="outcome_label", leakage_anchor=True,
            description="Settlement FAIL outcome (custody) — a target. LEAKAGE ANCHOR; not knowable "
                        "until T+n (settlement_cycle)."),
    Concept("redeemed", "label", is_a="outcome_label", leakage_anchor=True,
            description="Fund REDEMPTION outcome — a target. LEAKAGE ANCHOR; multi-state vs maturity."),

    # ── Still-missing area · regulatory reporting ────────────────────────────────────────────────
    Concept("regulatory_report_line", "categorical",
            description="A line/cell reference in a regulatory return (a template coordinate). Reporting "
                        "lineage; reference data."),
    Concept("anacredit_attribute", "categorical",
            description="An ECB AnaCredit granular loan-level reporting attribute. Reference data."),
    Concept("finrep_corep_line", "categorical",
            description="An EBA FINREP (financial) / COREP (own-funds) template line. Reporting lineage."),
    Concept("mifir_transaction_report", "categorical",
            description="A MiFIR/MiFID II transaction-report field/record (RTS 22, T+1). Reporting event."),
    Concept("emir_report", "categorical",
            description="An EMIR derivative trade-repository report record. Reporting event."),
    Concept("fatca_crs_classification", "categorical", sensitivity="proxy",
            description="FATCA/CRS reportable-person / tax-residency classification. Tax residency is a "
                        "national-origin PROXY — use-case-gate for credit."),

    # ── Still-missing area · open banking & embedded finance ─────────────────────────────────────
    Concept("consent_token", "eligibility", sensitivity="pii",
            description="Open-Banking consent grant/token (PSD2/FDX) — scopes + expiry; the lawful-basis "
                        "anchor for AIS/PIS access. A credential — read-scoped."),
    Concept("tpp_id", "identifier", entity_link="tpp",
            description="Links to the third-party provider (AISP/PISP) entity."),
    Concept("aisp_pisp_flag", "categorical",
            description="Open-Banking access role (AIS account-information vs PIS payment-initiation). "
                        "PSD2 classification."),
    Concept("api_call_event", "behavioural",
            description="An Open-Banking / embedded-finance API-call event — usage telemetry for "
                        "rate/velocity features."),

    # ── Still-missing area · crypto & digital assets (new group 'crypto') ────────────────────────
    Concept("digital_asset", "crypto",
            description="A crypto/digital asset (coin/token). Highly volatile; a position/instrument "
                        "classification."),
    Concept("wallet_address", "crypto", sensitivity="pii", entity_link="wallet",
            description="On-chain wallet address — pseudonymous but linkable (clustering/chain-analysis), "
                        "so treat as personal data; read-scoped. FATF travel-rule relevant."),
    Concept("stablecoin", "crypto",
            description="A fiat-referenced stablecoin (peg + reserve risk). Distinct from cbdc."),
    Concept("on_chain_txn", "crypto",
            description="An on-chain transaction/event — irreversible on block-confirmation finality; "
                        "AML chain-analysis input."),
    Concept("cbdc", "crypto",
            description="Central-bank digital currency (retail/wholesale) — programmable central-bank "
                        "money; distinct from private crypto/stablecoin."),

    # ── Still-missing area · securitization & structured finance ─────────────────────────────────
    Concept("tranche", "categorical",
            description="A securitization tranche (senior / mezzanine / equity) with attach/detach "
                        "points — ordinal loss priority. Structure-level (contrast lien_seniority)."),
    Concept("spv_id", "identifier", entity_link="spv",
            description="Links to the bankruptcy-remote SPV/issuer entity (securitization)."),
    Concept("waterfall_position", "categorical",
            description="Position in the cashflow waterfall (payment priority) — ordinal."),
    Concept("credit_enhancement", "monetary", additivity="semi_additive", is_a="monetary_stock",
            description="Credit enhancement absorbing losses (over-collateralisation / reserve fund / "
                        "subordination) — a STOCK. Semi-additive: latest over time."),

    # ── Still-missing area · pensions & retirement ───────────────────────────────────────────────
    Concept("contribution", "monetary", additivity="additive", is_a="monetary_flow",
            description="Pension/retirement contribution (employer/employee). A flow — additive."),
    Concept("annuity_factor", "quantity_risk", additivity="non_additive",
            description="Annuity conversion factor (pot→income) — actuarial (mortality + rates). "
                        "Non-additive."),
    Concept("vesting", "categorical",
            description="Vesting status/schedule — when benefits become owned. Gates entitlement."),
    Concept("decumulation", "categorical",
            description="Retirement decumulation (drawdown) phase, vs accumulation — sequencing/longevity "
                        "risk differs."),

    # ── Still-missing area · operational risk ────────────────────────────────────────────────────
    Concept("loss_event", "categorical",
            description="A Basel operational-risk loss event (7 L1 event categories) — the loss-data "
                        "collection unit. The op-risk modelling target lives here (leakage-risk)."),
    Concept("loss_amount", "monetary", additivity="additive", is_a="monetary_flow",
            description="Operational-loss amount (gross / net of recovery). A flow — additive. The "
                        "op-risk loss target (leakage-risk)."),
    Concept("risk_control_id", "identifier", entity_link="risk_control",
            description="Links to a risk/control entity (RCSA) — the op-risk taxonomy grain."),
    Concept("near_miss_flag", "flag",
            description="Operational near-miss (control failure, no/immaterial loss) — an early-warning "
                        "signal, not a loss."),

    # ── Still-missing area · tax ─────────────────────────────────────────────────────────────────
    Concept("withholding_amount", "monetary", additivity="additive", is_a="monetary_flow",
            description="Withholding tax deducted at source. A flow — additive; treaty/relief affects "
                        "the rate."),
    Concept("tax_lot", "identifier", entity_link="tax_lot",
            description="Links to a cost-basis tax lot (acquisition date + basis) — the CGT realisation "
                        "grain (FIFO/LIFO/spec-id)."),
    Concept("taxable_flag", "flag",
            description="Taxability indicator (taxable vs exempt / tax-advantaged, e.g. ISA/401k). "
                        "Gates net return."),

    # ── Still-missing area · financial inclusion & alternative data ──────────────────────────────
    Concept("alternative_data", "categorical", sensitivity="proxy",
            description="Non-traditional underwriting data (rent / utility / telco / psychometric) — "
                        "external + PROXY-RISK for protected attributes; use-case-gate for credit."),
    Concept("thin_file_flag", "flag",
            description="Thin-file / credit-invisible indicator — reject-inference + inclusion relevant. "
                        "Not a target."),
    Concept("cashflow_underwriting_signal", "quantity_risk", additivity="non_additive",
            description="Cash-flow-based underwriting signal (income stability / NSF / balance volatility "
                        "from transaction data) — an alt-to-bureau derived signal. Non-additive."),

    # ── Still-missing area · resolution & bank funding ───────────────────────────────────────────
    Concept("tlac_mrel", "regulatory_capital", additivity="semi_additive", is_a="monetary_stock",
            description="TLAC/MREL loss-absorbing capacity (bail-in-able liabilities) — a STOCK "
                        "(also expressed as % of RWA/LRE). Semi-additive: latest over time."),
    Concept("wholesale_funding", "monetary", additivity="semi_additive", is_a="monetary_stock",
            description="Wholesale/market funding balance (vs sticky retail deposits) — a funding STOCK; "
                        "liquidity/run-off risk. Semi-additive: latest over time."),
    Concept("resolution_group", "categorical",
            description="Resolution group / strategy classification (single vs multiple point of entry, "
                        "ring-fencing). Distinct from a customer group."),

    # ── Still-missing area · conduct & complaints ────────────────────────────────────────────────
    Concept("complaint_event", "categorical",
            description="A customer-complaint event (FCA DISP / conduct). Free-text body carries PII "
                        "(see free_text); a conduct + churn signal."),
    Concept("redress_amount", "monetary", additivity="additive", is_a="monetary_flow",
            description="Customer redress/compensation paid (remediation, e.g. PPI). A flow — additive; "
                        "a conduct cost."),
    Concept("root_cause_code", "categorical",
            description="Root-cause taxonomy code for a complaint/incident — thematic conduct analytics."),

    # ── Still-missing area · correspondent banking & SWIFT ───────────────────────────────────────
    Concept("swift_message_type", "categorical",
            description="SWIFT message type (MT103 customer / MT202 bank-to-bank / ISO 20022 MX). "
                        "Payment classification."),
    Concept("nested_correspondent_flag", "flag",
            description="Nested/downstream-correspondent indicator (a bank clearing for another bank's "
                        "clients) — elevated AML risk (visibility gap; FATF/Wolfsberg)."),

    # ── Still-missing area · nature & biodiversity ───────────────────────────────────────────────
    Concept("biodiversity_impact", "esg", additivity="non_additive",
            description="Nature/biodiversity impact-or-dependency (TNFD / SBTN) — ESTIMATED, nascent "
                        "data. Non-additive."),
    Concept("deforestation_flag", "esg",
            description="Deforestation-linked supply-chain flag (EUDR due-diligence). Not a target."),

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


def _validate_registry() -> None:
    """Fail fast at import if the registry drifts: no duplicate names, every ``is_a`` resolves to a
    real concept, and the flat ``CONCEPTS`` set mirrors the registry keys."""
    seen: set[str] = set()
    for c in _ALL:
        if c.name in seen:
            raise ValueError(f"duplicate concept name {c.name!r}")
        seen.add(c.name)
        if c.is_a is not None and c.is_a not in seen and c.is_a not in {x.name for x in _ALL}:
            raise ValueError(f"concept {c.name!r} has unresolved is_a {c.is_a!r}")
    if CONCEPTS != frozenset(CONCEPT_REGISTRY) or len(_ALL) != len(CONCEPT_REGISTRY):
        raise ValueError("CONCEPTS must mirror CONCEPT_REGISTRY keys (no dropped duplicates)")


_validate_registry()


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
