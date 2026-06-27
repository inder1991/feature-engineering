# SP-0 — Foundations: Design Spec

**Status:** Design (sub-project spec) · **Revision 3** (incorporates two architecture-review rounds)
**Date:** 2026-06-27
**Sub-project:** SP-0 (Phase A — Foundations)
**Parent:** [Reference architecture](./2026-06-27-feature-engineering-platform-design.md) · [Build roadmap](./2026-06-27-feature-engineering-platform-roadmap.md)
**Type:** Vendor-neutral design + a clearly-marked sample-stack appendix

> **Revision history.**
> **Rev 2** — incorporated the first review: identifier model, event schema evolution, versioned state machine, guard contract, DAG document lineage, normative Draft schema, rich identity + command authorization, full human-gate model, external-side-effect handling, projection/outbox mechanics, privacy/retention, expanded tests.
> **Rev 3** — incorporated the second review: a **workflow-run / candidate-version aggregate** separate from the feature and the approved version (so v1 can be in PRODUCTION while v2 is in DRAFT); explicit guard-failure events; corrected external-command idempotency claims; fail-closed state projections; denials to a security-audit stream; `raw_input` privacy handling; full-vs-privacy-degraded replay; `conflict` task status; a document/artifact schema registry.

---

## 1. Purpose and scope

SP-0 is the **backbone** every other sub-project plugs into. It provides the substrate on which feature work is stored, advanced, audited, and governed — but it contains **no business logic** of its own. It is the rails, not the trains.

### 1.1 In scope

- The **identifier model** (request / feature / run / version) and the two aggregates (feature, run).
- The **event store** (event-sourced truth), the **event + document schema registries**, and **projections**.
- The **immutable staged document chain** with DAG lineage, and the normative **Draft** schema.
- The **state-machine engine** (declarative, versioned, guard contract with failure events, lifecycle commands, failure routing).
- The **durable workflow runtime** (roll-your-own: atomic transaction boundary, transactional outbox + relay, idempotent handlers, external-side-effect handling, durable timers with race resolution, classified retries).
- The **audit log**, the reproducibility envelope, and **replay modes**.
- **Identity, authentication, command-authorization**, the **structural SoD** rules, and the **security-audit stream**.
- The **privacy / retention** model that bounds immutability.
- The **interfaces** later sub-projects implement against (step handlers, human gates, queries, commands).

### 1.2 Out of scope (owned by later sub-projects, but they run *on* SP-0)

- LLM intake/normalization and clarification *logic* (SP-2) — SP-0 defines the Draft *envelope and schema*, not how it is produced.
- Schema mapping, entity/grain/point-in-time resolution (SP-3); validators, Router, DSL compiler (SP-4); sandbox, DQ, evaluation/scoring (SP-5/SP-7).
- **Data-access** control — column-level permissions, purpose-based exposure, full data RBAC (SP-9/SP-11). (SP-0 still owns *command/transition* authorization — §6.2.)

### 1.3 Design decisions (ledger)

| Decision | Choice |
|---|---|
| Output form | Vendor-neutral spec + sample-stack appendix |
| Identifiers | `request_id` → `feature_id` → `run_id` (candidate version) → `feature_version_id` |
| Aggregates | **Feature aggregate** (active version + production lifecycle) and **Run aggregate** (the workflow state machine) |
| Contract storage | Immutable staged documents, **DAG lineage** (supersede/branch) |
| State & audit | Event-sourced + projections; audit *is* the event stream; denials in a separate security stream |
| Schema evolution | Registries for **events and documents**, per-record `schema_version`, upcast-on-read |
| State machine | Declarative, **versioned**, runs pin their version; guard contract with failure events |
| Runtime | Roll-your-own (DB + queue + poller), correctness via named patterns |
| Identity / authz | Rich identity + **command-level authorization** + structural SoD; data-access RBAC → SP-9 |
| Immutability | Bounded by privacy/retention (refs not raw PII, crypto-shred erasure, two replay modes) |

---

## 2. Identifier model and the two aggregates

A single id is insufficient: the parent requires that **approved versions are immutable and every change creates a new version** (parent §11.5), *and* a logical feature can have a live production version while a new version is being developed.

