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

## B1. Churn / attrition — the attrition FUNNEL

Attrition is a process, not an event. Signals sit at stages: **earlier = more lead-time but noisier;
later = near-certain but too late (and near-label → leakage risk).** A good model blends stages.

```
DISSATISFACTION → DISENGAGEMENT → FINANCIAL MIGRATION → UNBUNDLING → DEPARTURE ⚠near-label
```

**Stage 1 — Dissatisfaction (leading, weak, most lead-time)**
- `complaint_recent_flag` — a complaint filed in window. needs: complaint/interaction records.
- `fee_reversal_then_balance_drop` — fee dispute followed by balance decline. needs: fee events+balance.
- `dispute_unresolved_count` — open disputes. `failed_contact_rate` — unresolved service contacts.

**Stage 2 — Disengagement (behavioural, early)**
- `digital_login_decline` — `trend_slope` of logins (falling). needs: session events.
- `channel_abandonment` — stopped using a previously-used channel. needs: channel-tagged activity.
- `comms_disengagement` — stopped opening statements / unsubscribed. needs: comms open events.
- `engagement_decay` — `event_recency_trend` on logins/txns. `product_usage_decline` — fewer features used.

**Stage 3 — Financial migration (mid, STRONG — the money is moving)**
- `salary_cessation_flag` / `salary_decline_trend` — inbound salary stops/shrinks. needs: credit txns +
  salary tag. eligibility: income sensitive. `salary_irregularity` — `inter_event_gap_std` on salary.
- `external_own_transfer_trend` — own money → competitor (§A9). PII entity-resolution.
- `card_spend_decline_trend` — `trend_slope` of card volume. needs: card txns.
- `share_of_spend_decline` — this bank's spend ÷ total known spend. needs: external spend view.
- `net_inflow_decline` — total credits falling. `deposit_runoff` — term deposits maturing, not renewed.
- `balance_decline_slope` — `balance_trend` over 90d (the core drain signal).

**Stage 4 — Unbundling (late, STRONG — dismantling the relationship)**
- `direct_debit_cancellation_rate` — DDs (utilities/mortgage) cancelled. needs: DD/mandate data. *(strong
  — sticky "furniture" leaving.)*
- `standing_order_redirection` — SOs redirected external. needs: SO data + beneficiary. PII.
- `product_closure_count` — products closed. `tier_downgrade_flag` — premium→basic.
- `product_attrition` — drop in `product_breadth`. `mortgage_redemption_signal` — early redemption
  (remortgage elsewhere?).

**Stage 5 — Departure ⚠ (NEAR-LABEL — high leakage risk, usually FLAG/REJECT)**
- `account_switch_service_flag` (CASS) — a formal switch request. **⚠ almost the outcome itself → the
  3-part leakage control must flag/reject** (else the model predicts churn using churn).
- `full_balance_withdrawal_flag` — account emptied. **⚠ near-label — flag.**

**Composite**
- `relationship_erosion_score` — weighted blend, **weighted by lead-time × strength**; keep inspectable
  (`explain: H`) so a human sees which stage fired. `dormancy_days` = `recency_days` (the baseline signal).

> **Two funnel rules:** (1) **lead-time vs strength is a trade-off** — blend stages, don't rely on one;
> (2) **the bottom of the funnel is a leakage trap** — Stage-5 signals are *almost the label*; flag/reject
> (the sharper cousin of the `days_since_last_txn` case).

## B2. Credit risk — the DETERIORATION → DEFAULT funnel
```
HEALTHY → EARLY STRESS → EMERGING DISTRESS → DELINQUENCY → DEFAULT ⚠ → RECOVERY/LOSS
```
Maps to **IFRS9 staging** (Stage 1 performing → 2 SICR → 3 credit-impaired). Fair-lending: **no protected
attributes**; income/geo flagged.
- **Stage 0 — Origination baseline (static, at application):** `dti_at_origination`, `ltv_at_origination`,
  bureau score, `bureau_recent_inquiries`, tenure-at-application. use: application PD, pricing.
- **Stage 1 — Early stress (behavioural, leading):** `utilisation_trend` (rising), revolving-balance
  growth, `cash_advance_usage` (classic distress), `income_volatility`, deposit-balance decline,
  overdraft-usage rising.
- **Stage 2 — Emerging distress (stronger):** first late payment, `payment_ratio_avg` falling,
  `times_over_limit`, **cross-lender bureau deterioration** (new inquiries/delinquencies elsewhere),
  `nsf_returned_payments` (failed DDs).
- **Stage 3 — Delinquency (strong):** `max_dpd_in_window`, `delinquency_count`, consecutive misses,
  `roll_rate_signal` (→ worse bucket), `worst_status_in_window`.
- **Stage 4 — Default ⚠ (NEAR-LABEL):** 90+ DPD (**often IS the Basel default label** → leakage trap,
  flag/reject), charge-off, `forbearance_restructure_flag` (also near-label).
- **Stage 5 — Recovery/Loss:** `cure_probability` inputs, recovery rate, LGD/`downturn_lgd`.
> Trap: Stage-4 (90+ DPD, forbearance) ≈ the default label — the 3-part leakage control must flag/reject.

## B3. Fraud — the KILL-CHAIN (real-time; windows are minutes/hours, not weeks)
```
RECON → ACCESS/TAKEOVER → SETUP/STAGING → CASH-OUT ⚠
```
Types: card (CNP), account-takeover (ATO), application (synthetic-ID), first-party (bust-out).
- **Stage 1 — Recon/targeting:** `failed_login_spike` (credential-stuffing), unusual profile lookups,
  `application_velocity` (shared email/phone/device across apps → synthetic-ID).
