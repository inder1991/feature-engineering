# Contract-Driven Feature Engineering Platform — Reference Architecture

**Status:** Design (reference architecture)
**Date:** 2026-06-27
**Type:** Vendor-neutral reference architecture (no implementation)
**Audience:** ML platform architects, data platform owners, model-risk / compliance reviewers

---

## 1. Purpose, scope, and boundaries

### 1.1 Purpose

This document specifies the reference architecture for a **data-scientist-led, LLM-assisted, policy-aware, contract-driven, validator-enforced, human-approved, lifecycle-managed feature engineering platform** suitable for a regulated (banking-grade) environment.

The platform accepts an unstructured feature *hypothesis* from a data scientist in natural language and converts it — through a sequence of LLM assistance, deterministic validation, and human confirmation — into a **versioned, monitored, governed production feature**.

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
- **Batch-only serving.** Features are computed on a schedule and **materialized** to a store; serving is a lookup of the latest materialized value. Feature freshness is therefore bounded by the batch cadence (minutes to hours). **True sub-second, compute-at-request features (e.g. in-the-moment card-fraud scoring) are out of scope** for this revision. See §14 for the future path.
- **Layer 0 (data catalog) is integrated, not built.** The platform consumes an existing enterprise catalog and *augments* it with a Metadata Overlay (§6). Standing up an enterprise catalog from scratch is a prerequisite, not part of this design.
- **No model training framework.** The platform produces and governs *features*. It evaluates a feature's contribution to a model but does not own model training, deployment, or the model lifecycle. It *integrates with* the bank's model-risk process (§11).

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

**Composition:** the **existing enterprise catalog** (source of truth for what it records) **+ the Metadata Overlay** (§6), which supplies the ML-specific facts the catalog does not record (availability time, asserted grain, SCD effective-dating, approved join graph, use-case-scoped policy tags).

**Gate (Catalog Quality Gate):** a feature may not enter grounding (Layer 3) unless every metadata fact it requires is present and **VERIFIED** in the catalog or the overlay. Missing or unconfirmed facts route to the responsible human (§6).

### Layer 1 — Intake and normalization

**Purpose:** turn free text into a structured *draft* — never executable.

**Inputs → outputs:** free-text hypothesis → **Draft Feature Contract** + **Assumption Ledger**.

**Components:** Feature Intake UI/API; LLM Intake & Normalization Agent.

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

**Components:** Offline evaluation (lift, stability, out-of-time); **semantic/proxy leakage detection** (§7.4); fairness/proxy risk gate; cost evaluation; Critique Service (`EVALUATION_REVIEW` mode); **augmented human review** preparation (§5.6).

**Gate (hard):** no evaluation report → no final approval. A feature is **never** approved on a single improved metric (§6 evaluation rules).

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
- **Semantic / proxy leakage** — *not statically decidable*. Target leakage via a proxy column, label-derived aggregates, train/test contamination through entity overlap, wrong SCD effective-dates in the *source* data. These are caught **downstream**, at evaluation (suspiciously high lift, high target correlation, §6 Layer 6) **plus mandatory human review** — never by the validation gate.

> The word **PASSED** at the validation gate means *temporal and structural* safety only. It must never be read as "leakage-free." Semantic leakage is an evaluation-and-human concern.

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

## 14. Open questions and future work

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
Human confirms the important doubts (including which math defines the concept).
Schema Mapper connects it to real, allowed data (catalog + overlay).
Validators prove it is temporally and structurally safe.
The Router picks who writes the SQL (compiler, LLM, or human) — safest path first.
Sandbox proves it runs correctly on safe data.
Evaluation proves it is useful — and checks for semantic leakage and fairness.
A human approves it, with the platform's analysis surfaced (not raw SQL).
The Registry records an immutable version; the Store holds its batch-materialized values.
The Lifecycle Manager keeps it healthy, and promotes good LLM features into the DSL.
```
