# Contract-Driven Feature Engineering Platform — Reference Architecture

**Status:** Design (reference architecture)
**Date:** 2026-06-27
**Type:** Vendor-neutral reference architecture (no implementation)
**Audience:** ML platform architects, data platform owners, model-risk / compliance reviewers

---

## 1. Purpose, scope, and boundaries

### 1.1 Purpose

This document specifies the reference architecture for a **data-scientist-led, LLM-assisted, policy-aware, contract-driven, validator-enforced, human-approved, lifecycle-managed feature engineering platform** suitable for a regulated (banking-grade) environment.

The platform accepts the data scientist's intent — a loose **hypothesis** *or* a precise **feature definition** (§14.1) — **generates** a feature implementation from it, and converts that — through a sequence of LLM assistance, deterministic validation, and human confirmation — into a **versioned, monitored, governed production feature**.

### 1.2 The one rule everything else serves

> **The LLM suggests and structures. The platform validates and enforces. The human confirms business meaning. The registry governs the production lifecycle.**

No single actor is trusted to do another's job. This separation is the source of the platform's safety and is repeated as a constraint throughout this document.

### 1.3 What this is *not*

The platform is explicitly **not**:

```
Free text → LLM → SQL → feature store
```

That pipeline is unsafe because it lets an LLM's unverified output reach production. The platform instead routes every feature through a chain of confirmed contracts, deterministic gates, sandbox proof, evaluation, and human approval.

### 1.4 Scope boundaries (read these first)

These boundaries are deliberate decisions, not omissions:

- **Vendor-neutral.** This document states *capability requirements* ("the platform requires a catalog that exposes column-level availability time"), never product names. Binding to specific technologies (warehouse, orchestration engine, feature store, catalog) is left to implementation.
- **Batch-only serving.** Features are computed on a schedule and **materialized** to a store; serving is a lookup of the latest materialized value. Feature freshness is therefore bounded by the batch cadence (minutes to hours). **True sub-second, compute-at-request features (e.g. in-the-moment card-fraud scoring) are out of scope** for this revision. See §16 for the future path.
- **Layer 0 (data catalog) is integrated, not built.** The platform consumes an existing enterprise catalog and *augments* it with a Metadata Overlay (§6). Standing up an enterprise catalog from scratch is a prerequisite, not part of this design.
- **No model training framework.** The platform produces and governs *features*. It evaluates a feature's contribution to a model but does not own model training, deployment, or the model lifecycle. It *integrates with* the bank's model-risk process (§11).
- **Banking-only domain scope (enforced).** This is a **banking-vertical** platform, not a general-purpose feature store. Admissible work is bounded by the **closed banking Domain/Use-Case Catalog** (§15.5); a request whose use-case, entity, or concept does not resolve to that banking taxonomy is **rejected at intake**, never built generically. Banking entities, the banking business glossary, and banking regulatory frameworks (model risk, fair lending, AML, adverse action) are **first-class, not configuration**. (This is orthogonal to *vendor*-neutrality above: vendor-neutral on technology, deliberately *domain-specific* on banking.)

---

## 2. Core principles and the authority model

### 2.1 The authority model

Four actors, four non-overlapping authorities:

| Actor | Authority | May NOT |
|---|---|---|
| **LLM** | Understand, structure, suggest, draft, critique, explain | Approve, override a validator, bypass policy, execute unrestricted queries, mutate a confirmed contract, register a feature |
| **Deterministic validators** | Hard gates — pass/fail on checkable facts | Make business or compliance judgments |
| **Human (data scientist / owner / approver)** | Confirm business meaning; final approve/reject | Skip the deterministic gates |
| **Policy engine + Compliance** | Compliance authority — what data may be used, for what | — |
| **Registry** | Govern the production lifecycle; immutable versioning | — |

### 2.2 The convergent safety floor

The platform supports three ways to *draft* a feature's implementation (§5). They differ only in **who writes the SQL** and the **residual risk tier**. They are identical in one critical respect:

> **Every implementation path — compiler, LLM, or human — converges on the same safety floor: validation → sandbox → data-quality → augmented human review → evaluation → approval → registry → monitoring.**

The path changes the *drafter*, never the *gates*.

### 2.3 What the LLM is and is not allowed to do

**Allowed:** understand free text; normalize feature definitions; extract hypothesis/target/entity/concept; detect ambiguity; suggest assumptions and defaults; draft contracts; suggest schema mappings; draft candidate SQL; critique contracts/mappings/SQL/evaluations; explain logic; summarize results; suggest repairs.

**Not allowed:** approve production features; override deterministic validators; bypass policy; execute unrestricted queries; silently mutate a confirmed contract; register features directly; decide compliance alone; self-certify its own output as safe.

---

## 3. The seven layers

Each layer below lists its **purpose**, **inputs → outputs**, **components**, and the **gate** it enforces before the feature may proceed.

### Layer 0 — Metadata foundation (integration boundary)

**Purpose:** answer the questions every downstream layer depends on — *what data exists, what it means, who owns it, who may use it, when it is available, how it joins, and whether it is safe for this use case.*

**Composition:** the **existing enterprise catalog** (source of truth for what it records) **+ the Metadata Overlay** (§6), which supplies the ML-specific facts the catalog does not record (availability time, asserted grain, SCD effective-dating, approved join graph, use-case-scoped policy tags) **+ the Domain / Use-Case Catalog** (§15), which defines the use-case taxonomy and, per domain, the candidate-feature templates, allowed/blocked data, target definition, scoring metric, and risk tier that drive and constrain generation.

**Gate (Catalog Quality Gate):** a feature may not enter grounding (Layer 3) unless every metadata fact it requires is present and **VERIFIED** in the catalog or the overlay. Missing or unconfirmed facts route to the responsible human (§6).

### Layer 1 — Intake and normalization

**Purpose:** turn the data scientist's intent into a structured *draft* — never executable. Intent arrives in one of **two modes** (§14.1): a loose **hypothesis** (the platform generates candidate definitions) or a precise **feature definition** (the platform translates it faithfully). Intake also **rejects out-of-domain requests** — anything whose use-case/entity/concept does not resolve to the closed banking Domain Catalog (§15.5).

