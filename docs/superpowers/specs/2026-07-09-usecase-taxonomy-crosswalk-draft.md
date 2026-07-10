# Use-Case Taxonomy & 107-Tag Crosswalk — v2 (final calls locked)

**Status:** Agreed with domain owner. Authoritative input to the Phase-0 implementation.
**Supersedes:** the v1 draft (this file's prior version).
**Related:** `2026-07-09-intent-aware-recipe-selection-plan.md`

---

## 0. Governing principle (the classification rule)

**The taxonomy encodes modelling objectives and governed decisions — NOT the organisational chart.** Team ownership lives in metadata (`governance_owner`, `operating_owner`), so the tree survives Fraud/AML/Wealth re-orgs unchanged.

**Promotion test — a tag earns a use-case *leaf* only if it has a distinct:** prediction target, business decision, success measure, **or** policy regime.
Otherwise it is **context or metadata**, when it merely describes: a product/channel, a regulatory framework, a stage in a journey, a potential consumer, a feature theme, or an organisational owner.

---

## 1. Dimensions (each a separate, closed vocabulary)

| Dimension | Role | Members (seed) |
|---|---|---|
| **use_case** | the objective — **the only dimension applicability narrows on** | the tree in §3 |
| **modelling_context** | regulatory framework/regime | `ifrs9`, `frtb`, `xva`, `lcr`, `nsfr`, `lgd`, `irrbb`, `ftp` |
| **measure** | an output quantity, not an objective | `tracking_error`, `data_quality` |
| **product_context** | product/asset/channel | `deposits`, `credit_cards`, `mortgages`, `crypto_assets`, `derivatives` |
| **typology** | a specific pattern within a use-case | `app_scam`, `mule_account`, `synthetic_id`, `account_takeover`, `crypto_asset_laundering`, `trade_based_money_laundering` |
| **journey_stage** | position in a broader journey | `disengagement`, `unbundling`, `primacy_erosion`, `outflow`, `closure` |
| **business_outcome** | the benefit, not the target | `revenue_growth`, `cost_efficiency`, `loss_reduction` |
| **metadata** | ownership & capability | `governance_owner`, `operating_owner`, `consumers`, `capabilities` (`transaction_monitoring`, `anomaly_detection`, `network_analysis`) |

---

## 2. The seven locked decisions (with concrete recipe remap)

**D1 — `financial_crime` is a non-selectable domain parent.** `fraud` and `aml_cft` are independently selectable branches (they share signals but have distinct governed outcomes, regulatory regimes, action latency and workflows). The recognizer may return `financial_crime` **only** as an ambiguity `domain_hint`, never as a scoping result.

**D2 — `deposit_attrition` is a customer objective, not an ALM one.** → `customer.relationship_attrition.deposit_attrition`. The separate treasury objective is `treasury_alm.deposit_runoff_forecasting` (portfolio/segment/time-bucket grain). Classification follows the *target and decision*, not the feature ingredients or the downstream consumer — Treasury as a consumer is metadata (`consumers: [treasury_alm]`).

**D3 — Split `transaction_monitoring` by governed objective; keep the bare term only as a capability tag.** Deterministic by family:
- 11 **fraud**-family recipes → `fraud.transaction_fraud_detection` (primary): `card_testing_velocity, device_sharing_velocity, new_device_flag, geo_velocity_impossible, first_time_payee_high_value, merchant_risk_anomaly, txn_velocity_spike, amount_zscore_spike, cross_channel_rail_anomaly, cross_border_burst, amount_just_under_limit`.
- 11 **aml**-family recipes → `aml_cft.suspicious_transaction_monitoring` (primary): `structuring_smurfing, cash_intensity_ratio, rapid_movement_passthrough, round_amount_ratio, fan_in_fan_out, high_risk_corridor_exposure, nested_correspondent_flow, crypto_offramp_exposure, dormant_reactivation, screening_exposure, prior_alert_recidivism`.
- Cross-applicable velocity/network recipes may carry the other branch as **secondary**; recipes are not duplicated.
- **Owner decision confirmed (2026-07-10, choice A):** ALL transaction-monitoring recipes home on the family *monitoring* leaf. The specific AML **typology leaves** (`aml_cft.sanctions`, `.structuring`, `.screening`, `.kyc`, `.correspondent`) — and, analogously, `islamic.banking` under D6/D13 — are **governed structure for future dedicated recipes**: they carry **no primary recipes** by design (only secondary relevance), so they read as *unpopulated* (informational), NOT `intentionally_empty` (which requires zero secondaries too). A later choice-B could re-home typology-tagged recipes onto these leaves; not doing so now.

**D4 — Crypto is context, not a use-case.** The one recipe (`crypto_offramp_exposure`, aml) → `aml_cft.suspicious_transaction_monitoring` (primary) + `product_context: crypto_assets` + `typology: crypto_asset_laundering`. No crypto top-level.

**D5 — Pricing is a real family only where price/rate/fee is the target; `cost_efficiency` is a business-outcome.**
- `pricing` leaves (`credit_risk_based_pricing`, `deposit_rate_optimisation`, `fee_pricing`, `relationship_pricing`) are **declared but currently empty** — none of the 153 recipes are *primarily* pricing. The 7 recipes tagged `pricing` (`tenure_days`, `expense_ratio_competitiveness`, `claims_frequency_severity`, `mortality_morbidity_loading`, `profit_rate_exposure`, `clv_revenue_trajectory`, `tenure_upsell_readiness`) keep their real primary; `pricing` demotes to **secondary/consumer**. Bare `pricing` deprecated.
- `cost_efficiency` → `business_outcome` metadata. The one recipe (`cost_to_collect_ratio`, collections) stays `credit.collections` primary. An `operations` family (`process_cost_forecasting`, `workload_forecasting`, `manual_review_optimisation`) is **declared for future** genuine operational targets (no recipes yet).

**D6 — Promote `primacy_loss` + `wealth`; fold `unbundling` + `contactability`; split `cashflow` by objective.**
- `primacy_loss` → **promoted** leaf `customer.relationship_attrition.primacy_loss`. Recipes `salary_signal`, `external_own_transfer_trend` carry it as **secondary** (primary `customer.churn`).
- `wealth` → **promoted** family: `wealth.asset_outflow`, `wealth.client_attrition`. `external_own_transfer_trend` also maps to `wealth.asset_outflow` (secondary). `wealth.client_attrition` is declared-empty.
- `unbundling` → `journey_stage` metadata; `dd_cancellation_rate` stays `customer.churn` primary.
- `contactability` → `customer_state` metadata; `right_party_contact_intensity` stays `credit.collections` primary.
- `cashflow` / `cashflow_risk` → deprecated; `inflow_outflow_ratio` and `balance_volatility` keep `customer.churn` primary (each may carry a `treasury_alm`/`affordability` secondary per its semantics).

**D7 — Cross-cutting risk tags get precise homes, not a forced Credit/Markets fit.**
- `concentration_risk` → `portfolio_risk.concentration` (cross-cutting), with `risk_context` metadata. The 6 recipes (`rate_sensitive_concentration` [funding], `book_desk_concentration` [market], `sukuk_concentration`, `syndication_concentration`, `group_exposure_aggregation`, `guarantor_reliance` [credit]) all land here with the right `risk_context`.
- `counterparty_risk` → **promoted to top-level**: `exposure_monitoring` (← `notional_netting_exposure`, from generic `exposure_management`), `margin_call_risk` (← `margin_call_intensity`, from generic `margin`), `settlement_exposure` (declared-empty), plus the 5 `counterparty_risk`-tagged markets recipes.
- `basis_risk` → `markets.market_risk.basis_risk` (← `benchmark_basis_dislocation`); a `treasury_alm.irrbb.basis_risk` leaf is declared for banking-book basis recipes (none yet).
- Settlement stays `securities_services.custody.settlement` (the 4 custody recipes: `matching_break_rate, pre_settlement_aging, settlement_fail_rate, fail_ageing_buckets`).
- Generic `exposure_management`, `basis_risk`, `margin` deprecated.

---

## 3. Resulting use-case hierarchy

```text
customer
├── relationship_attrition {churn, deposit_attrition, primacy_loss}
├── cross_sell {next_best_action, share_of_wallet, whitespace}
├── clv
├── engagement
├── segmentation
├── campaign
└── overdraft_propensity

wealth {asset_outflow, client_attrition*}

credit
├── underwriting {affordability, seasoning, sme}
├── early_warning
├── monitoring {limit_management, exposure_management→obligor, credit_mitigation}
└── collections {recoveries, hardship, self_cure, workout}

financial_crime                               # non-selectable domain
├── fraud {transaction_fraud_detection, card_fraud, account_takeover, app_scam, synthetic_id, merchant_fraud}
└── aml_cft {suspicious_transaction_monitoring, structuring, sanctions, screening, kyc, correspondent, mule_account*, tbml*}

treasury_alm {deposit_stability, deposit_runoff_forecasting, liquidity, net_interest_margin, irrbb{basis_risk*}, cash_management}
portfolio_risk {concentration}
counterparty_risk {exposure_monitoring, settlement_exposure*, margin_call_risk}
markets {market_risk{basis_risk, portfolio}}
payments {behaviour, operations, merchant{interchange}, cross_border}
securities_services {custody{settlement, corporate_actions}, securities_lending, fund_administration}
insurance {lapse{surrender, persistency}, claims{claims_fraud}, reinsurance, bancassurance}
asset_management {redemption{fund_flows, fund_liquidity, aum_stability}, mandate_compliance, performance}
islamic {banking, sharia_compliance}
esg {scoring, climate{transition, physical}}
corporate_trade {trade_finance, supply_chain_finance, working_capital, receivables_finance}
pricing {credit_risk_based_pricing*, deposit_rate_optimisation*, fee_pricing*, relationship_pricing*}
operations {process_cost_forecasting*, workload_forecasting*, manual_review_optimisation*}
profitability {margin_forecasting*}
```

`*` = **intentionally-empty (declared-future) leaf** — governed structure ahead of content; Phase-0 coverage validation marks these "no authored recipe yet (intentional)" so the zero-recipe check passes with an explanation.

---

## 4. Ownership metadata model (separate from the tree)

```yaml
id: aml_cft.suspicious_transaction_monitoring
domain: financial_crime
governance_owner: group_financial_crime_compliance
operating_owner: aml_operations
consumers: [aml_operations, regulatory_reporting]
capabilities: [transaction_monitoring, anomaly_detection, network_analysis]
```

Whether Fraud and AML "share a roof" changes `governance_owner`/`operating_owner` — never taxonomy identity.

---

## 5. Remap execution note (input to Phase-0)

- **~84 tags remap 1:1** to a use-case leaf (mechanical — the v1 compact crosswalk, adjusted for the D1–D7 renames).
- **The ~20 hard tags are resolved above** to concrete recipes — no per-recipe archaeology remains except confirming secondaries.
- **`transaction_monitoring` (22)** splits deterministically by family (11 fraud / 11 aml) — listed in D3.
- **Reclassified out of use_case:** `ifrs9_staging, frtb, irrbb, lcr, nsfr, ftp` (modelling_context); `xva, lgd` (modelling_context per owner); `tracking_error, data_quality` (measure); `crypto`→product_context+typology; `unbundling, contactability`→journey_stage/customer_state; `cost_efficiency`→business_outcome; `authorised_push_payment`→merge `app_scam`; `sustainable_finance`→merge `esg.scoring`.
- **Deprecated generic tags** (split by objective, not folded): `exposure_management, basis_risk, margin, cashflow, cashflow_risk, pricing`.
