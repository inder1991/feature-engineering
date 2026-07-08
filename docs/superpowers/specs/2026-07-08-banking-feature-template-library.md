# Banking Feature Template Library (SME-authored)

**Status:** draft for review · **Date:** 2026-07-08 · **Author stance:** banking SME.
**What this is:** the B2 *content* — a comprehensive, **parametric, safe-by-construction** template
library (the "cookbook"). Templates **seed** generation; the LLM extends beyond them and un-templated
requests still work (scaffold-not-cage). These are **expert-curated / conventional** patterns — **not**
data-proven (no data plane); the golden set is the quality bar.

## Template schema (every entry uses this)

```
id            snake_case unique
family        which pattern group
intent        one-line business meaning
computes      the logic, with {parameters} and {column roles}
needs         required concept(s)/entity — the grounding contract (what columns it binds to)
params        {p} ∈ {allowed} (default)
grain         one value per {entity} per as_of
pit           point-in-time rule (leakage-safety BAKED IN — only pre-as_of data)
add           additivity of the OUTPUT: additive | semi | non | n/a
eligibility   sensitivity/regulatory note (e.g. never bind a protected_attribute)
explain       H / M / L (interpretability — H required for credit/pricing symbolic mode)
use           primary use-cases
```
> **Global PIT rule (applies to ALL):** a template binds only to columns whose values are knowable
> **before `as_of`**, over a **trailing** window `(as_of − {window}, as_of]`; it may NEVER read the
> target's `label_column` or its `source_columns` (leakage). Windows are trailing, never forward.

---

# PART A — Cross-cutting families (the workhorses, reused everywhere)

## A1. Recency, frequency, monetary (RFM) & inter-event

- **`recency_days`** — time since the last event. computes: `as_of − max({event_ts} < as_of)`.
  needs: an `event_timestamp` on {entity}. grain: per {entity} per as_of. pit: last event strictly
  before as_of. add: n/a. explain: H. use: churn, engagement, collections, fraud(dormancy).
- **`event_frequency`** — count of events in a trailing window. computes: `count({event} in window)`.
  params: window ∈ {7,30,90,180,365}d. needs: event on {entity}. pit: trailing. add: additive.
  explain: H. use: churn, cross-sell, AML(activity), fraud.
- **`monetary_sum`** — total value in window. computes: `sum({monetary_flow} in window)`. needs: a
  `monetary_flow` (+ `currency_code` — convert to base first). add: additive. explain: H. use: CLV,
  cross-sell, AML, credit-affordability.
- **`monetary_avg`** / **`monetary_max`** — mean / peak flow in window. add: n/a. use: pricing, credit.
- **`rfm_composite`** — the classic RFM score. computes: percentile-binned combine of `recency_days`,
  `event_frequency`, `monetary_sum`. explain: H. use: churn, cross-sell, segmentation, CLV.
- **`inter_event_gap_mean`** / **`inter_event_gap_std`** — regularity of behaviour. computes: mean/std of
  gaps between consecutive events in window. needs: event_timestamp. explain: M. use: churn (salary
  irregularity), fraud (bursty), AML (structuring cadence).
- **`event_recency_trend`** — is activity accelerating or decaying. computes: ratio of count in recent
  half-window vs prior half-window. explain: M. use: churn (decay), fraud (ramp-up).

## A2. Rolling aggregates & trends (time-series over an entity)

- **`rolling_sum`** / **`rolling_avg`** / **`rolling_min`** / **`rolling_max`** — window aggregate of a
  numeric column. params: window, agg. needs: a numeric column + timestamp. add: sum→additive,
  others n/a. explain: H. use: universal.
- **`rolling_std`** / **`volatility`** — dispersion in window. computes: std({col} in window). use:
  markets(vol), credit(income volatility), fraud(anomaly baseline).
- **`trend_slope`** — direction of a series. computes: OLS slope of {col} vs time over window.
  explain: H (monotone). use: churn(balance decay), credit(deteriorating), CLV(growth).
- **`pct_change`** / **`growth_rate`** — relative change. computes: `({col}@as_of − {col}@as_of−win)/…`.
  explain: H. use: deposit growth, spend growth.
- **`ma_crossover`** — short vs long moving-average signal. computes: `rolling_avg(short) −
  rolling_avg(long)`. params: short<long windows. use: markets, deposit-flow regime.
- **`seasonality_deviation`** — deviation from the entity's own day-of-week/month pattern. explain: M.
  use: fraud(off-pattern), cash-flow forecasting.
- **`streak_length`** — consecutive periods meeting a condition. computes: longest run where {col}
  {op} {threshold} in window. use: credit(consecutive months in credit), churn(consecutive dormant).

## A3. Balance / stock behaviour (semi-additive)