**Inputs → outputs:** free-text hypothesis *or* feature definition → **Draft Feature Contract** + **Assumption Ledger**.

**Components:** Feature Intake UI/API; LLM Intake & Normalization Agent; (hypothesis mode) the multi-candidate generation engine (§14.2), **primed by the Domain/Use-Case Catalog (§15)**.

**Gate:** none yet — output is explicitly marked `status: NEEDS_CLARIFICATION` and cannot proceed past Layer 2 without confirmation.

### Layer 2 — Contract control and human clarification (Human Gate #1)

**Purpose:** eliminate hidden LLM assumptions before any data work begins.

**Inputs → outputs:** Draft Feature Contract → **Confirmed Feature Contract**.

**Components:** Critique Service (`CONTRACT_REVIEW` mode); Ambiguity + Confidence scoring; **Doubt Router**; **Human Clarification Gate**; Contract Refinement Loop; Minimum Contract Validation.

**Gate (hard):** **No Confirmed Feature Contract → no mapping, no compilation, no execution.**

### Layer 3 — Data grounding

**Purpose:** turn business concepts into concrete, *allowed* data references.

**Inputs → outputs:** Confirmed Feature Contract → **Mapped Feature Contract**.

**Components:** Catalog Quality Gate; **Policy-Aware Schema Mapper**; Entity/Grain Resolver; Point-in-Time Mapper; SCD Mapper; Critique Service (`MAPPING_REVIEW` mode).

**Gates (hard):** no table grain → no production feature; no entity key → no production feature; no availability-time metadata → no production feature; no policy classification → no production feature; no approved join path → no production feature.

### Layer 4 — Deterministic validation and implementation routing

**Purpose:** prove the feature is *safe* before any code is generated, then decide *who drafts it*.

**Inputs → outputs:** Mapped Feature Contract → **Validated plan + path assignment**.

**Components:** Deterministic Validation Gate (validation packs, §7); **Implementation Router** (§5); for Path 1, the Feature DSL / Feature Plan Builder; Critique Service (`LOGIC_REVIEW` mode, advisory).

**Gate (hard):** no deterministic validation pass → no SQL generation of any kind.

### Layer 5 — Compilation and sandbox execution

**Purpose:** produce an executable artifact and prove it actually runs correctly on safe data.

**Inputs → outputs:** validated plan / candidate SQL → **sandbox-proven artifact + DQ report**.

**Components:** Feature Compiler (Path 1) or candidate-SQL pipeline (Paths 2/3); LLM self-critique **repair loop** (not a gate, §5.4); SQL Validation Gate; Sandbox Execution (on masked/sampled/synthetic data, §5.5); feature-type-specific data-quality checks.

**Gate (hard):** no sandbox pass → no evaluation.

### Layer 6 — Evaluation and risk review (Human Gate #2 preparation)

**Purpose:** prove the feature is *useful and safe*, not merely runnable.

**Inputs → outputs:** sandbox-proven artifact → **evaluation report + risk assessment**.

**Components:** Offline evaluation via **model-free scoring** (IV/WoE) with an optional throwaway probe (§14.3); stability and out-of-time validation; the **search-overfitting guard** (§14.4); **semantic/proxy leakage detection** (§7.4); fairness/proxy risk gate; cost evaluation; Critique Service (`EVALUATION_REVIEW` mode); **augmented human review** preparation (§5.6).

**Gate (hard):** no evaluation report → no final approval. A feature is **never** approved on a single improved metric, and reaches **USEFULNESS-CHECKED** (§14.5) only after clearing the overfitting guard (§14.4).

### Layer 7 — Approval, registry, and lifecycle

**Purpose:** make the human approval decision, register an immutable version, and keep the feature healthy in production.

**Inputs → outputs:** evaluation report → **registered, monitored production feature**.

**Components:** Final Human Approval (four-eyes / SoD, §11); **Feature Registry** (governance) and **Feature Store** (materialized values) — *separate systems* (§3.1 below); Feature Lifecycle Manager; Monitoring; Change-Impact Analyzer; Revalidation; Deprecation; **Path-2 → Path-1 promotion/harvest** (§5.7).

**Gates (hard):** no human approval → no registry promotion; no monitoring spec → no production feature.

### 3.1 Registry ≠ Store

Two distinct systems, deliberately separated:

- **Feature Registry** — the *governance* system of record. Holds contracts, versions, lineage, approvals, risk tier, policy decisions, monitoring specs, explainability artifacts. Immutable and auditable.
- **Feature Store** — the *materialized values*. Holds the computed feature data (batch-materialized, §1.4), keyed by entity + as-of time, served by lookup.

The registry governs *what a feature is and whether it may exist*; the store holds *its computed values*.

---

## 4. The Feature Contract lifecycle

The contract is the single artifact that flows through the platform, gaining structure and grounding at each stage. Each stage is a distinct, versioned document.

### 4.1 Draft Feature Contract (Layer 1 output)

Not executable. Captures the LLM's structured reading of free text, with everything uncertain marked `UNKNOWN`.

```json
{
  "hypothesis": "Customers with irregular salary credits are more likely to churn.",
  "target": "churn",
  "entity": "customer",
  "feature_concept": "salary irregularity",
  "source_concepts": ["salary credit", "transaction date", "transaction amount"],
  "lookback_window": "UNKNOWN",
  "prediction_time": "UNKNOWN",
  "calculation_method": "UNKNOWN",
  "salary_detection_rule": "UNKNOWN",
  "status": "NEEDS_CLARIFICATION"
}
```

### 4.2 Confirmed Feature Contract (Layer 2 output)

Every P0 field resolved, either by human confirmation or a recorded, human-acknowledged default.

