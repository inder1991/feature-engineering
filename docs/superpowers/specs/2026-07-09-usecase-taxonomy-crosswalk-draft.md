# Use-Case Taxonomy & 107-Tag Crosswalk — DRAFT for review

**Status:** Draft (Phase-0 artifact; domain review requested before it becomes code)
**Input:** the 107 distinct `use_cases` tags currently on the 153 recipes
**Purpose:** classify every legacy tag into the *right dimension* (not just use-case-vs-framework), and propose the governed use-case hierarchy the recognizer + applicability evaluator depend on.
**Related:** `2026-07-09-intent-aware-recipe-selection-plan.md` (§6 Phase 0)

> This reclassifies category errors rather than moving them around. Nothing here is final — the contentious calls in §5 are the ones I want your judgment on.

---

## 1. Dimension summary (107 tags)

| Dimension | Meaning | Count |
|---|---|---|
| **use_case** (leaf) | a business objective you can model | ~84 |
| **use_case** (parent/domain) | a grouping node (e.g. `financial_crime`) | ~3 |
| **modelling_context** | a regulatory framework/regime shaping feature semantics | 6 |
| **measure** | an analytical/risk output quantity, not an objective | 4 |
| **deprecated / merge** | redundant or a funnel-stage signal → folded into a parent | 7 |
| **flagged** | genuinely ambiguous — needs your call (§5) | 3 |

The headline: **~17 of the 107 tags are NOT use-cases** and must leave the use-case dimension, or the recognizer will "classify" a request as `frtb` or `lgd`.

---

## 2. Proposed use-case hierarchy

```text
customer                      (retail relationship lifecycle)
├── customer.churn            ← retail_churn  [folds: unbundling, primacy_loss]
├── customer.deposit_attrition← deposit_attrition  [folds: wealth_outflow]
├── customer.engagement       ← engagement
├── customer.overdraft_propensity ← overdraft_propensity
├── customer.cross_sell       ← cross_sell
│   ├── .next_best_action     ← next_best_action
│   ├── .share_of_wallet      ← share_of_wallet
│   └── .whitespace           ← whitespace
├── customer.clv              ← clv
├── customer.segmentation     ← segmentation
└── customer.campaign         ← campaign_analytics

credit                        (credit-risk lifecycle)   ← credit_risk (parent)
├── credit.underwriting       ← underwriting
│   ├── .affordability        ← affordability  [folds: cashflow]
│   ├── .seasoning            ← credit_seasoning
│   └── .sme                  ← sme_credit
├── credit.early_warning      ← early_warning
├── credit.monitoring
│   ├── .limit_management     ← limit_management
│   ├── .exposure_management  ← exposure_management
│   ├── .concentration        ← concentration_risk
│   └── .credit_mitigation    ← credit_mitigation
└── credit.collections        ← collections
    ├── .recoveries           ← recoveries
    ├── .hardship             ← hardship  [folds: contactability]
    ├── .self_cure            ← self_cure
    └── .workout              ← workout

financial_crime               (PARENT / domain — not a leaf)
├── financial_crime.transaction_monitoring ← transaction_monitoring (shared)
├── financial_crime.fraud     ← fraud
│   ├── .card_fraud           ← card_fraud
│   ├── .account_takeover     ← account_takeover
│   ├── .app_scam             ← app_scam  [merge: authorised_push_payment]
│   └── .synthetic_id         ← synthetic_id
└── financial_crime.aml       ← aml
    ├── .structuring          ← structuring
    ├── .sanctions            ← sanctions
    ├── .screening            ← screening
    ├── .kyc                  ← kyc
    └── .correspondent        ← correspondent_banking

deposits_alm                  (balance-sheet / treasury)  ← alm (parent)
├── deposits_alm.deposit_stability ← deposit_stability
├── deposits_alm.liquidity    ← liquidity_risk  [folds: cashflow_risk]
├── deposits_alm.irrbb        ← (objective; irrbb tag → modelling_context)
├── deposits_alm.basis_risk   ← basis_risk
└── deposits_alm.cash_management ← cash_management

markets                       (trading / capital markets)
├── markets.market_risk       ← market_risk  [folds: trading_risk, portfolio_risk]
└── markets.counterparty_risk ← counterparty_risk  [folds: margin]

payments
├── payments.behaviour        ← payments
├── payments.operations       ← payments_ops
├── payments.merchant         ← merchant_analytics
│   └── .interchange          ← interchange_optimisation
└── payments.cross_border     ← cross_border

securities_services           (custody / post-trade)  ← securities_services (parent)
├── securities_services.custody ← custody
│   ├── .settlement           ← settlement_risk
│   └── .corporate_actions    ← corporate_actions
├── securities_services.securities_lending ← securities_lending
└── securities_services.fund_administration ← fund_administration

insurance
├── insurance.lapse           ← lapse_risk  [folds: persistency, surrender]
├── insurance.claims          ← claims
│   └── .claims_fraud         ← claims_fraud  (x-ref financial_crime)
├── insurance.reinsurance     ← reinsurance
└── insurance.bancassurance   ← bancassurance

asset_management              (buy-side)
├── asset_management.redemption ← redemption_risk  [folds: fund_flows, fund_liquidity, aum_stability]
├── asset_management.mandate_compliance ← mandate_compliance
└── asset_management.performance ← fund_performance

islamic
├── islamic.banking           ← islamic_banking
└── islamic.sharia_compliance ← sharia_compliance

esg                           (sustainable finance)
├── esg.scoring               ← esg_scoring  [folds: sustainable_finance]
└── esg.climate
    ├── .transition           ← transition_risk  [folds: climate_risk]
    └── .physical             ← physical_risk

corporate_trade               (corporate/SME trade & supply-chain)
├── corporate_trade.trade_finance ← trade_finance
├── corporate_trade.supply_chain_finance ← supply_chain_finance
├── corporate_trade.working_capital ← working_capital
└── corporate_trade.receivables_finance ← receivables_finance

pricing                       (cross-cutting)  ← pricing
```

