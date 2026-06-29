# SP-0 — Foundations: Design Spec

**Status:** Design (sub-project spec) · **Revision 4** (incorporates three architecture-review rounds)
**Date:** 2026-06-27
**Sub-project:** SP-0 (Phase A — Foundations)
**Parent:** [Reference architecture](./2026-06-27-feature-engineering-platform-design.md) · [Build roadmap](./2026-06-27-feature-engineering-platform-roadmap.md)
**Type:** Vendor-neutral design + a clearly-marked sample-stack appendix

> **Revision history.**
> **Rev 2** — first review: identifier model, event schema evolution, versioned state machine, guard contract, DAG document lineage, normative Draft schema, rich identity + command authz, human-gate model, external-side-effect handling, projection/outbox mechanics, privacy/retention, tests.
> **Rev 3** — second review: separate run/feature aggregates; guard-failure events; external-idempotency caveats; fail-closed state projections; security-audit stream; `raw_input` privacy; replay modes; document schema registry.
> **Rev 4** — third review (independent multi-lens). Adds: a **Request aggregate** (home for multi-candidate generation); a normative **feature-version governance-attribute schema** (verification stamp, risk tier, use-case scoping, required-artifact refs); **use-case-scoped + experimental activation**; the **run-approval → activation handoff saga** with CAS on a base version; **feature-lifecycle table versioning**; aggregate-keyed queue partitioning; **candidate→primary promotion** events; **three-party SoD / independent validation**; break-glass review; inbound lifecycle signals; consumer/dependency edges; concept-claim reservation; cost-budget circuit breaker; attempt-memory store; tightened interfaces; unified command/authz vocabulary; naming cleanup; expanded tests.
>
> **Framing:** SP-0 provides the **mechanism** (typed slots, guards, events, aggregates) to *represent and enforce* governance; the **policies/values** (what a risk tier permits, verification thresholds) are owned by SP-9/SP-10/SP-12. After Rev 4, operational detail (saga timing, GC sweeps, queue tuning) is the domain of the **implementation plan**, not further document review.

---

## 1. Purpose and scope

SP-0 is the **backbone** every other sub-project plugs into: the substrate on which feature work is stored, advanced, audited, and governed. It contains **no business logic and no governance policy** of its own — it provides the typed structures, guards, and durable machinery those are expressed in. Rails, not trains.

### 1.1 In scope

- The **four-aggregate identifier model** (request / feature / run / version) and the relationships between them.
- The **event store**, **event + document schema registries**, **projections**, and the **attempt-memory** store.
- The **immutable staged document DAG** (with candidate→primary promotion) and the normative **Draft** schema.
- The **feature-version governance-attribute schema** (typed slots; policies owned elsewhere).
- Two **versioned state machines** (run workflow, feature lifecycle) with a guard contract + failure events; the **lifecycle command catalog**; the **run-approval → activation handoff saga**.
- The **durable runtime** (atomic transaction boundary + blob-GC, aggregate-keyed outbox/relay, idempotent handlers, external-effect handling with stale-result guards, durable timers + cost-budget breaker, classified retries).
- **Identity, authentication, command authorization** (humans *and* services), **structural SoD incl. three-party independent validation**, **break-glass review**, and the **security-audit stream**.
- The **privacy/retention** model (body classification, crypto-shred, replay modes) and the reproducibility envelope.
- The **interfaces** sub-projects implement against (step handlers, human gates, commands, queries).

### 1.2 Out of scope (owned elsewhere; they run *on* SP-0)

- Intake/mapping/validation/compile/sandbox/evaluation *logic* (SP-2…SP-7).
- Governance **policy/values**: risk-tier semantics, verification thresholds, use-case permission matrices, monitoring cadences (SP-9/SP-10/SP-12). SP-0 stores and enforces the *typed attributes and guards*; the values are theirs.
- **Data-access** control — column/row permissions, purpose-based exposure (SP-9/SP-11). SP-0 still owns *command/transition* authorization (§6.2).

### 1.3 Design decisions (ledger)

| Decision | Choice |
|---|---|
| Aggregates | **Request**, **Feature**, **Run**, immutable **Version** |
| Identifiers | `request_id` → (`feature_id`, n×`run_id`) → `feature_version_id` |
| Contract storage | Immutable staged documents, DAG lineage, explicit primary-promotion events |
| State & audit | Event-sourced + projections; audit *is* the event stream; denials in a security stream |
| Schema evolution | Versioned registries (events + documents), total/chained upcasters, pinned per run |
| State machines | Two versioned tables (run workflow, feature lifecycle); guard contract + failure events |
| Cross-aggregate | Approval mints a version in-tx; **activation is a CAS-guarded saga** with a base version |
| Runtime | Roll-your-own; aggregate-keyed partitioning; cost-budget breaker; blob-GC |
| Identity / authz | Rich identity (human + service) + command authz + structural SoD (incl. 3-party validation) |
| Governance | SP-0 = typed slots + guards (mechanism); values/policies → SP-9/SP-10/SP-12 |
| Immutability | Body classification (pii-erasable vs governance-retained), crypto-shred, two replay modes |

---

## 2. Aggregates and the identifier model

Four aggregates, each with its own event stream:

| Id / aggregate | Meaning | Lifecycle |
|---|---|---|
| **`request_id`** (Request) | One *ask* (hypothesis or definition); home of **multi-candidate generation**. | Opens at intake; owns 0..n candidate runs; resolves by selecting/closing candidates. |
| **`feature_id`** (Feature) | A *logical feature*; tracks active version(s) + production lifecycle. | Minted when a candidate run is **selected** to pursue a concept (new) or referenced (change). Stable across versions. |
| **`run_id`** (Run) | One **candidate version / workflow run** — one attempt to create/change the feature. | The workflow state machine runs on it; ends `APPROVED_*` or terminal-rejected. |
| **`feature_version_id`** (Version) | One *immutable, approved* realization with governance attributes (§3.8). | Minted frozen at approval; activated/superseded at the feature level; never edited. |

