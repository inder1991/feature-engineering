# Banking Taxonomy Reference — Entities · Domains · Vocabulary

**Status:** draft for review · **Date:** 2026-07-07 · **Companion to:** the Banking Domain Intelligence
spec (this is the *content* / foundation; that is the *design* / machinery).

Scope: a **universal / super-regional bank** (retail + commercial + corporate & investment + markets +
wealth + payments), synthesised to be comprehensive rather than the seed's 15-case stub. Everything here
is a **ratifiable default** — each bank adopts, prunes, and extends it (per the knowledge-base
lifecycle). Concepts carry **behaviour** (additivity · point-in-time role · sensitivity/regulatory ·
entity link) so the reasoning layer can actually use them.

---

## 1. Entities (the nouns)

### 1.1 Party / customer side
`customer` (retail individual) · `household` (retail grouping) · `customer_group` (corporate ultimate
parent) · `corporate_customer` · `business_customer` (SME sub-type) · `legal_entity` (LEI-identified) ·
`ultimate_beneficial_owner` · `beneficial_owner` · `director_signatory` · `counterparty` · `obligor` ·
`guarantor` · `related_party` · `prospect` · `issuer` (markets) · `broker_dealer` · `correspondent_bank`

> **Retail vs. corporate:** a retail `customer` is one person, one id. A **corporate customer is a
> hierarchy** (`customer_group → corporate_customer/subsidiary → legal_entity → account`), which is
> modelled as **relationships**, not flat nouns — see §1.9.

### 1.2 Account / product / position side
`account` (deposit/current/savings) · `loan_account` · `card_account` · `mortgage` · `credit_facility`
/ `line` · `sub_account` · `wallet` · `product` · `product_holding` · `position` (markets) ·
`portfolio` · `book` (trading) · `collateral` · `lien` · `overdraft`

### 1.3 Transaction / event side
`transaction` · `payment` (ACH/wire/SEPA/RTP) · `card_transaction` · `authorization` · `settlement` ·
`trade` · `order` · `execution` · `journal_entry` · `standing_order` / `direct_debit` · `disbursement`
/ `drawdown` · `repayment` · `fee_charge` · `interest_accrual` · `statement`

### 1.4 Credit / risk side
`exposure` · `facility` · `limit` · `rating` (internal/external) · `application` · `underwriting_decision`
· `guarantee` · `provision` / `ecl_bucket` · `delinquency_state` · `default_event` · `restructure` /
`forbearance` · `recovery` · `write_off`

### 1.5 Markets / treasury side
`instrument` / `security` · `derivative` · `notional_position` · `hedge` · `desk` · `funding_position` ·
`cashflow` · `curve` (rate/FX) · `benchmark` · `netting_set` · `margin_call`

### 1.6 Org / channel side
`branch` · `atm` · `terminal` · `channel` (mobile/web/branch/call-center) · `relationship_manager` /
`agent` · `employee` · `device` · `session` · `merchant` · `acquirer` · `campaign` · `offer`

### 1.7 Financial-crime / compliance side
`case` (AML/fraud) · `alert` · `sar` / `str` · `watchlist_entry` · `sanctions_entry` · `pep_record` ·
`kyc_profile` (retail) · `kyb_profile` (know-your-business, corporate) · `cdd_review` / `edd_review` ·
`ownership_chain` · `network_link` (transaction/ownership graph edge)

### 1.8 Servicing / interaction side
`interaction` · `complaint` · `dispute` / `chargeback` · `claim` · `service_request` · `consent_record`
· `document` (KYC/loan)

> **Design use:** identifiers in the vocabulary (§3.2) link to these entities; entities carry the
> **grain** and the **join graph**; cross-catalog reasoning matches the same entity across sources.

### 1.9 Entity relationships — the edges (model these, not just the nouns)

Nouns alone can't express corporate/wholesale banking; the **relationships between entities** are
first-class, because most wholesale value (and risk) lives in the edges.

**Corporate hierarchy** (central to commercial + corporate & investment banking):
```
customer_group  (ultimate parent)
   └─ part_of_group ─►  corporate_customer / subsidiary
        └─ operates_as ─►  legal_entity (LEI)
             └─ holds ─►  account / facility / position   (often across countries & currencies)
```
Edges: `parent_of` · `subsidiary_of` · `part_of_group` · `operates_as` · `holds`. These enable the key
corporate feature pattern — **group-level aggregation**: consolidated **group exposure**, **group
limit** utilisation, total **relationship value / revenue**, cross-subsidiary concentration. A flat
entity list *cannot* express *"aggregate everything under this ultimate parent"*; the hierarchy edges
are what make it possible.

**Ownership / control** (KYB · AML · sanctions): `owns` · `controls` edges to
`ultimate_beneficial_owner`, tracing the ownership chain through intermediate `legal_entity`s (often
opaque, multi-jurisdiction). Powers UBO features, control-percentage, sanctions/PEP proximity.

**Household** (retail): `member_of` edges grouping retail `customer`s → household-level features
(household balance, products-per-household).