---

## 3. Reclassified OUT of the use-case dimension

**modelling_context (6)** — a framework/regime, recognised as a *separate* dimension:
`ifrs9_staging`, `frtb`, `irrbb`, `lcr`, `nsfr`, `ftp`

**measure (4)** — an output quantity, not an objective:
`xva` (→ markets.counterparty_risk), `lgd` (→ credit), `tracking_error` (→ asset_management.performance), `data_quality` (a quality measure over any recipe)

**deprecated / merge (7)** — redundant or a funnel-stage *signal* folded into a parent use-case as stage metadata, not a standalone use-case:
`authorised_push_payment`→`app_scam`; `unbundling`→`customer.churn`; `primacy_loss`→`customer.churn`; `wealth_outflow`→`customer.deposit_attrition`; `contactability`→`credit.collections`; `cashflow`→`credit.underwriting.affordability`; `sustainable_finance`→`esg.scoring`

---

## 4. Full 107-tag crosswalk (compact)

Format: `tag (recipe-count) → dimension : target`

```text
financial_crime(23)        → use_case(parent) : financial_crime
transaction_monitoring(22) → use_case : financial_crime.transaction_monitoring
credit_risk(18)            → use_case(parent) : credit
early_warning(18)          → use_case : credit.early_warning
cross_sell(14)             → use_case : customer.cross_sell
aml(13)                    → use_case(parent) : financial_crime.aml
collections(13)            → use_case : credit.collections
fraud(12)                  → use_case(parent) : financial_crime.fraud
retail_churn(12)           → use_case : customer.churn
alm(11)                    → use_case(parent) : deposits_alm
insurance(10)              → use_case(parent) : insurance
liquidity_risk(10)         → use_case : deposits_alm.liquidity
payments(10)               → use_case : payments.behaviour
recoveries(10)             → use_case : credit.collections.recoveries
trade_finance(10)          → use_case : corporate_trade.trade_finance
esg_scoring(9)             → use_case : esg.scoring
asset_management(8)        → use_case(parent) : asset_management
climate_risk(8)            → use_case : esg.climate.transition [merge]
custody(8)                 → use_case : securities_services.custody
islamic_banking(8)         → use_case : islamic.banking
limit_management(8)        → use_case : credit.monitoring.limit_management
market_risk(8)             → use_case : markets.market_risk
merchant_analytics(8)      → use_case : payments.merchant
securities_services(8)     → use_case(parent) : securities_services
sharia_compliance(8)       → use_case : islamic.sharia_compliance
deposit_stability(7)       → use_case : deposits_alm.deposit_stability
pricing(7)                 → use_case : pricing            [FLAG §5]
transition_risk(7)         → use_case : esg.climate.transition
working_capital(7)         → use_case : corporate_trade.working_capital
clv(6)                     → use_case : customer.clv
concentration_risk(6)      → use_case : credit.monitoring.concentration [x-cut markets]
ifrs9_staging(6)           → modelling_context : framework.ifrs9
next_best_action(6)        → use_case : customer.cross_sell.next_best_action
redemption_risk(6)         → use_case : asset_management.redemption
account_takeover(5)        → use_case : financial_crime.fraud.account_takeover
counterparty_risk(5)       → use_case : markets.counterparty_risk
lapse_risk(5)              → use_case : insurance.lapse
share_of_wallet(5)         → use_case : customer.cross_sell.share_of_wallet
card_fraud(4)              → use_case : financial_crime.fraud.card_fraud
payments_ops(4)            → use_case : payments.operations
settlement_risk(4)         → use_case : securities_services.custody.settlement [x-cut payments]
trading_risk(4)            → use_case : markets.market_risk [merge]
affordability(3)           → use_case : credit.underwriting.affordability
engagement(3)              → use_case : customer.engagement
hardship(3)                → use_case : credit.collections.hardship
persistency(3)             → use_case : insurance.lapse [merge]
segmentation(3)            → use_case : customer.segmentation
self_cure(3)               → use_case : credit.collections.self_cure
bancassurance(2)           → use_case : insurance.bancassurance
claims(2)                  → use_case : insurance.claims
deposit_attrition(2)       → use_case : customer.deposit_attrition [FLAG §5]
ftp(2)                     → modelling_context : framework.ftp
interchange_optimisation(2)→ use_case : payments.merchant.interchange
irrbb(2)                   → modelling_context : framework.irrbb
lcr(2)                     → modelling_context : framework.lcr
lgd(2)                     → measure : lgd
mandate_compliance(2)      → use_case : asset_management.mandate_compliance
primacy_loss(2)            → deprecated : customer.churn (stage)
sanctions(2)               → use_case : financial_crime.aml.sanctions
sme_credit(2)              → use_case : credit.underwriting.sme
structuring(2)             → use_case : financial_crime.aml.structuring
supply_chain_finance(2)    → use_case : corporate_trade.supply_chain_finance
underwriting(2)            → use_case : credit.underwriting
whitespace(2)              → use_case : customer.cross_sell.whitespace
workout(2)                 → use_case : credit.collections.workout
app_scam(1)                → use_case : financial_crime.fraud.app_scam
aum_stability(1)           → use_case : asset_management.redemption.aum_stability
authorised_push_payment(1) → deprecated : financial_crime.fraud.app_scam (merge)
basis_risk(1)              → use_case : deposits_alm.basis_risk [x-cut markets]
campaign_analytics(1)      → use_case : customer.campaign
cash_management(1)         → use_case : deposits_alm.cash_management [x-cut corporate]
cashflow(1)                → deprecated : credit.underwriting.affordability
cashflow_risk(1)           → use_case : deposits_alm.liquidity.cashflow_risk
claims_fraud(1)            → use_case : insurance.claims.claims_fraud
contactability(1)          → deprecated : credit.collections (signal)
corporate_actions(1)       → use_case : securities_services.custody.corporate_actions
correspondent_banking(1)   → use_case : financial_crime.aml.correspondent
cost_efficiency(1)         → flagged : ? (operational)      [FLAG §5]
credit_mitigation(1)       → use_case : credit.monitoring.credit_mitigation
credit_seasoning(1)        → use_case : credit.underwriting.seasoning
cross_border(1)            → use_case : payments.cross_border
crypto(1)                  → flagged : financial_crime.aml.crypto? [FLAG §5]
data_quality(1)            → measure : data_quality
distribution(1)            → flagged : customer.campaign? insurance? [FLAG §5]
exposure_management(1)     → use_case : credit.monitoring.exposure_management [x-cut markets]
frtb(1)                    → modelling_context : framework.frtb
fund_administration(1)     → use_case : securities_services.fund_administration
fund_flows(1)              → use_case : asset_management.redemption.fund_flows
fund_liquidity(1)          → use_case : asset_management.redemption.fund_liquidity
fund_performance(1)        → use_case : asset_management.performance
kyc(1)                     → use_case : financial_crime.aml.kyc
margin(1)                  → use_case : markets.counterparty_risk.margin
nsfr(1)                    → modelling_context : framework.nsfr
overdraft_propensity(1)    → use_case : customer.overdraft_propensity
physical_risk(1)           → use_case : esg.climate.physical
portfolio_risk(1)          → use_case : markets.market_risk.portfolio [merge]
receivables_finance(1)     → use_case : corporate_trade.receivables_finance
reinsurance(1)             → use_case : insurance.reinsurance
screening(1)               → use_case : financial_crime.aml.screening
securities_lending(1)      → use_case : securities_services.securities_lending
surrender(1)               → use_case : insurance.lapse.surrender [merge]
sustainable_finance(1)     → deprecated : esg.scoring (merge)
synthetic_id(1)            → use_case : financial_crime.fraud.synthetic_id
tracking_error(1)          → measure : tracking_error
unbundling(1)              → deprecated : customer.churn (stage)
wealth_outflow(1)          → deprecated : customer.deposit_attrition
xva(1)                     → measure : xva
```

