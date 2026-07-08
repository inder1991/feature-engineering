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

## B8. Markets / trading — risk families + the COUNTERPARTY-RISK funnel
Positions/instruments, not customers. **High MRM tier** (VaR/XVA models heavily governed); MNPI /
Chinese-wall aware. Time-scale: intraday→daily.
- **Sensitivity families (point-in-time):** `greek_exposure` — delta/gamma/vega/theta/rho per
  position/book (params: greek; add: additive across a book per greek; explain: H). `position_concentration`
  — HHI of exposure by issuer/sector.
- **Risk metrics:** `var_1d` / `expected_shortfall` (tail loss over horizon; explain: M), `stress_pnl`
  (P&L under a {scenario} — CCAR/EBA).
- **XVA / counterparty exposure:** `expected_exposure` (EPE) / `potential_future_exposure` (PFE) —
  exposure profile over time; `cva` (expected counterparty-default loss); `wrong_way_risk`
  (corr(exposure, counterparty PD); explain: M).
- **PnL & control:** `pnl_daily`, `pnl_volatility`, `pnl_attribution` (decompose delta/gamma/vega/carry/
  residual), `unexplained_pnl` (the residual — large ⇒ booking/model issue; a **control** signal).
- **Counterparty-risk funnel (mirrors credit):** `HEALTHY → MARGIN PRESSURE (rising PFE, margin calls) →
  DISPUTE (collateral shortfall) → CLOSE-OUT ⚠ (default)`. Trap: close-out ≈ the default label.
- **Settlement/execution:** `settlement_fail_rate`; `slippage` / `market_impact` (TCA); `fill_ratio`.

## B9. Insurance / bancassurance — the LAPSE funnel + the CLAIMS-FRAUD journey
Two journeys. **Health/mortality data = special-category** → heavy consent, restricted use.
- **Lapse / persistency funnel (mirrors churn):** `ACTIVE → DISENGAGEMENT → ARREARS → SURRENDER REQUEST ⚠
  → LAPSED`. Signals: `premium_payment_regularity` (= `inter_event_gap_std` on premiums),
  `premium_arrears_flag`, `payment_method_failure`, `policy_tenure`, `surrender_value_ratio` (surrender
  value ÷ premiums — the incentive to surrender), `lapse_risk_score`. Near-label: surrender request.
- **Claims-fraud journey:** `INCEPTION → CLAIM EVENT → FILED → INVESTIGATION → SETTLE/DENY`. Signals:
  `early_claim_flag` (claim soon after inception — red flag), `claim_frequency`, `claim_amount_zscore`,
  `prior_claims_count`, `claim_network_degree` (staged-accident rings), `claim_inconsistency_score`
  (NLP over the claim narrative — derived downstream, §D.8). Near-label: confirmed-fraud/repudiation.
- **Underwriting:** `sum_assured_to_income`, `medical_disclosure_flag`, `mortality_morbidity_proxy`
  (age/health — **special-category, restricted**).

## B10. Custody & securities services — the SETTLEMENT-FAIL funnel
Operational / asset-servicing; institutional; operational-risk governed. Less PII.
- **Settlement-fail funnel:** `TRADE BOOKED → MATCHING (unmatched/mismatch) → PRE-SETTLEMENT
  (inventory/cash shortfall) → SETTLEMENT DATE → FAIL ⚠ → FAIL-AGING → BUY-IN`. Signals:
  `matching_break_rate`, `inventory_shortfall_flag`, `counterparty_fail_history`, `cutoff_proximity`
  (market/ccy cut-off), `settlement_fail_rate`, `fail_aging_days`. Near-label: the fail itself.
- **Corporate-action risk:** `ca_election_deadline_proximity`, `ca_complexity`, `missed_election_history`
  (missing an election = client loss).
- **Securities lending:** `sec_lending_utilisation` / `specials_demand`, `recall_risk`.
- **Fund admin / NAV:** `nav_error_rate`, `pricing_exception_count`, `reconciliation_break_rate`.

## B11. ESG / sustainable finance — scoring + the TRANSITION-RISK journey
**ESG data is often EXTERNAL** (ratings vendors, emissions disclosures) — availability/quality caveats;
an `esg_score` is itself a model output (a derived tag, §D.8). Geographic is climate-legitimate, **not a
credit proxy**.
- **Scoring:** `esg_score` (E/S/G pillars), `esg_trend` (improving/deteriorating), `controversy_flag`.
- **Transition-risk journey:** `ALIGNED → LAGGING → HIGH-RISK → STRANDED`. Signals: `carbon_intensity`
  (emissions ÷ revenue), `sector_transition_risk` (high-carbon exposure), `transition_alignment` (vs
  net-zero pathway), `stranded_asset_exposure`.
