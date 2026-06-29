# Banking Domain / Use-Case Catalog

**Status:** Draft (Layer 0 foundation artifact)
**Date:** 2026-06-29
**Implements:** Reference architecture §15 (Domain / Use-Case Catalog) and §15.5 (banking-only scope)
**Seed data:** [`banking-domain-catalog.seed.json`](./banking-domain-catalog.seed.json) — the machine-readable closed catalog the platform loads.

> This makes "banking-only" concrete as a **closed banking boundary with an open, growing use-case set** (architecture §15.5). The platform builds **any banking feature**; the entries here are the **known** use-cases that get the fast path (templates + governance defaults), and a **new banking use-case is onboarded and added** (§15.6), *not* rejected. Only **non-banking** requests (plus out-of-scope latency / policy violations) are refused. Values are **proposed defaults to be ratified** — the registered **domain/risk owner** confirms domain facts and **Compliance** confirms policy facts before a use-case is production-eligible (`compliance_confirmed: false` until then).

---

## 1. What this artifact is

The catalog is the **context** the whole pipeline reads off a feature's `use_case` (architecture §15.2): the generation prior + templates, the allowed/blocked data, the target + scoring metric, and the governance posture. It is a sibling of the Metadata Overlay — integrated/curated, versioned, owner-confirmed. Its **scope is banking (a closed boundary)** but its **use-case set is open**: the entries below are the *known* banking use-cases (fast path); a new banking use-case is **onboarded and added** (§15.6), not rejected. Only non-banking requests are refused.

Each entry follows the §15.1 schema: `use_case, domain, entity, target{name,definition}, primary_metric, feature_templates[], allowed_data_classes[], blocked_data_classes[], risk_tier, regulatory{adverse_action,fair_lending,mrm_tier,aml}, latency, owner, compliance_confirmed, version, status`.

---

## 2. The closed banking taxonomy + evaluation scorecard

Scored 1–5 per the §scorecard criteria (value, data availability/PIT, regulation, leakage sensitivity, fairness exposure, latency fit, feature reuse, time-to-value), rolled to a **wave** (pilot → wave 2 → wave 3 → out of scope).

| use_case | domain | entity | risk_tier | regulation | latency | wave |
|---|---|---|---|---|---|---|
| `retail_churn` | marketing | customer | low | low | batch | **Pilot** |
| `propensity_cross_sell` | marketing | customer | low | low | batch | **Pilot** |
| `customer_lifetime_value` | marketing | customer | low | low | batch | Wave 2 |
| `customer_segmentation` | marketing | customer | low | low | batch | Wave 2 |
| `aml_transaction_monitoring` | financial_crime | customer | medium | AML/BSA, MRM-high | batch | **Wave 2** |
| `kyc_risk_rating` | financial_crime | customer | medium | AML/BSA, MRM-high | batch | Wave 2 |
| `sanctions_screening_support` | financial_crime | transaction | medium | AML/BSA | batch | Wave 3 |
| `application_fraud` | fraud | application | medium | MRM-medium | batch | Wave 2 |
| `account_takeover` | fraud | customer | medium | MRM-medium | batch | Wave 3 |
| `collections_prioritization` | retail_credit | account | medium | fair-lending, MRM-medium | batch | Wave 2 |
| `behavioral_credit_scoring` | retail_credit | customer | high | adverse-action, fair-lending, MRM-high | batch | **Wave 3** |
| `credit_origination` | retail_credit | application | high | adverse-action, fair-lending, MRM-high | batch | Wave 3 |
| `ifrs9_ecl` | retail_credit | exposure | high | MRM-high | batch | Wave 3 |
| `risk_based_pricing` | pricing | application | high | adverse-action, fair-lending, MRM-high | batch | Wave 3 |
| `card_fraud_realtime` | fraud | transaction | high | MRM-high | realtime | **Out of scope** (§1.4) |

**Sequencing rationale:** pilot in **low-regulation, batch-native marketing** (churn / cross-sell) to prove the platform end-to-end and build the governance muscle; then **AML + fraud** (high mandate, batch-friendly, exercises the policy machinery); then **credit risk + pricing** (highest value *and* highest governance — once explainability + fair-lending guards are battle-tested). Real-time card fraud waits for the future serving path.

---

## 3. Three worked entries

### 3.1 `retail_churn` (pilot)
```json
{
  "use_case": "retail_churn", "domain": "marketing", "entity": "customer",
  "target": { "name": "churn", "definition": "no financial transaction for 90 days after as_of_date" },
  "primary_metric": "lift",
  "feature_templates": ["rolling_balance_trend", "login_frequency_change", "inter_event_irregularity", "rfm_recency_frequency_monetary"],
  "allowed_data_classes": ["transactions", "balances", "salary", "product_holdings", "digital_activity"],
  "blocked_data_classes": ["protected_attribute"],
  "risk_tier": "low",
  "regulatory": { "adverse_action": false, "fair_lending": false, "mrm_tier": "low", "aml": false },
  "latency": "batch"
}
```
*Your salary-irregularity hypothesis lives here:* salary is **allowed**, `inter_event_irregularity` is a seeded template, target = churn, scored by lift/IV. Low governance → fast pilot.