| Id | Meaning | Lifecycle |
|---|---|---|
| `request_id` | One *ask* from a data scientist (a hypothesis or definition). | Created at intake; never reused. May spawn 0..n runs. |
| `feature_id` | A *logical feature* (a named concept that evolves over time). | Stable across all its versions. |
| `run_id` | One **candidate version / workflow run** — a single attempt to create or change the feature. | Born when a run starts; the workflow state machine runs on it; ends `APPROVED` or `REJECTED`. |
| `feature_version_id` | One *immutable, approved* realization. | **Minted frozen at approval** of a run; activated/superseded at the feature level; never edited. |

### 2.1 Two aggregates

- **Run aggregate** — keyed by `run_id`. **This is what the big workflow state machine and its event stream track** (DRAFT → … → APPROVED/REJECTED). Multiple runs can exist concurrently under one `feature_id`.
- **Feature aggregate** — keyed by `feature_id`. A **separate, smaller** event stream tracking which `feature_version_id` is **active**, the version/supersession history, and the **production lifecycle** of the active version (monitoring, revalidation, deprecation).

> This split is the fix for the P0: **v1 = PRODUCTION** lives in the feature aggregate as the active version, while **v2 = DRAFT** is a *separate run* with its own stream and current state. One "current state per feature" never has to represent both.

### 2.2 Version creation, activation, supersession

- A run **produces** a `feature_version_id` **only when it reaches APPROVED** — frozen at that moment. Before approval there is no version, only a mutable run.
- **Activation** is a feature-level operation: the feature aggregate points its *active* version at a newly approved `feature_version_id`.
- **Supersession** is activation of a new version over a prior active one — recorded as a feature-level `VERSION_SUPERSEDED` event. The prior version remains immutable and queryable; nothing is edited.
- **Redesign / repair** of an *unapproved* run stays within that `run_id` (new documents via DAG lineage, §3.4); it mints no version.
- **Change to an approved feature** (new logic, schema change, a revalidation outcome) starts a **new run** under the same `feature_id`; on its approval a new version supersedes the old.
- **Duplicate**: a `request_id` found to duplicate an existing `feature_id` is linked via a `DUPLICATE_OF` event (parent §7.5), not merged in place.

---

## 3. The data model

### 3.1 Aggregate storage

```
feature_id  (FEATURE AGGREGATE)
   ├── feature event stream      (active-version + production lifecycle events)
   ├── feature_versions          (immutable approved versions; supersession chain)
   └── feature projections       (active version, production state)

   └── run_id  (RUN AGGREGATE, 0..n per feature)
         ├── run event stream     (truth — append-only, versioned, globally sequenced)
         ├── document DAG         (frozen stage documents for this run)
         └── run projections      (current workflow state, work-queues)
```

### 3.2 Event stream (the source of truth)

Run-stream events (the workflow); each is immutable and self-describing:

```json
{
  "event_id": "evt_01HZ...",
  "global_seq": 480213,
  "feature_id": "feat_01HZ...",
  "run_id": "run_01HZ...",
  "stream_version": 7,
  "type": "CONTRACT_CONFIRMED",
  "schema_version": 3,
  "actor": { /* §6.1 identity envelope */ },
  "occurred_at": "2026-06-27T10:14:22Z",
  "recorded_at": "2026-06-27T10:14:22Z",
  "payload": { "confirmed_contract_ref": "doc_01HZ..." },
  "caused_by": "evt_01HZ...",
  "provenance": { /* §8 reproducibility envelope */ },
  "transition_table_version": 12
}
```

- `stream_version` — per-**run**, drives **optimistic concurrency**: append succeeds only if the stream is still at the expected version. A stale append fails and the caller retries against the new version.
- `global_seq` — a **monotonic global position** across all streams (run and feature), used by projections for ordered, checkpointed consumption (§3.6).
- Feature-stream events (e.g. `VERSION_ACTIVATED`, `VERSION_SUPERSEDED`, `ENTERED_MONITORING_ALERT`, `REVALIDATION_REQUIRED`, `DEPRECATED`) share the same envelope, keyed by `feature_id` with `run_id` null.
- Events are **never** edited or deleted (subject to the privacy model, §9). Corrections are new compensating events.

### 3.3 Event schema registry and evolution