---

## 5. Contentious calls — your domain judgment, please

1. **`financial_crime` as a parent domain** (fraud + aml + transaction_monitoring under it) vs. `fraud` and `aml` as two independent top-level families. I chose the parent because "financial crime" is a real org unit and it lets `transaction_monitoring` sit above both — but you may run fraud and AML as entirely separate books.
2. **`deposit_attrition` under `customer`** (a customer-behaviour objective) vs. under `deposits_alm` (a balance-sheet objective). I put it with churn because the *feature* is customer behaviour; the *consumer* may be treasury.
3. **`transaction_monitoring` as one shared leaf** vs. split into `fraud.monitoring` + `aml.monitoring`. It genuinely spans both; one node keeps it simple but blurs which regime governs.
4. **`crypto`** — AML crypto-laundering (`financial_crime.aml.crypto`) vs. a markets/product crypto family vs. its own top-level. Only one recipe carries it, so low stakes, but the placement signals intent.
5. **`pricing` and `cost_efficiency`** — is `pricing` a first-class use-case family, or is it really a *modelling context/lens* applied across retail/credit/payments? And is `cost_efficiency` a modelling objective at all, or an operational metric that shouldn't be a use-case?
6. **The deprecations** (`primacy_loss`, `unbundling`, `wealth_outflow`, `contactability`, `cashflow`) — I'm folding these into parent use-cases as *funnel-stage/signal* metadata rather than keeping them as selectable use-cases. Confirm that's right, or name any you want promoted to real leaves.
7. **Cross-cutting risk tags** (`concentration_risk`, `exposure_management`, `basis_risk`, `margin`) straddle credit and markets. I assigned each a *primary* home; the crosswalk can also record a secondary. Confirm the primaries or move them.
