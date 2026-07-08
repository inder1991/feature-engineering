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
`portfolio` · `book` (trading) · `collateral` · `lien` · `overdraft` · `facility` · `contingent_exposure`
(LCs/guarantees) · `receivable` · `payable` · `leased_asset` · `pooling_structure` · `mandate` (capital
markets) · `hedge`

#### 1.2a Corporate / SME product families (the multi-product relationship)
A retail customer holds a handful of products; a **corporate/SME customer holds a *portfolio* of
products across families** — and that breadth *is* the relationship. Each product instance grounds to a
facility / position / account / exposure entity.

| Product family | Products | Grounds to |
|---|---|---|
| **Lending** | term loan · revolving credit facility (RCF) · overdraft · working-capital loan · asset-based lending · syndicated / bilateral loan · bridge · commercial real-estate finance · leveraged / acquisition finance | `facility` + `exposure` (+ `collateral`) |
| **Trade finance** | documentary letter of credit · standby LC / bank guarantee · documentary collection · import/export loan · bill discounting | `contingent_exposure` / trade `instrument` |
| **Supply-chain finance** | payables finance (reverse factoring) · receivables finance / factoring · invoice discounting · dynamic discounting · distributor/dealer finance | `facility` backed by `receivable` / `payable` |
| **Cash management / transaction banking** | operating / collection / disbursement accounts · notional & physical **cash pooling** · sweeps · virtual accounts · bulk & cross-border payments · liquidity mgmt | `account` + `pooling_structure` |
| **Markets / treasury (hedging & investment)** | FX (spot/forward/swap/option) · interest-rate derivatives (IRS/cap/floor) · commodity hedging · MM deposits · repo | `position` / `derivative` / `hedge` |
| **Asset finance / leasing** | equipment finance · fleet / vehicle leasing · finance & operating lease · hire purchase | `facility` + `leased_asset` |
| **Capital markets (larger corp)** | bond issuance (DCM) · equity issuance (ECM) · private placement · securitisation | `mandate` / markets `instrument` |
| **Guarantees & contingent** | performance bond · bid bond · advance-payment guarantee | `contingent_exposure` |
| **Merchant services** | card acquiring · payment gateway · POS | `merchant` account |