- An **event-type registry**: each `type` has a current `schema_version`, a JSON-schema per version, and an **owning sub-project** (the reducer owner).
- **`schema_version` stamped on every event** at write time; **upcast-on-read** transforms older events to current shape via registered, pure, versioned **upcasters**. Old events are never rewritten.
- **Compatibility rule**: only backward-compatible changes reuse a type; breaking changes mint a new `schema_version` with a mandatory upcaster, or a new type.
- The registry is versioned and snapshot-referenced from `provenance` so replay reconstructs the exact schema/upcaster set in effect.

### 3.4 Immutable staged document chain (DAG lineage)

Stage documents (Draft → Confirmed → Mapped → Plan, plus attached artifacts) are **frozen** and linked as a **directed acyclic graph** — because repair loops, multiple generated candidates (parent §14.2), rejected alternatives, and post-failure redesign all branch:

```json
{
  "doc_id": "doc_01HZ...",
  "feature_id": "feat_01HZ...",
  "run_id": "run_01HZ...",
  "stage": "CONFIRMED_CONTRACT",
  "schema_version": 2,
  "supersedes": ["doc_01HZ...(prior confirmed)"],
  "derived_from": ["doc_01HZ...(the draft)"],
  "branch_role": "primary",          // primary | candidate | rejected | repair
  "created_at": "2026-06-27T10:14:22Z",
  "created_by": { /* identity envelope */ },
  "content_hash": "sha256:...",
  "provenance": { /* §8 */ },
  "body_ref": "blob_01HZ..."         // payload stored by reference (§9)
}
```

Lineage rules: **write-once** (advancing/revising creates a new document); `derived_from` records inputs, `supersedes` records replacement; a stage may hold **multiple candidate documents** with exactly one `branch_role: primary` once chosen; the "current" document for a stage is the latest non-superseded `primary`. `content_hash` makes tampering detectable.

### 3.5 The normative Draft schema (owned by SP-0)

SP-0 owns the Draft **envelope and schema**; SP-2 owns how the Draft is *produced*.

```json
{
  "request_id": "req_01HZ...",
  "intake_mode": "hypothesis | definition",
  "raw_input_ref": "blob_01HZ...",        // see privacy handling below
  "raw_input_classification": "contains_pii | clean | unscanned",
  "hypothesis": "…",
  "target": "churn | UNKNOWN",
  "entity": "customer | UNKNOWN",
  "feature_concept": "salary irregularity | UNKNOWN",
  "source_concepts": ["…"],
  "candidate_calculations": ["…"],
  "open_fields": ["lookback_window", "prediction_time", "calculation_method"],
  "assumption_ledger_ref": "doc_01HZ...",
  "status": "NEEDS_CLARIFICATION"
}
```

- **Privacy of `raw_input` (resolves the contradiction with §9):** free-text input may contain PII/secrets, so it is **never stored inline**. It is classified (PII detection), optionally redacted, written to an **encrypted, access-restricted blob**, and referenced by `raw_input_ref` with a `raw_input_classification`. The Draft *body* (an event/document payload) carries only the reference + classification, satisfying "no raw PII in events or document bodies."
- **Unknown handling**: unresolved fields carry `UNKNOWN` and are listed in `open_fields`; a Draft with non-empty `open_fields` cannot pass Human Gate #1.
- **Default handling**: platform-applied defaults are recorded in the Assumption Ledger (field, value, rationale), never silently inlined.
- **Validation**: SP-0 validates the envelope + required-field presence; semantic validation is SP-2's.

### 3.6 Projections (read models)

- **Global ordering** via `global_seq`; each projection keeps a **checkpoint** and exposes **lag** (`checkpoint` vs `head`); reads can be tagged as-of a `global_seq` for **stale-read** awareness.
- **Poison-event handling — fail closed for state-bearing projections.** A workflow-state or work-queue projection that cannot apply an event must **not** skip-and-continue (that would make current state lie and let downstream work proceed on a false view). It **fails closed for the affected aggregate**: that feature/run's projection entry is marked `degraded` and downstream commands for it are blocked until the gap is resolved; the rest of the projection continues. Only **analytics/non-authoritative** projections may fail open (quarantine + continue).
- **Migration**: a projection schema change is built in parallel from `global_seq = 0`, then reads switch atomically — never mutated in place.
- **Rebuild**: any projection is droppable and deterministically rebuildable from the streams.

### 3.7 Document / artifact schema registry

