"""Governed use-case taxonomy — the single closed vocabulary applicability narrows on.

Each node is a small record of a governed modelling *objective* (not an org-chart box): a stable
dot-path ``id``, a ``parent`` pointer (the tree edge), whether it is ``selectable`` as a scoping
result, and whether it is an ``intentionally_empty`` declared-future leaf (governed structure ahead of
authored content). ``include_examples`` / ``exclude_examples`` carry banking-realistic disambiguation
prompts so the recognizer can tell a family apart from its neighbours.

Authored verbatim from ``docs/superpowers/specs/2026-07-09-usecase-taxonomy-crosswalk-draft.md`` §3
(the resulting hierarchy), applying §0 (the promotion test), §1 (dimensions) and §2 (the seven locked
decisions D1–D7). Behaviour-neutral: nothing here modifies ``templates.py`` or grounding — this is a
read-only registry that later tasks *derive* recipe applicability from.

Two shape conventions worth calling out (both are validated at import):

* ``financial_crime`` is a **non-selectable domain parent** (D1): the recognizer may return it only as
  an ambiguity ``domain_hint``, never as a scoping result. Its two branches ``fraud`` and ``aml_cft``
  are the independently-selectable objectives — and, per spec §3, they carry **bare** ids (``fraud``,
  ``aml_cft``) rather than ``financial_crime``-prefixed ones, so the dot-prefix==parent rule applies
  only to dotted ids.
* ``*``-marked spec leaves are ``intentionally_empty=True`` — declared-future objectives with no
  authored recipe yet. Phase-0 coverage validation reports these as "no authored recipe yet
  (intentional)" so the zero-recipe check passes with an explanation.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class UseCase:
    id: str                                         # stable dot-path id (e.g. "credit.early_warning")
    parent: str | None                              # tree edge; None for a top-level family / domain
    display_name: str
    description: str
    selectable: bool = True                         # False only for the financial_crime domain parent
    intentionally_empty: bool = False               # a declared-future ("*") leaf: valid, no recipe yet
    include_examples: tuple[str, ...] = ()          # objectives that DO belong here
    exclude_examples: tuple[str, ...] = ()          # objectives that look close but belong to a sibling


# Every node in spec §3, authored parents-before-children so `_ALL` doubles as a topological order.
# Defaults carry the common case (selectable, not empty, no examples); only what differs is set.
_ALL: tuple[UseCase, ...] = (
    # ── customer ──────────────────────────────────────────────────────────────────────────────────
    UseCase("customer", None, "Customer & Relationship",
            "Retail/relationship objectives about an individual customer's behaviour and value.",
            include_examples=(
                "Will this customer close their primary current account next quarter?",
                "Which customers are drifting to a competitor for day-to-day banking?",
                "Rank customers by the next-best cross-sell product.",
            ),
            exclude_examples=(
                "Detect a fraudulent card transaction in real time (financial_crime).",
                "Forecast the treasury deposit book run-off (treasury_alm).",
            )),
    UseCase("customer.relationship_attrition", "customer", "Relationship Attrition",
            "Loss of the primary banking relationship — churn, deposit attrition, primacy erosion."),
    UseCase("customer.relationship_attrition.churn", "customer.relationship_attrition", "Churn",
            "Full attrition of the customer / primary account (the closure outcome)."),
    UseCase("customer.relationship_attrition.deposit_attrition", "customer.relationship_attrition",
            "Deposit Attrition",
            "An individual customer draining balances to an external bank (a customer objective — D2).",
            include_examples=(
                "Predict a customer moving their savings balance to an external bank.",
                "Flag steady outflow of an individual's deposits before full churn.",
            ),
            exclude_examples=(
                "Forecast the deposit book run-off by segment for the ALM desk "
                "(treasury_alm.deposit_runoff_forecasting).",
                "Detect a fraudulent withdrawal (fraud).",
            )),
    UseCase("customer.relationship_attrition.primacy_loss", "customer.relationship_attrition",
            "Primacy Loss",
            "Erosion of primary-bank status (salary redirected, own-money leaving) short of full churn."),
    UseCase("customer.cross_sell", "customer", "Cross-sell",
            "Next-best-action, share-of-wallet and whitespace growth objectives."),
    UseCase("customer.cross_sell.next_best_action", "customer.cross_sell", "Next Best Action",
            "The next-best product/offer to present to a customer."),
    UseCase("customer.cross_sell.share_of_wallet", "customer.cross_sell", "Share of Wallet",
            "Estimated share of the customer's total banking wallet held with us."),
    UseCase("customer.cross_sell.whitespace", "customer.cross_sell", "Whitespace",
            "Unowned product whitespace — products a comparable customer holds but this one does not."),
    UseCase("customer.clv", "customer", "Customer Lifetime Value",
            "Projected lifetime value / worth of a customer relationship."),
    UseCase("customer.engagement", "customer", "Engagement",
            "Digital/product engagement intensity of a customer."),
    UseCase("customer.segmentation", "customer", "Segmentation",
            "Behavioural / value segmentation of the customer base."),
    UseCase("customer.campaign", "customer", "Campaign",
            "Marketing campaign response / targeting objectives."),
    UseCase("customer.overdraft_propensity", "customer", "Overdraft Propensity",
            "Propensity to use / need an arranged or unarranged overdraft."),

    # ── wealth ────────────────────────────────────────────────────────────────────────────────────
    UseCase("wealth", None, "Wealth Management",
            "Private-banking / wealth objectives about investable-asset retention and client attrition.",
            include_examples=(
                "Predict private-banking client attrition after a relationship-manager departure.",
                "Flag an HNW client moving investable assets to another wealth manager.",
            ),
            exclude_examples=(
                "Retail current-account switching (customer.relationship_attrition).",
                "Insurance policy lapse (insurance.lapse).",
            )),
    UseCase("wealth.asset_outflow", "wealth", "Asset Outflow",
            "Net outflow of investable assets from a wealth relationship."),
    UseCase("wealth.client_attrition", "wealth", "Client Attrition",
            "Attrition of a wealth-management client relationship.",
            intentionally_empty=True),

    # ── credit ────────────────────────────────────────────────────────────────────────────────────
    UseCase("credit", None, "Credit Risk",
            "Lending objectives across the credit lifecycle — underwriting, monitoring, collections.",
            include_examples=(
                "Score a mortgage application for affordability.",
                "Which performing loans show early signs of stress?",
                "Prioritise delinquent accounts for a collections treatment.",
            ),
            exclude_examples=(
                "Detect money-laundering structuring (aml_cft).",
                "Cross-sell a credit card (customer.cross_sell).",
            )),
    UseCase("credit.underwriting", "credit", "Underwriting",
            "New-lending decisioning — affordability, seasoning, SME appraisal."),
    UseCase("credit.underwriting.affordability", "credit.underwriting", "Affordability",
            "Affordability / repayment-capacity assessment at origination."),
    UseCase("credit.underwriting.seasoning", "credit.underwriting", "Seasoning",
            "Early-life performance / seasoning of newly originated loans."),
    UseCase("credit.underwriting.sme", "credit.underwriting", "SME Underwriting",
            "SME / business-lending appraisal."),
    UseCase("credit.early_warning", "credit", "Early Warning",
            "Pre-default deterioration signals on performing exposures (IFRS9 SICR / watchlist).",
            include_examples=(
                "Flag performing loans with a significant increase in credit risk (IFRS9 Stage 1->2).",
                "Detect covenant-headroom erosion before a default event.",
                "Watchlist a name showing adverse-media plus a utilisation spike.",
            ),
            exclude_examples=(
                "Assign a collections treatment after default (credit.collections).",
                "Score a brand-new application (credit.underwriting).",
            )),
    UseCase("credit.monitoring", "credit", "Monitoring",
            "Ongoing performing-book monitoring — limits, obligor exposure, credit-risk mitigation."),
    UseCase("credit.monitoring.limit_management", "credit.monitoring", "Limit Management",
            "Facility/limit utilisation and management on the performing book."),
    UseCase("credit.monitoring.obligor", "credit.monitoring", "Obligor Exposure",
            "Obligor-level exposure monitoring (spec §3 exposure_management -> obligor)."),
    UseCase("credit.monitoring.credit_mitigation", "credit.monitoring", "Credit Mitigation",
            "Credit-risk mitigation — collateral, guarantees, hedges on the performing book."),
    UseCase("credit.collections", "credit", "Collections",
            "Post-delinquency treatment — recoveries, hardship, self-cure, workout."),
    UseCase("credit.collections.recoveries", "credit.collections", "Recoveries",
            "Post-charge-off recovery of amounts owed."),
    UseCase("credit.collections.hardship", "credit.collections", "Hardship",
            "Hardship / forbearance treatment of a distressed borrower."),
    UseCase("credit.collections.self_cure", "credit.collections", "Self-cure",
            "Likelihood a delinquent account cures without intervention."),
    UseCase("credit.collections.workout", "credit.collections", "Workout",
            "Restructuring / workout of a defaulted or near-default exposure."),

    # ── financial_crime (non-selectable domain — D1) ────────────────────────────────────────────────
    UseCase("financial_crime", None, "Financial Crime",
            "Non-selectable domain parent (D1): returned only as an ambiguity domain_hint, never as a "
            "scoping result. Its fraud and aml_cft branches are the selectable objectives.",
            selectable=False,
            include_examples=(
                "An ambiguous 'suspicious activity' request that spans both fraud and AML.",
                "A transaction-monitoring alert of unclear type pending triage.",
            ),
            exclude_examples=(
                "Credit default prediction (credit).",
                "Deposit attrition (customer.relationship_attrition.deposit_attrition).",
            )),
    UseCase("fraud", "financial_crime", "Fraud",
            "Fraud-family objectives — real-time, loss-reduction, distinct regime from AML."),
    UseCase("fraud.transaction_fraud_detection", "fraud", "Transaction Fraud Detection",
            "Real-time detection of fraudulent transactions (the fraud-family transaction-monitoring "
            "objective — D3).",
            include_examples=(
                "Flag a card-testing velocity burst in real time.",
                "Detect an impossible-geo-velocity card-present anomaly.",
                "Score a first-time high-value payee for payment fraud.",
            ),
            exclude_examples=(
                "Detect structuring/smurfing for AML (aml_cft.suspicious_transaction_monitoring).",
                "Predict loan default (credit).",
            )),
    UseCase("fraud.card_fraud", "fraud", "Card Fraud",
            "Card-specific fraud (lost/stolen, CNP, counterfeit)."),
    UseCase("fraud.account_takeover", "fraud", "Account Takeover",
            "Account-takeover / credential-compromise fraud."),
    UseCase("fraud.app_scam", "fraud", "APP Scam",
            "Authorised-push-payment scams (the customer is socially engineered to pay)."),
    UseCase("fraud.synthetic_id", "fraud", "Synthetic Identity",
            "Synthetic-identity fraud (fabricated / blended identities)."),
    UseCase("fraud.merchant_fraud", "fraud", "Merchant Fraud",
            "Merchant / acquiring-side fraud (bust-out, collusion, first-party)."),
    UseCase("aml_cft", "financial_crime", "AML / CFT",
            "Anti-money-laundering & counter-terrorist-financing objectives — a distinct regulatory "
            "regime and workflow from fraud."),
    UseCase("aml_cft.suspicious_transaction_monitoring", "aml_cft", "Suspicious Transaction Monitoring",
            "Detection of money-laundering typologies in transaction flow (the AML-family "
            "transaction-monitoring objective — D3).",
            include_examples=(
                "Detect structuring/smurfing across cash deposits.",
                "Flag rapid pass-through / layering of funds.",
                "Surface fan-in/fan-out mule-network activity.",
            ),
            exclude_examples=(
                "Detect card-testing fraud velocity (fraud.transaction_fraud_detection).",
                "Score credit affordability (credit.underwriting.affordability).",
            )),
    UseCase("aml_cft.structuring", "aml_cft", "Structuring",
            "Structuring / smurfing to evade reporting thresholds."),
    UseCase("aml_cft.sanctions", "aml_cft", "Sanctions",
            "Sanctions-exposure detection and evasion typologies."),
    UseCase("aml_cft.screening", "aml_cft", "Screening",
            "Name / payment screening against sanctions, PEP and watchlists."),
    UseCase("aml_cft.kyc", "aml_cft", "KYC",
            "Know-your-customer / customer-due-diligence risk objectives."),
    UseCase("aml_cft.correspondent", "aml_cft", "Correspondent Banking",
            "Correspondent / nested-correspondent banking AML risk."),
    UseCase("aml_cft.mule_account", "aml_cft", "Mule Account",
            "Money-mule account detection (declared-future).",
            intentionally_empty=True),
    UseCase("aml_cft.tbml", "aml_cft", "Trade-based Money Laundering",
            "Trade-based money-laundering typologies (declared-future).",
            intentionally_empty=True),

    # ── treasury_alm ────────────────────────────────────────────────────────────────────────────────
    UseCase("treasury_alm", None, "Treasury & ALM",
            "Balance-sheet / asset-liability-management objectives at portfolio & time-bucket grain.",
            include_examples=(
                "Forecast 90-day deposit run-off by segment and time bucket for the ALM desk.",
                "Project net interest margin under a rate-shock scenario.",
                "Estimate liquidity coverage under a stress scenario.",
            ),
            exclude_examples=(
                "Will an individual customer leave the bank? (customer.relationship_attrition).",
                "Detect card fraud (fraud).",
            )),
    UseCase("treasury_alm.deposit_stability", "treasury_alm", "Deposit Stability",
            "Behavioural stability / stickiness of the deposit base."),
    UseCase("treasury_alm.deposit_runoff_forecasting", "treasury_alm", "Deposit Run-off Forecasting",
            "ALM-grain (portfolio/segment/time-bucket) deposit run-off forecasting (a treasury "
            "objective, distinct from individual deposit attrition — D2).",
            include_examples=(
                "Forecast non-maturing-deposit run-off by segment and time bucket for the ALM desk.",
                "Model behavioural deposit decay under a rate-rise scenario.",
            ),
            exclude_examples=(
                "Predict whether an individual customer will leave "
                "(customer.relationship_attrition.deposit_attrition).",
                "Score a single loan's default (credit).",
            )),
    UseCase("treasury_alm.liquidity", "treasury_alm", "Liquidity",
            "Liquidity risk / coverage (LCR/NSFR-adjacent behavioural inputs)."),
    UseCase("treasury_alm.net_interest_margin", "treasury_alm", "Net Interest Margin",
            "Net-interest-margin projection under rate paths."),
    UseCase("treasury_alm.irrbb", "treasury_alm", "IRRBB",
            "Interest-rate risk in the banking book."),
    UseCase("treasury_alm.irrbb.basis_risk", "treasury_alm.irrbb", "Basis Risk (Banking Book)",
            "Banking-book basis risk (declared-future; markets basis risk lives at "
            "markets.market_risk.basis_risk).",
            intentionally_empty=True),
    UseCase("treasury_alm.cash_management", "treasury_alm", "Cash Management",
            "Corporate / group cash-management and pooling objectives."),

    # ── portfolio_risk ──────────────────────────────────────────────────────────────────────────────
    UseCase("portfolio_risk", None, "Portfolio Risk",
            "Cross-cutting portfolio-level risk objectives that do not force a Credit/Markets fit.",
            include_examples=(
                "Measure name/sector/geography concentration across the lending book.",
                "Aggregate group exposure across related counterparties.",
            ),
            exclude_examples=(
                "Score a single borrower's default risk (credit).",
                "Real-time payment fraud (fraud).",
            )),
    UseCase("portfolio_risk.concentration", "portfolio_risk", "Concentration",
            "Concentration risk (name/sector/geography/funding), with risk_context metadata (D7)."),

    # ── counterparty_risk (promoted to top-level — D7) ──────────────────────────────────────────────
    UseCase("counterparty_risk", None, "Counterparty Risk",
            "Counterparty credit risk — exposure monitoring, margin-call risk, settlement exposure.",
            include_examples=(
                "Monitor OTC-derivative counterparty exposure against netting-set limits.",
                "Predict margin-call intensity for a clearing member.",
            ),
            exclude_examples=(
                "Retail customer churn (customer.relationship_attrition).",
                "Deposit run-off (treasury_alm.deposit_runoff_forecasting).",
            )),
    UseCase("counterparty_risk.exposure_monitoring", "counterparty_risk", "Exposure Monitoring",
            "Counterparty exposure monitoring (from generic exposure_management — D7)."),
    UseCase("counterparty_risk.settlement_exposure", "counterparty_risk", "Settlement Exposure",
            "Settlement / pre-settlement counterparty exposure (declared-future).",
            intentionally_empty=True),
    UseCase("counterparty_risk.margin_call_risk", "counterparty_risk", "Margin-call Risk",
            "Margin-call intensity / liquidity risk (from generic margin — D7)."),

    # ── markets ─────────────────────────────────────────────────────────────────────────────────────
    UseCase("markets", None, "Markets",
            "Trading / market-risk objectives.",
            include_examples=(
                "Estimate basis risk between a hedge and the underlying.",
                "Forecast trading-book portfolio market risk.",
            ),
            exclude_examples=(
                "Credit collections (credit.collections).",
                "AML screening (aml_cft.screening).",
            )),
    UseCase("markets.market_risk", "markets", "Market Risk",
            "Market-risk objectives across the trading book."),
    UseCase("markets.market_risk.basis_risk", "markets.market_risk", "Basis Risk",
            "Trading-book basis risk (from benchmark_basis_dislocation — D7)."),
    UseCase("markets.market_risk.portfolio", "markets.market_risk", "Portfolio Market Risk",
            "Portfolio-level market-risk aggregation."),

    # ── payments ────────────────────────────────────────────────────────────────────────────────────
    UseCase("payments", None, "Payments",
            "Payment-behaviour, operations, merchant-economics and cross-border objectives.",
            include_examples=(
                "Model customer payment behaviour across rails.",
                "Forecast cross-border remittance corridor volumes.",
                "Predict merchant interchange revenue.",
            ),
            exclude_examples=(
                "Loan default (credit).",
                "Insurance claims fraud (insurance.claims.claims_fraud).",
            )),
    UseCase("payments.behaviour", "payments", "Payment Behaviour",
            "Customer payment-behaviour patterns across rails and channels."),
    UseCase("payments.operations", "payments", "Payment Operations",
            "Payment-processing operations (throughput, exceptions, repair)."),
    UseCase("payments.merchant", "payments", "Merchant",
            "Merchant / acquiring economics."),
    UseCase("payments.merchant.interchange", "payments.merchant", "Interchange",
            "Interchange revenue / economics on card transactions."),
    UseCase("payments.cross_border", "payments", "Cross-border",
            "Cross-border / remittance corridor objectives."),

    # ── securities_services ─────────────────────────────────────────────────────────────────────────
    UseCase("securities_services", None, "Securities Services",
            "Custody, securities-lending and fund-administration objectives.",
            include_examples=(
                "Predict settlement fails in the custody book.",
                "Forecast corporate-action processing workload.",
            ),
            exclude_examples=(
                "Retail deposit attrition (customer.relationship_attrition.deposit_attrition).",
                "Card fraud (fraud.card_fraud).",
            )),
    UseCase("securities_services.custody", "securities_services", "Custody",
            "Custody objectives — settlement-failure risk, holdings dynamics and corporate actions."),
    UseCase("securities_services.custody.settlement_failure_risk", "securities_services.custody",
            "Settlement Failure Risk",
            "Settlement-fail / matching-break risk prediction (the 4 custody settlement recipes — D7); "
            "the objective is the FAIL outcome, not the stock of assets held."),
    UseCase("securities_services.custody.holdings_dynamics", "securities_services.custody",
            "Holdings Dynamics",
            "Assets / positions held under custody — growth/decline of the holdings level, concentration "
            "(HHI) and turnover; the custody-book stock-stability objective (distinct from settlement).",
            include_examples=(
                "Track the growth/decline of an account's assets-under-custody holdings over a window.",
                "Measure holdings concentration (HHI) and turnover across custodied positions.",
                "Flag a custody book draining assets to another custodian.",
            ),
            exclude_examples=(
                "Predict settlement fails or matching breaks "
                "(securities_services.custody.settlement_failure_risk) — that is the settlement "
                "objective, not the holdings stock-dynamics objective.",
                "Process a corporate action (securities_services.custody.corporate_actions).",
            )),
    UseCase("securities_services.custody.corporate_actions", "securities_services.custody",
            "Corporate Actions",
            "Corporate-action processing objectives."),
    UseCase("securities_services.securities_lending", "securities_services", "Securities Lending",
            "Securities-lending / SFT objectives."),
    UseCase("securities_services.fund_administration", "securities_services", "Fund Administration",
            "Fund-administration / NAV-oversight objectives."),

    # ── insurance ───────────────────────────────────────────────────────────────────────────────────
    UseCase("insurance", None, "Insurance",
            "Insurance objectives — lapse/persistency, claims, reinsurance, bancassurance.",
            include_examples=(
                "Predict life-policy lapse and surrender.",
                "Detect claims fraud on motor insurance.",
                "Model persistency for a protection book.",
            ),
            exclude_examples=(
                "Bank deposit run-off (treasury_alm.deposit_runoff_forecasting).",
                "AML structuring (aml_cft.structuring).",
            )),
    UseCase("insurance.lapse", "insurance", "Lapse",
            "Policy lapse — surrender and persistency."),
    UseCase("insurance.lapse.surrender", "insurance.lapse", "Surrender",
            "Voluntary policy surrender (cash-out) outcome."),
    UseCase("insurance.lapse.persistency", "insurance.lapse", "Persistency",
            "Policy persistency / retention over the cover period."),
    UseCase("insurance.claims", "insurance", "Claims",
            "Claims objectives — including claims fraud."),
    UseCase("insurance.claims.claims_fraud", "insurance.claims", "Claims Fraud",
            "Fraudulent-claim detection."),
    UseCase("insurance.actuarial", "insurance", "Actuarial",
            "Actuarial cost / risk objectives — the honest expected-loss view (not fraud, not price)."),
    UseCase("insurance.actuarial.claims_cost_modelling", "insurance.actuarial",
            "Claims Cost Modelling",
            "Expected claims cost from claim frequency × severity (or loss ratio) — the actuarial "
            "claims-cost signal consumed by pricing, reserving, reinsurance and capital; the objective "
            "is the expected cost itself, not detecting fraud and not setting the price.",
            include_examples=(
                "Model expected claims cost from claim frequency and severity for a motor book.",
                "Project incurred claims cost per policy to feed reserving and reinsurance.",
                "Estimate a loss ratio vs earned premium as the actuarial cost view.",
            ),
            exclude_examples=(
                "Detect fraudulent claims (insurance.claims.claims_fraud) — this leaf models the honest "
                "expected cost, it is NOT fraud detection.",
                "Set the premium / rate itself (pricing) — claims-cost modelling feeds pricing but the "
                "price is not this objective's target.",
            )),
    UseCase("insurance.underwriting", "insurance", "Underwriting",
            "New-business insurance underwriting — assessing applicant risk to decide eligibility, "
            "rating and loading (distinct from ceded-risk reinsurance)."),
    UseCase("insurance.underwriting.mortality_morbidity_risk_assessment", "insurance.underwriting",
            "Mortality / Morbidity Risk Assessment",
            "Assess an applicant's mortality / morbidity risk (rate level or loading vs the standard "
            "table) to drive eligibility, rating and underwriting loading; the objective is the "
            "applicant risk assessment, not reinsurance treaty economics.",
            include_examples=(
                "Assess a life applicant's mortality risk to set eligibility and a rating loading.",
                "Score morbidity risk on a protection policy for an underwriting loading factor.",
            ),
            exclude_examples=(
                "Structure ceded / retained risk with a reinsurer (insurance.reinsurance) — that is "
                "treaty economics, not the applicant's own risk assessment.",
                "Detect fraudulent claims (insurance.claims.claims_fraud).",
            )),
    UseCase("insurance.reinsurance", "insurance", "Reinsurance",
            "Reinsurance / ceded-risk objectives."),
    UseCase("insurance.bancassurance", "insurance", "Bancassurance",
            "Bancassurance cross-sell / distribution objectives."),

    # ── asset_management ────────────────────────────────────────────────────────────────────────────
    UseCase("asset_management", None, "Asset Management",
            "Fund flows, mandate compliance and performance objectives.",
            include_examples=(
                "Forecast fund net redemptions and outflows.",
                "Monitor mandate/benchmark compliance breaches.",
                "Predict AUM stability for a share class.",
            ),
            exclude_examples=(
                "Retail current-account churn (customer.relationship_attrition).",
                "Card fraud (fraud.card_fraud).",
            )),
    UseCase("asset_management.redemption", "asset_management", "Redemption",
            "Redemption / fund-flow objectives — flows, liquidity, AUM stability."),
    UseCase("asset_management.redemption.fund_flows", "asset_management.redemption", "Fund Flows",
            "Net fund flows (subscriptions less redemptions)."),
    UseCase("asset_management.redemption.fund_liquidity", "asset_management.redemption", "Fund Liquidity",
            "Fund-liquidity risk under redemption pressure."),
    UseCase("asset_management.redemption.aum_stability", "asset_management.redemption", "AUM Stability",
            "Stability of assets under management."),
    UseCase("asset_management.mandate_compliance", "asset_management", "Mandate Compliance",
            "Investment-mandate / benchmark compliance monitoring."),
    UseCase("asset_management.performance", "asset_management", "Performance",
            "Portfolio-performance / attribution objectives."),

    # ── islamic ─────────────────────────────────────────────────────────────────────────────────────
    UseCase("islamic", None, "Islamic Finance",
            "Islamic-banking behaviour and Sharia-compliance objectives.",
            include_examples=(
                "Assess Sharia-compliance screening breaches.",
                "Model Islamic-banking financing behaviour.",
            ),
            exclude_examples=(
                "Conventional interest-rate risk (treasury_alm.irrbb).",
                "Deposit run-off (treasury_alm.deposit_runoff_forecasting).",
            )),
    UseCase("islamic.banking", "islamic", "Islamic Banking",
            "Islamic-banking product / financing behaviour objectives."),
    UseCase("islamic.sharia_compliance", "islamic", "Sharia Compliance",
            "Sharia-compliance screening and purification objectives."),

    # ── esg ─────────────────────────────────────────────────────────────────────────────────────────
    UseCase("esg", None, "ESG & Climate",
            "ESG scoring and climate (transition / physical) risk objectives.",
            include_examples=(
                "Score a corporate borrower's ESG profile.",
                "Assess transition-risk alignment of a lending portfolio.",
                "Estimate physical climate-hazard exposure.",
            ),
            exclude_examples=(
                "Credit affordability (credit.underwriting.affordability).",
                "Payment fraud (fraud.transaction_fraud_detection).",
            )),
    UseCase("esg.scoring", "esg", "ESG Scoring",
            "ESG scoring / rating objectives (absorbs sustainable_finance)."),
    UseCase("esg.climate", "esg", "Climate",
            "Climate-risk objectives — transition and physical."),
    UseCase("esg.climate.transition", "esg.climate", "Transition Risk",
            "Transition / net-zero alignment risk."),
    UseCase("esg.climate.physical", "esg.climate", "Physical Risk",
            "Physical climate-hazard risk (flood/heat/wildfire)."),

    # ── corporate_trade ─────────────────────────────────────────────────────────────────────────────
    UseCase("corporate_trade", None, "Corporate & Trade",
            "Trade-finance, supply-chain-finance, working-capital and receivables objectives.",
            include_examples=(
                "Assess trade-finance / letter-of-credit risk.",
                "Forecast supply-chain-finance receivables performance.",
                "Model working-capital facility utilisation.",
            ),
            exclude_examples=(
                "Retail customer churn (customer.relationship_attrition).",
                "Insurance lapse (insurance.lapse).",
            )),
    UseCase("corporate_trade.trade_finance", "corporate_trade", "Trade Finance",
            "Trade-finance / letter-of-credit / guarantee objectives."),
    UseCase("corporate_trade.supply_chain_finance", "corporate_trade", "Supply-chain Finance",
            "Supply-chain / reverse-factoring finance objectives."),
    UseCase("corporate_trade.working_capital", "corporate_trade", "Working Capital",
            "Working-capital facility / utilisation objectives."),
    UseCase("corporate_trade.receivables_finance", "corporate_trade", "Receivables Finance",
            "Receivables / invoice-finance objectives."),

    # ── pricing (declared-future family — D5) ───────────────────────────────────────────────────────
    UseCase("pricing", None, "Pricing",
            "Objectives where price/rate/fee is the target (declared-future; no primary recipes yet).",
            include_examples=(
                "Optimise risk-based interest pricing for a loan.",
                "Set relationship-based deposit rates.",
            ),
            exclude_examples=(
                "Reduce cost-to-collect — that is a business-outcome, not pricing.",
                "Predict customer churn (customer.relationship_attrition).",
            )),
    UseCase("pricing.credit_risk_based_pricing", "pricing", "Credit Risk-based Pricing",
            "Risk-based loan pricing (declared-future).",
            intentionally_empty=True),
    UseCase("pricing.deposit_rate_optimisation", "pricing", "Deposit Rate Optimisation",
            "Deposit-rate optimisation (declared-future).",
            intentionally_empty=True),
    UseCase("pricing.fee_pricing", "pricing", "Fee Pricing",
            "Fee / charge pricing (declared-future).",
            intentionally_empty=True),
    UseCase("pricing.relationship_pricing", "pricing", "Relationship Pricing",
            "Relationship / bundle pricing (declared-future).",
            intentionally_empty=True),

    # ── operations (declared-future family — D5) ────────────────────────────────────────────────────
    UseCase("operations", None, "Operations",
            "Genuine operational-target objectives (declared-future; no recipes yet).",
            include_examples=(
                "Forecast back-office processing cost.",
                "Forecast manual-review workload for an ops queue.",
            ),
            exclude_examples=(
                "Price a product (pricing).",
                "Detect fraud (fraud).",
            )),
    UseCase("operations.process_cost_forecasting", "operations", "Process Cost Forecasting",
            "Operational process-cost forecasting (declared-future).",
            intentionally_empty=True),
    UseCase("operations.workload_forecasting", "operations", "Workload Forecasting",
            "Operational workload forecasting (declared-future).",
            intentionally_empty=True),
    UseCase("operations.manual_review_optimisation", "operations", "Manual Review Optimisation",
            "Manual-review-queue optimisation (declared-future).",
            intentionally_empty=True),

    # ── profitability (declared-future family — D5) ─────────────────────────────────────────────────
    UseCase("profitability", None, "Profitability",
            "Margin / contribution forecasting objectives (declared-future; no recipes yet).",
            include_examples=(
                "Forecast product / segment margin.",
                "Project net contribution by relationship.",
            ),
            exclude_examples=(
                "Set a price (pricing).",
                "Reduce cost-to-collect — that is a business-outcome.",
            )),
    UseCase("profitability.margin_forecasting", "profitability", "Margin Forecasting",
            "Margin / contribution forecasting (declared-future).",
            intentionally_empty=True),
)

# Public registry: dot-path id -> full UseCase record.
USE_CASE_REGISTRY: dict[str, UseCase] = {u.id: u for u in _ALL}


def _validate_registry() -> None:
    """Fail fast at import if the tree drifts: unique ids, resolvable parents, dot-path/parent
    consistency, a root has no dot, and a non-selectable node is a real (child-bearing) parent."""
    ids: set[str] = set()
    for u in _ALL:
        if u.id in ids:
            raise ValueError(f"duplicate use-case id {u.id!r}")
        ids.add(u.id)
    parents: set[str] = {u.parent for u in _ALL if u.parent is not None}
    for u in _ALL:
        # Every non-None parent resolves to a real node.
        if u.parent is not None and u.parent not in ids:
            raise ValueError(f"use-case {u.id!r} has unresolved parent {u.parent!r}")
        # A root (parent=None) is a bare, dot-free family/domain id.
        if u.parent is None and "." in u.id:
            raise ValueError(f"root use-case {u.id!r} must not contain a dot")
        # A dotted id must sit directly under its dot-prefix parent (e.g. "a.b.c" under "a.b").
        # (A bare id WITH a parent is the deliberate financial_crime branch case — fraud / aml_cft
        #  carry no domain prefix — so the dot-prefix rule only binds dotted ids.)
        if "." in u.id:
            prefix = u.id.rsplit(".", 1)[0]
            if prefix != u.parent:
                raise ValueError(f"use-case {u.id!r} dot-prefix {prefix!r} != parent {u.parent!r}")
        # A non-selectable node must be a real parent (>= 1 child): it exists only to group.
        if not u.selectable and u.id not in parents:
            raise ValueError(f"non-selectable use-case {u.id!r} must have at least one child")


_validate_registry()


def use_case(uid: str) -> UseCase | None:
    """The full record for a use-case id, or None if it is not in the registry."""
    return USE_CASE_REGISTRY.get(uid)


def is_known_use_case(uid: str) -> bool:
    return uid in USE_CASE_REGISTRY


def ancestors(uid: str) -> tuple[str, ...]:
    """The ancestor ids of ``uid`` in root -> immediate-parent order (walking the ``parent`` chain,
    NOT the dot-path — so fraud's domain parent financial_crime is reported even though fraud's id
    carries no prefix). Empty for a root or an unknown id."""
    node = USE_CASE_REGISTRY.get(uid)
    if node is None:
        return ()
    chain: list[str] = []
    parent = node.parent
    while parent is not None:
        chain.append(parent)
        parent_node = USE_CASE_REGISTRY.get(parent)
        if parent_node is None:
            break
        parent = parent_node.parent
    return tuple(reversed(chain))


def descendants(uid: str) -> tuple[str, ...]:
    """Every id below ``uid`` (any depth), in authoring/topological order. Empty for a leaf or an
    unknown id."""
    if uid not in USE_CASE_REGISTRY:
        return ()
    return tuple(u.id for u in _ALL if uid in ancestors(u.id))


def selectable_leaves() -> tuple[str, ...]:
    """Every selectable node that has no selectable child — the terminal, choosable objectives.
    Excludes the non-selectable financial_crime domain parent; includes intentionally-empty leaves
    (they are governed, choosable objectives that simply have no authored recipe yet)."""
    has_selectable_child: set[str] = {
        u.parent for u in _ALL if u.parent is not None and u.selectable}
    return tuple(u.id for u in _ALL if u.selectable and u.id not in has_selectable_child)