```json
{
  "feature_name": "salary_irregularity_90d",
  "target": {
    "name": "churn",
    "definition": "No financial transaction for 90 days after as_of_date"
  },
  "entity": "customer",
  "entity_key": "customer_id",
  "feature_grain": ["customer_id", "as_of_date"],
  "prediction_time": {
    "column": "as_of_date",
    "rule": "Use only data available before as_of_date"
  },
  "lookback_window": "90d",
  "feature_concept": "salary irregularity",
  "calculation_method": {
    "chosen": "stddev_days_between_salary_credits",
    "considered": [
      "stddev_days_between_salary_credits",
      "coefficient_of_variation_of_amounts",
      "count_missed_expected_paydays"
    ],
    "confirmed_by": "raj (data scientist)"
  },
  "status": "CONFIRMED"
}
```

### 4.3 Mapped Feature Contract (Layer 3 output)

Business concepts bound to concrete, *allowed* columns, with full time and policy mapping.

```json
{
  "feature_name": "salary_irregularity_90d",
  "mapping_status": "MAPPED",
  "entity_mapping": {
    "entity": "customer",
    "entity_key": "customer_id",
    "entity_table": "core.customers",
    "entity_key_column": "core.customers.customer_id"
  },
  "feature_grain": {
    "grain_columns": ["customer_id", "as_of_date"],
    "expected_output_uniqueness": "one_row_per_customer_id_as_of_date"
  },
  "source_tables": [
    {
      "table": "core.transactions",
      "table_grain": "one_row_per_transaction",
      "role": "base_event_table",
      "freshness_sla": "T+1",
      "partition_column": "transaction_date"
    }
  ],
  "source_columns": {
    "customer_id": "core.transactions.customer_id",
    "event_time": "core.transactions.transaction_date",
    "available_time": "core.transactions.posted_at",
    "amount": "core.transactions.transaction_amount",
    "salary_indicator": "core.transactions.transaction_type"
  },
  "filters": [
    { "column": "core.transactions.transaction_type", "operator": "=", "value": "SALARY",
      "purpose": "identify_salary_credit_transactions" }
  ],
  "time_mapping": {
    "prediction_time_column": "as_of_date",
    "event_time_column": "core.transactions.transaction_date",
    "available_time_column": "core.transactions.posted_at",
    "lookback_window": "90d",
    "rule": "transaction_date >= as_of_date - interval '90 days' AND posted_at < as_of_date",
    "future_data_allowed": false
  },
  "policy_tags": {
    "uses_pii": false,
    "uses_sensitive_financial_data": true,
    "uses_protected_attribute": false,
    "requires_compliance_review": true,
    "approved_use_cases": ["churn", "fraud"],
    "blocked_use_cases": ["credit_decisioning"]
  },
  "metadata_provenance": {
    "available_time": "overlay:VERIFIED:maria(payments owner):2026-05-10",
    "table_grain": "overlay:VERIFIED:maria(payments owner):2026-05-10",
    "policy_tags": "overlay:VERIFIED:compliance:2026-05-12"
  },
  "ready_for_validation": true
}
```

### 4.4 Feature Plan (Layer 4 output, Path 1 only)

The trusted intermediate representation for the compiler path. See §5.2.

```json
{
  "feature_name": "salary_irregularity_90d",
  "operation": "stddev_days_between_events",
  "entity_key": "customer_id",
  "as_of_column": "as_of_date",
  "source_table": "core.transactions",
  "event_time_column": "transaction_date",
  "available_time_column": "posted_at",
  "lookback_window": "90d",
  "filters": [{ "column": "transaction_type", "operator": "=", "value": "SALARY" }],
  "calculation": { "type": "stddev_days_between_events", "input_column": "transaction_date", "minimum_events_required": 2 },
  "output": { "feature_column": "salary_irregularity_90d", "grain": ["customer_id", "as_of_date"] }
}
```

---

## 5. The three implementation paths and the Implementation Router

### 5.1 Why three paths

A single safe-but-rigid path handles only features someone pre-built. A single flexible path re-introduces the very risk the platform exists to remove. The resolution: **three drafters, one safety floor (§2.2), each carrying an explicit risk tier.**

| Path | Drafter | Risk tier | Use |
|---|---|---|---|
| **Path 1** | DSL → trusted compiler | **Low** | Common, repeatable feature patterns |
| **Path 2** | LLM candidate SQL | **Medium** | Exploratory / custom features, long tail |
| **Path 3** | Human-authored SQL/PySpark | **Controlled (medium–high)** | Complex long-tail features the DSL can't express |

All three then pass through: SQL validation → sandbox → DQ → augmented human review → evaluation → approval → registry → monitoring.

### 5.2 Path 1 — DSL / compiler (low risk)

The LLM (or router) emits only a **Feature Plan** drawn from a **governed, versioned catalog of trusted operations** (e.g. `count_events`, `days_since_last_event`, `rolling_avg`, `ratio_windows`, `stddev_days_between_events`). The LLM may choose an operation and its parameters; it may **not** invent operations. A trusted, audited compiler turns the plan into SQL. Coverage grows deliberately by adding operations to the catalog — primarily via the harvest loop (§5.7).

### 5.3 Path 2 — LLM candidate SQL (medium risk)

When the concept genuinely cannot be expressed by the DSL catalog, the LLM generates **candidate SQL**. The term is deliberate:

> **LLM-generated SQL is a *candidate implementation*, never an automatically trusted one.** It is untrusted until it passes deterministic validation, sandbox execution, data-quality checks, augmented human review, and approval.

### 5.4 Path 3 — human-authored SQL/PySpark (controlled)

For the complex long tail. The author is named and a justification is recorded. To remain the exception (not the default escape hatch), Path 3 carries additional friction: senior review and explicit justification. Human-authored SQL passes through **exactly the same** validation and sandbox gates as the other paths — human authorship is not a trust shortcut.

### 5.5 The Implementation Router

A component in Layer 4 that decides the path. Its design constraints:

- **Defaults to the safest path.** It selects Path 1 whenever the DSL can express the concept; falls to Path 2 only when it genuinely cannot; Path 3 only when Path 2 is unsuitable.
- **The routing decision and its reason are recorded** in the contract and audit trail.
- **A data scientist may force a *lower*-risk path** (e.g. insist on Path 1) but may **not** silently force a higher-risk path — escalating risk requires the corresponding justification and review.
- The router is biased toward governance: when in doubt, it prefers the safer path and surfaces the choice.

