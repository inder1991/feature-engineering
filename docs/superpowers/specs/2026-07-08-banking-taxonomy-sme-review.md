# Banking Taxonomy — SME Review & Improvement Report

**Reviewer stance:** banking subject-matter expert (universal/super-regional bank).
**Scope:** the entities (§1), domains (§2), vocabulary (§3) in `banking-taxonomy-reference.md`.
**Date:** 2026-07-08 · **Verdict:** strong breadth, uneven depth — five whole business lines missing,
the regulatory/capital + time + data-eligibility dimensions under-modelled. Fixable; prioritised below.

> **Status: ALL findings (P0 + P1 + P2) applied to `banking-taxonomy-reference.md`.** Entities: added
> insurance (§1.10), custody/securities services (§1.11), regulatory/finance/accounting (§1.12),
> reference/market-data & tax (§1.13), data-governance & fee (§1.14), lifecycle + reference-vs-
> transactional dimensions (§1.15). Domains: added insurance (§2.15), custody (§2.16), asset management
> (§2.17), Islamic banking (§2.18), ESG (§2.19), payments-as-business (§2.20); deepened financial-crime
> typologies (§2.10) + credit/markets depth notes. Vocabulary: added bi-temporal time (§3.13), currency/
> FX (§3.14), data-eligibility (§3.15), regulatory-capital & accounting spine (§3.16), ESG flags (§3.17).
> Four deterministic safety checks now named in §4 (leakage · point-in-time incl. bi-temporal · currency
> · eligibility).

---

## 1. Executive summary

The taxonomy is **broad and well-structured** for retail, commercial, credit, and financial crime, and
the recent hierarchy/product-family additions are genuinely good. But reviewed as a bank SME, it has
**systematic gaps** in three shapes:

1. **Missing business lines** — insurance/bancassurance, custody & securities services, asset
   management, Islamic banking, ESG/sustainable finance. A universal bank runs these; the taxonomy
   doesn't see them.
2. **Under-modelled regulatory spine** — Basel capital (RWA/PD/LGD/EAD), IFRS9/CECL staging, and the
   accounting basis (fair value vs. amortised cost, impairment) are the *backbone* of a bank's data and
   are barely present.
3. **Thin cross-cutting dimensions** — banking is deeply **bi-temporal** (value date vs. booking date
   vs. as-of; restatement), **multi-currency**, and governed by **data-eligibility beyond fair-lending**
   (consent/purpose, residency, retention). These are under-specified and each is a feature-safety risk.