- **Physical climate risk:** `physical_hazard_exposure` (flood/wildfire/heat of collateral/operations by
  geography). eligibility: geographic — climate-legitimate.
- **Greenwashing / SLL:** `green_proceeds_deviation` (green-bond proceeds not actually green),
  `sll_kpi_trend` / `sll_kpi_breach_flag` (triggers a margin ratchet), `esg_claim_vs_data_gap`.

## B12. Asset management (buy-side) — the REDEMPTION funnel + mandate compliance
Funds/mandates, driven by **relative performance + liquidity**. Regulatory: IMA/mandate compliance,
open-ended fund liquidity.
- **Investor-flow / redemption funnel (mirrors churn):** `INVESTED → DISENGAGEMENT (reduced allocation) →
  REDEMPTION-RISK (underperformance, partial redemptions) → REDEMPTION NOTICE ⚠ → REDEEMED`. Signals:
  `fund_flow_trend` (net subs − redemptions), `relative_performance` (vs benchmark — underperformance
  drives outflows), `investor_concentration` (few big investors = run risk), `distribution_partner_flow`
  (platform/advisor flows), `redemption_notice_flag` (near-label).
- **Mandate / portfolio risk:** `mandate_breach_proximity` (drift toward a sector/issuer/rating limit),
  `style_drift` (portfolio vs stated style), `tracking_error`, `fund_liquidity_coverage` (liquid assets ÷
  expected redemptions — the run-risk mismatch), `concentration_vs_limit`.

## B13. Islamic banking — conventional funnels + the SHARIA-COMPLIANCE overlay
Most B1–B7 funnels APPLY (churn/credit/deposits), reframed: **profit-rate, not interest**. The
distinctive layer is **Sharia compliance = a hard eligibility gate** (like a regulatory rule), ratified
by the **Sharia board** (a domain-specific ratification, cf. Compliance).
- **Sharia-compliance features:** `sharia_compliance_flag`, `prohibited_activity_exposure` (haram-sector
  screen — alcohol/gambling/conventional-interest), `purification_amount` (non-compliant income to
  purify), `profit_rate` (replaces interest in all rate features).
- **Product-specific behavioural:** Murabaha `installment_payment_behavior` (= credit B2); Ijara
  `lease_utilisation` + residual-value risk; Mudaraba/Musharaka `profit_share_volatility` (partner
  performance); Sukuk = bond features; Takaful = insurance (B9 lapse/claims).
- **Deposit attrition:** `islamic_deposit_beta` (profit-rate sensitivity) + Sharia-compliance-concern
  churn (a distinctive driver). eligibility: Sharia non-compliance is a **HARD block**.

## B14. Payments-as-a-business (beyond cards)
RTP/instant, correspondent banking, cross-border/remittance, open banking, merchant acquiring.
- **RTP / instant-payment fraud (real-time, like B3):** `app_scam_pattern` (authorised push payment —
  victim tricked: new payee + high value + urgency), `mule_inflow_pattern` (receiving side — in-then-
  straight-out), `payment_velocity`, `beneficiary_risk`. Near-label: the scam/fraud payment.
- **Correspondent banking:** `correspondent_exposure`, `nested_correspondent_flag` (respondent serving
  other banks — AML), `unusual_corridor_flow`, `sanctions_corridor_exposure`.
- **Cross-border / remittance AML:** `corridor_risk` (high-risk corridor), `structuring_remittance`,
  `agent_velocity`, `sender_receiver_network`.
- **Open banking / TPP:** `tpp_consent_anomaly`, `aggregator_scraping_flag`, `consent_scope_creep`.
  eligibility: **data-governance heavy** (consent/purpose).
- **Merchant acquiring (a churn+credit funnel):** `merchant_txn_decline` (attrition), `chargeback_rate`
  (fraud/credit), `merchant_bust_out_risk` (volume spike then vanish), `settlement_delay_risk`,
  `merchant_credit_risk` (for merchant cash advance).

## B15. Corporate / SME — trade & supply-chain finance (multi-product, GROUP-level)
Corporate is **multi-product + hierarchical** — features aggregate across product families AND up the
group (§A6 `group_exposure_sum`). Cash-flow / trade-flow-based, not just financials.
- **Trade finance (LC/guarantee):** `trade_cycle_length` (issue→settlement — lengthening = stress),
  `document_discrepancy_rate`, `contingent_utilisation` (undrawn LCs being drawn = stress),
  `trade_counterparty_concentration`.
- **Invoice / receivables finance:** `invoice_dilution_rate` (unpaid/credit-noted), `debtor_concentration`,
  `dso_trend` (days-sales-outstanding rising = cash stress), `invoice_verification_gap` (fake-invoice fraud).