Documents evolve too (Confirmed/Mapped/Plan bodies change as sub-projects mature), so SP-0 defines a **document/artifact schema registry** mirroring the event one:

- Each `stage`/artifact type has registered `schema_version`s, a JSON-schema per version, an **owning sub-project**, and **reader/upcaster** functions for older documents.
- Same compatibility rule (backward-compatible reuse; breaking change → new version + upcaster).
- A document's `schema_version` (§3.4) is resolved against this registry on read; the registry snapshot is referenced from `provenance`.

---

## 4. The state machine

### 4.1 Declarative definition, guard contract, and failure events

States (parent §9.1) and transitions are **data, not code**:

```json
{
  "from": "CONFIRMED_CONTRACT",
  "to": "SCHEMA_MAPPED",
  "trigger": "MAPPING_COMPLETED",
  "guard": "confirmed_contract_exists AND catalog_quality_passed",
  "guard_inputs": ["confirmed_contract_ref", "catalog_quality_result_ref"],
  "precedence": 100,
  "emits": "FEATURE_MAPPED",
  "on_guard_fail": "MAPPING_REVIEW_FAILED"
}
```

**Guard contract:** pure and deterministic (reads only declared `guard_inputs`, no side effects/I-O); **deterministic precedence** when multiple transitions match (highest `precedence`; ties are a registration-time error); a missing required input is itself a guard failure.

**Guard-failure auditing (explicit events).** A transition outcome is *always* an event, success or failure:
- On success → `emits` the success event, with the guard's resolved inputs + result on `provenance`.
- On failure → a **`GUARD_FAILED`** (and, for authorization/precondition rejection, **`TRANSITION_REJECTED`**) event carrying the resolved `guard_inputs`, the boolean/why, and the `route` taken (`on_guard_fail`). Failure routing is itself an audited transition, never a silent jump.

### 4.2 State-machine versioning

The transition table + guard definitions are **versioned**; each **run pins** the `transition_table_version` it began under (stamped on every event). A live run continues on its pinned version; migration to a newer table happens only via an explicit, audited `migrate_workflow_version` command (§4.4). **Replay always uses the pinned historical version**, so changing a guard never alters historical replay or strands in-flight runs.

### 4.3 States — run-level vs feature/version-level

The parent's ~35 states split across the two aggregates:

- **Run-level (workflow):** `DRAFT … CONFIRMED_CONTRACT … SCHEMA_MAPPED … READY_FOR_APPROVAL … APPROVED_EXPERIMENTAL | APPROVED_PRODUCTION | REJECTED | POLICY_BLOCKED`. A run is **terminal** at `REJECTED`/`POLICY_BLOCKED`, or at `APPROVED_*` (which *produces a version* and hands off to the feature aggregate).
- **Feature/version-level (lifecycle):** an active version moves `PRODUCTION → MONITORING_ALERT → REVALIDATION_REQUIRED → (PRODUCTION | DEPRECATED)` (parent §13), or `PRODUCTION → DEPRECATED`. **`PRODUCTION` is long-lived, not terminal** (corrects Rev 1). When `REVALIDATION_REQUIRED` implies a change, the feature aggregate **starts a new run** (the next candidate version).
- **Terminal overall:** `DEPRECATED`, `REJECTED`, `POLICY_BLOCKED`.

### 4.4 Lifecycle command catalog

Each command is authorized (§6.2), emits an audited event, and has defined effects. **Run-level** unless marked *(feature)*:

| Command | Effect |
|---|---|
| `create_run` | Open the first run for a request → `DRAFT` |
| `start_new_version_run` *(feature)* | Open a new run for an existing feature (change/revalidation) |
| `submit_human_signal` | Provide a human-gate answer (§7) |
| `cancel` / `withdraw` | Stop an in-flight run → `REJECTED` (reason recorded) |
| `reject` | Reviewer rejection → `REJECTED` (reason feeds dedup, parent §7.5) |
| `reopen` | Re-activate a rejected/parked run to a prior valid state |
| `park` / `unpark` | Suspend/resume a run; parked runs leave active queues with a named owner |
| `activate` *(feature)* | Point the active version at a newly approved `feature_version_id` |
| `supersede` *(feature)* | Activate a new version over the prior active one (prior stays immutable) |
| `deprecate` *(feature)* | Active version `PRODUCTION → DEPRECATED` |
| `duplicate_of` | Link a request to an existing feature (parent §7.5) |
| `manual_retry` | Operator-initiated business-repair retry (§5.6) |
| `admin_correct` | Append a compensating/correction event (privileged, dual-control) |
| `migrate_workflow_version` | Move a run to a newer transition-table version (§4.2) |