> **SME lens:** even a *small* SME typically holds a bundle — business current account + overdraft +
> business loan + **invoice finance** + **asset finance** + business cards + (if importing/exporting)
> **trade finance** + **FX hedging**. So corporate/SME banking is inherently **multi-product per
> relationship**, which drives its signature features: **product breadth**, **share of wallet**,
> **combined cross-product exposure** (a group's true risk sums lending + trade + derivatives + SCF),
> and **cross-sell propensity** (SMEs are the prime cross-sell segment). Combined exposure especially
> must aggregate across product families *and* up the group hierarchy (§1.9).

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

### 1.10 Insurance / bancassurance
`policy` · `claim` · `premium` · `beneficiary` · `underwriting_case` · `reinsurance_treaty` ·
`lapse_surrender_event` · `annuity`

### 1.11 Custody & securities services (asset servicing)
`custody_holding` · `safekeeping_account` · `corporate_action` · `settlement_instruction` · `fund` ·
`nav_record` · `securities_loan`

### 1.12 Regulatory / finance / accounting
`gl_account` (general ledger) · `cost_center` · `rwa_bucket` · `capital_component` (CET1/AT1/T2) ·
`provision_stage` (IFRS9 stage 1/2/3) · `impairment` · `regulatory_report` · `stress_scenario`

### 1.13 Reference / market data & tax
`instrument_reference` · `product_master` · `index_benchmark` · `rating_agency` · `fx_rate` ·
`curve` (have) · `tax_lot` · `withholding` · `fatca_crs_classification`

### 1.14 Data governance & fee/billing
`consent` (extends `consent_record`) · `data_sharing_agreement` · `purpose` · `source_system` (lineage)
· `data_quality_flag` · `fee_schedule` · `pricing_tier` · `rebate`

### 1.15 Two cross-cutting entity dimensions
- **Lifecycle state** — every product/account/facility carries a `lifecycle_state`
  (origination → active → delinquent → default → restructured → closed/written-off). Features condition
  on it; targets often *are* transitions between states (e.g. active → default).
- **Reference vs. transactional** — banking separates slowly-changing **reference/master** data
  (`product_master`, `instrument_reference`, customer static) from time-stamped **event/transactional**
  data. Different point-in-time semantics: a reference value is *"current as-of"* (bi-temporal, §3.13);
  an event is dated at occurrence.

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
business lending · **trade finance** · **supply-chain finance** · **asset finance / leasing** · cash
management / transaction banking · treasury / FX · merchant acquiring.
*Use-cases:* `sme_credit_risk` (cash-flow-based), `working_capital_need`, `invoice_finance_risk`,
`supply_chain_finance_limit`, `trade_finance_fraud`, `product_breadth_cross_sell`,
`combined_exposure_early_warning`, `covenant_breach_prediction`, `fx_hedging_propensity`,
`asset_finance_default`, `merchant_attrition`, `cash_flow_forecast`.

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
*Typologies (the real `outcome_label`s):* AML — `structuring`/smurfing, `layering`,
`trade_based_ml`, `rapid_movement`, `round_tripping`; Fraud — `app_scam` (authorized push payment),
`synthetic_identity`, `first_party_fraud`, `mule_account`, `card_not_present`, `friendly_fraud`.

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

### 2.15 Insurance / bancassurance
life · general (P&C) · credit protection · annuities.
*Use-cases:* `lapse_surrender_prediction`, `persistency`, `claims_fraud`, `underwriting_risk`,
`protection_cross_sell`.

### 2.16 Custody & securities services (asset servicing)
custody · fund administration · corporate actions · securities lending.
*Use-cases:* `settlement_fail_prediction`, `corporate_action_risk`, `securities_lending_demand`,
`nav_error_detection`.

### 2.17 Asset management (buy-side)
*Use-cases:* `fund_redemption`, `flow_forecast`, `mandate_compliance_breach`, `style_drift`.

### 2.18 Islamic banking
Sharia-compliant parallel products (Murabaha, Ijara, Mudaraba, Sukuk, Takaful).
*Use-cases:* `sharia_compliance_check`, `profit_rate_pricing`, `islamic_deposit_attrition`.

### 2.19 ESG / sustainable finance
green bonds · sustainability-linked loans (SLL) · climate & ESG risk.
*Use-cases:* `esg_scoring`, `greenwashing_risk`, `climate_transition_risk`, `physical_climate_risk`,
`sll_kpi_tracking`.

### 2.20 Payments as a business (beyond cards)
real-time/instant payments · correspondent banking · cross-border / remittance · open banking / BaaS.
*Use-cases:* `rtp_fraud`, `correspondent_risk`, `remittance_aml`, `open_banking_tpp_risk`.

> **Depth notes.** Credit-risk (§2.8) is a **lifecycle** (origination → behavioural → early-warning →
> collections → recovery → write-off) over the **Basel/IFRS9** apparatus (PD/LGD/EAD/RWA, staging, IRB,
> stress CCAR/EBA — §3.16). Markets (§2.5) adds `xva` (CVA/DVA/FVA), `greeks`, `settlement_fail`,
> `margin` depth.

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
| `monetary_stock` *(is-a monetary)* | **semi-additive** (sum across entities; latest over time) | balance, exposure, position value, collateral value, limit, AUM, receivable, payable |
| `contingent_exposure` *(is-a monetary_stock)* | semi-additive; **off-balance-sheet** — converts on drawdown (credit-conversion-factor) | undrawn facility, LC / guarantee amount, committed line |
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

### 3.13 Bi-temporal time (P0 — correctness)
Every fact has **two** time axes: `valid_time` (the date it's *about* — as_of/effective) and
`system_time` (the date it was *recorded/known* — a.k.a. knowledge/transaction time). Plus `value_date`,
`booking_date`, `business_day_convention`, `reporting_period`.
**Rule:** a leakage-safe feature uses only rows where **both** `valid_time ≤ as_of` **and**
`system_time ≤ as_of` — the second condition drops values *restated later*, which you did not actually
know at prediction time. Today the taxonomy has only valid-time; `system_time` is the missing axis.

### 3.14 Currency / FX consistency (P0 — correctness)
`currency_code` (unit) · `base_currency` vs `local_currency` · `fx_conversion_rate` · `cross_rate`.
**Rule (like additivity):** never aggregate across currencies without first converting to a base
currency via a **point-in-time** fx_rate. Mixing USD + EUR in one sum is a wrong number.

### 3.15 Data eligibility (P0 — compliance)
Beyond `pii`/`protected_attribute`: `data_purpose` · `consent_status` · `retention_class` ·
`data_residency` · `special_category` (GDPR: health/biometric).
**Rule:** a feature is *eligible* only if every input satisfies consent + purpose + residency +
retention — a first-class check **alongside** leakage and fair-lending, not a substitute.

### 3.16 Regulatory capital & accounting (P0 — the spine)
Capital: `risk_weight` · `rwa` · `capital_ratio` *(is-a ratio)* · `ccf` (credit-conversion-factor) ·
`pd_ttc` / `pd_pit` · `downturn_lgd`. Accounting: `fair_value` · `amortised_cost` · `impairment_stage`
(IFRS9 1/2/3) · `accrual` · `provision_amount`. Rates/curves: `benchmark_rate` (SOFR/SONIA/€STR) ·
`tenor` · `discount_factor` · `haircut` · `advance_rate`.

### 3.17 ESG & compliance flags
`esg_score` · `carbon_intensity` · `green_flag` · `sharia_compliant_flag`.

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
- **Safety checks — ENFORCED vs. DECLARED (be honest; no data plane).**
  **Enforced on metadata (actually blocking, deterministic):** (1) **leakage** —
  `(catalog_source,object_ref)` set-membership of the target's label/source columns in the derives; (2)
  **eligibility** — the derives' consent/purpose/residency/fair-lending/`protected_attribute` tags
  (§3.15); (3) **currency-mismatch** — derives carry different `currency_code` (§3.14); (4)
  **additivity** — summing a `monetary_stock`/rate where invalid.
  **Declared + flagged only (needs data we don't have — a *claim*, not a verified guarantee):**
  **point-in-time / bi-temporal** restatement (§3.13) and runtime currency-value mixing. `DESIGN-CHECKED`
  = *"passed the enforced + declared the rest."* The LLM never overrides an enforced check.
- Everything is **ratifiable + extensible per bank** and **grows via learning + curation** — this
  reference is the *seed of the seed*, not a fixed constant.

## 5. Open decisions

- **Depth vs. breadth to ship:** how many use-cases get *deep* (real targets + parametric templates)
  vs. *named-only* on day one. *Rec: deep for the top ~2 per domain; the rest onboarded/curated.*
- **Concept granularity:** how fine to split (e.g. `monetary_flow` vs. separate `fee`/`interest`).
  *Rec: keep the ~50 here; split further only where a distinct additivity/regulatory behaviour demands.*
- **Ontology store:** where the vocabulary/entities/domains live (DB-backed governed store, §8 of the
  Domain Intelligence spec) and the curation surface.