- **Supply-chain finance:** `anchor_buyer_dependence` (SCF program hinges on the anchor's health),
  `payment_term_extension` (buyer extending terms = stress), `program_utilisation_trend`.
- **Working capital / facility:** `facility_utilisation_trend`, `covenant_headroom` (proximity to breach),
  `overdraft_persistence` (hardcore overdraft never clearing).
- **Corporate deterioration funnel (mirrors credit, at GROUP level):** `HEALTHY → EARLY STRESS
  (utilisation↑, DSO↑, term extension) → COVENANT PRESSURE (headroom↓) → BREACH ⚠ → DEFAULT/RESTRUCTURE`.
  Signals: `combined_exposure_trend` (across products + subsidiaries), `cross_product_stress_count` (#
  product lines simultaneously stressed — a strong early-warning), `trade_flow_decline` (business slowing).
  Near-label: covenant breach for a breach-prediction target.

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
**All 15 business lines are now drafted** at funnel/family level (B1 churn · B2 credit · B3 fraud · B4 AML ·
B5 cross-sell · B6 collections · B7 treasury · B8 markets · B9 insurance · B10 custody · B11 ESG · B12
asset-management · B13 Islamic · B14 payments · B15 corporate/trade-SCF) plus 8 cross-cutting families
(A1–A8) + relationship-outflow (A9). Remaining work is **depth, not breadth**: expand each stage's compact
signals into full parametric templates (`needs/params/pit/eligibility` schema, like §A9), starting with the
pilot use-case (retail_churn) for B2 of the build. Coverage then grows per-domain via curation + the
flywheel, not one big freeze.

---

# PART F — Appendix: `retail_churn` full parametric set (PILOT — feeds build C2 + engine B2)

The pilot templates at **executable-spec depth** — what the template engine (B2) grounds and the SME (C2)
ratifies. Each is groundable by concept-matching, safe-by-construction (PIT baked in), and carries a
degrade path. Concept names match the taxonomy (§3). Templates that can't fully ground **degrade or skip**
— never silently pass a partial.

**Grounding requirements — a "churn-ready" retail catalog needs:**
| Concept role | Typical column | Required by |
|---|---|---|
| `monetary_stock` + history | `accounts.balance` snapshots over time | balance_trend, volatility, days_below |
| `as_of_date` | `accounts.snapshot_date` | every point-in-time feature |
| `monetary_flow` (+ direction) | `transactions.amount` (+ dr/cr) | inflow_outflow, rfm, salary |
| `event_timestamp` | `transactions.txn_date` | dormancy, frequency_trend, rfm |
| `customer_identifier` (entity) | `customers.customer_id` | grain of every feature |
| `category_code` (salary/DD tag) | `transactions.type` | salary_*, dd_cancellation (degrade if absent) |
| `product_holding` + open/close | `holdings.*` | product_breadth/attrition |
| `effective_date` (origination) | `customers.signup_date` | tenure |
| `outcome_label` (target) | `customers.churned` | leakage anchor (never a feature input) |

### 1 · `balance_trend_{window}` *(Stage 3 — headline drain signal)*
- **computes:** OLS slope of `{stock_col}` vs time over trailing `{window}` days per `{entity}`;
  `measure=normalized` divides slope by window-mean balance (scale-free).
- **needs:** `monetary_stock {stock_col}` **with time history** · `as_of_date` · entity `{entity}`.
- **params:** `window ∈ {30,60,90}` (def 90) · `measure ∈ {slope,normalized}` (def normalized).
- **grain:** per `{entity}` per as_of. **pit:** rows in `({asof}−{window}, {asof}]`, strictly ≤ as_of.
- **add:** n/a. **eligibility:** bind a `monetary_stock` (not flow); single currency (convert first).
  **explain:** H. **degrade:** only a *current* balance (no history) → **skip** (no trend from one point).

### 2 · `dormancy_days` *(baseline recency — ⚠ near-label)*
- **computes:** `{asof} − max({event_ts})` over `{event_ts} ≤ {asof}`. **needs:** `event_timestamp` on
  `{entity}`. **params:** `event_filter` (def: any txn). **grain/pit:** last event ≤ as_of. **add:** n/a.
  **explain:** H. **⚠ leakage:** if churn = "no activity in N days," this ≈ the label → 3-part control
  **flags** (confirm pre-as_of only, and window ≠ label window).

### 3 · `txn_frequency_trend_{window}` *(Stage 2 — engagement decay)*
- **computes:** `count(events in recent half of {window}) / count(prior half)`; `<1` = declining.
- **needs:** `event_timestamp` on `{entity}`. **params:** `window ∈ {60,90,180}` · `measure ∈
  {halves_ratio,slope}`. **grain/pit:** trailing ≤ as_of. **add:** n/a. **explain:** H.

### 4 · `inflow_outflow_ratio_{window}` *(Stage 3 — net draining?)*
- **computes:** `sum(debit {amount} in window)/sum(credit {amount} in window)`; `measure=net` →
  `credits − debits`. **needs:** `monetary_flow {amount}` · a **direction** (dr/cr, or amount sign) ·
  `event_timestamp` · `{entity}`. **params:** `window` · `measure ∈ {ratio,net}`. **grain/pit:** trailing.
  **add:** `net` additive / `ratio` non-additive. **eligibility:** single currency (convert first).
  **explain:** H. **degrade:** no dr/cr flag → infer from amount sign (declared derivation, §D.8).

### 5 · `days_below_threshold_{window}` *(Stage 3 — near-empty)*
- **computes:** `count(distinct days where {stock_col} < {threshold})` in trailing window. **needs:**
  `monetary_stock` history · `as_of_date` · `{entity}`. **params:** `window` · `threshold` (absolute or a
  percentile of own history). **grain/pit:** trailing. **add:** additive (day count). **explain:** H.

### 6 · `salary_signal_{window}` *(Stage 3 — salary cessation/irregularity)*
- **computes:** over salary-tagged credits — `cessation_flag` (no salary in `{window}` when previously
  regular) · `gap_std` (std of inter-salary gaps) · `latest_gap` (days since last salary). **needs:**
  `monetary_flow` credits · **salary tag** (`category_code`) · `event_timestamp` · `{entity}`. **params:**
  `window` · `measure ∈ {cessation_flag,gap_std,latest_gap}`. **grain/pit:** trailing. **add:** n/a.
  **eligibility:** income **sensitive** — churn-permitted, flagged. **explain:** H. **degrade:** no salary
  tag → derive from recurring same-amount ~monthly credits (declared derivation §D.8; probabilistic, flag).

### 7 · `product_breadth` / `product_attrition_{window}` *(Stage 4 — unbundling)*
- **computes:** `breadth = count(distinct product_holding active at {asof})`; `attrition =
  breadth({asof}) − breadth({asof}−{window})`. **needs:** `product_holding` · open/close `effective_date`s
  · `{entity}`. **params:** `window`. **grain/pit:** products with open ≤ as_of < close. **add:** additive
  (count). **explain:** H.

### 8 · `tenure_days` *(context)*
- **computes:** `{asof} − {origination_date}`. **needs:** `effective_date` (signup) · `as_of_date` ·
  `{entity}`. **grain/pit:** origination ≤ as_of. **add:** n/a. **explain:** H.

### 9 · `balance_volatility_{window}` *(Stage 3 — instability)*
- **computes:** `std({stock_col} in window) / mean({stock_col} in window)` (coeff. of variation).
  **needs:** `monetary_stock` history · `as_of_date` · `{entity}`. **params:** `window`. **grain/pit:**
  trailing. **add:** n/a. **explain:** H.

### 10 · `rfm_composite` *(baseline workhorse)*
- **computes:** percentile-binned blend of `recency_days`, `txn_frequency({window})`,
  `monetary_sum({window})`. **needs:** `event_timestamp` · `monetary_flow` · `{entity}`. **params:**
  `window`. **grain/pit:** trailing. **add:** n/a. **explain:** H (components inspectable).

### 11 · `dd_cancellation_rate_{window}` *(Stage 4 — sticky commitments leaving)*
- **computes:** `count(DD mandates cancelled in window) / count(DDs active at window start)`. **needs:**
  `direct_debit` mandate events (setup/cancel) · `event_timestamp` · `{entity}`. **params:** `window`.
  **grain/pit:** trailing. **add:** non-additive (rate). **explain:** H. **degrade:** **skip** if no
  DD/mandate data.

### 12 · `external_own_transfer_trend` *(Stage 3 — primacy loss)*
- Fully specified in **§A9** (own-account flag via `name_match`, downstream + PII). Included for banks with
  beneficiary + name data; **degrade** to `external_outflow_growth` if no name to match.

**Composite (optional, not MVP):** `relationship_erosion_score` blends 1–12 by lead-time × strength;
`explain: H` (shows which fired).

**Build note (C2/B2):** these 12 map 1:1 to the `templates.py` model — `needs`→grounding contract,
`params`→parameter schema, `pit`→trailing-window guard, `degrade`→fallback. The pilot **golden set**
(kick-off) should exercise each of 1–12 **plus** the `dormancy_days` near-label flag and the
`dd_cancellation` / `external_own_transfer` degrade paths.