---

## 5. The durable runtime (roll-your-own, made safe)

### 5.1 The atomic transaction boundary

A step's effects must be **one atomic database transaction**:

```
BEGIN
  1. append domain event(s)            (with stream_version check)
  2. insert any new document(s)        (frozen, §3.4)
  3. upsert timer rows                 (create/cancel, §5.5)
  4. record processed-message ledger   (idempotency, §5.3)
  5. insert outbox message(s)          (next step / external command, §5.2/§5.4)
COMMIT
```

A failed `stream_version` check rolls back the whole transaction; the message is re-delivered (§5.6). Large/PII document **bodies** are written to the blob store **before** the transaction and referenced by id+hash inside it (idempotent by content hash), keeping the transactional unit small.

### 5.2 Transactional outbox and relay (operational behavior)

- Outbox write is inside the §5.1 transaction; a **relay** publishes outbox rows to the queue.
- **Relay leasing** so multiple relays don't double-publish; expired leases reclaimed.
- **Publish-then-mark-sent**: a row is `sent` only after broker ack; a crash in between yields an at-least-once **duplicate publish**, harmless because consumers are idempotent (§5.3).
- **Partitioning by `run_id`** → per-run ordered delivery (handlers may rely on it); no cross-run global order assumed.
- **Dead-letter queue** for messages past their delivery budget (§5.6); **stuck-message detection** sweeps for rows neither `sent` nor dead-lettered past a threshold and alerts.
- **Backpressure**: relays throttle on saturation; outbox rows wait durably, so backpressure never loses work.

### 5.3 Idempotent handlers + optimistic concurrency

Handlers append with the **expected `stream_version`** (duplicate → no-op) and a **processed-message ledger** keyed by message id is the secondary guard. Together these make at-least-once delivery safe and stop two workers double-advancing one run.

### 5.4 External side effects (and the honest limits of idempotency)

Idempotency via `stream_version` protects the *append*, not external calls (LLM, sandbox, metadata writes, uploads) made *before* it.

- **External-command outbox:** a side-effecting handler records an **external command** with a caller-supplied **idempotency key** in the §5.1 transaction; a dispatcher executes it and writes the result back as an event.
- **Honest caveat (resolves the overstatement):** at-least-once dispatch is only *exactly-once-effect* when **the external system honors the idempotency key** (server-side dedup) **or exposes a reconcilable job handle** (the dispatcher queries status before re-invoking). If the external system offers neither, a dispatcher crash *after* the external call but *before* writing the result **can re-invoke it** — this residual duplicate risk must be **explicitly accepted per integration**, and high-cost/non-idempotent integrations (e.g. expensive sandbox jobs) MUST provide a job handle or key.
- **Result caching by idempotency key** avoids paying twice for a re-dispatch (reinforces parent §12 cost control).

### 5.5 Durable timers and race resolution

```json
{
  "timer_id": "tmr_01HZ...", "run_id": "run_01HZ...",
  "kind": "GATE_SLA", "due_at": "2026-07-04T17:00:00Z",
  "business_calendar": "BANK_UK", "lease_owner": null,
  "idempotency_key": "run_01HZ:gate:sla:1",
  "guard_state_version": 7, "on_fire": "ESCALATE",
  "status": "armed"   // armed | fired | cancelled
}
```

Escalation ladder: SLA → reminder → escalation → **auto-park**. `due_at` resolves against a named **business calendar**. **Timer/response race:** a human answer and an SLA escalation both attempt the transition with an **optimistic CAS** on `stream_version` (`guard_state_version`); first writer wins, the loser is a no-op. A firing timer takes a **lease** and is **cancelled atomically** when the gate is answered, so a late timer cannot escalate an answered gate.

### 5.6 Retry semantics

Two distinct kinds, never conflated:
- **Delivery retry (infra):** transient processing failure → re-deliver with **backoff + jitter** up to a per-message budget; on exhaustion → DLQ. Does not change run state.
- **Business repair loop (workflow):** a *valid failure* (e.g. compilation failed) routes to a failure state; a bounded `N` repair attempts, each an **attempt event**; on exhaustion → human-routed state. `manual_retry` re-arms it.
- **Error classification:** handlers mark failures **retryable** (transient) vs **permanent** (deterministic); permanent skips delivery retry. `max_elapsed_time` caps total delivery-retry duration.