- **Stage 2 — Access/takeover:** `device_change_flag` (novel device), `geo_velocity_impossible`,
  `time_since_credential_change` (password/contact just changed), MFA change, dormant-account reactivation.
- **Stage 3 — Setup/staging:** `new_beneficiary_flag`, limit-increase request, payee added then a quiet
  "aging" gap (the mule trick), contact-detail change before a payment.
- **Stage 4 — Cash-out ⚠ (NEAR-LABEL):** `txn_velocity` spike, `amount_zscore` spike, rapid drain,
  high-value transfer to a new beneficiary, mule-pattern outflow. **⚠ the fraudulent txn IS often the
  label → flag.**
> Note: fraud is **real-time** — `pit` windows are short; features must compute on the live pre-txn state.

## B4. AML — the LAUNDERING cycle (typology-driven)
```
PLACEMENT → LAYERING → INTEGRATION
```
Labels are **SARs (suspicion, not proof)** — weak/noisy; a filed SAR is **near-label** (don't use as a
feature). Geo/nationality are proxies → AML-permitted but bias-watched.
- **Placement (dirty money enters):** `cash_intensity`, `structuring_score` (just under threshold),
  rapid third-party deposits.
- **Layering (obscure the trail):** `rapid_movement_ratio` (in-then-out), `round_amount_share`,
  round-tripping, `network_degree` (mule rings), pass-through accounts, `high_risk_geo_share`.
- **Integration (clean money returns):** asset purchase, business-income mixing, **TBML** (over/under-
  invoicing), `shortest_path_to_flagged` (proximity to known-bad).
> Cross-cutting: `zscore_vs_own_history` (out-of-pattern), velocity, network position.

## B5. Cross-sell / CLV — the GROWTH journey (the INVERSE of attrition)
```
ONBOARDING → ACTIVATION → DEEPENING → MATURITY → ADVOCACY
```
The **positive mirror of B1** — the *same* signals read in reverse (rising salary/breadth = growth;
falling = attrition). Eligibility: **no protected-attribute inference** (can't infer pregnancy/health for
targeting).
- **Onboarding:** account funded, first salary credit (**primacy won**), early logins.
- **Activation:** `direct_debit_setup` (sticky), card activated, digital enrolled, regular usage.
- **Deepening (cross-sell windows):** `product_breadth` growing, `product_gap_flag`, `life_event_proxy`
  (salary jump → mortgage; large inflow → wealth), `channel_engagement`.
- **Maturity:** high `share_of_wallet_proxy`, multi-product, high `revenue_trend`/CLV, stable.
- **Advocacy:** referrals, sustained high engagement.

## B6. Collections & recoveries — the DELINQUENCY → RECOVERY journey
```
PRE-DELINQUENCY → EARLY (1–29 DPD) → MID (30–89) → LATE (90+) → RECOVERY / CHARGE-OFF
```
Optimise by **balance-at-risk × cure-probability × contactability**; segment self-curers from
needs-intervention. Conduct: **vulnerability** flag (sensitive) → different handling.
- **Pre-delinquency:** the B2 early-warning signals (predict who'll miss).
- **Early (1–29):** first miss, `self_cure_likelihood`, `promise_to_pay` behaviour.
- **Mid (30–89):** `roll_rate_signal`, `promise_kept_ratio`, `right_party_contact_rate`, partial payments.
- **Late (90+):** severity, `balance_at_risk`, hardship indicators.
- **Recovery/charge-off:** `cure_probability`, recovery rate, settlement propensity, legal/write-off.

## B7. Deposit / liquidity / treasury (ALM) — the STABILITY spectrum
```
STABLE CORE → RATE-SENSITIVE → SURGE / HOT MONEY → RUNOFF-PRONE → OUTFLOW ⚠
```
Not a customer funnel — a **deposit-behaviour spectrum** per depositor/segment; feeds LCR/NSFR, FTP, ALM.
- **Stable core:** `nmd_stability` (low volatility, low beta), long tenure.
- **Rate-sensitive:** `deposit_beta_proxy` (`pct_change(balance)` vs benchmark-rate change).
- **Surge / hot money:** `surge_deposit_flag` (sudden large inflow, high beta), short expected life.
- **Runoff-prone:** `net_flow_trend` negative, `concentration_by_depositor` (few big depositors),
  correlated-withdrawal risk.
> Ties to B1: a depositor sliding STABLE→OUTFLOW is also churning — the deposit-attrition overlap.

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
9. **The FUNNEL/journey meta-pattern (B1–B7).** Most banking targets are the end of a *process*, so signals
   stage along a journey: **early = more lead-time but noisier; late = near-certain but too late.** Two
   consequences hold in *every* domain: (a) **blend stages** — don't rely on one (a good model mixes
   lead-time and strength); (b) **the bottom of every funnel is a leakage trap** — the last-stage signal
   is *almost the label* (churn: CASS switch; credit: 90+ DPD; fraud: the cash-out txn; AML: a filed SAR;
   collections: charge-off). The 3-part leakage control must **flag/reject** these. When authoring a new
   use-case, **map its funnel first**, then place each template on it and mark the near-label tail.

# PART E — Open / to-deepen
Markets desk templates (greeks, XVA, PnL-attribution), insurance (lapse/claims), securities-services
(settlement-fail), ESG (carbon-intensity trend) — named here, to be authored as those domains are
prioritised. Coverage grows per-domain via curation, not one big freeze.