### 5.6 The LLM self-critique is a repair loop, not a gate

Paths 2/3 include an LLM pass that critiques the drafted SQL. Its status is explicit:

> The LLM self-critique is a **cheap, pre-sandbox repair pass** — it catches obvious errors before a sandbox run is spent. **It is not a safety control.** The safety controls are the deterministic SQL validation, the sandbox, and the augmented human review. Nothing in the platform may count the self-critique as a gate; doing so would build in false confidence (a model rarely catches its own correlated blind spots).

### 5.7 Augmented human review (not raw-SQL eyeballing)

Plausible-looking SQL is the hardest to review — e.g. `WHERE transaction_date < as_of_date` looks correct but is leaky if it omits `AND posted_at < as_of_date`. Unaided human review of generated SQL therefore degrades into rubber-stamping. The review screen must instead **surface, not hide**, the platform's analysis:

- the deterministic validation findings,
- the **point-in-time analysis** (with availability-time handling explicitly highlighted),
- the sandbox results and DQ report,
- the evaluation and risk summary.

The deterministic point-in-time check runs **regardless of path** and is always shown to the reviewer.

### 5.8 The harvest / promotion loop

When a Path-2 feature recurs and proves useful, the platform **proposes harvesting it into a Path-1 DSL operation**. This is the mechanism by which the DSL catalog grows from *real demand* rather than guesswork, and by which features migrate from medium-risk to low-risk over time. Without it, the DSL stagnates and everything drifts permanently to Path 2.

---

## 6. The Metadata Overlay (Layer 0 gap handling)

### 6.1 The problem

The platform integrates with an existing enterprise catalog, but general-purpose catalogs reliably hold only names, types, ownership, and *maybe* PII tags. They almost never hold the ML-specific metadata every downstream gate depends on: **column-level availability time, asserted table grain, SCD effective-dating, the approved join graph, and use-case-scoped policy tags.**

### 6.2 The mechanism: overlay-fills-gaps, with a hard floor

The **Metadata Overlay** is a platform-owned, versioned store that *annotates* catalog objects with the missing ML facts. It never replaces the catalog. Reads use a **merged view: catalog first, overlay fills gaps.**

The floor is non-negotiable: **if a required fact exists *nowhere* — not in the catalog, not VERIFIED in the overlay — the feature cannot proceed.** No silent guessing.

### 6.3 What the overlay stores

Per annotated catalog object: availability-time designation; grain assertion; SCD effective-dating columns; approved join keys; use-case-scoped policy tags. Each entry carries: value, **status** (`DRAFT` / `VERIFIED` / `STALE` / `RE-VERIFY`), **who confirmed it**, **when**, and version history.

### 6.4 How entries are created — when and where

**One owning component** (the Metadata Overlay Service) is the sole writer.

Two creation moments:
- **Proactively, at table onboarding** — a data team records ML facts up front (clean path), written as `VERIFIED`.
- **Lazily, during a feature build** — when a gate hits a missing fact, the platform writes a best-guess `DRAFT` (LLM reading + data profiling) and **pauses the feature** pending confirmation.

Two write situations:
- **Write #1 — platform proposes a `DRAFT`** (LLM + profiling). Never trusted on its own.
- **Write #2 — a human confirms → `VERIFIED`** via the metadata console.

### 6.5 Confirmation authority (strict)

- **Data facts** (availability time, grain, joins, SCD) → confirmed only by the table's **registered data owner**.
- **Policy facts** (use-case scoping of sensitive columns) → confirmed only by **Compliance**.
- Neither may sign off on the other's domain. Every confirmation records identity + timestamp.

This gives the platform **segregation of duties on the metadata itself**, at no extra cost — a property a model-risk reviewer will expect.

### 6.6 Freshness (so the overlay can't silently rot)

- Every entry is versioned and dated.
- Entries **expire**: a `VERIFIED` fact older than a configured horizon (e.g. 6 months) flips to `RE-VERIFY` and must be re-confirmed before reuse.
- Catalog changes **invalidate** related entries: a dropped/changed column flips dependents to `STALE` and notifies the owner (driven by the Change-Impact Analyzer, §13).

### 6.7 Entry lifecycle

```
(created) DRAFT ──human confirms──► VERIFIED ──horizon passes──► RE-VERIFY
                                       │                            │
                                       └──table changes──► STALE ◄──┘
                                                            │
                                                    human re-confirms → VERIFIED
```

---

## 7. Deterministic validation packs

Validation packs are the **hard gates**. Each is a deterministic, checkable assertion. Critically, the spec records **what each pack can and cannot catch**, so no pack's "PASSED" is over-read.

### 7.1 The packs

`SchemaValidationPack`, `ColumnTypeValidationPack`, `EntityValidationPack`, `GrainValidationPack`, `JoinSafetyValidationPack`, `PointInTimeValidationPack`, `SCDValidationPack`, `TemporalLeakageValidationPack`, `PolicyValidationPack`, `AccessControlValidationPack`, `ServingFeasibilityValidationPack` (here: batch-materialization feasibility within freshness SLA), `BackfillFeasibilityValidationPack`, `CostValidationPack`, `FeatureDuplicationValidationPack`.

### 7.2 Example validation output

```json
{
  "validation_status": "PASSED",
  "checks": {
    "schema_validity": "PASSED",
    "grain_validity": "PASSED",
    "join_safety": "PASSED",
    "point_in_time_correctness": "PASSED",
    "scd_correctness": "NOT_APPLICABLE",
    "temporal_leakage_check": "PASSED",
    "policy_check": "PASSED_WITH_REVIEW_REQUIRED",
    "serving_feasibility": "PASSED",
    "backfill_feasibility": "PASSED",
    "feature_duplication": "NO_DUPLICATE_FOUND"
  },
  "ready_for_feature_plan": true
}
```

### 7.3 What deterministic validation catches