### 5.7 Crash recovery

Automatic: truth is in the event store and the outbox drives work, so a crashed worker/relay/dispatcher resumes from durable state — no in-memory state lost.

---

## 6. Identity, authentication, authorization

### 6.1 Identity envelope (on every event/command)

```json
{
  "subject": "user:raj | service:intake-agent",
  "authenticated": true, "auth_method": "oidc",
  "actor_kind": "human | service",
  "role_claims": ["data_scientist"],          // claims AT TIME OF ACTION
  "groups": ["payments-ds"], "tenant": "retail-bank",
  "on_behalf_of": null,                        // delegated authority
  "impersonation": null,                       // admin acting as another
  "break_glass": false,                        // emergency override (heavily audited)
  "source_of_authority": "iam-snapshot@2026-06-27T10:14Z"
}
```

Role claims are captured **as of the action**; `service` actors are first-class and distinct from humans; `impersonation`/`break_glass` are recorded and alertable.

### 6.2 Command / transition authorization, and denial handling

Full *data-access* RBAC is deferred to SP-9, but **command-level write authorization lives in SP-0** — else `role` is self-asserted. A coarse policy maps `actor_kind`/`role` → permitted command/transition:

| Capability | Permitted |
|---|---|
| `create_run`, `submit_human_signal` (clarify) | data scientist (owner of the request) |
| confirm **data** facts | registered **data owner** of the table |
| confirm **policy** facts | **Compliance** |
| approve (`→ APPROVED_*`) | approver role, **and** subject ≠ requester (§6.3) |
| `activate`/`supersede`/`deprecate` | release/owner role |
| register/modify handlers or the **transition table** | platform-admin (deploy identity) |
| read audit | auditor / compliance / owner (scoped) |
| `admin_correct`, `break_glass` | platform-admin, dual-control |

**Denial handling (resolves "COMMAND_DENIED as domain event"):** an unauthorized attempt is recorded in a **separate security-audit stream**, **not** the feature/run domain stream — because the attempt may carry no valid id, could leak feature existence, or could be used to spam a governed stream. A denial is added to the domain stream **only** when the actor is already authorized to know that feature/run exists.

### 6.3 Structural segregation of duties

- **Requester ≠ approver** (four-eyes) at the approval transition.
- **Confirmation authority** — data facts → data owner; policy facts → Compliance (parent §6.5); wrong-role confirmation rejected.
- **Dual control** for `admin_correct` and `break_glass`.

---

## 7. Human-gate API and task model

A human gate is a first-class, durable **task** with an explicit lifecycle:

```json
{
  "task_id": "task_01HZ...", "run_id": "run_01HZ...",
  "gate": "CLARIFICATION | FINAL_APPROVAL | DATA_STEWARD | COMPLIANCE",
  "eligible_assignees": { "role": "data_owner", "scope": "core.transactions" },
  "allowed_responses": ["confirm", "edit", "reject"],
  "quorum": { "required": 1, "of_role": "data_owner" },
  "delegation_allowed": true, "sla": "7d",
  "state_version_at_open": 7,
  "status": "open"   // open | answered | conflict | expired | cancelled | superseded
}
```

Defined behaviors: **eligible assignees** are role+scope (only eligible or validly-delegated subjects may answer); **allowed responses** enumerated per gate; **quorum** (default 1) supports multi-party approval, completing when a quorum of *distinct* eligible subjects answer consistently; **conflicting answers** route the task to **`conflict`** (now in the enum) and escalate, never silently pick one; **delegation** records `on_behalf_of`; **duplicate submissions** are idempotent by `(task_id, subject)` and don't double-count quorum; **stale responses** (answer's `state_version_at_open` older than current run version) are rejected; **cancellation** when the run advances marks the task `cancelled`/`superseded` and refuses late answers; the **timer/response race** is resolved by the §5.5 CAS.

---

## 8. Audit, reproducibility, and replay modes

The audit log is **a view over the event streams** — complete and immutable by construction. The **provenance envelope** on every event/document records what's needed to reproduce a value (parent §11.4):