**Counterparty / transaction network** (fraud · AML · markets): `transacts_with` · `shares_device` ·
`shares_account` · `guarantees` edges → **network features** (degree, community, shortest-path to a
flagged node, ring detection) — see §3.12 `relationship_edge`.

> **Design use:** these edges add three things a flat catalog lacks — (1) **hierarchical grain** (a
> feature computed *"per group"* aggregating all subsidiaries), (2) **graph features** (network
> position), and (3) **regulatory reach** (UBO/ownership for KYB/sanctions). The reasoning layer must
> treat relationships as groundable structure, not just tables.

---

## 2. Domains / business lines (the where) + representative use-cases

### 2.1 Retail / consumer banking
deposits & savings · consumer lending · **cards** · **mortgages / home lending** · overdrafts.
*Use-cases:* `retail_churn/attrition`, `deposit_balance_attrition`, `cross_sell_propensity`,
`next_best_action`, `overdraft_propensity`, `mortgage_prepayment`, `savings_goal_engagement`.

### 2.2 Wealth management / private banking
advisory · discretionary portfolio mgmt · trust & estate.
*Use-cases:* `advisor_churn`, `share_of_wallet`, `investment_propensity`, `suitability_flag`,
`portfolio_drift_alert`.

### 2.3 Commercial / business banking (SME + mid-corp)
business lending · cash management / treasury services · **trade finance** · merchant acquiring.
*Use-cases:* `sme_credit_risk`, `working_capital_need`, `merchant_attrition`, `trade_finance_fraud`,
`cash_flow_forecast`.

### 2.4 Corporate & investment banking (wholesale)
DCM/ECM origination · M&A advisory · syndicated lending · **prime brokerage** · securities services /
custody.
*Use-cases:* `deal_propensity`, `client_coverage_prioritization`, `credit_limit_optimization`,
`counterparty_credit_risk`.

### 2.5 Markets / trading (sales & trading)
rates · credit · FX · equities · commodities · structured products · derivatives.
*Use-cases:* `trade_surveillance` (spoofing/insider), `best_execution_analytics`, `flow_prediction`,
`liquidity_scoring`, `xva_pricing` (CVA/DVA/FVA).

### 2.6 Cards & payments
credit/debit cards · payment processing · merchant services · digital wallets.
*Use-cases:* `card_fraud_realtime`, `authorization_optimization`, `interchange_optimization`,
`installment_propensity`, `chargeback_prediction`.

### 2.7 Treasury / ALM
liquidity mgmt · funding · interest-rate risk (IRRBB) · FTP.
*Use-cases:* `deposit_beta_modeling`, `nmd_behavioralization` (non-maturity deposits),
`liquidity_stress_projection`, `prepayment_speed`.

### 2.8 Credit risk (regulatory modeling)
*Use-cases:* `pd_scoring` (application/behavioral), `lgd_modeling`, `ead_modeling`, `ifrs9_cecl_ecl`,
`stress_testing` (CCAR/EBA), `credit_origination`, `limit_setting`, `early_warning_signals`.

### 2.9 Market / counterparty / model risk
*Use-cases:* `var_backtesting`, `frtb_sensitivities`, `counterparty_exposure`, `wrong_way_risk`,
`model_performance_monitoring`, `climate_esg_risk_scoring`.

### 2.10 Financial crime & fraud
AML · KYC/CDD/EDD · sanctions · anti-bribery · fraud (application / transaction / **card** / **account
takeover** / first-party / synthetic-identity / authorized-push-payment).
*Use-cases:* `aml_transaction_monitoring`, `kyc_risk_rating`, `sanctions_screening_support`,
`sar_prioritization`, `application_fraud`, `account_takeover`, `app_scam_detection`, `mule_detection`.

### 2.11 Marketing / CRM / customer analytics
*Use-cases:* `churn`, `clv/ltv`, `segmentation`, `propensity/cross-sell/up-sell`, `campaign_response`,
`offer_optimization`, `channel_preference`, `sentiment`.

### 2.12 Collections & recoveries
*Use-cases:* `collections_prioritization`, `roll_rate_prediction`, `cure_probability`,
`settlement_propensity`, `recovery_amount`.

### 2.13 Pricing
*Use-cases:* `risk_based_pricing`, `deposit_pricing`, `fee_optimization`, `price_elasticity`,
`relationship_pricing`.

### 2.14 Servicing / operations
*Use-cases:* `complaint_prediction`, `dispute_outcome`, `contact_center_routing`, `call_deflection`,
`document_classification`, `nsf_prediction`.

> **Design use:** each use-case entry (Domain Intelligence spec §4) carries its precise `target`,
> `feature_templates`, `allowed/blocked_data_classes`, and `regulatory` flags — regulatory intensity
> rises sharply from marketing → credit/pricing → financial-crime.

---

## 3. Concept vocabulary (the what-kind-of-data — structured ontology)

Each concept carries: **additivity** (how it may aggregate), **PIT role**, **sensitivity/regulatory
class**, **entity link**. `is-a` edges shown where they aid generalisation.