Temporal correctness and structural safety: column/table existence and types; entity-key presence; output grain/uniqueness; join fan-out safety; **temporal leakage** (use of a column whose `available_time ≥ as_of_date`, future-dated joins, target-in-features); SCD effective-date correctness; policy/access violations; batch-materialization feasibility; backfill feasibility; cost ceiling; duplication against existing and prior features (§7.5).

### 7.4 What deterministic validation CANNOT catch (and where it's handled)

This split is a core safety decision. **"Leakage" is two different problems:**

- **Temporal leakage** — *deterministically decidable*, a hard gate (`TemporalLeakageValidationPack`). Example: using `transaction_date` without also requiring `posted_at < as_of_date`.
- **Semantic / proxy leakage** — *not statically decidable*. Target leakage via a proxy column, label-derived aggregates, train/test contamination through entity overlap, wrong SCD effective-dates in the *source* data. These are caught **downstream**, at evaluation (suspiciously high lift, high target correlation, Layer 6 / §14.3) **plus mandatory human review** — never by the validation gate.

> The word **PASSED** at the validation gate means *temporal and structural* safety only. It must never be read as "leakage-free." Semantic leakage is an evaluation-and-human concern.

A third, related risk — **search-induced overfitting**, when many candidates are generated and scored and the best is kept — is likewise not a validation-gate concern; it is handled at evaluation by the guard in §14.4.

### 7.5 Feature-duplication checks the full space

`FeatureDuplicationValidationPack` checks the proposed feature against **approved, in-flight, and rejected** features — not just the approved registry. Rejected features carry their **rejection reason**, so the same bad feature is not re-litigated from scratch (knowledge accumulation, §9.4).

---

## 8. The Critique Service

### 8.1 One service, five modes

A single reusable service, not a sprawl of independent critique agents. Modes: `CONTRACT_REVIEW`, `MAPPING_REVIEW`, `LOGIC_REVIEW`, `CODE_REVIEW`, `EVALUATION_REVIEW`. Output is always structured:

```json
{
  "review_type": "MAPPING_REVIEW",
  "status": "NEEDS_REVIEW",
  "findings": [
    {
      "severity": "HIGH",
      "category": "AMBIGUOUS_MAPPING",
      "evidence": "Both transaction_type and payroll_category_code can identify salary.",
      "recommendation": "Ask human to confirm salary detection rule.",
      "blocks_progress": true
    }
  ]
}
```

### 8.2 Where the Critique Service IS and IS NOT authoritative

LLM-critiquing-LLM has correlated blind spots and sycophancy risk. The spec is therefore precise about each mode:

| Mode | Role | Authoritative? |
|---|---|---|
| `CONTRACT_REVIEW` | Detect ambiguity, assess business-meaning plausibility | **Strong LLM fit** — advisory input to Human Gate #1 |
| `MAPPING_REVIEW` | Spot ambiguous/implausible mappings | **Strong LLM fit** — advisory input to mapping confirmation |
| `LOGIC_REVIEW` | Sanity-check feature logic | **Advisory only** — overlaps deterministic validators, which win |
| `CODE_REVIEW` | Catch obvious SQL errors | **Advisory / repair only** — deterministic validation + static analysis are the real controls |
| `EVALUATION_REVIEW` | Summarize and flag evaluation results | **Advisory** input to Human Gate #2 |

> The Critique Service is a **challenger**, never a gate. The deterministic validator is the hard gate; the human is the business authority; the policy engine is the compliance authority. The Critique Service **may not silently rewrite a confirmed contract.**

---

## 9. State machine and failure routing

### 9.1 States

```
DRAFT
NEEDS_HUMAN_CLARIFICATION
HUMAN_FEEDBACK_RECEIVED
REFINED_BY_LLM
READY_FOR_CONTRACT_VALIDATION
CONFIRMED_CONTRACT
CATALOG_QUALITY_FAILED
NEEDS_DATA_STEWARD
SCHEMA_MAPPED
MAPPING_REVIEW_FAILED
VALIDATION_FAILED
READY_FOR_FEATURE_PLAN
PATH_ASSIGNED
FEATURE_PLAN_CREATED
COMPILATION_FAILED
COMPILED
SANDBOX_FAILED
SANDBOX_PASSED
DQ_FAILED
EVALUATION_FAILED
READY_FOR_APPROVAL
APPROVED_EXPERIMENTAL
APPROVED_PRODUCTION
REGISTERED
PRODUCTION
MONITORING_ALERT
REVALIDATION_REQUIRED
DEPRECATED
REJECTED
POLICY_BLOCKED
```

### 9.2 Failure routing

