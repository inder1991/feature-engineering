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
    #: Identifier namespace — the ISSUER's value space this identifier draws from (three-axis
    #: model). Two columns are join-candidates iff their concepts share a namespace: equal values
    #: must denote the same thing. `customer_id` and `counterparty_id` BOTH declare "cif" (one
    #: bank's CIF registry); a BIC is "swift_bic" whatever entity role carries it. Identifier
    #: concepts MUST declare one; nothing else may (validated at import). Namespaces are
    #: single-issuer names — issuer-scope them (e.g. "cif@<institution>") the moment a second
    #: institution's catalogs arrive.
    namespace: str | None = None
    is_a: str | None = None         # parent concept name (is-a edge), else None
    leakage_anchor: bool = False    # True for outcome_label + the target-defining flags (§3.10/§3.7)
    near_label: bool = False        # True for funnel-tail signals that BORDER the label (forbearance,
    #                                 stage-3 impairment, 90+ DPD, CASS switch, filed SAR) — the 3-part
    #                                 leakage control must FLAG these (softer than leakage_anchor).
    #: True for THE LABEL THAT STANDS BESIDE A CODE FOR THE SAME THING — a branch NAME beside
    #: `branch_id`, a status DESCRIPTION beside the status code, a merchant's trading name beside
    #: `merchant_id`. Every concept that sets it says so in its own description ("(the label beside
    #: X)"), and each adds the same warning in prose: "never a join key", "the id joins, the name
    #: does not", "conflates what you GROUP BY with what you DISPLAY". This field is that sentence
    #: made readable by the feature USE gate. It follows the `leakage_anchor` precedent exactly — a
    #: behaviour-carrying boolean, not a name pattern.
    #:
    #: DELIBERATELY NOT every name. `party_name`, `beneficiary_name` and `postal_address` are NOT
    #: descriptive, because the registry documents a real computable use for each: a beneficiary
    #: name is the match input of `external_own_transfer_trend` (§A9), and an address "generalises
    #: to a region or distance feature". They are personal data, which is a POLICY question with a
    #: policy answer — not a structural one. Marking them here would tell a reviewer "no approval
    #: can ever help" about a feature an approval is exactly what unblocks.
    #:
    #: AND DELIBERATELY NOT every free-text column. The `text` GROUP was once swept in wholesale;
    #: the sweep never applied the criterion above to its members, and the result told a reviewer
    #: that `payment_narrative` — which the registry's own description calls "the single richest
    #: signal in transaction data … it drives categorisation, merchant identification and AML
    #: screening" — could never be built from, and sent them to "use the CODE column beside it"
    #: when a narration has no code beside it and no approval could have helped. Every `text`
    #: concept is now adjudicated on its OWN description (§3.9), and none of them sets this field:
    #: they are PII-laden computable text, which is the policy class. The rule holds again in both
    #: directions — every concept that sets `descriptive` says so in its own description, and every
    #: concept whose description says so sets it.
    descriptive: bool = False
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
    Concept("interest_income", "monetary", additivity="additive", is_a="monetary_flow",
            description="Interest income recognized in a governed period. The concept identifies the "
                        "economic role but does not assert whether the physical values are signed or "
                        "positive magnitudes."),
    Concept("interest_expense", "monetary", additivity="additive", is_a="monetary_flow",
            description="Interest expense recognized in a governed period. The concept identifies the "
                        "economic role but does not assert whether the physical values are signed or "
                        "positive magnitudes."),
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
    Concept("customer_id", "identifier", namespace="cif", entity_link="customer", description="Links to the customer entity."),
    Concept("account_id", "identifier", namespace="internal_account", entity_link="account",
            description="Links to the account entity via THIS bank's internal account number. "
                        "An external bank's account is external_account_ref; a virtual/shadow "
                        "account is virtual_account_id."),
    Concept("card_id", "identifier", namespace="card", entity_link="card_account", description="Links to the card_account entity."),
    Concept("transaction_id", "identifier", namespace="core_serial", entity_link="transaction",
            description="Links to the transaction entity via the core system's own id. UETR, "
                        "end-to-end, clearing and channel references have their OWN concepts — "
                        "each is a different value space, never this."),
    Concept("application_id", "identifier", namespace="application", entity_link="application", description="Links to the application entity."),
    Concept("product_id", "identifier", namespace="product_catalog", entity_link="product", description="Links to the product entity."),
    Concept("facility_id", "identifier", namespace="facility", entity_link="facility", description="Links to the facility entity."),
    Concept("instrument_id", "identifier", namespace="instrument", entity_link="instrument", description="Links to the instrument entity."),
    Concept("counterparty_id", "identifier", namespace="cif", entity_link="counterparty",
            description="Links to the counterparty entity via the counterparty's CIF at THIS bank "
                        "(same cif namespace as customer_id — a counterparty may be our customer). "
                        "A BIC is bank_bic, not this; a scheme code is clearing_member_code."),
    Concept("merchant_id", "identifier", namespace="merchant_scheme", entity_link="merchant", description="Links to the merchant entity."),
    Concept("lei", "identifier", namespace="lei", entity_link="legal_entity",
            description="Legal Entity Identifier — links to the LEI-identified legal_entity."),
    Concept("branch_id", "identifier", namespace="branch_sol", entity_link="branch",
            description="Links to the branch entity via a branch/SOL CODE. A branch NAME or "
                        "DESCRIPTION is branch_name, never this."),
    # ── Three-axis namespace repair (ingestion-richness Task 1): atomic identifier namespaces.
    # Descriptions state the NEGATIVE deliberately — they are the LLM's routing signal.
    Concept("bank_bic", "identifier", namespace="swift_bic", entity_link="bank",
            description="SWIFT BIC of a BANK (8/11 alphanumeric). Identifies the institution, "
                        "never the counterparty person/company — a counterparty's CIF is "
                        "counterparty_id; the bank's code is this."),
    Concept("clearing_member_code", "identifier", namespace="correspondent_scheme_code",
            entity_link="bank",
            description="A clearing/correspondent scheme member code (national or scheme-local). "
                        "Bank-level, scheme-scoped; not a BIC and not a counterparty. Split into "
                        "per-scheme namespaces the moment a second scheme appears — equal values "
                        "across schemes mean nothing."),
    Concept("swift_uetr", "identifier", namespace="swift_uetr", entity_link="transaction",
            description="SWIFT gpi UETR — a UUID tracing one payment end-to-end. Its own "
                        "namespace; never equal to an internal transaction id."),
    Concept("end_to_end_reference", "identifier", namespace="iso20022_end_to_end",
            entity_link="transaction",
            description="ISO 20022 EndToEndId assigned by the initiating party. Distinct from "
                        "UETR, scheme references and internal serials."),
    Concept("clearing_system_reference", "identifier", namespace="clearing_system_ref",
            entity_link="transaction",
            description="The clearing system's own reference for the instruction."),
    Concept("channel_reference", "identifier", namespace="channel_ref", entity_link="transaction",
            description="A channel-assigned reference (branch/mobile/host). Channel-scoped."),
    Concept("internal_transaction_serial", "identifier", namespace="core_serial",
            entity_link="transaction",
            description="A core-banking internal serial/partition number. Meaningless outside "
                        "its own system."),
    Concept("external_account_ref", "identifier", namespace="external_account",
            entity_link="account",
            description="An account held AT ANOTHER INSTITUTION (e.g. a counterparty's account "
                        "number). Never joinable to internal account_id."),
    Concept("virtual_account_id", "identifier", namespace="virtual_account", entity_link="account",
            description="A virtual/shadow account identifier issued for reconciliation."),
    Concept("party_name", "sensitive", sensitivity="pii",
            description="A person or organisation NAME. Names display and group; they are "
                        "never identifiers and never join keys."),
    Concept("module_id", "categorical",
            is_a="category_code",
            description="A source-system module/product code (which subsystem produced the row). "
                        "System-scoped categorical; not a business category and not a key."),

    # ── §3.3 Temporal (point-in-time critical) ────────────────────────────────────────────────────
    Concept("as_of_date", "temporal", pit_role="as_of",
            is_a="valid_time",
            description="Decision reference date — the point features are computed as-of."),
    Concept("effective_date", "temporal", pit_role="effective",
            is_a="valid_time",
            description="State start date — when a value/state became effective."),
    Concept("origination_date", "temporal", pit_role="event",
            is_a="event_timestamp",
            description="When a loan/account/facility was originated (an occurrence)."),
    Concept("maturity_date", "temporal", pit_role="maturity",
            description="Contractual maturity/expiry date."),
    Concept("trade_date", "temporal", pit_role="event", is_a="event_timestamp",
            description="Date a trade was struck."),
    Concept("value_date", "temporal", pit_role="effective",
            is_a="effective_date",
            description="Date value/funds become economically effective (FX/payments)."),
    Concept("settlement_date", "temporal", pit_role="event",
            is_a="event_timestamp", description="Date a trade/payment settles (an occurrence)."),
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
            is_a="score_probability",
            description="Basel probability of default (generalises pd_ttc/pd_pit). Non-additive. "
                        "LEAKAGE-RISK when a model output — flag before use as a feature."),

    # ── §3.5 Categorical & coded ──────────────────────────────────────────────────────────────────
    Concept("category_code", "categorical", description="Generic coded category."),
    Concept("product_type", "categorical", is_a="category_code",
            description="Product classification."),
    Concept("account_type", "categorical", is_a="category_code",
            description="Account classification (current/savings/loan/…)."),
    Concept("transaction_type", "categorical", is_a="category_code",
            description="Transaction classification."),
    Concept("direct_debit", "categorical",
            description="Direct-debit mandate + its lifecycle events (setup / amend / cancel). Distinct "
                        "from a one-off transaction — cancellation is a Stage-4 churn signal "
                        "(§B1 / PART F dd_cancellation_rate)."),
    Concept("standing_order", "categorical",
            description="Standing-order mandate + events (setup / redirect / cancel). Redirection to an "
                        "external bank is a primacy-loss signal (§A9)."),
    Concept("debit_credit_indicator", "categorical",
            is_a="category_code",
            description="Flow DIRECTION on a transaction (debit vs credit / dr-cr / sign). Required by "
                        "every cash-flow feature (inflow_outflow_ratio §A4) — distinct from boolean_flag."),
    Concept("beneficiary_bank", "categorical",
            description="The payee's destination bank / sort-code / scheme, with an internal-vs-EXTERNAL "
                        "flag. Powers the own-money-to-a-competitor primacy signal (§A9)."),
    Concept("channel", "categorical", is_a="category_code",
            description="Origination/servicing channel (mobile/web/branch/call-center)."),
    Concept("country_code", "categorical", sensitivity="proxy",
            is_a="category_code",
            description="ISO country code. When it encodes nationality/residence it is a national-"
                        "origin PROXY (ECOA/fair-lending) — proxy-flagged; use-case-gate for credit."),
    Concept("industry_code", "categorical", is_a="category_code",
            description="Industry classification (NAICS/SIC)."),
    Concept("mcc", "categorical", is_a="category_code", description="Merchant category code."),
    Concept("instrument_type", "categorical", is_a="category_code",
            description="Instrument classification."),
    Concept("lifecycle_state", "categorical",
            is_a="category_code",
            description="Lifecycle state / status (origination→active→delinquent→default→restructured→"
                        "closed/written-off). Features condition on it; transitions are often the target."),

    # ── §3.6 Geographic (fair-lending proxy) ──────────────────────────────────────────────────────
    Concept("geographic", "geographic", sensitivity="proxy",
            description="Zip/postcode/region/branch location. Fair-lending PROXY — treat as a "
                        "protected-attribute proxy; block/flag for credit & pricing."),

    # ── §3.7 Flags (boolean) — some are targets (leakage anchors) ─────────────────────────────────
    Concept("boolean_flag", "flag", description="Generic boolean flag."),
    Concept("delinquency_flag", "flag", is_a="boolean_flag", leakage_anchor=True,
            description="Delinquency indicator. LEAKAGE ANCHOR — is the target for delinquency models."),
    Concept("default_flag", "flag", is_a="boolean_flag", leakage_anchor=True,
            description="Default indicator. LEAKAGE ANCHOR — is the target for PD/default models."),
    Concept("fraud_flag", "flag", is_a="boolean_flag", leakage_anchor=True,
            description="Fraud indicator. LEAKAGE ANCHOR — is the target for fraud models."),
    Concept("restructured_flag", "flag", is_a="boolean_flag", near_label=True,
            description="Restructure / forbearance indicator. NEAR-LABEL: forbearance ≈ the default "
                        "label (§B2 Stage-4) — the 3-part leakage control must flag it."),
    Concept("sanctions_hit_flag", "flag", sensitivity="pii", is_a="boolean_flag", near_label=True,
            description="Sanctions-screening hit — sensitive (read-scoped, AML-lawful-basis; not fair-"
                        "lending-blocked). NEAR-LABEL: a filed hit ≈ the sanctions-model target."),
    Concept("pep_flag", "flag", sensitivity="pii",
            is_a="boolean_flag",
            description="Politically-exposed-person indicator — GDPR-sensitive (political); read-scoped "
                        "and AML-lawful-basis. Tagged pii (usable for AML), NOT special_category-blocked."),

    # ── §3.8 Sensitive / regulatory ───────────────────────────────────────────────────────────────
    Concept("pii", "sensitive", sensitivity="pii",
            description="LEGACY sensitivity-class catch-all, kept for compatibility. Prefer the "
                        "SEMANTIC concept: a name is party_name, an address is postal_address, a "
                        "phone is phone_number, an email is email_address — each carries the same "
                        "pii sensitivity class, so the read-scope floor is identical. Read-scoped."),
    Concept("protected_attribute", "sensitive", sensitivity="protected_attribute",
            description="age, gender, race, ethnicity, marital status, national origin, religion. "
                        "REGULATORY-BLOCKED for credit/pricing (ECOA/fair-lending)."),
    Concept("special_category", "sensitive", sensitivity="special_category",
            description="Health, biometric (GDPR special category). Read-scoped + eligibility-gated."),
    Concept("kyc_document", "sensitive", sensitivity="pii",
            description="KYC identity document — carries PII; read-scoped."),
    Concept("beneficiary_name", "sensitive", sensitivity="pii", entity_link="beneficiary",
            is_a="party_name",
            description="Payee name on a transfer — PII, read-scoped. Name-matched against the customer "
                        "name to DERIVE the own-account flag downstream (§A9 external_own_transfer_trend; "
                        "§D.8 derived intermediate — probabilistic PII entity-resolution)."),

    # ── §3.9 Text & documents ─────────────────────────────────────────────────────────────────────
    # THE USE-GATE ADJUDICATION FOR THE WHOLE `text` GROUP, recorded once here because the members
    # live in three sections (§3.9, §3.19 record_author, §3.20 payment_narrative + kyc_narrative).
    # `descriptive` means ONE thing — the label that stands beside a CODE for the same thing — and
    # not one of these six is that. A narration has no code column beside it, so the structural
    # refusal ("use the CODE column beside it") names a column that does not exist, and the
    # structural family says "no approval can ever help" about text the registry itself documents a
    # computable use for. Each is therefore adjudicated on its own description:
    #
    #   payment_narrative  POLICY  — "drives categorisation, merchant identification and AML
    #                                 screening"; the richest computable text in the catalog, and
    #                                 pii because it carries names and account numbers.
    #   free_text          POLICY  — memo / complaint text: complaint-driven features are ordinary
    #                                 conduct and churn signals; pii because it may carry PII.
    #   kyc_narrative      POLICY  — high-risk rationale prose; an AML/CDD input under a policy.
    #   unstructured_doc   POLICY  — document bodies; pii, possibly special-category CONTENT, but
    #                                 the content is not a declared special-category ATTRIBUTE.
    #   record_author      POLICY  — a named member of staff; "rarely a legitimate feature" is a
    #                                 judgement about usefulness, not a structural impossibility.
    #   document_reference NEITHER — a pointer with no declared sensitivity and no label-beside-a-
    #                                 code semantics. Counting documents per customer is an ordinary
    #                                 feature, so the gate leaves it alone entirely — the same
    #                                 "absence is not an assertion" rule the whole gate is built on.
    Concept("free_text", "text", sensitivity="pii",
            description="Memo, notes, complaint text. Tagged pii: may carry PII — read-scoped + screen "
                        "on egress (a deterministic gate, not just a prose warning)."),
    Concept("document_reference", "text", description="Reference/pointer to a stored document."),
    Concept("unstructured_doc", "text", sensitivity="pii",
            is_a="free_text",
            description="Loan/KYC document body. Tagged pii: may carry PII/special-category content — "
                        "read-scoped + screen on egress."),

    # ── §3.10 Labels / outcomes (the leakage anchor) ──────────────────────────────────────────────
    Concept("outcome_label", "label", leakage_anchor=True,
            description="This IS a target: churned/defaulted/charged_off/prepaid/fraud/converted/"
                        "complaint/roll/recovery/mule. THE leakage anchor — features must never be "
                        "built from it or from its defining source columns."),

    # ── §3.11 Behavioural / digital ───────────────────────────────────────────────────────────────
    Concept("event_type", "behavioural", is_a="category_code",
            description="Digital event classification."),
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
            is_a="system_time",
            description="Date an entry was booked to the ledger (a knowledge/system-time date)."),
    Concept("business_day_convention", "temporal",
            description="Rule for adjusting dates to business days (following/modified-following/…)."),
    Concept("reporting_period", "temporal", pit_role="as_of",
            description="The period a report covers; its end acts as an as-of reference."),

    # ── §3.14 Currency / FX consistency (P0 correctness) ──────────────────────────────────────────
    Concept("currency_code", "currency",
            description="The monetary UNIT. CANNOT mix currencies in a sum — convert to a base "
                        "currency via a point-in-time fx_rate first; mixing USD+EUR is a wrong number."),
    Concept("base_currency", "currency", is_a="currency_code",
            description="Reporting/base currency all amounts convert to."),
    Concept("local_currency", "currency", is_a="currency_code",
            description="Native/local currency of the amount."),
    Concept("fx_conversion_rate", "currency", additivity="non_additive",
            description="Point-in-time FX rate used to convert local→base. Non-additive."),
    Concept("cross_rate", "currency", additivity="non_additive",
            is_a="fx_conversion_rate",
            description="Currency cross-rate (via a common base). Non-additive."),

    # ── §3.15 Data eligibility (P0 compliance) ────────────────────────────────────────────────────
    Concept("data_purpose", "eligibility", description="Declared purpose the data may be used for."),
    Concept("consent_status", "eligibility", description="Whether consent covers the intended use."),
    Concept("retention_class", "eligibility", is_a="category_code",
            description="Retention policy class / max retention window."),
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
            is_a="monetary_stock",
            description="Fair-value carrying amount (a valuation stock). Semi-additive (latest over time)."),
    Concept("amortised_cost", "accounting", additivity="semi_additive",
            is_a="monetary_stock",
            description="Amortised-cost carrying amount (a balance). Semi-additive (latest over time)."),
    Concept("impairment_stage", "accounting", is_a="category_code", near_label=True,
            description="IFRS9 stage 1/2/3 (ordinal). Not aggregatable — condition on it. NEAR-LABEL: "
                        "stage 3 (credit-impaired) ≈ the default label — the 3-part leakage control "
                        "must flag it."),
    Concept("accrual", "accounting", additivity="additive",
            is_a="monetary_flow",
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
    Concept("green_flag", "esg", is_a="boolean_flag",
            description="Green/sustainable-finance eligibility flag."),
    Concept("sharia_compliant_flag", "esg", is_a="boolean_flag",
            description="Sharia-compliance flag (Islamic banking)."),

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
            is_a="category_code",
            description="Kind of limit (facility / counterparty / country / sector / settlement / "
                        "single-name). Disambiguates a limit's scope."),
    Concept("covenant", "quantity_risk", additivity="non_additive", near_label=True,
            description="Loan covenant threshold / actual / headroom (leverage, DSCR, ICR). Non-additive. "
                        "NEAR-LABEL: a breach borders the default/forbearance label — the leakage control "
                        "must flag headroom/breach features."),
    Concept("collateral_type", "categorical",
            is_a="category_code",
            description="Kind of collateral (cash / real-estate / securities / receivables / guarantee). "
                        "Drives haircut + advance_rate."),
    Concept("lien_seniority", "categorical",
            is_a="category_code",
            description="Priority of the security interest (first / second lien, senior / subordinated) "
                        "— ordinal; drives recovery/LGD. Loan-level (contrast tranche)."),
    Concept("netting_set_id", "identifier", namespace="netting_set", entity_link="netting_set",
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
    Concept("invoice_id", "identifier", namespace="invoice", entity_link="invoice",
            description="Links to the invoice entity (trade finance / receivables / supply-chain)."),
    Concept("pooling_structure_id", "identifier", namespace="pooling_structure", entity_link="pooling_structure",
            description="Links to a cash-pooling structure (notional/zero-balancing) — the grain for "
                        "group cash management."),
    Concept("implied_volatility", "quantity_risk", additivity="non_additive",
            description="Option-implied volatility (a market observable / surface point). Non-additive "
                        "across strikes/expiries."),
    Concept("position_direction", "categorical",
            is_a="category_code",
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
    Concept("scenario_id", "identifier", namespace="scenario", entity_link="scenario",
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
    Concept("sicr_flag", "flag", is_a="boolean_flag", near_label=True,
            description="IFRS9 Significant-Increase-in-Credit-Risk trigger (Stage 1→2). NEAR-LABEL: the "
                        "staging trigger borders the default label — flag."),
    Concept("delinquency_bucket", "quantity_risk", additivity="non_additive", is_a="category_code",
            near_label=True,
            description="Ordinal delinquency bucket (current / 1-29 / 30-59 / 60-89 / 90+ DPD). "
                        "Non-additive. NEAR-LABEL: the 90+ bucket is a default backstop — flag."),
    Concept("exposure_class", "categorical",
            is_a="category_code",
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
    Concept("npe_flag", "flag", is_a="boolean_flag", near_label=True,
            description="Non-performing-exposure flag (EBA NPE: 90+ DPD / unlikely-to-pay). NEAR-LABEL: "
                        "NPE overlaps the default definition — flag (a distinct-but-adjacent target)."),
    Concept("watchlist_hit_flag", "flag", is_a="boolean_flag", near_label=True,
            description="Internal credit watchlist / early-warning hit. NEAR-LABEL: watchlisting borders "
                        "the default/forbearance funnel — flag."),
    Concept("adverse_media_flag", "flag", sensitivity="pii", is_a="boolean_flag", near_label=True,
            description="Negative-news (adverse-media) screening hit — AML, read-scoped (may carry "
                        "special-category/criminal data). NEAR-LABEL: borders the financial-crime label."),
    Concept("collateral_value", "monetary", additivity="semi_additive", is_a="monetary_stock",
            description="Appraised/market value of collateral — a valuation STOCK. Semi-additive: latest "
                        "over time. haircut/advance_rate apply; distinct from collateral_type."),
    Concept("ownership_percentage", "quantity_risk", additivity="non_additive",
            description="Beneficial/parent ownership stake (%) — the consolidation weight on a group "
                        "edge. Non-additive (a proportion)."),
    Concept("model_tier", "categorical",
            is_a="category_code",
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
            is_a="category_code",
            description="Settlement lifecycle status (pending / settled / failed / partial). Distinct "
                        "from settlement_date; a fail is the settlement_fail outcome."),
    Concept("settlement_cycle", "temporal",
            description="Settlement convention (T+1 / T+2 / T+0). PIT-critical: a fail is not KNOWABLE "
                        "until T+n — features must respect that lag (system_time)."),
    Concept("corporate_action", "categorical",
            description="Corporate-action event (dividend / split / merger / rights). Entitlement is "
                        "fixed at record_date, priced at ex_date, paid at pay_date."),
    Concept("record_date", "temporal", pit_role="effective",
            is_a="effective_date",
            description="Corporate-action record date — entitlement is FIXED (effective) as-of this date."),
    Concept("ex_date", "temporal", pit_role="as_of",
            description="Ex-dividend/ex-entitlement date — entitlement is read AS-OF here (the price "
                        "drops by the entitlement on this date)."),
    Concept("pay_date", "temporal", pit_role="event",
            is_a="event_timestamp",
            description="Corporate-action payment date — the cash/stock pays (an occurrence)."),
    Concept("securities_loan", "monetary", additivity="semi_additive", is_a="monetary_stock",
            description="Securities lending/borrowing (SFT) position — a STOCK. Semi-additive: sum "
                        "across positions, latest over time."),
    Concept("custody_holding", "monetary", additivity="semi_additive", is_a="monetary_stock",
            description="Assets-under-custody holding — a position STOCK. Semi-additive: sum across "
                        "accounts, latest over time."),

    # ── Specialist · asset & wealth management (gap-review §B) ──────────────────────────────────
    Concept("fund", "identifier", namespace="fund", entity_link="fund",
            description="Links to the fund entity (the pooled vehicle) — the grain above share_class."),
    Concept("share_class", "identifier", namespace="share_class", entity_link="share_class",
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
            is_a="monetary_rate",
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
            is_a="scope_3_emissions",
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
            is_a="category_code",
            description="Payment rail (FPS / BACS / CHAPS / SEPA / ACH / Fedwire / RTGS / card). Drives "
                        "speed, cost and settlement finality."),
    Concept("scheme", "categorical",
            is_a="category_code",
            description="Card/payment SCHEME (Visa / Mastercard / Amex). Distinct from the rail."),
    Concept("interchange", "monetary", additivity="additive", is_a="monetary_flow",
            description="Interchange fee (issuer revenue on a card transaction). A flow — additive."),
    Concept("merchant_discount_rate", "monetary", additivity="non_additive",
            is_a="monetary_rate",
            description="Merchant discount rate (MDR) — the acquiring fee % charged to a merchant. "
                        "Non-additive (a rate)."),
    Concept("corridor", "categorical", sensitivity="proxy",
            description="Remittance/payment corridor — the send→receive country pair (cross-border). "
                        "PROXY: correlates with national origin — use-case-gate for credit."),
    Concept("settlement_finality", "categorical",
            description="The irrevocability point of a payment. PIT-critical: real-time (APP-scam) "
                        "scoring must DECIDE BEFORE finality — a batch trailing-window model cannot."),
    Concept("nostro_vostro", "categorical",
            is_a="category_code",
            description="Correspondent-account type (nostro = our account abroad / vostro = their "
                        "account here). Reconciliation + liquidity grain."),
    Concept("iso20022_purpose_code", "categorical",
            is_a="category_code",
            description="ISO 20022 payment purpose code (SALA / SUPP / …) — structured payment context "
                        "for AML/analytics."),

    # ── Cross-cutting · provenance, metadata & guards (gap-review §B/§E) ──────────────────────────
    Concept("reference_data", "categorical",
            description="Reference / master data (slowly-changing) vs transactional facts — different "
                        "PIT semantics: join AS-OF and watch restatement (system_time), don't event-date it."),
    Concept("model_output", "flag",
            is_a="boolean_flag",
            description="Provenance marker: this column is a MODEL OUTPUT (score/PD/ESG derived), not "
                        "observed. Leakage-risk when its target overlaps the feature target; also a "
                        "model-monitoring input."),
    Concept("data_quality_flag", "flag",
            is_a="category_code",
            description="Data-quality marker (missing / imputed / stale / reconciliation-break). Gate "
                        "features on it; not a target."),
    Concept("source_system", "categorical",
            description="Provenance: the originating system-of-record. Lineage / reconciliation / "
                        "join disambiguation."),
    Concept("segment", "categorical",
            is_a="category_code",
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
            is_a="boolean_flag",
            description="FCA Consumer-Duty vulnerable-customer indicator — highly sensitive (may derive "
                        "from health/capacity): read-scoped + eligibility-gated. MUST support fair "
                        "treatment, never disadvantage."),
    Concept("household_id", "identifier", namespace="household", entity_link="household",
            description="Links to the household entity (relationship/primacy aggregation grain)."),
    Concept("portfolio_id", "identifier", namespace="portfolio", entity_link="portfolio",
            description="Links to the portfolio entity — a markets/AM aggregation grain."),
    Concept("book_id", "identifier", namespace="book", entity_link="book",
            description="Links to the trading book entity — a markets grain (netting/PnL)."),
    Concept("desk_id", "identifier", namespace="desk", entity_link="desk",
            description="Links to the trading desk entity — a markets grain."),
    Concept("bureau_provenance", "flag",
            is_a="boolean_flag",
            description="Provenance marker: EXTERNAL bureau/third-party data — FCRA-regulated and heavily "
                        "lagged/restated (use system_time to avoid restated-data leakage)."),
    Concept("collateral_id", "identifier", namespace="collateral", entity_link="collateral",
            description="Links to the collateral entity."),
    Concept("policy_id", "identifier", namespace="policy", entity_link="policy",
            description="Links to the insurance policy entity."),
    Concept("claim_id", "identifier", namespace="claim", entity_link="claim",
            description="Links to the insurance claim entity."),
    Concept("case_id", "identifier", namespace="case", entity_link="case",
            description="Links to an investigation/case entity (AML/fraud/complaint case management)."),
    Concept("alert_id", "identifier", namespace="alert", entity_link="alert",
            description="Links to a monitoring alert entity (transaction-monitoring / screening)."),
    Concept("campaign_id", "identifier", namespace="campaign", entity_link="campaign",
            description="Links to a marketing campaign entity."),
    Concept("relationship_manager_id", "identifier", namespace="rm", entity_link="relationship_manager",
            description="Links to the relationship-manager (banker) entity — book/advisor-attrition grain."),
    Concept("gl_account", "identifier", namespace="gl_account", entity_link="gl_account",
            description="Links to the general-ledger account entity (finance/reconciliation)."),
    Concept("obligor_id", "identifier", namespace="obligor", entity_link="obligor",
            description="Links to the obligor entity (the party obliged to repay — the credit grain)."),
    Concept("guarantor_id", "identifier", namespace="guarantor", entity_link="guarantor",
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
            is_a="regulatory_report_line",
            description="An EBA FINREP (financial) / COREP (own-funds) template line. Reporting lineage."),
    Concept("mifir_transaction_report", "categorical",
            description="A MiFIR/MiFID II transaction-report field/record (RTS 22, T+1). Reporting event."),
    Concept("emir_report", "categorical",
            description="An EMIR derivative trade-repository report record. Reporting event."),
    Concept("fatca_crs_classification", "categorical", sensitivity="proxy",
            is_a="category_code",
            description="FATCA/CRS reportable-person / tax-residency classification. Tax residency is a "
                        "national-origin PROXY — use-case-gate for credit."),

    # ── Still-missing area · open banking & embedded finance ─────────────────────────────────────
    Concept("consent_token", "eligibility", sensitivity="pii",
            description="Open-Banking consent grant/token (PSD2/FDX) — scopes + expiry; the lawful-basis "
                        "anchor for AIS/PIS access. A credential — read-scoped."),
    Concept("tpp_id", "identifier", namespace="tpp", entity_link="tpp",
            description="Links to the third-party provider (AISP/PISP) entity."),
    Concept("aisp_pisp_flag", "categorical",
            is_a="category_code",
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
            is_a="digital_asset",
            description="A fiat-referenced stablecoin (peg + reserve risk). Distinct from cbdc."),
    Concept("on_chain_txn", "crypto",
            description="An on-chain transaction/event — irreversible on block-confirmation finality; "
                        "AML chain-analysis input."),
    Concept("cbdc", "crypto",
            is_a="digital_asset",
            description="Central-bank digital currency (retail/wholesale) — programmable central-bank "
                        "money; distinct from private crypto/stablecoin."),

    # ── Still-missing area · securitization & structured finance ─────────────────────────────────
    Concept("tranche", "categorical",
            is_a="category_code",
            description="A securitization tranche (senior / mezzanine / equity) with attach/detach "
                        "points — ordinal loss priority. Structure-level (contrast lien_seniority)."),
    Concept("spv_id", "identifier", namespace="spv", entity_link="spv",
            description="Links to the bankruptcy-remote SPV/issuer entity (securitization)."),
    Concept("waterfall_position", "categorical",
            is_a="category_code",
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
            is_a="category_code",
            description="Vesting status/schedule — when benefits become owned. Gates entitlement."),
    Concept("decumulation", "categorical",
            is_a="category_code",
            description="Retirement decumulation (drawdown) phase, vs accumulation — sequencing/longevity "
                        "risk differs."),

    # ── Still-missing area · operational risk ────────────────────────────────────────────────────
    Concept("loss_event", "categorical",
            description="A Basel operational-risk loss event (7 L1 event categories) — the loss-data "
                        "collection unit. The op-risk modelling target lives here (leakage-risk)."),
    Concept("loss_amount", "monetary", additivity="additive", is_a="monetary_flow",
            description="Operational-loss amount (gross / net of recovery). A flow — additive. The "
                        "op-risk loss target (leakage-risk)."),
    Concept("risk_control_id", "identifier", namespace="risk_control", entity_link="risk_control",
            description="Links to a risk/control entity (RCSA) — the op-risk taxonomy grain."),
    Concept("near_miss_flag", "flag",
            is_a="boolean_flag",
            description="Operational near-miss (control failure, no/immaterial loss) — an early-warning "
                        "signal, not a loss."),

    # ── Still-missing area · tax ─────────────────────────────────────────────────────────────────
    Concept("withholding_amount", "monetary", additivity="additive", is_a="monetary_flow",
            description="Withholding tax deducted at source. A flow — additive; treaty/relief affects "
                        "the rate."),
    Concept("tax_lot", "identifier", namespace="tax_lot", entity_link="tax_lot",
            description="Links to a cost-basis tax lot (acquisition date + basis) — the CGT realisation "
                        "grain (FIFO/LIFO/spec-id)."),
    Concept("taxable_flag", "flag",
            is_a="boolean_flag",
            description="Taxability indicator (taxable vs exempt / tax-advantaged, e.g. ISA/401k). "
                        "Gates net return."),

    # ── Still-missing area · financial inclusion & alternative data ──────────────────────────────
    Concept("alternative_data", "categorical", sensitivity="proxy",
            description="Non-traditional underwriting data (rent / utility / telco / psychometric) — "
                        "external + PROXY-RISK for protected attributes; use-case-gate for credit."),
    Concept("thin_file_flag", "flag",
            is_a="boolean_flag",
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
            is_a="category_code",
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
            is_a="category_code",
            description="Root-cause taxonomy code for a complaint/incident — thematic conduct analytics."),

    # ── Still-missing area · correspondent banking & SWIFT ───────────────────────────────────────
    Concept("swift_message_type", "categorical",
            is_a="category_code",
            description="SWIFT message type (MT103 customer / MT202 bank-to-bank / ISO 20022 MX). "
                        "Payment classification."),
    Concept("nested_correspondent_flag", "flag",
            is_a="boolean_flag",
            description="Nested/downstream-correspondent indicator (a bank clearing for another bank's "
                        "clients) — elevated AML risk (visibility gap; FATF/Wolfsberg)."),

    # ── Still-missing area · nature & biodiversity ───────────────────────────────────────────────
    Concept("biodiversity_impact", "esg", additivity="non_additive",
            description="Nature/biodiversity impact-or-dependency (TNFD / SBTN) — ESTIMATED, nascent "
                        "data. Non-additive."),
    Concept("deforestation_flag", "esg",
            is_a="boolean_flag",
            description="Deforestation-linked supply-chain flag (EUDR due-diligence). Not a target."),

    # ── Legacy aliases — the original 11 vocabulary strings retained so live enriched columns and
    #    the current classifier are never orphaned. Superseded names carry a "# legacy alias" note. ──
    # legacy alias — superseded by monetary_stock / monetary_flow (generic; additivity unknown)
    Concept("monetary_amount", "monetary",
            description="Legacy alias — generic monetary amount; superseded by monetary_stock / "
                        "monetary_flow (which carry the correct additivity)."),
    # legacy alias — superseded by account_id
    Concept("account_identifier", "identifier", namespace="internal_account", entity_link="account",
            description="Legacy alias — superseded by account_id."),
    # legacy alias — superseded by customer_id
    Concept("customer_identifier", "identifier", namespace="cif", entity_link="customer",
            description="Legacy alias — superseded by customer_id."),
    # legacy alias — superseded by event_timestamp
    Concept("timestamp", "temporal", pit_role="event",
            description="Legacy alias — superseded by event_timestamp."),
    # legacy alias — superseded by monetary_rate / rank_percentile
    Concept("rate_or_ratio", "monetary", additivity="non_additive",
            description="Legacy alias — generic rate/ratio; superseded by monetary_rate / rank_percentile."),

    # ── §3.18 Customer master — the party record itself ───────────────────────────────────────────
    # This section exists because a real customer-master table had nowhere to land: the registry was
    # built for credit risk and payments, so 12 columns collapsed to `boolean_flag` and 13 to
    # `category_code` — the only honest answers available, which is precisely the problem. Every
    # concept below is grounded in a column present in a loaded catalog, never invented from a
    # taxonomy. NONE is additive: these are states, flags and identifiers, not measures to sum.
    Concept("new_to_bank_flag", "flag",
            is_a="boolean_flag",
            description="Whether the party is within the bank's new-to-bank (NTB) window. Usually "
                        "arrives as a PAIR at different offsets (current vs 9 months prior); the pair "
                        "is what yields tenure movement and first-year cohort behaviour, so the "
                        "offset belongs in the column's own definition, not in this concept."),
    Concept("customer_relationship_status", "categorical",
            is_a="category_code",
            description="Lifecycle state of the bank's relationship with the party — active, dormant, "
                        "closed, special handling. The relationship's own state, distinct from any "
                        "single source system's record status (see source_system_status)."),
    Concept("source_system_status", "categorical",
            is_a="category_code",
            description="A party's record status WITHIN one originating system (Finacle, Calypso, "
                        "FinOne, Advent, VisionPlus). Operational plumbing, not a business state: it "
                        "says whether that system holds the party, never how the bank regards them."),
    Concept("staff_indicator", "flag", sensitivity="pii",
            is_a="boolean_flag",
            description="Whether the party is an employee of the bank. A POPULATION control as much "
                        "as a feature — staff accounts carry preferential pricing and insider "
                        "controls, so they are routinely excluded from behavioural models. Personal "
                        "employment data about an identifiable person, hence the pii floor."),
    Concept("legal_entity_type", "categorical",
            is_a="category_code",
            description="What KIND of party this is — individual, sole proprietor, corporate, joint, "
                        "trust. Constitution / legal structure. Drives which features are even "
                        "meaningful, since an individual and a corporate share few attributes."),
    Concept("residency_status", "categorical",
            is_a="category_code",
            description="The party's residency for regulatory and tax purposes — resident, "
                        "non-resident, free-zone. A jurisdictional eligibility fact, not a location."),
    Concept("restriction_status", "flag", is_a="boolean_flag", near_label=True,
            description="Whether the party is under a servicing restriction — suspended, negated, "
                        "blacklisted, watch-listed. near_label: these are AML/fraud CONSEQUENCES, so "
                        "a financial-crime model trained on them reads its own answer back. Not a "
                        "hard leakage anchor — they are legitimate as controls and as filters."),
    Concept("restriction_reason", "categorical", is_a="category_code", near_label=True,
            description="WHY a party is restricted — the suspension / negation / blacklist reason "
                        "code or note. Borders an outcome for the same reason restriction_status "
                        "does, and is more specific, so it leaks more readily."),
    Concept("nominee_indicator", "flag",
            is_a="boolean_flag",
            description="Whether the party holds in a nominee capacity — the named party is not the "
                        "beneficial owner. Material to AML beneficial-ownership treatment and to "
                        "whether party-level behaviour can be attributed to a real person."),
    Concept("customer_group_id", "identifier", namespace="customer_group", entity_link="customer_group",
            description="Identifier of the corporate GROUP a party belongs to (parent group / "
                        "conglomerate). The join key for group-level exposure and concentration."),
    Concept("parent_customer_id", "identifier", namespace="cif", entity_link="customer",
            is_a="customer_id",
            description="Reference to another PARTY that is this one's parent — a self-referencing "
                        "hierarchy. Shares the `customer` entity link with customer_id deliberately: "
                        "a different entity would make the hierarchy unbridgeable across catalogs."),
    Concept("record_deleted_flag", "flag",
            is_a="boolean_flag",
            description="Soft-delete marker on the record. A POPULATION filter, never a predictor — "
                        "a model that treats it as an ordinary feature trains on rows the bank "
                        "considers deleted, and must exclude them instead."),
    Concept("record_author", "text", sensitivity="pii",
            description="The user who created or last updated the record (create_user / update_user). "
                        "A named member of staff, so personal data; audit lineage rather than "
                        "customer behaviour, and rarely a legitimate feature."),
    # ── §3.20 Payments — narrative, party roles, contact details ──────────────────────────────────
    # Grounded in what FTR's 126 transaction columns landed on: four narrative columns on
    # `free_text`, six personal/party columns on a bare `pii`, and a row hash on `unclassified`.
    #
    # `pii` is a CLASSIFICATION, not a concept. Collapsing an address, a phone number and an ISO
    # 20022 party role into one word loses the handling difference — a phone can be tokenised, an
    # address generalised to a region feature, a party role is a structural field of the message.
    Concept("payment_narrative", "text", sensitivity="pii",
            is_a="free_text",
            description="Free-text remittance information on a payment (narration, "
                        "sender-to-receiver info, inter-bank information). The single richest signal "
                        "in transaction data — it drives categorisation, merchant identification and "
                        "AML screening — and it routinely CONTAINS names and account numbers, hence "
                        "the pii floor."),
    Concept("initiating_party", "categorical", sensitivity="pii",
            description="ISO 20022 InitgPty — the party that initiated the payment instruction, "
                        "which is not necessarily the party being debited. Distinct from the "
                        "ultimate debtor: who pushed the button versus whose money it is."),
    Concept("ultimate_debtor", "categorical", sensitivity="pii",
            description="ISO 20022 UltmtDbtr — the party the funds are ULTIMATELY from, behind any "
                        "intermediary. The question a sanctions/AML screen actually asks, and the "
                        "one a bare `pii` label erases."),
    Concept("ultimate_creditor", "categorical", sensitivity="pii",
            description="ISO 20022 UltmtCdtr — the party the funds are ULTIMATELY for, behind any "
                        "intermediary or collection agent. The receiving mirror of ultimate_debtor."),
    Concept("postal_address", "sensitive", sensitivity="pii",
            description="A physical or correspondence address. Kept distinct from the generic `pii` "
                        "because the handling differs: an address generalises to a region or "
                        "distance feature, where a raw identifier cannot be used at all."),
    Concept("phone_number", "sensitive", sensitivity="pii",
            description="A telephone or mobile number. A SHARED phone across parties is a fraud and "
                        "AML signal, never a join key — linking catalogs on it would silently merge "
                        "unrelated parties."),
    Concept("email_address", "sensitive", sensitivity="pii",
            description="An email address. Like a phone number, a shared value is a linkage SIGNAL "
                        "to be modelled, not an identifier to join on."),
    Concept("row_hash", "categorical",
            description="A surrogate or dedupe hash over a row's contents — a technical key, not a "
                        "business entity. Deliberately NOT an identifier: a hash column that "
                        "bridged would pair with every other hash column in every catalog."),
    Concept("statement_visibility_flag", "flag",
            is_a="boolean_flag",
            description="Whether a transaction is shown on, or suppressed from, the customer "
                        "statement. Presentation rather than economics — a suppressed entry still "
                        "moved money, so it must not be mistaken for a reversal or exclusion."),

    # ── §3.19 Labels — the readable side of a code ────────────────────────────────────────────────
    # A NAME is not an IDENTIFIER. `cust_prim_branch_nm` was classified `branch_id` because
    # `branch_id` was the only branch word in the registry, and `derive_bridge_candidates` pairs any
    # two columns sharing an identifier concept — so six columns holding `branch_id` produced eight
    # cross-catalog "links", none of them a real join (`cust_prim_branch_nm <-> sol_desc` pairs a
    # name with a description). A mislabelled flag misdescribes a column; a mislabelled identifier
    # MANUFACTURES JOINS.
    #
    # Every concept here is `categorical` with NO entity_link — the two conditions the bridge
    # derivation requires — so a label can never be proposed as a join key. The paired identifier is
    # untouched and still links catalogs; only the name stops pretending to.
    Concept("branch_name", "categorical", is_a="code_label", descriptive=True,
            description="Human-readable name of a branch (the label beside branch_id). Groups and "
                        "displays; never a join key — two catalogs' branch names are text that may "
                        "coincide, not a shared identifier."),
    Concept("relationship_manager_name", "categorical", sensitivity="pii", is_a="code_label",
            descriptive=True,
            description="Name of the relationship manager (the label beside "
                        "relationship_manager_id). An identifiable employee, so it carries a pii "
                        "floor for the same reason record_author does."),
    Concept("merchant_name", "categorical", is_a="code_label", descriptive=True,
            description="Trading name of a merchant (the label beside merchant_id). Notoriously "
                        "inconsistent across acquirers — the id joins, the name does not."),
    Concept("account_name", "categorical", is_a="code_label", descriptive=True,
            description="Display name or title of an account (the label beside account_id). Often "
                        "carries the holder's name, so treat as free text rather than a key."),
    Concept("instrument_name", "categorical", is_a="code_label", descriptive=True,
            description="Readable name of a financial instrument (the label beside instrument_id). "
                        "The ISIN/CUSIP identifies it; the name only describes it."),
    Concept("counterparty_name", "categorical", is_a="code_label", descriptive=True,
            description="Name of the counterparty to a transaction (the label beside "
                        "counterparty_id). Distinct from beneficiary_name, which names the party a "
                        "payment is FOR rather than the party it is WITH."),
    Concept("code_label", "categorical", descriptive=True,
            description="The readable description of a coded value — a sector description beside a "
                        "sector code, a reason description beside a reason code. Landing these on "
                        "the CODE's own concept conflates what you GROUP BY with what you DISPLAY, "
                        "and doubles every code into two apparently-equal columns."),

    Concept("kyc_narrative", "text", sensitivity="pii",
            is_a="free_text",
            description="Free-prose KYC commentary — nature of business, corporate background, "
                        "high-risk rationale. Uploader-authored text about an identifiable party, so "
                        "it carries a pii floor and is read-scoped rather than freely searchable."),
)

# Public registry: name -> full Concept record.
CONCEPT_REGISTRY: dict[str, Concept] = {c.name: c for c in _ALL}

# Backward-compat: the flat set of every known concept name (is_known_concept works on the full set).
CONCEPTS: frozenset[str] = frozenset(CONCEPT_REGISTRY)


def _validate_registry(records: tuple[Concept, ...] = _ALL) -> None:
    """Fail fast at import if the registry drifts: no duplicate names, every ``is_a`` resolves to a
    real concept, the ``is_a`` graph is ACYCLIC, and the flat ``CONCEPTS`` set mirrors the registry
    keys. ``records`` defaults to the live registry; tests validate synthetic tuples."""
    by_name: dict[str, Concept] = {}
    for c in records:
        if c.name in by_name:
            raise ValueError(f"duplicate concept name {c.name!r}")
        by_name[c.name] = c
        # Three-axis model: namespace <=> identifier. An identifier without a value space cannot
        # participate in join candidacy; a namespace on a non-identifier is a modelling bug.
        if c.group == "identifier" and not c.namespace:
            raise ValueError(f"identifier concept {c.name!r} declares no namespace")
        if c.group != "identifier" and c.namespace is not None:
            raise ValueError(f"non-identifier concept {c.name!r} declares namespace {c.namespace!r}")
        # `descriptive` and `identifier` are contradictory claims about the SAME column: one says
        # "this is prose that displays", the other "this is a value space you may join on". A
        # concept asserting both would make the USE gate refuse a legitimate join key.
        if c.descriptive and c.group == "identifier":
            raise ValueError(f"concept {c.name!r} is both descriptive and an identifier")
    for c in records:
        if c.is_a is not None and c.is_a not in by_name:
            raise ValueError(f"concept {c.name!r} has unresolved is_a {c.is_a!r}")
    # Every is_a chain must terminate: the old check added each name to `seen` BEFORE testing it,
    # so a self-loop (is_a = own name) and mutual loops validated — and a chain-walking consumer
    # (concept_path, taxonomy derivation) would spin forever.
    for c in records:
        chain = [c.name]
        cur = c.is_a
        while cur is not None:
            if cur in chain:
                raise ValueError(
                    f"concept {c.name!r} has an is_a cycle: {' -> '.join([*chain, cur])}")
            chain.append(cur)
            cur = by_name[cur].is_a
    if records is _ALL and (
            CONCEPTS != frozenset(CONCEPT_REGISTRY) or len(_ALL) != len(CONCEPT_REGISTRY)):
        raise ValueError("CONCEPTS must mirror CONCEPT_REGISTRY keys (no dropped duplicates)")


_validate_registry()


def is_known_concept(c: str) -> bool:
    return c in CONCEPTS


def humanize(c: str) -> str:
    return c.replace("_", " ")


def concept(name: str) -> Concept | None:
    """The full behaviour record for a concept name, or None if it isn't in the registry."""
    return CONCEPT_REGISTRY.get(name)


# ── the USE-class predicates (Bar-4 feature use gate) ────────────────────────────────────────────
#
# WHAT THESE ARE FOR, and what they deliberately are NOT. `sensitivity` has always answered "who may
# SEE this column" (read_scope). It never answered "may a feature be BUILT from it", and the
# Release-A evaluation measured the consequence: a visible PII column, a visible protected
# characteristic, a visible currency-blind amount and a visible free-text label all landed as
# DESIGN_CHECKED with zero requirements. The predicates below are the registry half of the USE
# answer — the behaviour each concept already declares, read as a question about USE.
#
# They are REGISTRY predicates over a concept NAME, never over a column name: `sol_desc` is refused
# because its concept is `branch_name` and `branch_name.descriptive` is True, not because the string
# ends in "_desc". A column with no concept answers False everywhere and is untouched.

#: THERE IS NO GROUP SWEEP, and the absence is the fix. `DESCRIPTIVE_GROUPS = {"text"}` used to
#: pull six concepts into the structural class without ever asking them the question `descriptive`
#: asks — see the §3.9 adjudication block for the per-concept answers. A group is a taxonomy
#: bucket; `descriptive` is a claim about a specific column's semantics, and only a concept can
#: make that claim about itself.

#: The sensitivity classes a POLICY can never license as a model input. ECOA/fair-lending
#: (`protected_attribute`) and GDPR Article 9 (`special_category`) do not have an "allow" switch —
#: refusing these is a statement about the column, not about a missing setting.
PROTECTED_SENSITIVITIES: frozenset[str] = frozenset({"protected_attribute", "special_category"})

#: The sensitivity class a lawful-basis policy COULD license (AML use of a pep_flag is the standing
#: example). No such policy surface exists yet, so today this refuses — with wording that names the
#: missing policy rather than the column.
PERSONAL_DATA_SENSITIVITY = "pii"

#: The concept group whose values are denominated in a currency, and the group that carries the
#: denomination itself.
MONETARY_GROUP = "monetary"
CURRENCY_GROUP = "currency"


def is_descriptive(name: str | None) -> bool:
    """Is this concept THE LABEL THAT STANDS BESIDE A CODE for the same thing — a branch name beside
    `branch_id`, a status description beside the status code?

    Reads the `descriptive` FIELD and nothing else: one concept, one self-declaration. Unknown /
    absent concepts answer False (absence is not an assertion). A concept that says only "this is
    free prose" is NOT this — prose can be computed over, and the answer to PII-laden text is a
    policy, which :func:`is_personal_data` routes to.
    """
    record = CONCEPT_REGISTRY.get(name or "")
    return record is not None and record.descriptive


def is_protected_characteristic(name: str | None) -> bool:
    """Is this concept a protected characteristic or a GDPR special category?

    WHAT THIS CAN ACTUALLY SEE, stated so no caller over-reads it. The registry holds exactly THREE
    concepts in these sensitivity classes — the umbrellas `protected_attribute` and
    `special_category`, plus `vulnerability_flag` — and NO per-attribute concept:
    `protected_attribute` enumerates "age, gender, race, ethnicity, marital status, national origin,
    religion" INSIDE its own description, so there is no `gender` or `ethnicity` concept for a column
    to land on. This therefore answers True only when ENRICHMENT chose one of those three. A
    `gender_cd` column left unclassified, or landed on some ordinary categorical, is invisible here,
    and the USE gate built on this predicate will not refuse it. That is a limit of the VOCABULARY,
    not of the gate — the fix is per-attribute concepts in the registry, and until they exist this
    is a floor rather than a guarantee.
    """
    record = CONCEPT_REGISTRY.get(name or "")
    return record is not None and record.sensitivity in PROTECTED_SENSITIVITIES


def is_personal_data(name: str | None) -> bool:
    """Is this concept personal data (the registry's `pii` sensitivity class)?"""
    record = CONCEPT_REGISTRY.get(name or "")
    return record is not None and record.sensitivity == PERSONAL_DATA_SENSITIVITY


def carries_currency(name: str | None) -> bool:
    """Is a value of this concept denominated in a currency (every `monetary` group member)?"""
    record = CONCEPT_REGISTRY.get(name or "")
    return record is not None and record.group == MONETARY_GROUP


def is_currency_denomination(name: str | None) -> bool:
    """Does this concept NAME a denomination — the currency dimension that sits beside an amount?

    The `currency` group holds two different things: the CODES that say what an amount is
    denominated in (`currency_code`, `base_currency`, `local_currency`) and the conversion RATES
    that move between them (`fx_conversion_rate`, `cross_rate`). Only the codes answer "in what
    currency is this number", so only the codes count here. The discriminator is a registry field
    and not a name list: a rate IS a number and therefore declares its additivity, while a code
    declares none — so a currency concept added later is classified by what it declares about
    itself rather than by anyone remembering to extend a set.
    """
    record = CONCEPT_REGISTRY.get(name or "")
    return record is not None and record.group == CURRENCY_GROUP and record.additivity == "n/a"


def denomination_concepts() -> frozenset[str]:
    """Every concept name that denotes a currency dimension — bound into the sibling-column query
    so the "the currency column is right there on the same table" check is a registry lookup."""
    return frozenset(c.name for c in _ALL if is_currency_denomination(c.name))


def concept_path(name: str | None) -> tuple[str, ...]:
    """The selected concept followed by every ``is_a`` ancestor (semantic plan Task 1).

    ``unclassified`` is a SENTINEL, never a registry member — it (like ``None`` and any unknown
    name) returns the EMPTY tuple; the semantic-context bundle carries the closed
    ``concept_unclassified`` missing-context code beside it, so "no hierarchy" is honest output
    rather than a lookup error. Registry validation (:func:`_validate_registry`) makes an ``is_a``
    cycle impossible at import, but this READER still refuses a corrupt registry (a mutated entry
    at runtime) rather than spinning forever: a revisited name raises ``ValueError``."""
    if not name or name == UNCLASSIFIED:
        return ()
    record = CONCEPT_REGISTRY.get(name)
    if record is None:
        return ()
    path: list[str] = [name]
    cur = record.is_a
    while cur is not None:
        if cur in path:
            raise ValueError(
                f"concept registry is corrupt: is_a cycle {' -> '.join([*path, cur])}")
        parent = CONCEPT_REGISTRY.get(cur)
        if parent is None:
            raise ValueError(
                f"concept registry is corrupt: {path[-1]!r} names unknown parent {cur!r}")
        path.append(cur)
        cur = parent.is_a
    return tuple(path)


# The legacy aliases are retained so already-enriched data + the pre-B1b classifier are never
# orphaned, but they are NOT classification targets — the classifier should choose the richer §3
# concept instead. `counterparty_id` joined the set under semantic Task 2 (D12.1): `counterparty`
# is a PARTY ROLE (the third axis, `party_vocab`), not an entity — the identifier is a CIF that
# links the CUSTOMER entity. The registry member itself is preserved byte-stable (its
# `entity_link` feeds governed bridge fact keys), and only NEW classification stops targeting it.
_LEGACY_ALIASES: frozenset[str] = frozenset({
    "monetary_amount", "account_identifier", "customer_identifier", "timestamp", "rate_or_ratio",
    "counterparty_id",
})

# The canonicalization half of the ONE alias seam: a legacy alias with an unambiguous successor
# maps to it; aliases whose successor is ambiguous (monetary_amount, rate_or_ratio, ...) have no
# entry and stay themselves. Keys must be `_LEGACY_ALIASES` members; targets must be non-alias
# registry members (validated below) — a parallel alias mechanism is forbidden.
_CANONICAL_ALIAS_TARGETS: dict[str, str] = {
    "counterparty_id": "customer_id",
}


def _derive_entity_alias_targets() -> dict[str, str]:
    """The ENTITY half of the ONE alias seam, DERIVED from :data:`_CANONICAL_ALIAS_TARGETS`.

    `counterparty_id -> customer_id` already says everything needed: the entity an aliased
    identifier links (`counterparty`) is the entity its canonical successor links (`customer`).
    Restating that as a second hardcoded entity map would be a parallel alias mechanism — the exact
    thing `_validate_alias_seam` exists to forbid — and could only ever drift from the concept
    table it duplicates. Pairs whose two concepts link the SAME entity (or link none) contribute
    nothing and are omitted, so the map stays empty until an alias actually moves an entity."""
    targets: dict[str, str] = {}
    for alias, target in _CANONICAL_ALIAS_TARGETS.items():
        alias_concept = CONCEPT_REGISTRY.get(alias)
        target_concept = CONCEPT_REGISTRY.get(target)
        if alias_concept is None or target_concept is None:
            continue
        source_entity, canonical = alias_concept.entity_link, target_concept.entity_link
        if source_entity and canonical and source_entity != canonical:
            targets[source_entity] = canonical
    return targets


_ENTITY_ALIAS_TARGETS: dict[str, str] = _derive_entity_alias_targets()


def _validate_alias_seam() -> None:
    for alias, target in _CANONICAL_ALIAS_TARGETS.items():
        if alias not in _LEGACY_ALIASES or alias not in CONCEPT_REGISTRY:
            raise ValueError(f"canonical alias source {alias!r} must be a legacy-alias registry member")
        if target not in CONCEPT_REGISTRY or target in _LEGACY_ALIASES:
            raise ValueError(f"canonical alias target {target!r} must be a non-alias registry member")
    # One hop only: a canonical entity that is itself an alias source would make
    # `canonical_entity` order-dependent (A->B and B->C canonicalize differently depending on which
    # is applied), which is not a canonical form at all.
    for source_entity, canonical in _ENTITY_ALIAS_TARGETS.items():
        if canonical in _ENTITY_ALIAS_TARGETS:
            raise ValueError(
                f"entity alias {source_entity!r} -> {canonical!r} chains: the target is itself "
                "an alias source")


_validate_alias_seam()


def is_classifier_producible(name: str) -> bool:
    """True when a NEW classification can produce this name today: a registry member that is NOT a
    retired legacy alias. The recipe registry validates every ``Need`` against THIS (router plan
    Task 1) rather than bare registry membership — a need for a retired alias passed the old check
    and then silently never ground again, because no column could ever be classified to it."""
    return name in CONCEPT_REGISTRY and name not in _LEGACY_ALIASES


def canonical_concept_name(name: str) -> str:
    """The canonical registry name for a NEW selection attempt (semantic Task 2).

    A legacy alias with an unambiguous successor canonicalizes (`counterparty_id` ->
    `customer_id`); every other name — including aliases with no single successor — is returned
    unchanged. STORED values are never rewritten through this seam: historical `counterparty_id`
    evidence, decisions and bridge fact keys stay byte-stable (D12.1 fact-key preservation)."""
    return _CANONICAL_ALIAS_TARGETS.get(name, name)


def display_entity(concept_name: str | None, entity: str | None) -> str | None:
    """The READ-TIME display entity for a column, through the alias seam (D12.1-revised).

    When `concept_name` is an aliased concept and the stored/derived `entity` is that alias's own
    `entity_link` (or absent), the DISPLAY entity is the canonical concept's `entity_link` —
    `counterparty_id` therefore displays `customer` (a counterparty is our customer seen through a
    party ROLE). An explicitly different stored entity is a decision, never an alias artifact, and
    passes through untouched.

    READ SURFACES ONLY (`entity_map._endpoint_view`, semantic-bundle display values, asset-detail
    renders). NOTHING stored or derivation-feeding may route through this function — not
    `graph_node.entity`, not axis-projection fills, not grounding's `concept_entity`: that value
    flows into `advisory_entity_id` -> `_entity_pick` -> `fact_key`, and seaming it would re-key
    governed bridge facts (a REJECTED decoy would resurrect under a fresh key; a VERIFIED link
    would duplicate). The registry member's persisted `entity_link` stays the byte-stable key
    input everywhere."""
    if not concept_name:
        return entity
    canonical = canonical_concept_name(concept_name)
    if canonical == concept_name:
        return entity
    alias = CONCEPT_REGISTRY.get(concept_name)
    target = CONCEPT_REGISTRY.get(canonical)
    if alias is None or target is None or target.entity_link is None:
        return entity
    if entity is None or entity == alias.entity_link:
        return target.entity_link
    return entity


def canonical_entity(entity: str | None) -> str | None:
    """The canonical form of ONE entity name, through the derived entity alias seam — the operand
    normalizer :func:`display_entity` is not.

    `display_entity` answers "what entity should this COLUMN display?", and it can only normalize
    when the column's own concept is the alias. Comparing a SOURCE-declared entity against a
    concept's `entity_link` is a different question with two independent operands: D12.1-revised
    leaves them on opposite sides of the alias (stored/declared entities keep saying `counterparty`
    because bridge fact keys are byte-stable, while a new classification canonicalizes to
    `customer_id`, whose link is `customer`). Both operands must be reduced to the same canonical
    form or the seam reports the alias itself as a disagreement.

    COMPARISON AND DISPLAY ONLY, exactly like `display_entity`: nothing stored or
    derivation-feeding may route through this function — not `graph_node.entity`, not axis
    projection, not grounding's `concept_entity` — because that value reaches `advisory_entity_id`
    -> `_entity_pick` -> `fact_key`, and re-keying governed bridge facts is how a REJECTED decoy
    resurrects under a fresh key. Returns ``None`` for a blank entity (absent is not a name)."""
    if not entity:
        return None
    return _ENTITY_ALIAS_TARGETS.get(entity, entity)


def classification_vocabulary() -> tuple[dict, ...]:
    """The vocabulary the enrichment classifier chooses from — each concept's ``name``, ``group`` and
    its ``hint`` (the WHOLE description), EXCLUDING the legacy aliases. Passed to the LLM (B1b) so it
    classifies into the full structured vocabulary rather than a hardcoded subset; an unrecognised
    answer still falls back to ``unclassified`` at the caller.

    ``hint`` used to be ``description.split(".")[0][:120]`` — the FIRST SENTENCE. That silently
    discarded the half of every description this file writes ON PURPOSE for the classifier (see the
    "Descriptions state the NEGATIVE deliberately" note above the identifier block): ``bank_bic``
    sent "SWIFT BIC of a BANK (8/11 alphanumeric)" and cut "never the counterparty person/company —
    a counterparty's CIF is counterparty_id", and ``clearing_member_code`` cut "not a BIC and not a
    counterparty". Those are precisely the sentences that separate the concepts seven live FTR
    columns were misclassified across. Sending the whole description costs ~16k characters across
    318 entries on a payload that rides ``shared_metadata`` ONCE per batch, not per item.

    Still bounded: a registry description is authored text, and an unbounded registry field must not
    become an unbounded prompt. 320 clears the current longest (306) with headroom.

    THE ABSTAIN ENTRY is offered last and is not a registry concept — it is the answer that means
    "none of these fits". Without it a closed answer set can only report a vocabulary GAP as a
    confident wrong label: on 2026-07-21 the classifier had to name a concept for a column defined
    as "Correspondent Intermediary Bank BIC" ten days before ``bank_bic`` existed, and the nearest
    neighbour it returned went on to propose eight bridges from a bank's SWIFT code to a customer
    number. ``_accept_concept`` has always treated ``unclassified`` as VALID; it was simply
    unreachable without violating the response contract. Offering it makes the honest answer a legal
    move, so a gap surfaces as a gap."""
    return (
        *(
            {"name": c.name, "group": c.group, "hint": c.description.strip()[:320]}
            for c in _ALL if c.name not in _LEGACY_ALIASES
        ),
        {"name": UNCLASSIFIED, "group": "none",
         "hint": "NONE of the concepts above fits. Prefer a genuine match wherever one exists — "
                 "this is not a way to avoid deciding. Choose it only when no concept applies, in "
                 "preference to a near-neighbour: a wrong concept silently sets join eligibility, "
                 "visibility and aggregation safety."},
    )