```json
{
  "artifact_type": "confirmed_contract", "schema_version": 2,
  "producing_component": "sp2-intake@1.4.0",
  "tool_versions": { "llm_model": "claude-…", "prompt_version": "p-2026-06-01",
                     "validator": "…", "compiler": "…" },
  "source_snapshots": ["delta:core.transactions@v8821"],
  "event_registry_snapshot": "event-schema-registry@v37",
  "doc_registry_snapshot": "doc-schema-registry@v11",
  "external_refs": ["llm_call:idem_…", "sandbox_run:…"]
}
```

**Two replay modes (resolves the crypto-shred vs replay tension):**
- **Full replay** — all bodies intact: reconstructs state *and* document bodies/feature values. Available while data is retained.
- **Privacy-degraded audit replay** — after crypto-shredding (§9), event/document **skeletons** (type, time, actor, hashes, provenance) remain and reconstruct *the decision trail and structure*, but erased **bodies/values are unrecoverable**. The platform marks a replay's mode and which artifacts are degraded, so an auditor is never misled into thinking a value was reproduced when only its skeleton survives.
Reproduction uses the pinned `transition_table_version`, the referenced registry snapshots, and the immutable `source_snapshots`.

---

## 9. Privacy, retention, and the limits of immutability

- **No raw PII/secrets in events or document bodies.** Records carry **references** (ids, hashes, snapshot pointers); sensitive bodies — including Draft `raw_input` (§3.5) — live in an encrypted, access-controlled blob store.
- **Encryption at rest** with **per-feature (or per-subject) keys** where feasible.
- **Crypto-shredding for erasure**: destroying the key renders a payload unrecoverable while the event/document **skeleton** remains for audit integrity (→ privacy-degraded replay, §8).
- **Retention & legal hold**: retention policies and legal-hold flags govern when crypto-shred may run; **data under legal hold or an open audit obligation is exempt from erasure until released** (this is how the immutability/erasure tension is reconciled).
- **Audit-read access control**: reading the audit/event log is itself an authorized, logged action (§6.2).
- **Key rotation** without rewriting events (envelope references key id/version).

---

## 10. The interfaces SP-0 exposes

- **Step-handler registration** — handler is **idempotent**, reads current documents + triggering event, returns new events and optionally a new document, and **declares external effects** (§5.4). SP-0 owns delivery, retries, version checks, the transaction, the audit write.
- **Human-gate API** — open a task (§7); SP-0 owns the task lifecycle, timers, escalation, resume-on-signal; the sub-project owns task *content*.
- **Projection / query API** — current-state and work-queues with as-of/lag and `degraded` semantics (§3.6). Read-only.
- **Command API** — issue lifecycle commands (§4.4), subject to authorization (§6.2).

---

## 11. What SP-0 deliberately does NOT decide

- The **content schemas** of Confirmed/Mapped/Plan documents (owned by SP-2/SP-3/SP-4; SP-0 owns the envelope + Draft schema + the registries they register into).
- Any **business rule** inside a handler or a guard's domain truth (the guard *contract* is SP-0's).
- The **data-access** control model (SP-9/SP-11).
- The **concrete technology** bindings (appendix — sample only).

---

## 12. Error handling and testing

Required coverage:

- **Concurrent versions** — v1 in `PRODUCTION` (feature aggregate) while a v2 run is in `DRAFT/VALIDATION` (run aggregate) — both states coexist without collapsing.
- **Version lifecycle** — repair stays in one run (no version); approval mints a frozen `feature_version_id`; `activate`/`supersede` keep prior versions immutable.
- **Replay determinism + schema evolution** — rebuild is identical using pinned table + registry snapshots; old-`schema_version` **events and documents** upcast correctly; breaking changes caught by the compatibility rule.
- **Transition-table migration** — in-flight run on an old version unaffected; explicit migration maps correctly.
- **Document/event atomicity** — a mid-step failure leaves no orphan document and no event pointing at a missing document.
- **Idempotency** — duplicate message → one transition; duplicate outbox publish → one effect.
- **External side-effect edge case** — dispatcher crash *after* an external call: with a job handle / idempotency key → no duplicate; without → the documented residual is detected and surfaced, not silently double-applied.
- **Crash/recovery** — kill worker/relay/dispatcher mid-step → resumes, no duplicate.
- **Optimistic-concurrency conflict** — two writers: one wins, the other retries.
- **Guard-failure auditing** — a failed guard emits `GUARD_FAILED`/`TRANSITION_REJECTED` with inputs/result/route.
- **Timer lifecycle & race** — SLA→reminder→escalation→auto-park across restarts; human-answer vs escalation resolves to one outcome; late timer can't escalate an answered gate; business calendar respected.
- **Retries** — transient retry with backoff; permanent skips to business path; repair loop stops after N → human.
- **Projection fail-closed** — a poison event on a state projection marks the aggregate `degraded` and blocks its downstream commands (does not silently skip); analytics projection may fail open; rebuild/migration identical and atomic.
- **Authorization & denials** — unauthorized command denied; **denial recorded in the security stream, not the domain stream** (unless actor already authorized); wrong-role confirmation rejected; requester=approver rejected; break-glass flagged.
- **Human signals** — stale rejected; duplicate doesn't double-count quorum; conflicting quorum → `conflict` + escalation.
- **Privacy** — no raw PII in events/bodies (incl. `raw_input` stored by ref); crypto-shred → payload unrecoverable, skeleton intact; **legal hold blocks erasure**; **full vs privacy-degraded replay** correctly labeled.
- **Production lifecycle** — `PRODUCTION → MONITORING_ALERT → REVALIDATION_REQUIRED → (PRODUCTION|DEPRECATED)`; revalidation-requiring-change spawns a new run.

---

## Appendix A — Sample stack (non-binding)

| Concern | Sample choice | Capability required |
|---|---|---|
| Event store | Relational tables, unique `(run_id, stream_version)` and `(feature_id, stream_version)`, monotonic `global_seq` | Atomic append + optimistic concurrency + global ordering |
| Document bodies / raw_input | Object store, write-once, content-addressed, encrypted | Write-once + hashing + encryption + access control |
| Outbox + relay | `outbox` table written in-tx; leased relay | Transactional write + leased publisher + DLQ |
| Worker queue | At-least-once queue, partitioned by `run_id` | At-least-once + per-key ordering |
| Timers | `timers` table + poller, business-calendar lib | Durable scheduled wake-ups |
| External commands | `external_commands` table + dispatcher | At-least-once + idempotency-key/job-handle reconciliation |
| Projections | Tables / materialized views with checkpoints + `degraded` flags | Re-derivable read models with global position + fail-closed |
| Schema registries | Versioned registry store + upcaster libraries (events + documents) | Per-type schema versions + upcast-on-read |
| Identity/authz | OIDC + a policy store + a security-audit stream | Authenticated claims-at-time + command policy + denial log |
| Crypto | KMS with per-feature keys | Encryption + key destruction (crypto-shred) |

---

## Appendix B — End-to-end (plain language)

```
A request can spawn one or more RUNS; each run is one attempt at a version, with its own
  append-only, globally-ordered ledger and its own current state. A run that gets approved
  FREEZES a version; the FEATURE then activates it (and supersedes the old one). So v1 can be
  live in production while v2 is still a draft run — two streams, never collapsed.
Stage documents are frozen and linked as a branching graph (candidates, repairs, redesigns),
  each stamped with the tool/model/prompt/snapshot versions that made it; both events AND
  documents have schema registries so old ones can still be read.
A versioned, declarative table says which moves are allowed and what must be true to move;
  guards are pure and audited, and EVERY outcome — pass or fail — is an event.
A roll-your-own runtime advances runs safely: one transaction writes the event, document,
  timers, and "do next" message together; duplicates are harmless; external calls go through an
  idempotent command outbox (and we're honest that exactly-once needs the other side's key or
  a job handle); timers chase humans on a business calendar and never escalate an answered gate;
  failures retry (transient) or repair a bounded number of times (business) before calling a person.
Every action records WHO with claims-as-of-then; you can't approve your own feature, only the
  right expert confirms their facts, admin overrides are dual-controlled, and unauthorized
  attempts go to a SECURITY log — not the feature's own history.
The audit trail IS the ledger. "Erase" destroys keys, not history — so replay comes in two
  flavors: full (values intact) and privacy-degraded (the decision trail survives, the erased
  values don't), and we always say which.
Other sub-projects just plug in handlers, human-gates, and queries; SP-0 handles the hard parts.
```