**Cardinality & minting:**
- A request → **0..n candidate runs**, and may yield **1..n features** (parent §14.2 hypothesis intake can produce genuinely distinct features). The `request_id → feature_id` relationship is therefore **1:n**, not 1:1.
- `feature_id` is minted by a **`select_candidate`** event (§4.4) at Human Gate #1 for a *new* concept, or is the existing `feature_id` for a *change* run. Before selection, candidate runs are grouped only by `request_id`.
- **Concept-claim reservation (resolves "two near-identical requests in flight," parent §15):** `create_run`/intake places an **advisory optimistic claim** on a normalized concept key. A second concurrent request hitting the same key is pointed at the in-flight one (a `DUPLICATE_OF` candidate-link); first-committed wins. The claim is advisory (never blocks legitimate distinct work) but provides the foundation-level converge point the parent flagged as missing.

---

## 3. The data model

### 3.1 Aggregate storage

```
request_id  (REQUEST AGGREGATE)
   ├── request event stream     (candidate creation, candidate selection/rejection, duplicate links)
   └── request projections      (candidate runs + their scores/states; ranked options for Gate #1)

feature_id  (FEATURE AGGREGATE)
   ├── feature event stream     (version activation/supersession; production lifecycle)
   ├── feature_versions         (immutable versions + governance attributes, §3.8; supersession chain)
   ├── consumers                (dependency edges: which models/features consume this)
   └── feature projections      (active version per use-case; production state)

run_id  (RUN AGGREGATE, 0..n per feature/request)
   ├── run event stream         (the workflow; append-only, versioned, globally sequenced)
   ├── document DAG             (frozen stage documents, candidate/primary, §3.4)
   └── run projections          (current workflow state, work-queues, cost-budget counter)

attempt-memory  (CROSS-AGGREGATE, §3.9)
```

### 3.2 Event stream (the source of truth)

A single normative envelope across all streams; the **identity field is `actor` everywhere** (no `created_by`):

```json
{
  "event_id": "evt_01HZ...",
  "global_seq": 480213,
  "request_id": "req_01HZ...",
  "feature_id": "feat_01HZ...",          // null on pure request-stream events
  "run_id": "run_01HZ...",               // null on request-/feature-stream events
  "aggregate": "run",                    // request | feature | run
  "stream_version": 7,                   // per-aggregate-instance optimistic-concurrency counter
  "type": "CONTRACT_CONFIRMED",
  "schema_version": 3,
  "actor": { /* §6.1 identity envelope */ },
  "occurred_at": "2026-06-27T10:14:22Z",
  "recorded_at": "2026-06-27T10:14:22Z",
  "payload": { "confirmed_contract_ref": "doc_01HZ..." },
  "caused_by": "evt_01HZ...",
  "provenance": { /* §8 */ },
  "table_version": 12                    // pinned state-machine table version (run or feature, §4.2)
}
```