| Failure | Routes to |
|---|---|
| Ambiguous business meaning | Human clarification (Gate #1) |
| Catalog/overlay metadata missing | Data owner / Compliance (per §6.5) |
| Mapping ambiguity | Human / data owner |
| Temporal leakage failure | Reject or redesign |
| Compiler failure | Compiler repair loop (bounded) |
| Sandbox shape failure | Feature-plan repair (bounded) |
| Policy failure | Compliance review or hard reject |
| Weak evaluation | Reject or experimental backlog |
| Production drift | Revalidation |
| Upstream schema change | Change-impact analysis |

### 9.3 Durable execution (orchestration capability requirement)

The state machine is a **durable, long-running, human-gated workflow** — some features will sit for days awaiting a data owner or Compliance. The platform therefore requires a **durable workflow engine** (vendor-neutral) that can:

- **persist** a feature's state across days/restarts;
- **retry with hard loop limits** — no infinite compiler/plan repair loops; after N attempts a feature fails to a human;
- put **human gates on a clock** — SLA → reminder → escalation → auto-park if unanswered, with named ownership of parked features;
- maintain a **replayable, immutable audit trail** of every state transition.

Binding to a specific engine is left to implementation.

### 9.4 Knowledge accumulation

`REJECTED` and `POLICY_BLOCKED` are not dead ends: their reasons are retained and consulted by `FeatureDuplicationValidationPack` (§7.5) so the platform does not repeatedly re-propose features it has already rejected.

---

## 10. Hard gates (the non-negotiable list)

```
No confirmed feature contract        → no mapping.
No catalog/overlay quality pass      → no mapping.
No access authorization              → no mapping.
No availability-time metadata        → no production feature.
No entity/grain resolution           → no validation.
No temporal point-in-time validation → no compilation.
No deterministic validation pass     → no feature plan / no SQL.
No feature plan or candidate SQL     → no compiler/sandbox.
No sandbox pass                      → no evaluation.
No evaluation report                 → no final approval.
No human approval                    → no registry promotion.
No monitoring spec                   → no production feature.
```

---

## 11. Governance (full, framework-aware)

This platform is intended for credit-, fraud-, and risk-adjacent features, which fall under bank model governance. The architecture treats governance as a first-class set of requirements, referencing regulatory *concepts* while remaining jurisdiction-neutral.

### 11.1 Model-risk management hooks

Features feeding governed models must integrate with the bank's **model-risk framework** (e.g. SR 11-7-style expectations): registration in the **model/feature inventory**, support for **independent validation** (an independent reviewer can reproduce the feature and its evaluation), and conformance to **documentation standards**. The Feature Registry exposes the interfaces the bank's MRM process consumes.

### 11.2 Explainability as a registered artifact

Features used in customer-impacting decisions (e.g. credit) require explainability for **adverse-action** obligations. Business explainability is therefore not a checkbox but a **first-class, versioned registry artifact** attached to the feature.

### 11.3 Segregation of duties (mechanically enforced)

- **Requester ≠ approver.**
- **Four-eyes** for compliance-sensitive features.
- Metadata SoD per §6.5 (data owner vs Compliance).
- Roles are named and the workflow engine enforces these separations; they are not advisory.

### 11.4 Audit and reproducibility

- **Immutable versioning** of every artifact: feature contract, mapped contract, feature plan, generated artifact, schema snapshot, **prompt version, LLM model version, validator version, compiler version**, evaluation dataset reference, approval decision, owner, SLA, refresh frequency, monitoring rules, lineage.
- **Reproducibility requires data, not just code.** To recreate a feature value for a regulator, point-in-time recompute requires **immutable / time-travelable source data** (snapshots or table time-travel). The architecture states this as a requirement on the data platform; without it, point-in-time recompute is unverifiable.
- Every state transition is audit-logged with actor identity and timestamp.

### 11.5 Immutability rule

> **Approved feature versions are immutable. Any change creates a new version.**

---

## 12. Security and access control

```
RBAC for feature creation and approval
Purpose-based (use-case-scoped) access control
Policy-aware schema exposure
PII / sensitive metadata masking
Audit logs for every state transition
LLM prompt/response versioning
No raw sensitive data sent to the LLM unless explicitly approved
Sandbox isolation
Query cost limits
Row-level and column-level access enforcement
Approval segregation of duties
Compliance review for sensitive features
Immutable lineage
```

**Policy-aware exposure rule:** the Schema Mapper shows only the columns the user **and the use case** are permitted to use. A column may be available for fraud detection yet blocked for credit decisioning — the mapper enforces this at exposure time, not after the fact.

**Sandbox data (a real constraint in banking):** the sandbox runs on **masked / tokenized, representatively sampled, or synthetic** data — never unrestricted raw PII. The spec records the tradeoff: a feature can pass on masked data and behave differently on the real distribution; downstream monitoring (§13) is the backstop.

**Platform cost control:** beyond the per-feature `CostValidationPack`, the platform itself bounds its own cost — per-request LLM/sandbox/evaluation budgets, caching of repeated LLM and metadata lookups, and batching — so high request volume does not become unbounded spend.

---

## 13. Monitoring and lifecycle

### 13.1 What the Lifecycle Manager monitors

```
freshness SLA · null rate · distribution drift / PSI · schema changes ·
source-table delays · pipeline failures · feature usage by models ·
cost trend · owner acknowledgement · revalidation status · deprecation candidates
```

Monitoring **intensity and re-review cadence scale with the feature's risk tier** (§5.1): Path-2 (LLM-SQL) features are watched more closely and re-reviewed more often than Path-1 features.

### 13.2 Change-Impact Analyzer

On any upstream change (schema change, column-semantic change, new code values, freshness-SLA breach, source pipeline delay) the analyzer:

```
find dependent features → rerun validators → rerun a sandbox sample →
alert owners → pause promotion if needed → trigger reapproval if semantics changed
```

It also flips dependent **overlay** entries to `STALE` (§6.6).

### 13.3 Risk tier is a lifecycle attribute

A feature's risk tier (from its implementation path) follows it into production. It governs **allowed use cases** (e.g. Path-2 features may be barred from credit-decisioning until promoted), **monitoring intensity**, and **re-review cadence**. Promotion of a recurring Path-2 feature to Path-1 (§5.8) lowers its tier.

---

## 14. Feature generation, scoring, and staged verification

The platform's responsibilities include **generating a feature implementation from the data scientist's intent**, not only validating one handed to it. This section specifies how generation works (bounded by human intent), how candidates are scored **without a trained downstream model**, and the honest verification status every feature carries. These additions adapt the LLM-FE program-search approach (arXiv:2503.14434), deliberately subordinated to the authority model (§2.1): the engine *proposes and scores*; the gates and the human *decide*.

### 14.1 Two intake modes

Intent arrives in one of two forms, which need different amounts of generation:

- **Hypothesis-driven (loose intent → platform generates).** A belief, not a formula — e.g. *"customers with irregular salary credits are more likely to churn."* The platform proposes multiple candidate feature definitions/calculations (§14.2) for the scientist to confirm at Human Gate #1.
- **Definition-driven (precise spec → platform translates).** The scientist states the exact feature — e.g. *"stddev of days between SALARY transactions over a 90-day lookback, grained by customer × as_of_date."* There is little to invent; the platform **grounds, compiles, and validates faithfully** rather than generating alternatives.

Both modes converge on the same Confirmed Feature Contract (§4.2) and the same safety floor (§2.2).

### 14.2 Multi-candidate generation and scored selection (assistive, bounded)

For hypothesis-driven intake the platform generates **several candidate definitions** of the concept instead of silently committing to one — this resolves the calculation-method ambiguity flagged in §4.2. For "salary irregularity" it might generate: stddev of inter-credit gaps; coefficient of variation of amounts; count of missed expected paydays.

What keeps this safe and human-led:

- **Domain-primed.** The candidate set is seeded by the **Domain/Use-Case Catalog (§15)** — the use-case's known feature templates, allowed data, and target — so generation proposes *domain-salient* features on permitted columns, not generic transforms.
- **Bounded, not autonomous.** The engine explores the *implementation space of the scientist's hypothesis*, never an open-ended space of all possible features. This bounding preserves the "data-scientist-led" principle and limits search-induced overfitting (§14.4).
- **Proposes, never approves.** Candidates flow through the normal pipeline; the human confirms at Gate #1 and approves at Gate #2.
- **Attempt memory + diversity.** The platform keeps a persistent memory of *every* attempt — definition, score, and (for rejects) the reason — and uses it to bias future suggestions and avoid re-proposing dead ends. To avoid converging prematurely on one mediocre idea, it keeps several diverse candidate lines alive in parallel (the "islands" pattern). This upgrades the duplication check (§7.5) and the harvest loop (§5.8) from static dedup into an active learning memory.
- **Fast discard.** Candidates exceeding configured runtime/memory caps are dropped immediately during exploration, before consuming a full sandbox/evaluation cycle.

Each surviving candidate is **scored (§14.3)** and the scientist is shown ranked options with evidence — a confirmation, not a blind guess.

### 14.3 Scoring candidates without a trained model

LLM-FE scores candidates by training a model in-loop. A feature platform usually has **no production model available at build time**, and may not even have data yet. Scoring is therefore **tiered by what is available**, and the platform is explicit about which tier it reached.

**Tier 1 — model-free, label-aware scoring (default when labels exist).** The Confirmed Contract already defines the target (e.g. the churn definition, §4.2). Using a point-in-time-correct historical sample of `(feature_value, label)`, predictive power is measured with **no model**:

- **Information Value (IV) / Weight of Evidence** — the banking standard. Bucket the feature; compare the target rate across buckets; large movement = strong feature (rule of thumb: IV > ~0.1 is worth keeping).
- Equivalently, mutual information or single-feature AUC.

Example: bucketing "salary irregularity" yields churn rates 10% → 20% → 40% across low/medium/high → IV ≈ 0.18 (strong). A flat churn rate across buckets → IV ≈ 0 (useless).

**Tier 1b — throwaway probe for *incremental* value (optional).** To ask "does this add information beyond features we already have," train a small **disposable** model (shallow gradient-boosted tree or logistic regression) on `existing` vs `existing + candidate` and compare. This probe is a measuring stick, **not** the production model, and is discarded after scoring.

**Tier 0 — no label available.** Predictive power cannot be measured honestly. The platform scores only **quality and novelty** (coverage / non-null rate, variance, stability over time, redundancy vs existing features) and marks the feature **"predictive value unverified."** It never fabricates a score.

The historical scoring sample must itself be **point-in-time correct** (feature computed as-of past dates using only then-available data); otherwise the score is leaky and misleading.

### 14.4 The search-overfitting guard

Generating and scoring **many** candidates and keeping the best introduces a multiple-comparisons risk: the winner may look good **by chance**, and the risk grows with the number of candidates tried. (LLM-FE does not guard against this; for banking it is mandatory.)

> **Guard:** any feature selected from a multi-candidate search must re-clear its score on a **separate out-of-time (and/or nested) holdout** before approval. The bar scales with how many candidates were tried. A winner whose score collapses out-of-time was a fluke and is rejected.

This guard is *in addition to* the temporal-leakage gate (§7.3) and the semantic/proxy-leakage detection (§7.4).

### 14.5 Staged verification stamp

Different checks need different inputs. Every feature carries an explicit, honest stamp of **how far it has actually been verified**, so a metadata-only candidate is never mistaken for a production-ready feature:

```
DESIGN-CHECKED      (needs only schema + definitions + overlay)
   plausible (LLM) · columns/types/grain valid · point-in-time safe ·
   policy-allowed · not a duplicate

DATA-CHECKED        (needs a data sample — sandbox, Layer 5)
   actually runs · null rates acceptable · values sane · no key duplication

USEFULNESS-CHECKED  (needs data + labels — scoring, §14.3)
   predictive signal confirmed (IV / probe) · survives the overfitting guard (§14.4)
```

A feature may be **DESIGN-CHECKED** with only schema, column names, and definitions — a real and valuable result (it is sensible, safe, and well-formed) — but it is **not production-eligible** until it reaches **USEFULNESS-CHECKED**. The stamp is a registry attribute carried with the feature version, and the **hard approval gate (§10) requires USEFULNESS-CHECKED for production promotion.**

---

## 15. The Domain / Use-Case Catalog

Feature engineering is domain-specific: the *same* raw data yields different features, on different permitted columns, against different targets, under different governance, depending on the **business domain (use-case)**. The **Domain / Use-Case Catalog** is the Layer 0 foundation artifact that encodes this — a sibling of the Metadata Overlay (§6), and a **closed banking taxonomy** (§15.5). Like the overlay it is **integrated/curated, not invented per feature**, is versioned, and is confirmed by the responsible owners (the domain/risk owner for domain facts; **Compliance** for policy facts, §6.5).

### 15.1 What a catalog entry holds

Each `use_case` is a governed record:

```json
{
  "use_case": "retail_churn",
  "domain": "marketing",
  "entity": "customer",
  "target": { "name": "churn", "definition": "no financial transaction for 90 days after as_of_date" },
  "primary_metric": "lift",                       // the §14.3 scoring metric for this domain
  "feature_templates": ["rolling_balance_trend", "login_frequency_change",
                        "inter_event_irregularity", "rfm_recency_frequency_monetary"],
  "allowed_data_classes": ["transactions", "balances", "digital_activity", "salary"],
  "blocked_data_classes": ["protected_attribute"],
  "risk_tier": "low",
  "regulatory": { "adverse_action": false, "fair_lending": false, "mrm_tier": "low" },
  "latency": "batch",
  "owner": "marketing-analytics",
  "compliance_confirmed": true,
  "version": 3
}
```

Contrast `credit_origination`: `risk_tier: high`, `regulatory.adverse_action: true`, `fair_lending: true`, `mrm_tier: high`; salary allowed but `protected_attribute` blocked; explainability artifact required.

### 15.2 What it feeds (five hooks into the pipeline)

The catalog is the **context** every stage reads off the feature's `use_case`:

1. **Generation prior + templates (Layer 1, §14.2).** `feature_templates` seed the candidate set so generation proposes *domain-salient* features (churn → balance-decline, login-drop) rather than generic transforms; recurring templates promote into Path-1 DSL operations (§5.2).
2. **Policy-scoped grounding (Layer 3, §12).** `allowed_/blocked_data_classes` drive the policy-aware Schema Mapper — *salary allowed for fraud, blocked for credit decisioning; protected attributes blocked for lending.*
3. **Target + scoring (Layer 6, §14.3).** `target` and `primary_metric` are what model-free scoring (IV/WoE) ranks candidates against — no domain target, no usefulness score.
4. **Governance guards (Layer 7).** `risk_tier`, `regulatory`, and the use-case scoping populate the feature-version governance attributes and the activation guards (a feature may not activate into a blocked use-case or above its tier ceiling); `adverse_action` makes the explainability artifact required.
5. **Serving/latency check.** `latency` is checked against the batch-only scope (§1.4); real-time-only domains (in-the-moment card fraud) are flagged out of scope.

### 15.3 Hard rule

> Every feature carries a `use_case` that **must resolve to a Domain Catalog entry**. Generation, grounding, scoring, and governance all key off that entry. A feature whose `use_case` is unknown to the catalog cannot proceed — the same fail-closed discipline as the Metadata Overlay (§6.2).

### 15.4 Relationship to existing use-case scoping

The catalog is the *source* of the use-case facts already referenced throughout: `approved_use_cases`/`blocked_use_cases` (§4.3), policy-aware exposure (§12), and risk-tier-as-lifecycle-attribute (§13.3). Previously those values were assumed; the catalog is where they are **defined, owned, and versioned** — and how a new domain is onboarded (score it, define its templates/policy/target/tier, have the owners + Compliance confirm).

### 15.5 Banking-only scope (closed taxonomy)

This platform is a **banking vertical**, not a general-purpose feature store — an *enforced* scope, not a preference:

- **The Domain Catalog is a *closed* banking taxonomy.** Admissible use-cases are the banking set only — e.g. `retail_churn`, `credit_origination`, `behavioral_credit_scoring`, `collections`, `ifrs9_ecl`, `application_fraud`, `account_takeover`, `aml_transaction_monitoring`, `kyc_risk`, `sanctions_screening`, `propensity_cross_sell`, `clv`, `risk_based_pricing`. (Real-time card fraud is recognized but out of scope per §1.4.) A `use_case` outside the set has no catalog entry and is **rejected at intake** under the §15.3 fail-closed rule.
- **Banking entities are first-class.** The entity/grain resolver (Layer 3) resolves only banking entities — customer, account, product, transaction, counterparty, exposure, application — and nothing else.
- **A banking business glossary** (delinquency, DPD, charge-off, utilization, SAR, structuring, churn/attrition, …) primes the LLM intake and generation so concepts map to *banking* meaning; a non-banking concept fails to ground.
- **Banking regulatory frameworks are built-in, not configured:** model risk (SR 11-7-style), fair lending / disparate impact, adverse-action explainability (ECOA-style), AML/BSA, and PII/data-residency — they parameterize the governance guards (Layer 7) and the policy-aware mapper (§12).

**Out-of-domain rejection:** if a hypothesis cannot be resolved to a banking use-case, a banking entity, and banking-glossary concepts, intake **rejects it with a clear reason** rather than generating a generic feature. The platform never silently builds a non-banking feature.

---

## 16. Open questions and future work

- **Real-time (sub-second) serving.** Out of scope here (§1.4). A future revision adds a single-definition-compiled-to-both path (offline + online) with **mandatory equivalence testing**, and keeps **Path-2 LLM SQL batch-only** (un-vetted generated SQL must never reach a real-time path it can't be equivalence-proven on).
- **DSL coverage metric.** Track the percentage of real requests Path 1 can express; use it to prioritize the harvest loop (§5.8).
- **Calibration of LLM confidence.** The Doubt Router should rely on the **deterministic P0 field list** for blocking decisions and treat LLM-reported confidence only as a tunable hint (LLM confidence is poorly calibrated).
- **Compile-target breadth.** This revision assumes a single primary compile target; SQL/PySpark/dbt multi-target is future work (each target multiplies compiler surface area).
- **Concurrency.** Define behavior for simultaneous near-identical requests, deprecation racing adoption, and mid-flight schema changes.

---

## Appendix A — End-to-end summary (plain language)

```
Data scientist gives the idea.
LLM turns it into a draft contract.
Platform generates several candidate versions and scores them without a model (churn rates / Information Value).
Human confirms the important doubts (including which math defines the concept), choosing from scored options.
Schema Mapper connects it to real, allowed data (catalog + overlay).
Validators prove it is temporally and structurally safe.
The Router picks who writes the SQL (compiler, LLM, or human) — safest path first.
Sandbox proves it runs correctly on safe data.
Evaluation proves it is useful — re-checks the winner on a different time period (so it isn't luck), and checks for semantic leakage and fairness.
A human approves it, with the platform's analysis surfaced (not raw SQL).
Every feature carries an honest stamp: DESIGN-CHECKED → DATA-CHECKED → USEFULNESS-CHECKED.
The Registry records an immutable version; the Store holds its batch-materialized values.
The Lifecycle Manager keeps it healthy, and promotes good LLM features into the DSL.
```