**Top-5 gaps to fix first (P0):**
1. Regulatory/capital & IFRS9 concepts + entities (the bank's spine).
2. Bi-temporal time model + business-day/value-date semantics (leakage + reporting correctness).
3. Currency/FX consistency (base vs. local, conversion) — a correctness issue like additivity.
4. Data-eligibility dimension (consent/purpose, residency, retention) beyond protected attributes.
5. Insurance + custody/securities-services business lines (whole missing revenue areas).

---

## 2. What's already strong (keep)

- The **structured, behaviour-carrying vocabulary** (additivity/PIT/sensitivity/entity-link) — right idea.
- **Stock-vs-flow** monetary split and **outcome_label as leakage anchor** — correct and important.
- **Corporate hierarchy (§1.9)** + **product families (§1.2a)** + **combined/group exposure** — good depth.
- **Network/graph** concept for AML/fraud — modern.
- **Living/ratifiable** knowledge-base posture — architecturally right.

---

## 3. Findings

### A. Missing business lines / domains

| # | Missing domain | Why it matters | Representative use-cases |
|---|---|---|---|
| A1 | **Insurance / bancassurance** | most universal banks sell life/GI/credit-protection | `lapse_prediction`, `claims_fraud`, `underwriting_risk`, `protection_cross_sell` |
| A2 | **Custody & securities services** | huge fee business (asset servicing) | `settlement_fail_prediction`, `corporate_action_risk`, `securities_lending_demand` |
| A3 | **Asset management (buy-side)** | many banks own an AM arm | `fund_redemption`, `flow_forecast`, `mandate_compliance` |
| A4 | **Islamic banking** | parallel Sharia-compliant product set (Murabaha, Ijara, Sukuk, Takaful) | `sharia_compliance_check`, `profit_rate_pricing` |
| A5 | **ESG / sustainable finance** | fast-growing + regulated (green bonds, SLLs, climate) | `esg_scoring`, `greenwashing_risk`, `climate_transition_risk` |
| A6 | **Payments as a business** (beyond cards) | RTP, correspondent, cross-border/remittance, open banking/BaaS | `payment_fraud_rtp`, `correspondent_risk`, `remittance_aml` |

*(Financial-crime & fraud depth is present but should be deepened with typologies — see D1.)*

### B. Missing entities

- **Insurance:** `policy`, `claim`, `premium`, `beneficiary`, `underwriting_case`.
- **Custody/securities services:** `custody_holding`, `corporate_action`, `settlement_instruction`,
  `fund`, `securities_loan`.
- **Regulatory / finance / accounting:** `gl_account` (general ledger), `cost_center`, `rwa_bucket`,
  `regulatory_report`, `provision_stage` (IFRS9 stage 1/2/3), `capital_component`.
- **Reference / market data:** `instrument_reference`, `index_benchmark`, `rating_agency`,
  `curve` (have), `fx_rate`, `product_master`.
- **Tax:** `tax_lot`, `withholding`, `fatca_crs_classification`.
- **Data governance:** `consent` (have `consent_record`), `data_sharing_agreement`, `purpose`,
  `source_system` (lineage).
- **Fee/billing:** `fee_schedule`, `pricing_tier`, `rebate`.

### C. Missing vocabulary / concepts

- **Regulatory capital:** `risk_weight`, `rwa`, `capital_ratio` (is-a ratio), `pd_ttc` vs `pd_pit`,
  `downturn_lgd`, `ccf` (credit-conversion-factor).
- **Accounting basis:** `fair_value`, `amortised_cost`, `impairment_stage`, `accrual`, `provision_amount`.
- **Rates / curves (post-LIBOR):** `benchmark_rate` (SOFR/SONIA/€STR), `tenor`, `curve_point`, `spread`
  (have), `discount_factor`.
- **FX / currency:** `fx_conversion_rate`, `base_currency` vs `local_currency` — supports the
  don't-mix-currencies rule with an actual conversion path.
- **Collateral specifics:** `haircut`, `margin_requirement`, `ltv` (have as ratio), `advance_rate`.
- **Time (deepen §3.3):** `value_date`, `booking_date`, `business_day_convention`, `reporting_period`,
  plus **bi-temporality** (`valid_time` vs `system_time`) — see E1.
- **Consent / purpose:** `data_purpose`, `consent_status`, `retention_class`, `data_residency` — a
  feature's *eligibility* dimension (see E3).
- **ESG:** `esg_score`, `carbon_intensity`, `green_flag`, `sharia_compliant_flag`.

### D. Under-depth in areas that ARE present

- **D1 — Financial-crime typologies (labels/use-cases):** add the actual patterns — AML: `structuring`,
  `layering`, `trade_based_ml`, `rapid_movement`, `round_tripping`; Fraud: `app_scam` (authorized push
  payment), `synthetic_identity`, `first_party_fraud`, `mule_account`, `card_not_present`,
  `friendly_fraud`. These are the real `outcome_label`s the models predict.
- **D2 — Credit-risk lifecycle & regulatory:** make the lifecycle first-class (origination → behavioural
  → early-warning → collections → recovery → write-off) and the Basel/IFRS9 apparatus explicit
  (PD/LGD/EAD/RWA, staging, IRB, stress CCAR/EBA). Today it's a few named use-cases.
- **D3 — Markets depth:** instruments/positions are thin — add `greeks` (delta/gamma/vega — have
  `sensitivity_greek`), `pnl_attribution`, `xva` (CVA/DVA/FVA), `settlement`/`fail`, `margin`.

### E. Cross-cutting structural gaps (the SME-level, highest-leverage)

- **E1 — Bi-temporal time.** Banking data is restated; regulatory reporting and leakage-safety BOTH
  require *point-in-time-correct* snapshots (what did we know *as of* date X, per the data *as it stood
  then*). The taxonomy has as-of/effective/timestamp but not **valid-time vs. system-time** nor
  **value/booking date**. This is a **feature-correctness** issue (a restated value used as-of the wrong
  time is a subtle leak).
- **E2 — Currency/unit consistency.** Like additivity, mixing currencies in an aggregate is *wrong*.
  Needs `base_currency`, `fx_conversion_rate`, and a deterministic "convert-before-aggregate" rule.
- **E3 — Data-eligibility beyond fair-lending.** A feature's usability depends on **consent/purpose**
  (GDPR purpose limitation), **data residency**, **retention**, and **special-category** data — not just
  protected attributes. This should be a first-class eligibility check alongside leakage + fair-lending.
- **E4 — Product/relationship lifecycle.** origination → active → delinquent → default → closed is a
  cross-cutting state dimension features condition on; make `lifecycle_state` first-class per product.
- **E5 — Reference vs. transactional data.** Banking distinguishes slowly-changing **reference/master
  data** (product master, customer static, instrument reference) from **event/transactional** data. They
  have different PIT semantics (a reference value is "current"; an event is time-stamped). Worth naming.

---

## 4. Prioritised recommendations

**P0 — correctness & regulatory spine (do first, they're safety/correctness):**
- E1 bi-temporal + value/booking date; E2 currency/FX consistency; E3 data-eligibility dimension.
- C regulatory-capital + accounting concepts; B regulatory/finance entities; D2 credit-risk regulatory.

**P1 — coverage of missing revenue (breadth):**
- A1 insurance, A2 custody/securities services, A5 ESG — plus their entities (B) and concepts (C).
- D1 financial-crime typologies (the real labels).

**P2 — depth & long-tail:**
- A3 asset management, A4 Islamic banking, A6 payments-as-business; D3 markets depth; tax/fee/lineage
  entities; E4 lifecycle, E5 reference-vs-transactional as explicit dimensions.

## 5. Severity table

| Area | Gap | Type | Priority |
|---|---|---|---|
| Time | bi-temporal / value-date | **correctness** (leakage/reporting) | **P0** |
| Currency | mix-currency aggregation | **correctness** | **P0** |
| Data eligibility | consent/purpose/residency | **compliance safety** | **P0** |
| Regulatory | Basel/IFRS9/RWA/staging | spine | **P0** |
| Business lines | insurance, custody, ESG | coverage | P1 |
| Fin-crime | typologies (APP, mule, structuring…) | label depth | P1 |
| Markets/credit | XVA, greeks, lifecycle | depth | P2 |
| Long-tail | AM, Islamic, tax, lineage | coverage | P2 |

---

## 6. Bottom line

The taxonomy is a **strong 70%**: excellent structure and retail/commercial/credit/fin-crime breadth,
with real corporate depth. To be a *bank-grade* foundation it needs (a) the **regulatory/capital +
accounting spine**, (b) the **bi-temporal, currency, and data-eligibility** cross-cutting dimensions
(all three are *correctness/compliance*, not nice-to-have), and (c) the **five missing business lines**.
None is a rewrite — they're additions along the axes already established (structured, behaviour-carrying,
ratifiable). Recommend folding the **P0** set in before we lock the taxonomy for the build.