### 3.2 `credit_origination` (high governance)
```json
{
  "use_case": "credit_origination", "domain": "retail_credit", "entity": "application",
  "target": { "name": "default_12m", "definition": "90+ days past due within 12 months of opening" },
  "primary_metric": "ks",
  "feature_templates": ["credit_utilization", "delinquency_history_count", "income_stability", "debt_to_income"],
  "allowed_data_classes": ["applications", "credit_bureau", "transactions", "balances", "salary"],
  "blocked_data_classes": ["protected_attribute"],
  "risk_tier": "high",
  "regulatory": { "adverse_action": true, "fair_lending": true, "mrm_tier": "high", "aml": false }
}
```
*Same salary data, different rules:* salary still allowed, but **protected attributes blocked**, **fair-lending proxy checks** fire, **adverse-action → explainability artifact required**, and production needs the **three-party independent-validation** gate.

### 3.3 `aml_transaction_monitoring` (financial crime)
```json
{
  "use_case": "aml_transaction_monitoring", "domain": "financial_crime", "entity": "customer",
  "target": { "name": "sar_filed", "definition": "a SAR was filed within 90 days of the alert period" },
  "primary_metric": "precision_at_k",
  "feature_templates": ["structuring_pattern", "velocity_spike", "counterparty_novelty", "cross_border_ratio", "cash_intensity"],
  "allowed_data_classes": ["transactions", "payments", "counterparty", "geolocation", "kyc_documents", "watchlists"],
  "blocked_data_classes": ["protected_attribute"],
  "risk_tier": "medium",
  "regulatory": { "adverse_action": false, "fair_lending": false, "mrm_tier": "high", "aml": true }
}
```

---

## 4. What is rejected vs. onboarded

The platform builds **any banking feature**. It refuses only what falls **outside banking** (plus per-use-case policy violations and out-of-scope latency). A **new banking** use-case is *onboarded* (§15.6), not rejected:

| Request | Why rejected |
|---|---|
| "Predict which **Netflix shows** a user will watch" | No banking use-case / entity / glossary concept |
| "Score **e-commerce cart abandonment**" | Non-banking domain; `cart` is not a banking entity |
| "Real-time **card-fraud** score at authorization" | Recognized but `latency: realtime` → out of scope (§1.4) |
| "Use **ethnicity** to improve the credit model" | `protected_attribute` blocked for `credit_origination` (fair lending) |
| "Build a churn feature from **salary** for **credit decisioning**" | Cross-use-case data-class violation (salary blocked for credit) |

**Onboarded, not rejected** — a *new banking* use-case (e.g. `mortgage_prepayment`, `deposit_attrition`, `merchant_acquiring_risk`) is **not** refused: at the clarification gate its target, allowed/blocked data, and risk tier are defined, the domain/risk owner + Compliance confirm, the entry is added (versioned), and the feature proceeds (§15.6).

---

## 5. Governance, ownership, versioning

- **Confirmation authority** (architecture §6.5): the **domain/risk owner** ratifies domain facts (entity, target, templates, allowed data); **Compliance** ratifies policy facts (blocked data, regulatory flags, risk tier). `compliance_confirmed` flips true only then.
- **Versioned + immutable history:** each entry has a `version`; changes create a new version (a use-case's governance posture is auditable over time).
- **Onboarding a new banking use-case:** score it on the scorecard → draft the entry → owner + Compliance confirm → add to the closed set. Adding a *non-banking* domain is an explicit scope decision, not a routine catalog edit.

## 6. How it loads into the platform

| Catalog field | Consumed by |
|---|---|
| `feature_templates` | Layer 1 generation prior (§14.2); recurring ones promote to Path-1 DSL ops (§5.2) |
| `allowed_/blocked_data_classes` | Policy-aware Schema Mapper (Layer 3, §12) + overlay use-case policy tags |
| `target`, `primary_metric` | Model-free scoring (Layer 6, §14.3) |
| `risk_tier`, `regulatory`, use-case scoping | Feature-version governance attributes + activation guards (Layer 7) |
| `latency` | Batch-only scope check (§1.4) |
| `symbolic_synthesis` | Enables the optional **interpretable parametric-feature** generation mode (§14.6) — on for credit/pricing only; absent/false = template generation |
| `entity` | Entity/grain resolver (Layer 3) |

When SP-9 (governance) is built, it loads `banking-domain-catalog.seed.json` as the authoritative use-case registry; until then this doc + seed are the design-time source of truth.