- **`balance_end_of_period`** — latest balance as-of. add: semi. use: universal.
- **`balance_avg`** / **`balance_min`** / **`balance_max`** — window stats of a `monetary_stock`.
  use: credit(min balance), churn(draining).
- **`balance_trend`** — slope of a `monetary_stock` over window (the salary/churn workhorse). eligibility:
  bind a `monetary_stock`, not a flow. explain: H. use: churn, deposit attrition, early-warning.
- **`days_below_threshold`** — days the balance sat under a floor. computes: count(days {stock} <
  {threshold} in window). params: threshold. use: overdraft propensity, churn, hardship.
- **`balance_volatility`** — std of daily balance / mean. use: cash-flow risk, SME credit.
- **`drawdown_depth`** — peak-to-trough drop in window. use: markets, liquidity stress.

## A4. Ratios & cross-features (non-additive — compute per row, never sum)

- **`utilization_ratio`** — used vs limit. computes: `{drawn}/{limit}`. add: non. explain: H. use:
  credit(card utilisation), early-warning, pricing.
- **`debt_to_income`** — obligations vs income. computes: `sum({obligations})/{income}`. eligibility:
  income is sensitive; permitted for credit. use: credit_origination, affordability.
- **`loan_to_value`** — exposure vs collateral. computes: `{exposure}/{collateral_value}`. use:
  mortgage, secured lending, IFRS9-LGD.
- **`inflow_outflow_ratio`** — credits vs debits in window. use: cash-flow, SME credit, churn.
- **`fee_to_balance`** / **`interest_coverage`** — profitability/stress ratios. use: pricing, CLV,
  early-warning.
- **`payment_to_due_ratio`** — paid vs scheduled. computes: `sum(payments)/sum(due)` in window. use:
  collections, behavioral credit, delinquency.

## A5. Categorical, mix & diversity

- **`category_count_distinct`** — variety used. computes: `count(distinct {category_code} in window)`.
  use: merchant-category diversity (fraud/AML), product breadth.
- **`category_share`** — concentration in a category. computes: share of events/amount in {category}.
  use: channel preference, spend mix, AML(cash share).