### 3.1 Monetary
| Concept | Additivity | Examples |
|---|---|---|
| `monetary_stock` *(is-a monetary)* | **semi-additive** (sum across entities; latest over time) | balance, exposure, position value, collateral value, limit, AUM |
| `monetary_flow` *(is-a monetary)* | **fully additive** | transaction amount, payment, fee, interest paid/earned, drawdown, repayment, P&L, revenue |
| `monetary_rate` *(is-a monetary)* | **non-additive** | interest rate, coupon, APR, yield, spread |
| `price` *(is-a monetary)* | non-additive | instrument price, strike, NAV |
| `notional` *(is-a monetary)* | additive (gross) / netted | derivative notional |

### 3.2 Identifiers → entity links
`customer_id → customer` · `account_id → account` · `card_id → card_account` · `transaction_id →
transaction` · `application_id → application` · `product_id → product` · `facility_id → facility` ·
`instrument_id → instrument` · `counterparty_id → counterparty` · `merchant_id → merchant` · `lei →
legal_entity` · `branch_id → branch`. *(Role: join key + grain + entity.)*

### 3.3 Temporal (point-in-time critical)
`as_of_date` (decision reference) · `effective_date` (state start) · `origination_date` · `maturity_date`
· `trade_date` · `value_date` · `settlement_date` · `event_timestamp` · `duration_tenure` (days_since,
account_age, months_on_book) · `vintage` (cohort).

### 3.4 Quantities & risk metrics
`count` (num_transactions, logins) · `quantity_units` (shares, contracts) · `score_probability`
(credit_score, **PD**, risk_score — *flag leakage-risk when a model output*) · `rank_percentile` ·
`lgd` · `ead` · `ecl` · `var` · `sensitivity_greek` · `rating` (internal/external) · `dpd` (days past
due) · `beta` (deposit beta).

### 3.5 Categorical & coded
`category_code` · `product_type` · `account_type` · `transaction_type` · `channel` · `currency_code`
(unit — **cannot mix currencies in a sum**) · `country_code` · `industry_code` (NAICS/SIC) · `mcc`
(merchant category) · `instrument_type` · `lifecycle_state` / `status`.

### 3.6 Geographic (proxy-risk flagged)
`geographic` (zip, postcode, region, branch location) — **fair-lending proxy risk**; treat as a
protected-attribute *proxy* for credit/pricing.

### 3.7 Flags (boolean)
`boolean_flag` · `delinquency_flag` · `default_flag` · `fraud_flag` · `restructured_flag` ·
`sanctions_hit_flag` · `pep_flag`. *(Often a label — see 3.10.)*

### 3.8 Sensitive / regulatory
`pii` (email, phone, ssn, address, name, DOB) → read-scope · `protected_attribute` (age, gender, race,
ethnicity, marital status, national origin, religion) → **regulatory-blocked for credit/pricing
(ECOA/fair-lending)** · `special_category` (health, biometric — GDPR) · `kyc_document`.

### 3.9 Text & documents
`free_text` (memo, notes, complaint text) · `document_reference` · `unstructured_doc` (loan/KYC docs).

### 3.10 Labels / outcomes (leakage anchors)
`outcome_label` — *this IS a target*: churned, defaulted, charged_off, prepaid, fraud, converted,
complaint, roll (delinquency roll), recovery, mule. **The leakage anchor** — features must not be built
from these (or from their defining source columns; §5-leakage of the contract-flow spec).

### 3.11 Behavioural / digital
`event_type` · `session` · `clickstream` · `channel_usage` · `device_fingerprint` · `geolocation`
(digital, distinct from 3.6) · `login_event` · `page/app_event`.

### 3.12 Network / graph
`relationship_edge` — counterparty links, beneficial-ownership graph, transaction network, shared-device
/ shared-account rings (fraud/AML). *(Enables network features: degree, community, shortest-path to a
flagged node.)*

---

## 4. How this foundation is used (recap)

- **Concepts** drive deterministic reasoning: `monetary_stock` → *don't sum over time*; `currency_code`
  → *don't mix currencies*; `protected_attribute`/`geographic` → *block/flag for credit*; `outcome_label`
  → *leakage anchor*; identifiers → *joins + grain + entity*.
- **Entities + their relationships** carry grain, the join graph, cross-catalog matching, AND the
  **hierarchy/graph** (§1.9) — enabling group-level aggregation (per-parent), network features, and
  UBO/ownership reach that a flat noun list can't express.
- **Domains/use-cases** provide the fast path (target + templates + regulatory rules), and the
  regulatory intensity scales by domain.
- Everything is **ratifiable + extensible per bank** and **grows via learning + curation** — this
  reference is the *seed of the seed*, not a fixed constant.

## 5. Open decisions

- **Depth vs. breadth to ship:** how many use-cases get *deep* (real targets + parametric templates)
  vs. *named-only* on day one. *Rec: deep for the top ~2 per domain; the rest onboarded/curated.*
- **Concept granularity:** how fine to split (e.g. `monetary_flow` vs. separate `fee`/`interest`).
  *Rec: keep the ~50 here; split further only where a distinct additivity/regulatory behaviour demands.*
- **Ontology store:** where the vocabulary/entities/domains live (DB-backed governed store, §8 of the
  Domain Intelligence spec) and the curation surface.
