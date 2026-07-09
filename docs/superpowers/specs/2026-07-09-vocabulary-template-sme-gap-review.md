# Vocabulary & Template Library — Banking-SME Gap Review (synthesis of 5 parallel SME audits)

**Date:** 2026-07-09 · **Method:** 5 independent banking-SME reviewers (consumer · wholesale · risk &
compliance · specialist lines · vocabulary/cross-cutting) audited `concepts.py` (116 concepts), the
taxonomy reference, and the template library for completeness + correctness. This is the deduplicated,
prioritized synthesis.

## The headline (all reviewers converged)

**1. The registry ↔ template *coherence gap*.** The template library (B-sections) references **~30–40
concepts that do not exist in the implemented registry** — the taxonomy defined specialist *entities*
(§1.10 insurance, §1.11 custody, §1.9 hierarchy) but §3/`concepts.py` were never extended with the
matching *concepts*. So **many templates can never ground.** This even bites the *implemented* pilot:
`dd_cancellation_rate` and `external_own_transfer_trend` need `direct_debit`/`beneficiary` concepts that
aren't in the registry — so the flagship churn signals only **degrade or skip** today.

**2. Behaviour-tag *correctness bugs* in the shipped registry** — four capital/accounting stocks tagged
`additive` violate the module's own semi-additive-over-time invariant (will silently sum a stock across
time = a wrong number), and several personal/proxy data-kinds are tagged `public` (the eligibility gate
won't fire). These are **safety/correctness defects, not gaps.**

---

## A. Correctness & safety bugs in the IMPLEMENTED registry — **P0, fix first**

These produce *wrong numbers* or *unsafe passes* today.

### A1 — Additivity traps (stocks tagged `additive`)
`ead`, `ecl`, `rwa`, `provision_amount` are **balances/snapshots** but tagged `additive` → summing across
reporting dates is meaningless, and it contradicts `monetary_stock`/`fair_value`/`amortised_cost` (all
correctly `semi_additive`). **Fix: retag to `semi_additive`.** Also questionable: `notional`,
`quantity_units` (position attributes — `semi` over time). Domain-specific additivity errors: **insurance
`premium`** (written vs earned — `monetary_flow`'s "fully additive" double-counts over a policy period);
**ESG emissions** (Scope 1+2+3 summed across counterparties double-counts — one firm's Scope 1 is
another's Scope 3); **group exposure** (`group_exposure_sum` naively sums — intra-group loans + parent
guarantees double-counted, no elimination guard).

### A2 — Sensitivity under-tagging (personal/proxy data tagged `public`)
The eligibility/read-scope gates can't fire on these:
| Concept | Current | Should be | Why |
|---|---|---|---|
| `geolocation` | public | pii / proxy | precise location = personal data + protected-class proxy |
| `device_fingerprint` | public | pii | GDPR online identifier / fraud quasi-ID |
| `country_code` | public | proxy (when nationality/residence) | national-origin proxy (ECOA) |
| `pep_flag`, `sanctions_hit_flag` | public | sensitive | political-exposure / highly sensitive (+ screening-model leakage) |
| `free_text`, `unstructured_doc` | public | pii/screen | descriptions warn "PII — screen on egress" but nothing fires |

### A3 — Near-label representation missing
Only a binary `leakage_anchor` exists. The whole funnel design (Part D.9) turns on **near-label** tail
signals (90+ DPD, forbearance/`restructured_flag`, CASS switch, filed SAR, charge-off) that must be
flagged/rejected — currently invisible to enforcement. **Fix: add a `near_label` behaviour** (or a
graded leakage tag), and set it on `restructured_flag`, `impairment_stage`(stage 3), etc.

---

## B. Missing concepts (the coherence gap) — **P0 where they block implemented/flagship templates**

- **P0 · `direct_debit`/`mandate` + `beneficiary`/`payee` (name + destination bank)** — unblock the
  *implemented* churn pilot (Stage-4 unbundling + §A9 primacy-loss). **Highest leverage** (the only
  domain in code).
- **P0 · Hierarchy/ownership** — `customer_group_id`/`ultimate_parent_id` identifier + an
  **ownership-weighted parent→subsidiary edge** (distinct from fraud-ring `relationship_edge`). Without
  it, group-level aggregation — *the* corporate value prop — cannot ground (+ needs the intra-group
  elimination guard, A1).
- **P0 · `debit_credit_indicator`** — every cash-flow feature (`inflow_outflow_ratio`, A4) needs flow
  direction; today it mis-classifies as `boolean_flag`.
- **P0 · Specialist concepts (~30, the biggest cluster)** — insurance (`premium`, `claim_reserve`/IBNR,
  `sum_assured`, `surrender_value`, `reinsurance_recoverable`); custody (`nav`, `settlement_status`/cycle,
  `corporate_action` + ex/record/pay dates); asset-mgmt (`fund`/`share_class`, `fund_flow`, `mandate`,
  `benchmark` — distinct from `benchmark_rate`); **Islamic `profit_rate`** (today mislabeled as interest —
  compliance *and* modeling error) + `purification_amount`/`prohibited_activity_exposure`; ESG
  (`scope_1/2/3_emissions`, `financed_emissions`/PCAF, `taxonomy_alignment`, `emissions_data_quality`);
  payments (`payment_rail`/scheme, `interchange`, `corridor`).
- **P1 · Risk/credit** — `macro_variable`/`scenario_id`/`weight` (IFRS9 forward-looking + CCAR
  unbuildable without it); `recovery_amount`/`write_off_amount`/`cost_to_collect` (LGD); **bureau
  attribute family** (`bureau_score`/`inquiry`/`tradeline` — its own FCRA regime, flagged by *two*
  reviewers); `sicr_flag`, `delinquency_bucket` (ordinal), `exposure_class` (Basel segment),
  `customer_risk_rating` (AML), `expected_loss`, lifetime-PD.
- **P1 · Cross-cutting** — `vulnerability_flag` (FCA Consumer Duty — flagged by *three* reviewers);
  reference-vs-transactional tag (§1.15, different PIT semantics); `model_output` provenance (score/pd/
  esg_score are model outputs → leakage-risk); `data_quality_flag`/`source_system`; `segment`/`peer_group`;
  `scheduled_amount`/contractual-due; markets grain identifiers (`portfolio_id`/`book_id`/`netting_set_id`/
  `desk_id`); `household_id`; `unit_of_measure` guard (non-monetary analogue of `currency_code`).
- **P1 · Wholesale** — `limit`/`limit_type` (a ceiling, NOT a `monetary_stock` — nests, non-fungible),
  `covenant`, `collateral_type`/`lien_seniority`, `netting_set_id`, `margin` (IM/VM), `syndication_share`,
  `lcr`/`nsfr`/HQLA (no *liquidity* metric), IRRBB `pv01`/`repricing_gap`.

---

## C. Missing template families / use-cases — **P1/P2 content depth**

*(Only the strongest per domain; the library has ~130 signals but these clusters are thin or named-only.)*
- **Consumer:** cards/payments is thinnest vs revenue — transactor-vs-revolver, interchange/spend-mix,
  BNPL propensity, cardholder chargeback, rewards engagement, auth/decline. **Wealth has named use-cases
  with zero templates** (portfolio-drift, net-new-money, advisor-attrition, suitability-drift). Retail
  overdraft/NSF family; refi/prepayment incentive; rate-reset payment-shock.
- **Wholesale:** repo/SFT, margin/collateral, IRRBB/repricing-gap, prepayment (CPR/SMM), cash-management/
  working-capital cycle, project/CRE finance (DSCR/LLCR), corporate-financials credit (leverage/coverage/
  Altman-Z), syndication, facility-utilisation depth (dash-for-cash, undrawn→funded), covenant depth,
  XVA depth (FVA/MVA/KVA), prime brokerage. Use-cases: RWA optimisation, RAROC/RORAC, counterparty-limit,
  refinancing/maturity-wall, contingent-liability crystallisation.
- **Risk:** roll-rate/transition-matrix family, vintage/cohort loss curves, macro-overlay (feature ×
  scenario), recovery/workout-cashflow (LGD), model-monitoring (PSI/CSI as features), sanctions/PEP
  screening, survival/hazard (time-to-event). Use-cases: IFRS9 *staging* (distinct from ECL), reject
  inference, affordability, fair-lending *testing* (controlled-purpose carve-out), NPE/forbearance.
- **Specialist:** insurance loss-ratio + reserving/run-off + persistency-curve; custody fails-aging +
  reconciliation-break; AM liquidity-bucketing (gating/swing-pricing); Islamic Sharia-screening +
  profit-distribution; ESG financed-emissions attribution + estimation-cascade; payments auth-optimisation
  + interchange economics + sub-second velocity.

---

## D. Edge cases missed — **P1/P2 (methodology/correctness)**

- **Reject inference / survivorship** (risk) — models observe only booked, surviving accounts; bias
  unacknowledged. **P1.**
- **Competing risks / censoring** (insurance, custody, AM) — the single-outcome funnel meta-pattern is the
  *wrong actuarial frame*: lapse/surrender/death/maturity/claim are competing risks with right-censoring;
  redemption vs maturity is multi-state. **P1.**
- **Bureau bi-temporality** (risk) — bureau files are heavily lagged/restated; retail PD silently leaks
  restated data. **P1.**
- **Netting-set grain + intra-group elimination + revolving-vs-term** (wholesale) — summing trade MTMs or
  group exposures without netting/elimination massively overstates; a zero-drawn revolver still has full
  commitment at risk. **P0/P1.**
- **Settlement-cycle PIT + corporate-action date sequencing** (custody) — a fail isn't knowable until
  T+2; entitlement is fixed at record date, read as-of ex-date. **P1.**
- **Profit-vs-interest** (Islamic) — modeling a Mudaraba deposit as a guaranteed `monetary_stock`
  misrepresents the risk; Sharia-board ratification is a *hard gate* with no eligibility representation.
  **P1.**
- **Emissions cross-scope double-counting + estimated-data provenance** (ESG). **P1.**
- **Real-time/irrevocability window** (payments) — APP-scam scoring must finish before an irrevocable
  event in sub-second time; the trailing-window batch PIT model has no "decide before finality" notion.
  **P1.**
- **Definition-of-default nuances** (risk) — 90-DPD backstop + unlikeliness-to-pay + materiality, diverging
  across Basel/IFRS9/collections; the same `default` label means different things. **P2.**
- **Consumer:** joint accounts (2 customers ↔ 1 account breaks per-customer grain), card reissue/PAN
  change (history splits), balance-transfers/refunds/chargebacks misread as income, packaged accounts,
  dormant→reactivated, deceased/estate. **P1/P2.**

---

## E. is_a / entity-link / classification-target gaps — **P1/P2**

- **Missing `is_a` edges:** `ead`/`ecl`/`rwa`/`provision_amount`/`notional` → no `monetary` parent (would
  let additivity inherit consistently — ties to A1); no abstract `ratio`/`score` parents.
- **Hierarchy vs network conflation:** `relationship_edge` mixes tree edges (group `part_of_group`, per-
  parent summation) with graph edges (fraud rings, degree/community) — they aggregate differently.
- **Missing entity links:** `collateral_id`, `policy_id`/`claim_id`, `case_id`/`alert_id`, `campaign_id`,
  `relationship_manager_id`, `beneficiary_id`, `gl_account`, `obligor_id`/`guarantor_id`, and the markets
  grain ids.
- **Classification collisions** (the enricher can't disambiguate): PD has 4 valid targets
  (`score_probability`/`pd`/`pd_ttc`/`pd_pit`); `ecl` vs `provision_amount`; `impairment_stage` vs
  `lifecycle_state`; `limit` vs `balance` (both inside `monetary_stock`); "exposure" ambiguous;
  external-bureau vs internal score indistinguishable.

---

## Recommended fix sequence

1. **P0 correctness (A) — do immediately, it's shipped-registry safety:** retag the 4 additive stocks →
   semi_additive; retag the under-tagged sensitivities; add a `near_label` behaviour + set it. Small,
   high-value, closes real safety/correctness holes. *(Add the additivity guards — premium, emissions,
   intra-group — as authoring rules.)*
2. **P0 coherence concepts (B):** add `direct_debit`/`beneficiary` (unblocks the implemented pilot),
   `debit_credit_indicator`, the hierarchy/ownership concept, and the ~30 specialist concepts — so the
   template library actually grounds. Fix `profit_rate` (Islamic).
3. **P1 risk/cross-cutting concepts (B):** macro/scenario, recovery/write-off, bureau family,
   vulnerability_flag, model-output provenance, reference-vs-transactional, segment, markets grain ids.
4. **P1 template families + edge-case authoring rules (C, D):** the thin domains + the competing-risks /
   reject-inference / netting-grain / real-time edge rules in Part D.
5. **P2 long tail + is_a/classification (E):** generalisation edges, collision tiebreakers, remaining
   concepts/families.

**Net:** the vocabulary + templates are a strong, broad foundation, but (a) have **real correctness/safety
bugs** in the shipped registry (A), (b) a **coherence gap** where templates outran the concepts (B), and
(c) predictable **depth/edge-case gaps** per line (C/D/E). None is a rewrite — all are additions/retags
along the established axes. Fix **A + the pilot-unblocking B concepts first.**