- **`preferred_category`** — modal category in window (target-encode, don't one-hot high-cardinality).
  explain: H. use: next-best-action, channel routing.
- **`herfindahl_concentration`** — HHI over a categorical distribution. use: revenue concentration,
  counterparty concentration risk.

## A6. Entity aggregation — children → parent, and group hierarchy (§1.9)

- **`child_count`** — number of child entities. computes: `count({child} of {parent})`. e.g. accounts
  per customer, transactions per account. add: additive up the tree. use: engagement, exposure.
- **`child_amount_sum`** / **`child_amount_avg`** — aggregate a child metric to the parent. pit: child
  rows pre-as_of. use: customer-level spend, group-level revenue.
- **`group_exposure_sum`** — **combined exposure across a corporate group** (sum lending + trade + SCF +
  derivatives up the `part_of_group` hierarchy). needs: exposure + group edges. use: early-warning,
  limit-setting, concentration. *(Corporate-critical — a subsidiary's risk needs the group total.)*
- **`product_breadth`** — distinct product families held. use: share-of-wallet, cross-sell, churn.
- **`share_of_wallet_proxy`** — held products vs the catalog of eligible products. use: cross-sell, CLV.

## A7. Tenure, lifecycle & vintage

- **`tenure_days`** — age of the relationship/account. computes: `as_of − {origination_date}`. explain:
  H. use: churn, credit(seasoning), pricing.
- **`months_on_book`** — for credit behavioural scoring. use: PD-behavioral, IFRS9.
- **`time_to_maturity`** — for lending/markets. computes: `{maturity_date} − as_of`. use: prepayment,
  ALM, markets.
- **`lifecycle_state_at`** — the product's state as-of (origination/active/delinquent/…). needs:
  `lifecycle_state` + valid history. use: gating any downstream feature; collections.

## A8. Distributional, peer-relative & anomaly

- **`percentile_in_peer_group`** — rank within a segment. computes: percentile of {metric} within
  {segment} as-of. eligibility: segment must not be a protected class. explain: M. use: pricing,
  credit, anomaly.
- **`zscore_vs_segment`** — deviation from segment mean. use: fraud, early-warning.
- **`zscore_vs_own_history`** — deviation from the entity's own baseline (anomaly). computes: `({col}@as_of
  − rolling_avg)/rolling_std`. explain: M. use: fraud (spend spike), AML (out-of-pattern).
- **`novelty_flag`** — first-seen {attribute} for this entity (new merchant/country/device). use: fraud,
  AML. eligibility: geolocation is a proxy — flag, don't use as a credit input.

## A9. Primacy / relationship-outflow (money moving to a competitor) — needs a DERIVED intermediate

Signals that a customer is quietly relocating their primary relationship — a top-tier pre-attrition
indicator. **Distinctive because the key flag is not in the data — it must be derived** (see Part D.8).

- **`external_own_transfer_trend`** — rising transfers of the customer's OWN money to their accounts at
  OTHER banks. **derive:** `is_own_external_transfer := name_match(customer.name, beneficiary_name) ≥
  {threshold} AND beneficiary_bank ≠ home_bank` *(computed **downstream** — no data plane here)*.
  **computes:** growth of {amount|count} of `is_own_external_transfer`, recent window vs baseline.
  needs: `transactions.beneficiary_name` + `beneficiary_bank` + amount + timestamp; `customer.name`;
  {customer}. params: window · baseline · measure · `match_method ∈ {exact,token,fuzzy}` ·
  `match_threshold`. pit: trailing. add: n/a. **eligibility:** uses `customer_name` + `beneficiary_name`
  → PII entity-resolution — consent/purpose/residency REQUIRED. **match-risk:** probabilistic — false-pos
  (same name), false-neg (initials/order/joint accounts) → DECLARE method+threshold; `explain: M`.
  use: retail_churn, deposit_attrition, primacy_loss, wealth_outflow.
- **`external_outflow_growth`** *(fallback when no name to match)* — growth of ALL external outflows.
  Weaker + **FLAGGED** (includes third-party payments — noisier). use: same, as a proxy.
- **`salary_diversion_flag`** — inbound salary credit stops/shrinks while an external own-transfer rises.
  use: primacy_loss (the strongest variant — losing the salary is losing the relationship).

---

# PART B — Domain-specific templates

## B1. Churn / attrition
- **`dormancy_days`** — recency of any financial activity (the churn signal). = `recency_days` on
  transactions. use: retail_churn, deposit_attrition.
- **`balance_decline_slope`** — `balance_trend` over 90d (declining = churn risk). use: churn.
- **`engagement_decay`** — `event_recency_trend` on logins/txns. use: churn, advisor_churn.
- **`salary_irregularity`** — `inter_event_gap_std` on salary-credit events. use: retail_churn.
- **`product_attrition`** — drop in `product_breadth` vs prior period. use: churn, share-of-wallet.

## B2. Credit risk (application, behavioural, IFRS9)
- **`max_dpd_in_window`** — worst days-past-due. computes: `max({dpd} in window)`. explain: H. use:
  behavioral PD, IFRS9 staging, early-warning.
- **`delinquency_count`** — number of delinquent periods in window. use: PD, collections.
- **`times_over_limit`** — count of over-limit events. use: card PD, early-warning.
- **`worst_status_in_window`** — worst `lifecycle_state` reached. use: staging, PD.
- **`utilisation_trend`** — `trend_slope` of utilisation (rising = stress). use: early-warning.
- **`payment_ratio_avg`** — `payment_to_due_ratio` averaged. use: behavioral PD, cure probability.
- **`income_volatility`** — `rolling_std` of income proxy. eligibility: income sensitive (credit-permitted).
- **`bureau_recent_inquiries`** — count of credit inquiries in window (from `credit_bureau`). use:
  application PD, application_fraud.
- **`ltv_at_origination`** / **`dti_at_origination`** — snapshot ratios. use: origination PD, pricing.

## B3. Fraud (card, account-takeover, application)
- **`txn_velocity`** — count/amount in a very short window (mins/hours). params: window ∈ {1h,24h}.
  explain: M. use: card_fraud_realtime, ATO.
- **`amount_zscore`** — `zscore_vs_own_history` on transaction amount. use: card fraud.
- **`new_beneficiary_flag`** — first payment to this payee. = `novelty_flag`. use: app_scam, ATO.
- **`geo_velocity_impossible`** — two txns whose distance/time implies impossible travel. eligibility:
  geolocation — fraud-permitted, flagged. use: card fraud, ATO.
- **`device_change_flag`** — txn from a `novelty` device. use: ATO.
- **`time_since_credential_change`** — recency of password/contact change before a high-risk action.
  use: ATO, app_scam.
- **`application_velocity`** — many applications sharing an attribute (email/phone/device) in window.
  use: application_fraud, synthetic_identity.

## B4. AML (typology-driven)
- **`structuring_score`** — many transactions just under a reporting threshold. computes: `count(txn in
  ({threshold}−δ, {threshold}) in window)`. params: threshold (jurisdiction). use: aml_txn_monitoring.
- **`rapid_movement_ratio`** — funds in then out within N days. computes: `outflow within {N}d of
  inflow / inflow`. use: layering, mule_detection.
- **`round_amount_share`** — share of round-number transactions. use: TBML, structuring.
- **`cash_intensity`** — cash share of volume. = `category_share(cash)`. use: aml risk rating.
- **`network_degree`** — number of distinct counterparties (from `relationship_edge`). use: mule rings.
- **`shortest_path_to_flagged`** — graph distance to a known-bad node. use: aml, sanctions proximity.
- **`high_risk_geo_share`** — share of flows to/from high-risk jurisdictions. use: aml, sanctions.

## B5. Cross-sell / propensity / CLV
- **`product_gap_flag`** — eligible product not yet held (from `product_master` − held). use:
  propensity_cross_sell, next_best_action.
- **`channel_engagement`** — `event_frequency` per channel. use: propensity, NBA.
- **`revenue_trend`** — `trend_slope` of fee+interest income. use: CLV, pricing.
- **`life_event_proxy`** — pattern shift suggesting a life event (salary jump, large inflow). use:
  cross-sell (mortgage/wealth). eligibility: no protected inference.

## B6. Collections & recoveries
- **`roll_rate_signal`** — movement to a worse delinquency bucket vs prior period. use:
  collections_prioritization, roll_rate.
- **`promise_kept_ratio`** — kept payment promises / made (from interaction history). use: cure prob.
- **`right_party_contact_rate`** — successful contacts / attempts. use: contactability.
- **`balance_at_risk`** — `monetary_stock` outstanding × delinquency severity. add: semi. use: recovery.

## B7. Deposit / liquidity / treasury (ALM)
- **`deposit_beta_proxy`** — deposit-rate sensitivity: `pct_change(balance)` vs benchmark-rate change.
  use: deposit_beta_modeling, pricing.
- **`nmd_stability`** — `balance_volatility` of non-maturity deposits. use: nmd_behaviouralization, LCR.
- **`net_flow_trend`** — `trend_slope` of `inflow_outflow_ratio`. use: liquidity projection.
- **`concentration_by_depositor`** — `herfindahl_concentration` of balances. use: liquidity risk.

---

# PART C — Coverage matrix (family × use-case)

| Family \ Use-case | churn | credit | fraud | AML | cross-sell | collections | treasury |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| A1 RFM/recency | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | · |
| A2 rolling/trend | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| A3 balance/stock | ✓ | ✓ | · | ✓ | · | ✓ | ✓ |
| A4 ratios | · | ✓ | · | · | ✓ | ✓ | ✓ |
| A5 categorical/mix | ✓ | · | ✓ | ✓ | ✓ | · | · |
| A6 entity/group agg | ✓ | ✓ | ✓ | ✓ | ✓ | · | ✓ |
| A7 tenure/lifecycle | ✓ | ✓ | · | · | ✓ | ✓ | ✓ |
| A8 distributional/anomaly | · | ✓ | ✓ | ✓ | · | ✓ | · |
| B-domain specific | B1 | B2 | B3 | B4 | B5 | B6 | B7 |

# PART D — Authoring rules & safety (SME notes)

1. **PIT everywhere** — trailing windows only; never bind the target's label/source columns. The engine
   rejects a template whose grounding would touch them.
2. **Additivity honoured** — a template's `add` field drives valid roll-ups; never sum a `semi`/`non`.
3. **Currency** — any cross-currency aggregate converts to a base currency first (point-in-time fx).
4. **Eligibility** — never bind a `protected_attribute`; `geographic`/income are flagged and
   use-case-gated (credit-permitted-with-care, blocked as a proxy where fair-lending applies).
5. **Explainability** — credit/pricing (`symbolic` mode) require `explain: H` templates (monotone,
   inspectable); reject low-explainability templates for those use-cases.
6. **Scaffold-not-cage** — this library SEEDS generation; the LLM composes/adapts/extends and handles
   un-templated requests. Grow the library from curated + flywheel-approved patterns.
7. **Not proven** — these are expert-curated/conventional patterns; quality is gated by the golden set,
   never claimed as data-validated.
8. **Derived intermediates + no-data-plane matching.** Some features need a flag the raw catalog does
   NOT contain and that must be **derived** — e.g. an *own-account* flag from `name_match(customer.name,
   beneficiary_name)` (§A9). Rules: (a) the template **specifies** the derivation (method + threshold) but
   the platform **cannot run it** (no data plane) — the match executes **downstream**; here it is a
   *declared* step. (b) Such derivations are **probabilistic** (entity resolution: false-pos same-name,
   false-neg initials/order/joint-accounts) → `explain: M`, declare method+threshold, and the feature's
   quality depends on the downstream matcher. (c) Name/beneficiary matching is **PII entity-resolution** →
   consent/purpose/residency eligibility REQUIRED, not optional.

# PART E — Open / to-deepen
Markets desk templates (greeks, XVA, PnL-attribution), insurance (lapse/claims), securities-services
(settlement-fail), ESG (carbon-intensity trend) — named here, to be authored as those domains are
prioritised. Coverage grows per-domain via curation, not one big freeze.