- `stream_version` — **per aggregate instance** (per-run for run events, per-feature for feature events, per-request for request events) — drives **optimistic concurrency**: append only if still at the expected version.
- `global_seq` — monotonic across **all** streams; projections consume in `global_seq` order (§3.6). (Generation: a single logical sequence allocator; the implementation plan owns whether it's a DB sequence, a Lamport-style hi-lo, etc. — the *contract* is strict monotonicity + no gaps that block ordering.)
- Events are never edited/deleted (subject to §9). Corrections are compensating events.

### 3.3 Event schema registry and evolution

- An **event-type registry**: each `type` has registered `schema_version`s (JSON-schema per version), a **schema-owning sub-project** (owns the type's shape and upcasters — *not* "the reducer," since many projections reduce a type), and registered **upcasters**.
- **`schema_version` stamped at write**; **upcast-on-read** to the consumer's expected version. Upcasters are **pure, total** (never throw/partial — a partial upcaster would create a poison event, §3.6) and **chained stepwise** (v1→v2→v3), each step registered.
- **Backward-compatible (concretely):** add optional field, widen a type, add an enum value consumers treat as "unknown." Anything else is **breaking** → new `schema_version` + mandatory upcaster, or a new type (cross-type migration is an explicit projector rule, since upcasters are within-type).
- **Deprecation lifecycle:** a `schema_version` may be marked `deprecated` (no new writes) then `withdrawn` (only reachable via upcast); in-flight documents/events at a withdrawn version remain readable via the upcaster chain. **Runs pin the registry snapshot version** (in `provenance`) so replay is deterministic.

### 3.4 Immutable staged document DAG

Stage documents (Draft → Confirmed → Mapped → Plan, plus first-class artifacts — see §3.8) are **frozen**, linked as a **DAG**:

```json
{
  "doc_id": "doc_01HZ...",
  "request_id": "req_01HZ...",
  "feature_id": "feat_01HZ...",
  "run_id": "run_01HZ...",
  "stage": "CONFIRMED_CONTRACT",         // from the published stage/artifact enum (§3.7)
  "schema_version": 2,
  "derived_from": ["doc_01HZ...(draft)"],   // inputs; MUST reference already-committed docs
  "supersedes": ["doc_01HZ...(prior)"],     // replacement
  "branch_role": "candidate",            // candidate | primary | rejected | repair — IMMUTABLE per doc
  "created_at": "2026-06-27T10:14:22Z",
  "actor": { /* identity */ },
  "content_hash": "sha256:...",
  "provenance": { /* §8 */ },
  "body_ref": "blob_01HZ..."             // payload by reference (§9)
}
```

Rules:
- **Write-once.** `branch_role` is immutable per document. Promotion of a candidate to primary is **not a mutation** — it is an explicit **`PRIMARY_SELECTED` event** (emitted by the responsible handler or human gate per stage), which records the chosen `doc_id`. There is **no in-place flip.**
- **"Current primary for a stage"** = the document named by the latest `PRIMARY_SELECTED` event for `(run_id, stage)` ordered by **`global_seq`** (never `created_at`/wall-clock). A uniqueness invariant enforces **one live primary per `(run_id, stage)`**.
- **Acyclicity is guaranteed by construction:** write-once + `derived_from`/`supersedes` may only reference **already-committed** documents (lower `global_seq`), so no cycle can form.
- A stage may hold **multiple candidate documents** (multi-candidate generation, parent §14.2; or repair attempts). `branch_role: rejected` carries a reason (feeds dedup, parent §7.5).

### 3.5 The normative Draft schema (owned by SP-0)

```json
{
  "request_id": "req_01HZ...",
  "intake_mode": "hypothesis | definition",
  "raw_input_ref": "blob_01HZ...",
  "raw_input_classification": "contains_pii | clean | unscanned",
  "hypothesis": "…", "target": "churn | UNKNOWN", "entity": "customer | UNKNOWN",
  "feature_concept": "salary irregularity | UNKNOWN", "source_concepts": ["…"],
  "candidate_calculations": ["…"],
  "open_fields": ["lookback_window", "prediction_time", "calculation_method"],
  "assumption_ledger_ref": "doc_01HZ...",
  "status": "NEEDS_CLARIFICATION"
}
```

- **`raw_input` privacy:** free text may contain PII/secrets, so it is **never inline** — classified (PII scan), optionally redacted, written to an **encrypted, access-restricted blob**, referenced by `raw_input_ref` + `raw_input_classification`. Satisfies §9.
- **Unknown handling:** unresolved fields are `UNKNOWN` and listed in `open_fields`; a Draft with non-empty `open_fields` cannot pass Gate #1.
- **Defaults** are recorded in the Assumption Ledger (field/value/rationale), never silently inlined.
- SP-0 validates the envelope + required-field presence; semantic validation is SP-2's.

### 3.6 Projections (read models)

- **Global ordering** via `global_seq`; each projection keeps a **checkpoint** and exposes **lag** (`checkpoint` vs `head`); reads may be tagged **as-of a `global_seq`** for stale-read awareness.
- **Fail closed for state-bearing projections:** a workflow/lifecycle/work-queue projection that cannot apply an event marks the affected aggregate `degraded` and **blocks downstream commands for it** until cleared (never skip-and-continue, which would let work proceed on a false view). The **`resolve_degraded`** command (§4.4) clears it after remediation. Only **analytics** projections may fail open.
- **Migration:** new projection built in parallel from `global_seq=0`, reads switch atomically.
- **Rebuild:** any projection is droppable and deterministically rebuildable.

### 3.7 Document / artifact schema registry

Mirrors §3.3 for documents/artifacts: each stage/artifact type has registered `schema_version`s, a **schema-owning sub-project**, total/chained **reader-upcasters**, the same backward-compat + deprecation lifecycle, and runs pin the doc-registry snapshot. The **stage/artifact enum is published normatively** here:
`DRAFT_CONTRACT, ASSUMPTION_LEDGER, CONFIRMED_CONTRACT, MAPPED_CONTRACT, FEATURE_PLAN, CANDIDATE_SQL, VALIDATION_REPORT, SANDBOX_RESULT, DQ_REPORT, EVALUATION_REPORT, RISK_ASSESSMENT, EXPLAINABILITY, MONITORING_SPEC, APPROVAL_RECORD`. These are **first-class artifact types** (not "attached artifacts"), each attachable to a run and/or a `feature_version`.

### 3.8 Feature-version governance attributes (mechanism, not policy)

So the parent's hard approval gates are *representable and enforceable*, SP-0 defines a normative typed attribute schema on each `feature_version` (the **values/policies** are owned by SP-9/SP-10/SP-12):

```json
{
  "feature_version_id": "fv_01HZ...",
  "feature_id": "feat_01HZ...",
  "produced_by_run": "run_01HZ...",
  "base_feature_version_id": "fv_01HY...",        // the active version this run started from (per use-case); null if first
  "verification_stamp": "USEFULNESS-CHECKED",     // DESIGN-CHECKED | DATA-CHECKED | USEFULNESS-CHECKED (parent §14.5)
  "risk_tier": "medium",                          // parent §13.3; lowered by harvest
  "approval_type": "PRODUCTION",                  // EXPERIMENTAL | PRODUCTION
  "approved_use_cases": ["churn", "fraud"],
  "blocked_use_cases": ["credit_decisioning"],
  "required_artifact_refs": {
    "evaluation_report": "doc_…", "risk_assessment": "doc_…",
    "explainability": "doc_…", "monitoring_spec": "doc_…"
  },
  "dsl_operation_catalog_version": "ops@v9",       // pinned for Path-1 reproducibility (parent §5.2)
  "approval": { "conditions": ["…"], "expires_at": "2026-12-31", "max_uses": null,
                "reviewed_evidence_refs": ["doc_…"] },
  "immutable": true
}
```

Approval and activation transitions carry **guards over these declared inputs** (e.g. `verification_stamp == 'USEFULNESS-CHECKED'` for production promotion, parent §10/§14.5; `monitoring_spec` present; target use-case ∉ `blocked_use_cases`; `risk_tier` ≤ the use-case ceiling). SP-0 supplies the slots + guard hooks; the thresholds are policy.

### 3.9 Attempt-memory store (cross-aggregate)

Multi-candidate generation fast-discards candidates **before** they become runs/documents, yet the parent needs (a) the **count of candidates explored** to set the overfitting bar (§14.4) and (b) cross-feature dedup against **discarded and rejected** ideas (§7.5). SP-0 provides a durable **attempt-memory** projection/store: `{definition_hash, score, disposition, reason, request_id?, feature_id?}`. `candidates_explored_count` + search scope are recorded in run/request `provenance`. Attempt-memory entries are **non-PII by construction** (hashes/metadata) and **exempt from routine crypto-shred** so dedup knowledge survives erasure of source bodies.

---

## 4. The state machines

### 4.1 Declarative definition, guard contract, failure events

Run-workflow and feature-lifecycle are both **declarative tables**. A transition:

```json
{
  "from": "CONFIRMED_CONTRACT", "to": "SCHEMA_MAPPED", "trigger": "MAPPING_COMPLETED",
  "guard": "confirmed_contract_exists AND catalog_quality_passed",
  "guard_inputs": { "confirmed_contract_exists": "confirmed_contract_ref",
                    "catalog_quality_passed": "catalog_quality_result_ref" },
  "precedence": 100,
  "on_success": { "to": "SCHEMA_MAPPED", "emits": "FEATURE_MAPPED" },
  "on_guard_fail": { "to": "MAPPING_REVIEW_FAILED", "emits": "GUARD_FAILED" }
}
```

**Guard contract:**
- **Predicate registry:** guard predicates (`confirmed_contract_exists`, …) are registered functions, each declaring its **input binding** (predicate → declared `guard_inputs` ref). "Pure, reads only declared inputs" is therefore mechanically checkable. Predicates are **pure and deterministic**: no side effects, no I/O, and **may read frozen documents/version-attributes but not mutable projections** (so replay is deterministic).
- **Selection & failure:** when several transitions match a trigger, the highest-`precedence` one is selected (ties = registration-time error). A **failed guard routes to `on_guard_fail` — no fall-through** to lower-precedence transitions.
- **Symmetric typing & audit:** both `on_success` and `on_guard_fail` declare a target **state** *and* an emitted **event** (`GUARD_FAILED`/`TRANSITION_REJECTED`), carrying the resolved inputs + boolean result in `provenance`. Every outcome is an audited event.

### 4.2 State-machine versioning (both tables)

- **Two versioned tables:** `run_transition_table_version` (run workflow) and `feature_lifecycle_table_version` (feature lifecycle). Each aggregate **pins** its table version; events stamp it (`table_version`, §3.2). Replay uses the pinned version, so changing a table never alters historical replay or strands in-flight aggregates.
- Migration is explicit/audited (`migrate_workflow_version` for runs; `migrate_feature_lifecycle_version` for features), recording the old→new mapping.
- Runs/features also pin the **event- and document-registry snapshot versions** (§3.3/§3.7).

### 4.3 States

- **Run-level (workflow):** `DRAFT … CONFIRMED_CONTRACT … SCHEMA_MAPPED … READY_FOR_APPROVAL … APPROVED_EXPERIMENTAL | APPROVED_PRODUCTION | REJECTED | POLICY_BLOCKED`. **Terminal:** `REJECTED`, `POLICY_BLOCKED`; or `APPROVED_*` (which mints a version and hands off, §5.8).
- **Feature/version-level (lifecycle):** an active version is `ACTIVE_EXPERIMENTAL` or `PRODUCTION`; `PRODUCTION → MONITORING_ALERT → REVALIDATION_REQUIRED → (PRODUCTION | DEPRECATED)`; `PRODUCTION → DEPRECATED`. **`PRODUCTION` is long-lived, not terminal.** Revalidation-requiring-change **starts a new run**.
- **Use-case-scoped active map:** the feature aggregate holds **`active_versions: { use_case → feature_version_id }`** (not a single pointer), so v2 can be active for fraud while v1 stays active for credit, and experimental and production versions coexist per use-case.
- **Terminal overall:** `DEPRECATED`, `REJECTED`, `POLICY_BLOCKED`.

### 4.4 Lifecycle command catalog

Every command has a §6.2 authz row and emits an audited event. **R** = run, **F** = feature, **Q** = request aggregate.

| Command | Agg | Effect |
|---|---|---|
| `create_request` | Q | Open a request; place concept-claim (§2) |
| `create_run` | Q→R | Open a candidate run under a request → `DRAFT` |
| `select_candidate` | Q | Choose a candidate run at Gate #1; mint/bind `feature_id`; close siblings (`reject_sibling_candidates`) |
| `submit_human_signal` | R/F | Answer a human-gate task (clarify / confirm-data / confirm-policy / approve / independent-validate) — §7 |
| `open_task` | R/F | Open a human-gate task (emitted by a handler/gate) — §7 |
| `cancel` / `withdraw` | R | Stop an in-flight run → `REJECTED` (reason recorded) |
| `reject` | R | Reviewer rejection → `REJECTED` (reason feeds dedup) |
| `reopen_as_new_run` | Q→R | Rejected runs are terminal; this opens a **new run** linked (`reopened_from`) to the rejected one, preserving its rejection knowledge |
| `park` / `unpark` | R | Suspend/resume a run; parked runs leave active queues with a named owner |
| `resolve_degraded` | any | Clear a `degraded` projection entry after remediation (§3.6) |
| `activate` | F | CAS-activate an approved version into a use-case (§5.8) |
| `supersede` | F | Activate a new version over the prior active one in a use-case (prior stays immutable) |
| `deprecate` | F | Active version → `DEPRECATED` (guarded by consumer check, §6.3/§4.4-note) |
| `retier` | F | Change `risk_tier` (e.g. harvest Path-2→Path-1 lowers it) — dual-controlled |
| `register_consumer` / `deregister_consumer` | F | Maintain dependency edges (models/features consuming this) |
| `raise_monitoring_alert` / `require_revalidation` / `record_revalidation_outcome` | F | Inbound lifecycle signals (service-actor authz) |
| `fact_confirmed_resume` | R | Inbound signal: a blocking fact (e.g. overlay confirmation) arrived → wake parked runs waiting on that fact |
| `duplicate_of` | Q | Link a request/candidate to an existing feature (parent §7.5) |
| `manual_retry` | R | Operator-initiated business-repair retry (§5.6) |
| `admin_correct` | any | Compensating/correction event (privileged, dual-control) |
| `migrate_workflow_version` / `migrate_feature_lifecycle_version` | R / F | Move to a newer table version (§4.2) |

**`deprecate` guard:** blocked (or forced through impact-analysis + a quiesce/grace transition) when active **consumers** exist (resolves "deprecation racing adoption," parent §15).

---

## 5. The durable runtime (roll-your-own, made safe)

### 5.1 Atomic transaction boundary (+ blob GC)

One atomic DB transaction per step:

```
BEGIN
  append domain event(s)  (stream_version check)
  insert document(s)      (frozen, §3.4; derived_from must reference committed docs)
  upsert timer rows       (§5.5)
  record processed-message ledger   (§5.3)
  insert outbox message(s)          (§5.2 / §5.4)
COMMIT
```

A failed `stream_version` check rolls back the whole transaction. **Blob lifecycle:** large/PII bodies are written to the blob store **before** the transaction and referenced by id+hash. A rollback therefore leaves an **orphan blob** (possibly containing sensitive input), so SP-0 requires **unreferenced-blob GC**: a mark-and-sweep against committed `*_ref` references, with **quarantine** for sensitive orphans, retention/erasure handling per §9, and **audited** GC runs.

### 5.2 Transactional outbox and relay

- Outbox write is in the §5.1 transaction; a **leased relay** publishes (publish-then-mark-sent; a crash between yields a harmless at-least-once duplicate, §5.3); **DLQ** + **stuck-message detection**; **backpressure** via durable waiting.
- **Partitioning by aggregate key** — `run:{run_id}`, `feature:{feature_id}`, `request:{request_id}` — so feature-/request-level events (where `run_id` is null) get per-aggregate ordering too. (Fixes run-only partitioning.)

### 5.3 Idempotent handlers + optimistic concurrency

Append with the expected `stream_version` (duplicate → no-op) + a processed-message ledger keyed by message id. Stops double-advancing one aggregate. (Ledger growth: pruned by `global_seq` watermark once all projections pass it.)

### 5.4 External side effects (and honest limits)

- **External-command outbox:** side-effecting calls (LLM, sandbox, metadata writes, uploads) are recorded as an external command with an **idempotency key** in the §5.1 transaction; a dispatcher executes and writes the result back as an event.
- **Honest caveat:** exactly-once-effect requires the external system to **honor the idempotency key** *or* expose a **reconcilable job handle**. Without either, a dispatcher crash after the call but before the result write **can re-invoke**; this residual is **logged and flagged as an accepted risk per integration** (no false dedup claim). High-cost/non-idempotent integrations (e.g. sandbox jobs) **must** provide a key or handle.
- **Stale-result acceptance guard:** a result event carries `expected_run_id` + `expected_stream_version`/`expected_task_id`. If the run was cancelled/superseded/advanced past the awaited point, the result is **accepted-and-ignored as stale** (or routed to compensation) — never blindly applied to a moved-on run.
- **Result caching by idempotency key** avoids paying twice (parent §12 cost control).

### 5.5 Durable timers and race resolution

Durable `timers` rows fired by a poller, resolved against a named **business calendar**. Ladder: SLA → reminder → escalation → **auto-park**. **Timer/answer race:** both attempt the transition under an optimistic **CAS on the gate task's version** (§7); first writer wins; a firing timer takes a lease and is cancelled atomically when the gate is answered, so a late timer can't escalate an answered gate. (Missed fires after long downtime: the poller fires overdue timers on recovery, idempotently by `idempotency_key`.)

### 5.6 Retry semantics + cost-budget breaker

- **Delivery retry (infra):** transient failure → backoff+jitter to a per-message budget → DLQ. No state change.
- **Business repair loop (workflow):** valid failure routes to a failure state; bounded `N` attempts, each an **attempt event**; exhaustion → human. `manual_retry` re-arms.
- **Classification:** retryable (transient) vs permanent (deterministic, skips delivery retry). `max_elapsed_time` caps total.
- **Cost-budget circuit breaker:** a durable **per-run/per-request cost counter** (LLM/sandbox/eval spend, candidate count). On ceiling, the run **auto-parks to a human** (mirrors the §5.5 ladder) — bounding the multi-candidate × repair-loop × sandbox cost amplifier (parent §12).

### 5.7 Crash recovery

Automatic: truth in the event store, work driven by the outbox; crashed worker/relay/dispatcher resumes from durable state.

### 5.8 Run-approval → activation handoff (saga)

Approval and activation cross an aggregate boundary (run → feature), so they cannot be one transaction. The handoff is an explicit **saga**:

1. A run reaching `APPROVED_*` **mints the `feature_version_id` (frozen, with §3.8 attributes) in the run's own transaction**, and emits an **activation-request outbox command** to the feature aggregate. The run is now terminal; the version exists but is **not yet active**.
2. The feature aggregate processes activation **idempotently** with a **CAS on the expected active version**: each run records `base_feature_version_id` (the active version, per use-case, it started from); activation succeeds only if the feature's current active version for that use-case is still that base. **This prevents two runs both branching from v1, both approving, and the later one silently overwriting the first** — the loser's activation fails CAS.
3. **On CAS failure / activation failure:** the version stays minted-but-inactive and routes to a human as `ACTIVATION_CONFLICT` → revalidate against the new base (a new run), or supersede explicitly. A transient feature-side failure retries (idempotent); it never leaves a half-applied state because activation is a single feature-aggregate transaction.
4. `APPROVED_EXPERIMENTAL` activates into `ACTIVE_EXPERIMENTAL` (use-case-scoped); experimental versions carry an **`expires_at`** (a timer auto-deactivates them, §3.8/G13) so experimental approvals don't silently become permanent.

---

## 6. Identity, authentication, authorization

### 6.1 Identity envelope (humans and services)

```json
{
  "subject": "user:raj | service:intake-agent",
  "authenticated": true, "auth_method": "oidc | workload-identity",
  "actor_kind": "human | service",
  "role_claims": ["data_scientist"],          // AT TIME OF ACTION
  "groups": ["payments-ds"], "tenant": "retail-bank",
  "on_behalf_of": null, "impersonation": null, "break_glass": false,
  "source_of_authority": "iam-snapshot@2026-06-27T10:14Z",
  "attestation": "signed-deploy-id:sp2-intake@1.4.0"   // services: how the role_claims are attested
}
```

**Service actors** are first-class and **most transitions are emitted by services** (handlers). Their authority is **bootstrapped via workload/deploy identity** (`attestation`), not self-asserted: a service's `role_claims` are issued by the platform's identity system against its signed deploy identity, and the authz table (§6.2) has explicit service rows.

### 6.2 Command authorization (one vocabulary; humans + services)

Data-access RBAC is deferred to SP-9, but **command-level authorization lives in SP-0**. **One canonical action vocabulary** — every §4.4 command has a row; approval/confirmation are `submit_human_signal(gate=…)` rows:

| Action | Permitted |
|---|---|
| `create_request`, `create_run`, `submit_human_signal(gate=CLARIFICATION)` | data scientist (request owner); `service:intake-agent` for system-initiated |
| `submit_human_signal(gate=DATA_STEWARD)` | registered **data owner** of the table |
| `submit_human_signal(gate=COMPLIANCE)` | **Compliance** |
| `submit_human_signal(gate=INDEPENDENT_VALIDATION)` | validator role, **disjoint from author and approver** (§6.3) |
| `submit_human_signal(gate=FINAL_APPROVAL)` | approver role, **subject ≠ requester** (§6.3) |
| `select_candidate` | data scientist (request owner) |
| handler-emitted workflow events (`open_task`, stage transitions) | **service** actors with attested role for that step |
| `activate`/`supersede`/`deprecate`/`retier` | release/owner role; **four-eyes** for compliance-sensitive (§6.3) |
| inbound lifecycle (`raise_monitoring_alert`, `require_revalidation`, `fact_confirmed_resume`, …) | `service:monitoring`/`service:overlay` (attested) |
| register/modify handlers or **either state table** | platform-admin (deploy identity) |
| read domain audit | auditor / compliance / owner (scoped) |
| read **security stream** | security/compliance only (scoped); not feature owners |
| `admin_correct`, `break_glass` | platform-admin, **dual control** |

**Denials:** unauthorized attempts are recorded in the **security-audit stream** (append-only, tamper-evident), **not** the domain stream — avoiding existence leakage / governed-stream spam. Security-stream **read access is restricted** (security/compliance), and it has a **regulator-driven retention class exempt from routine crypto-shred**, with a separate redaction path for any embedded PII. A denial appears in the domain stream only if the actor is already authorized to know the aggregate exists.

### 6.3 Segregation of duties

- **Two-party (four-eyes):** requester ≠ approver at `FINAL_APPROVAL`; also enforced on `activate`/`supersede`/`deprecate` for compliance-sensitive features.
- **Three-party (MRM independent validation, parent §11.1):** for high-risk-tier / customer-impacting versions, an **`INDEPENDENT_VALIDATION`** gate with a **disjoint-subject guard: author ≠ validator ≠ approver**. Its outcome is a registered `RISK_ASSESSMENT`/validation artifact.
- **Confirmation authority:** data facts → data owner; policy facts → Compliance (parent §6.5).
- **Break-glass:** dual-control to invoke, **plus a mandatory `break_glass_review` gate** (independent reviewer, SLA, sign-off event) after the fact.

---

## 7. Human-gate API and task model

A gate is a durable **task** with its own version and lifecycle:

```json
{
  "task_id": "task_01HZ...", "task_version": 1,
  "run_id": "run_01HZ...", "feature_id": null,
  "gate": "CLARIFICATION | DATA_STEWARD | COMPLIANCE | INDEPENDENT_VALIDATION | FINAL_APPROVAL",
  "required_inputs": ["confirmed_contract_ref"],   // the inputs whose change invalidates a pending answer
  "eligible_assignees": { "role": "data_owner", "scope": "core.transactions" },
  "allowed_responses": ["confirm", "edit", "reject"],
  "quorum": { "required": 1, "of_role": "data_owner" },
  "delegation_allowed": true, "sla": "7d",
  "status": "open"   // open | answered | conflict | expired | cancelled | superseded
}
```

- **Opened by `open_task`** (a handler or gate), not implicitly. **Eligible assignees** (role+scope); only eligible or validly-delegated subjects may answer; **allowed responses** enumerated; **quorum** (default 1) of *distinct* subjects; **conflicting** quorum answers → `conflict` + escalation; **delegation** records `on_behalf_of`; **duplicate** submissions idempotent by `(task_id, subject)`.
- **Staleness is keyed to the task, not the run** (fixes the deadlock): an answer is rejected only if the task's **`required_inputs` changed** since it opened (tracked by `task_version`), **not** because the run's `stream_version` advanced for unrelated reasons (reminders, timers, sibling-candidate writes, comments). The **timer/answer race** uses CAS on `task_version` (§5.5).
- **Cancellation:** if the run advances past the gate or is cancelled/superseded, the task → `cancelled`/`superseded` and late answers are refused.

---

## 8. Audit, reproducibility, replay modes

Audit *is* the event streams. The **provenance envelope** on events/documents:

```json
{
  "artifact_type": "CONFIRMED_CONTRACT", "schema_version": 2,
  "producing_component": "sp2-intake@1.4.0",
  "tool_versions": { "llm_model": "…", "prompt_version": "…", "validator": "…", "compiler": "…" },
  "dsl_operation_catalog_version": "ops@v9",
  "source_snapshots": ["delta:core.transactions@v8821"],
  "event_registry_snapshot": "events@v37", "doc_registry_snapshot": "docs@v11",
  "evaluation_dataset_ref": "doc_…", "holdout_partition_spec": "oot:2025H2", "random_seed": 42,
  "candidates_explored_count": 3,
  "external_refs": ["llm_call:idem_…", "sandbox_run:…"]
}
```

(`artifact_type` casing now matches the §3.7 stage/artifact enum.) `evaluation_dataset_ref` + `holdout_partition_spec` + `random_seed` make the IV/probe score and the overfitting-guard re-check (parent §14.3/§14.4) reproducible and un-gameable.

**Two replay modes:** **full replay** (bodies intact → reconstructs state *and* values) and **privacy-degraded audit replay** (after crypto-shred → skeletons + provenance reconstruct the decision trail; erased bodies/values unrecoverable). The platform labels which mode and which artifacts are degraded. Reproduction pins the run's table versions + registry snapshots + `dsl_operation_catalog_version` against immutable `source_snapshots`.

---

## 9. Privacy, retention, immutability bounds

- **No raw PII/secrets in events or document bodies** — references only; sensitive bodies (incl. Draft `raw_input`) in an encrypted, access-controlled blob store.
- **Body classification:** each body is `pii-erasable` (e.g. `raw_input`) or **`governance-retained`** (Confirmed/Mapped contract, evaluation report, reject reason — needed for dedup §7.5, MRM reproduction §11, adverse-action). **Crypto-shred targets `pii-erasable` bodies; `governance-retained` bodies of active/governed versions are auto-retained** (retention driven by **governance status**, not only legal hold).
- **Crypto-shred for erasure:** destroying the key renders a `pii-erasable` payload unrecoverable while the skeleton remains (→ privacy-degraded replay).
- **Retention & legal hold:** data under legal hold or an open audit obligation is exempt from erasure until released; the **security stream** has its own regulator retention class.
- **Audit-read** is itself authorized + logged; **key rotation** without rewriting events.

---

## 10. The interfaces SP-0 exposes

- **Step-handler registration** — handler is **idempotent**; inputs = current documents + triggering event; **returns** new events (which **must validate against the event registry**) and optionally a new document (whose `derived_from` **must reference committed docs**); signals **retryable vs permanent via its typed return**, not exceptions; has a per-invocation **timeout** (→ delivery retry on breach); **prohibited** from emitting feature-/request-stream events, writing outside its `run_id`, or reading mutable projections in guards. Handler **registration is itself versioned**.
- **Human-gate API** — `open_task` / answer (§7); SP-0 owns task lifecycle, timers, escalation, resume.
- **Command API** — issue §4.4 commands; **command-level idempotency keys** (UI double-submit safe); typed request/response schema; authorized per §6.2.
- **Query API** — current-state + work-queues + attempt-memory, with **parameters, pagination, and `as-of global_seq`** and `degraded` flags. Read-only.

---

## 11. What SP-0 deliberately does NOT decide

- The **content schemas** of Confirmed/Mapped/Plan and other artifacts (owned by SP-2…SP-7; SP-0 owns envelopes, the Draft schema, and the registries they register into).
- Governance **policy/values**: risk-tier meaning, verification thresholds, use-case permission matrices, monitoring cadences (SP-9/SP-10/SP-12) — SP-0 owns the typed slots + guard *hooks*.
- Any **business rule** inside a handler or a guard predicate's domain truth.
- **Data-access** control (SP-9/SP-11).
- **Concrete technology** bindings (appendix — sample only) and operational tuning (saga timing, GC cadence, queue sizing — the implementation plan's domain).

---

## 12. Error handling and testing

Required coverage:

- **Concurrent versions & activation CAS** — v1 `PRODUCTION` + v2 `DRAFT` coexist; two runs branching from v1 both approve → the later activation **fails CAS** → `ACTIVATION_CONFLICT` to a human (no silent overwrite).
- **Approval→activation saga** — version minted in the run tx; activation idempotent; transient feature-side failure retries; no half-applied state.
- **Use-case-scoped & experimental activation** — v2 active for fraud while v1 active for credit; `APPROVED_EXPERIMENTAL` → `ACTIVE_EXPERIMENTAL`; experimental `expires_at` auto-deactivates.
- **Governance gates** — production promotion blocked unless `verification_stamp=='USEFULNESS-CHECKED'` and required artifacts present; activation into a `blocked_use_case` rejected; over-tier-ceiling rejected.
- **Multi-candidate / request aggregate** — N candidate runs at different stages concurrently; `select_candidate` mints/binds `feature_id`, closes siblings; `candidates_explored_count` recorded; one request → multiple features.
- **Candidate→primary promotion** — `PRIMARY_SELECTED` event sets the current primary; uniqueness (one live primary per `(run_id, stage)`); "current" resolved by `global_seq`, not `created_at`; **DAG acyclicity** invariant holds (edges only to committed docs).
- **Schema evolution** — old-version **events and documents** upcast (total, chained); breaking change caught; **deprecated/withdrawn** schema versions still readable for in-flight; runs pin registry snapshots.
- **Both table versions** — in-flight run on old `run_transition_table_version` unaffected; **feature-lifecycle table versioning/replay** works and is pinned.
- **Atomicity & blob GC** — no orphan documents/events; rolled-back step leaves a blob → **GC quarantines/sweeps** it; GC audited.
- **Idempotency** — duplicate message/outbox-publish → one effect.
- **External effects** — dispatcher crash after call: with key/handle → no dup; without → residual **logged/flagged as accepted** (no false dedup claim); **stale result** after run advanced is ignored/compensated, not applied.
- **Crash/recovery**, **optimistic-concurrency conflict**, **guard-failure auditing** (`GUARD_FAILED`/`TRANSITION_REJECTED` with inputs/result/route; no fall-through; precedence ties rejected at registration).
- **Timers** — ladder across restarts; overdue fire on recovery; late timer can't escalate an answered gate; business calendar.
- **Retries & cost breaker** — transient backoff; permanent skips; repair loop stops after N → human; **cost ceiling auto-parks**.
- **Queue partitioning** — feature-/request-level events get per-aggregate ordering (not dropped by run-only partitioning).
- **Authorization & denials** — unauthorized command denied → **security stream**, not domain; service-actor authority attested, not self-asserted; wrong-role confirmation rejected; requester=approver rejected; **three-party** validation enforced (author≠validator≠approver); break-glass flagged + **review gate** required; security-stream read restricted.
- **Human gates** — staleness keyed to **task `required_inputs`/version** (a reminder/timer does NOT invalidate a valid answer — no deadlock); duplicate doesn't double-count quorum; conflict → escalation; **positive quorum** (distinct subjects, consistent answers) completes; `open_task`/cancel/supersede.
- **Lifecycle** — `PRODUCTION → MONITORING_ALERT → REVALIDATION_REQUIRED → (PRODUCTION|DEPRECATED)`; revalidation-change spawns a new run; **inbound signals** (`fact_confirmed_resume`) wake parked runs (not just timers); **`deprecate` blocked when consumers exist**; mid-flight **`SOURCE_CHANGED_REVALIDATE`** when an input snapshot advances before approval.
- **Privacy** — no raw PII in events/bodies; **body classification** (pii-erasable vs governance-retained); crypto-shred → unrecoverable payload, skeleton intact; governance-retained bodies of governed versions survive; legal hold blocks erasure; **full vs privacy-degraded replay** labeled; key rotation.
- **Misc** — `park`/`unpark`/`reopen_as_new_run` (new run linked to rejected); `DUPLICATE_OF` home + first-committed race rule; `resolve_degraded`; `as-of`/lag reads; command idempotency (double-submit); concept-claim reservation race.

---

## Appendix A — Sample stack (non-binding)

| Concern | Sample | Capability |
|---|---|---|
| Event store | Relational, unique `(aggregate, aggregate_id, stream_version)`, monotonic `global_seq` | Atomic append + optimistic concurrency + global order |
| Document/blob bodies | Object store, write-once, content-addressed, encrypted, **GC'd** | Write-once + hashing + encryption + access control + orphan sweep |
| Outbox + relay | `outbox` in-tx; leased relay; DLQ | Transactional publish + at-least-once + dead-letter |
| Worker queue | At-least-once, partitioned by **aggregate key** (`run:` / `feature:` / `request:`) | Per-aggregate ordering |
| Timers | `timers` + poller + business-calendar | Durable wake-ups |
| External commands | `external_commands` + dispatcher | At-least-once + idempotency-key/job-handle + stale-result guard |
| Projections | Tables/views + checkpoints + `degraded` flags + attempt-memory | Re-derivable, fail-closed, global position |
| Schema registries | Versioned stores + total/chained upcasters (events + docs) | Upcast-on-read + deprecation lifecycle |
| Identity/authz | OIDC (humans) + workload identity (services) + policy store + security stream | Attested claims-at-time + command policy + denial log |
| Crypto | KMS, per-body keys, classification-aware | Encryption + selective crypto-shred |
| Cost metering | Per-run/request counter + breaker | Durable budget + auto-park |

---

## Appendix B — End-to-end (plain language)

```
A REQUEST can generate several candidate runs (the multi-candidate idea); each run is its own
  workflow with its own ledger, so candidates can sit at different stages at once. At Gate #1 the
  scientist SELECTS one — that names the feature — and the siblings close.
A run that gets approved FREEZES a version stamped with its governance facts (verification stamp,
  risk tier, allowed/blocked use-cases, required artifacts, the data/holdout/seed used). Approval
  mints the version; ACTIVATION is a separate, CAS-guarded step at the feature: it only goes live
  if the version it branched from is still active — so two runs from v1 can't silently clobber
  each other. Versions are active PER USE-CASE, so v2-for-fraud and v1-for-credit coexist, and
  experimental approvals expire on their own.
Documents are frozen and linked in a branching graph; a candidate becomes "primary" by an explicit
  event (never an edit), and "the current one" is decided by ledger order, not wall-clock.
A roll-your-own runtime advances aggregates safely: one transaction writes event+doc+timers+next-step;
  duplicates are harmless; orphan blobs from rolled-back steps get swept; external calls go through an
  idempotent outbox (and we're honest where exactly-once needs the other side's key/handle, and we
  drop results that come back after the run moved on); timers chase humans on a business calendar and
  never escalate an answered gate; runaway cost auto-parks the run.
Every action records WHO with claims-as-of-then — humans by login, SERVICES by attested deploy
  identity. You can't approve your own feature; high-risk ones need a THIRD independent validator;
  admin overrides are dual-controlled AND reviewed after the fact; unauthorized attempts go to a
  locked-down SECURITY log, not the feature's history.
Human answers are judged stale only if THEIR question's inputs changed — a reminder never voids a
  valid answer. Parked features wake when the blocking FACT arrives, not just when a timer fires.
The audit trail IS the ledger. "Erase" destroys keys for personal data while KEEPING governance
  knowledge, so replay comes in two honest flavors: full, and privacy-degraded.
Other sub-projects plug in handlers, gates, commands, and queries; SP-0 owns the hard machinery and
  the typed slots — the governance VALUES live in SP-9/10/12.
```
