# SP-0 — Foundations: Design Spec

**Status:** Design (sub-project spec) · **Revision 2** (incorporates the 20-point architecture review)
**Date:** 2026-06-27
**Sub-project:** SP-0 (Phase A — Foundations)
**Parent:** [Reference architecture](./2026-06-27-feature-engineering-platform-design.md) · [Build roadmap](./2026-06-27-feature-engineering-platform-roadmap.md)
**Type:** Vendor-neutral design + a clearly-marked sample-stack appendix

> **Revision 2 note.** This revision incorporates a full architecture review. Major additions: a three-level identifier model (request / feature / version), event schema evolution, a versioned state machine, a guard contract, a DAG document-lineage model, a normative Draft schema, a rich identity + command-authorization model, full human-gate task semantics, external-side-effect handling, projection and outbox operational mechanics, a privacy/retention model, and an expanded test suite. Scope is intentionally larger than Revision 1.

---

## 1. Purpose and scope

SP-0 is the **backbone** every other sub-project plugs into. It provides the substrate on which a feature request is stored, advanced, audited, and governed — but it contains **no business logic** of its own. It is the rails, not the trains.

### 1.1 In scope

- The **identifier model** (request / feature / version) and supersession lineage.
- The **feature aggregate** and its storage.
- The **event store** (event-sourced source of truth), the **event schema registry**, and **projections**.
- The **immutable staged document chain** with DAG lineage, and the normative **Draft** schema.
- The **state-machine engine** (declarative, versioned, with a guard contract, lifecycle commands, failure routing).
- The **durable workflow runtime** (roll-your-own: atomic transaction boundary, transactional outbox + relay, idempotent handlers, external-side-effect handling, durable timers with race resolution, classified retries).
- The **audit log** and reproducibility envelope.
- **Identity, authentication, command-authorization**, and the **structural segregation-of-duties** rules.
- The **privacy / retention** model that bounds immutability.
- The **interfaces** later sub-projects implement against (step handlers, human gates, queries).

### 1.2 Out of scope (owned by later sub-projects, but they run *on* SP-0)

- LLM intake/normalization and clarification *logic* (SP-2) — SP-0 defines the Draft *envelope and schema*, not how it is produced.
- Schema mapping, entity/grain/point-in-time resolution (SP-3); validators, Router, DSL compiler (SP-4); sandbox, DQ, evaluation/scoring (SP-5/SP-7).
- **Data-access** control — column-level permissions, purpose-based exposure, full data RBAC (SP-9/SP-11). (SP-0 still owns *command/transition* authorization — §6.2.)

### 1.3 Design decisions (ledger)

| Decision | Choice |
|---|---|
| Output form | Vendor-neutral spec + sample-stack appendix |
| Identifiers | Three levels: `request_id` → `feature_id` → `feature_version_id` |
| Contract storage | Immutable staged documents, **DAG lineage** (supersede/branch) |
| State & audit | Event-sourced + projections; audit *is* the event stream |
| Schema evolution | Event-type registry, per-event `schema_version`, upcast-on-read |
| State machine | Declarative, **versioned**, features pin their version; guard contract |
| Runtime | Roll-your-own (DB + queue + poller), correctness via named patterns |
| Identity / authz | Rich identity capture + **command-level authorization** + structural SoD; data-access RBAC → SP-9 |
| Immutability | Bounded by a privacy/retention model (refs not raw PII, crypto-shred erasure) |

---

## 2. Identifier model: request → feature → version

A single id is insufficient: the parent requires that **approved versions are immutable and every change creates a new version** (parent §11.5). SP-0 uses three identifiers:

| Id | Meaning | Lifecycle |
|---|---|---|
| `request_id` | One *ask* from a data scientist (a hypothesis or definition). | Created at intake; never reused. May spawn 0..n features. |
| `feature_id` | A *logical feature* (a named concept that can evolve over time). | Stable across versions. |
| `feature_version_id` | One *immutable, approved* realization of a feature. | Frozen at approval; superseded, never edited. |

Rules:

- The **event stream and document chain are keyed by `feature_id`** (the aggregate). Each terminal approval produces a new `feature_version_id`.
- **Redesign / repair** of an unapproved feature stays within the same `feature_id` (new documents via DAG lineage, §3.4), producing no new version until approved.
- **Change to an approved feature** (new logic, schema change, revalidation outcome) creates a **new `feature_version_id`** that **supersedes** the prior one; the prior remains immutable and queryable.
- **Duplicate/merge**: a `request_id` found to duplicate an existing `feature_id` is linked (not merged-in-place) via a `DUPLICATE_OF` event.

---

## 3. The data model

### 3.1 The feature aggregate

```
feature_id ──┬── event stream      (truth — append-only, versioned, globally sequenced)
             ├── document chain     (DAG of frozen stage documents)
             ├── feature_versions   (immutable approved versions, supersession chain)
             └── projections        (current-state, work-queues — derived from events)
```

### 3.2 Event stream (the source of truth)

Each event is immutable and self-describing:

```json
{
  "event_id": "evt_01HZ...",
  "global_seq": 480213,
  "feature_id": "feat_01HZ...",
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

- `stream_version` — per-feature, drives **optimistic concurrency**: append succeeds only if the stream is still at the expected version (the primary concurrency control). A stale append fails and the caller retries against the new version.
- `global_seq` — a **monotonic global position** across all streams, used by projections for ordered, checkpointed consumption (§3.6).
- Events are **never** edited or deleted (subject to the privacy/retention model, §9). Corrections are new compensating events.

### 3.3 Event schema registry and evolution

Event sourcing requires that old events remain replayable as payloads evolve. SP-0 mandates:

- An **event-type registry**: every `type` is registered with its current `schema_version`, a JSON-schema definition per version, and an **owning sub-project** (the "reducer owner" responsible for that event's meaning and upcasters).
- **`schema_version` is stamped on every event** at write time.
- **Upcast-on-read**: when a consumer (reducer/projection) reads an event at an older `schema_version`, a registered **upcaster** transforms it to the current shape. Upcasters are pure, total, and themselves versioned. Old events are *never* rewritten in place.
- **Compatibility rule**: only backward-compatible schema changes (add optional field, widen) may reuse a type; breaking changes mint a new `schema_version` with a mandatory upcaster, or a new event type.
- The registry is itself versioned and snapshot-referenced from `provenance` so a replay can reconstruct exactly which schema/upcaster set was in effect.

### 3.4 Immutable staged document chain (DAG lineage)

Stage documents (Draft → Confirmed → Mapped → Plan, plus attached artifacts) are **frozen** and linked as a **directed acyclic graph**, not a single linked list — because repair loops, multiple generated candidates (parent §14.2), rejected alternatives, and post-failure redesign all branch:

```json
{
  "doc_id": "doc_01HZ...",
  "feature_id": "feat_01HZ...",
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

Lineage rules:
- **Write-once.** Advancing or revising creates a *new* document; nothing is edited in place.
- `derived_from` records inputs; `supersedes` records replacement. A stage may have **multiple candidate documents** (e.g. three generated calculation methods); exactly one is marked `branch_role: primary` once chosen; the others become `candidate`/`rejected` with a reason.
- The "current" document for a stage is the latest non-superseded `primary` in that stage.
- `content_hash` makes tampering detectable and supports reproducibility.

### 3.5 The normative Draft schema (owned by SP-0)

SP-0 owns the Draft **envelope and schema**; SP-2 owns how the Draft is *produced*. Normative Draft body:

```json
{
  "request_id": "req_01HZ...",
  "intake_mode": "hypothesis | definition",
  "raw_input": "Customers with irregular salary credits are more likely to churn.",
  "hypothesis": "…",                      // required if intake_mode=hypothesis
  "target": "churn | UNKNOWN",
  "entity": "customer | UNKNOWN",
  "feature_concept": "salary irregularity | UNKNOWN",
  "source_concepts": ["…"],
  "candidate_calculations": ["…"],        // optional; populated by SP-2 generation
  "open_fields": ["lookback_window", "prediction_time", "calculation_method"],
  "assumption_ledger_ref": "doc_01HZ...", // links to the Assumption Ledger document
  "status": "NEEDS_CLARIFICATION"
}
```

Rules:
- Required: `request_id`, `intake_mode`, `raw_input`, `status`, `open_fields`, `assumption_ledger_ref`.
- **Unknown handling**: any unresolved field carries the literal `UNKNOWN` and is listed in `open_fields`. A Draft with a non-empty `open_fields` cannot advance past Human Gate #1.
- **Default handling**: a default applied by the platform is recorded as an entry in the Assumption Ledger (not silently inlined), with the field name, value, and rationale.
- **Validation**: SP-0 validates the *envelope and required-field presence*; semantic validation of the content is SP-2's.

### 3.6 Projections (read models)

Derived, disposable views rebuilt from the event stream. Operational mechanics SP-0 mandates:

- **Global ordering** via `global_seq`; each projection maintains a **checkpoint** (last consumed `global_seq`).
- **Ordering guarantee**: events are applied in `global_seq` order; within a feature, `stream_version` order is preserved.
- **Lag reporting**: each projection exposes `checkpoint` vs `head` so staleness is observable; reads may be tagged "as-of `global_seq`" for **stale-read** awareness.
- **Poison-event handling**: an event a projection cannot apply is quarantined (dead-lettered) with an alert; the projection continues rather than stalling globally, and the gap is tracked.
- **Migration**: a projection schema change is handled by building the new projection from `global_seq = 0` in parallel, then atomically switching reads — never by mutating in place.
- **Rebuild**: any projection can be dropped and rebuilt deterministically from the event stream.

---

## 4. The state machine

### 4.1 Declarative definition and the guard contract

States (parent §9.1) and transitions are **data, not code**. Each transition:

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

**Guard contract:**
- **Pure and deterministic** — a guard reads only its declared `guard_inputs` (drawn from prior events/documents) and produces a boolean; it performs **no side effects** and no external I/O.
- **Auditable** — the guard's resolved inputs and its boolean result are recorded on the emitted event's `provenance`, so a replay reproduces the decision.
- **Deterministic precedence** — if multiple transitions match a trigger, the one with the highest `precedence` wins; ties are a configuration error rejected at registration.
- **Failure precedence** — a failing guard routes to its `on_guard_fail` state; a missing required input is itself a guard failure, not an exception.

### 4.2 State-machine versioning

- The transition table (and the guard definitions) are **versioned**; each feature **pins** the `transition_table_version` it began under (stamped on every event, §3.2).
- A live feature continues on its pinned version; it migrates to a newer table only via an explicit, audited `MIGRATE_WORKFLOW_VERSION` command (§4.4) that records the old→new mapping.
- **Replay always uses the pinned historical version**, so changing a guard never alters historical replay or strands in-flight features.

### 4.3 States — terminal vs long-lived

- **Terminal:** `DEPRECATED`, `REJECTED`, `POLICY_BLOCKED`.
- **`PRODUCTION` is long-lived/active, not terminal** (corrects Rev 1). It participates in lifecycle transitions: `PRODUCTION → MONITORING_ALERT → REVALIDATION_REQUIRED → (PRODUCTION | DEPRECATED)` (parent §13), and `PRODUCTION → DEPRECATED`.
- Failure states (`VALIDATION_FAILED`, `SANDBOX_FAILED`, etc.) route per the parent failure table to a bounded repair loop (§5.6) or a human.

### 4.4 Lifecycle command catalog

Explicit commands (each is authorized per §6.2, emits an audited event, and has defined state effects):

| Command | Effect |
|---|---|
| `create_feature` | Open a feature from a request → `DRAFT` |
| `submit_human_signal` | Provide a human-gate answer (§7) |
| `cancel` / `withdraw` | Stop an in-flight feature → `REJECTED` (reason recorded) |
| `reject` | Reviewer rejection → `REJECTED` (reason recorded; feeds dedup, parent §7.5) |
| `reopen` | Re-activate a rejected/parked feature to a prior valid state |
| `park` / `unpark` | Manually suspend/resume; parked features leave active queues with a named owner |
| `supersede` | Create a new `feature_version_id` replacing an approved one |
| `deprecate` | `PRODUCTION → DEPRECATED` |
| `duplicate_of` | Link a request to an existing feature (parent §7.5) |
| `manual_retry` | Operator-initiated business-repair retry (§5.6) |
| `admin_correct` | Append a compensating/correction event (privileged, heavily audited) |
| `migrate_workflow_version` | Move a feature to a newer transition-table version (§4.2) |

---

## 5. The durable runtime (roll-your-own, made safe)

### 5.1 The atomic transaction boundary

A step's effects must be **one atomic database transaction**, or events can point at missing documents and documents can be orphaned. The single unit is:

```
BEGIN
  1. append domain event(s)            (with stream_version check)
  2. insert any new document(s)        (frozen, §3.4)
  3. upsert timer rows                 (create/cancel, §5.5)
  4. record processed-message ledger   (idempotency, §5.3)
  5. insert outbox message(s)          (next step / external command, §5.2/§5.4)
COMMIT
```

If the `stream_version` check fails, the whole transaction rolls back and the message is retried (delivery retry, §5.6). Document *bodies* (large/PII) are written to the blob store **before** the transaction and referenced by id+hash inside it, so the transactional unit stays small and the body write is idempotent by content hash.

### 5.2 Transactional outbox and relay (operational behavior)

- **Outbox write** is inside the §5.1 transaction; a **relay** publishes outbox rows to the worker queue.
- **Relay leasing**: relays take a lease on a batch of outbox rows so multiple relays don't double-publish; expired leases are reclaimed.
- **Publish-then-mark-sent**: a row is marked `sent` only after the broker acks; a crash between publish and mark yields an at-least-once **duplicate publish**, which is harmless (consumers are idempotent, §5.3).
- **Partitioning & ordering**: queue partitioning is **by `feature_id`**, giving per-feature ordered delivery (handlers may rely on it); no global ordering is assumed across features.
- **Dead-letter queue**: a message failing past its delivery-retry budget (§5.6) goes to a DLQ with full context; **stuck-message detection** sweeps the outbox/queue for rows neither `sent` nor dead-lettered past a threshold and alerts.
- **Backpressure**: relays throttle when the queue/worker pool signals saturation; outbox rows simply wait (durable), so backpressure never loses work.

### 5.3 Idempotent handlers + optimistic concurrency

- Handlers append events with the **expected `stream_version`**; a duplicate delivery loses the check and becomes a **no-op**.
- A **processed-message ledger** keyed by message id is the secondary guard.
- Together these make at-least-once delivery safe and prevent two workers double-advancing one feature.

### 5.4 External side effects

Idempotency via `stream_version` protects the *append*, not external calls (LLM, sandbox jobs, metadata writes, artifact uploads) made *before* it. SP-0 provides:

- **External-command outbox**: a side-effecting handler does not call the external system inline. It records an **external command** (with a caller-supplied **idempotency key**) in the same §5.1 transaction; a dedicated dispatcher executes it **at-least-once** and writes the result back as an event.
- **Result caching by idempotency key**: a re-dispatch of the same key returns the cached result instead of re-invoking (so a retried LLM/sandbox call is not paid for twice — reinforces the parent platform-cost control, §12).
- Handlers that *must* call inline (rare) must be themselves idempotent and declare it.

### 5.5 Durable timers and race resolution

Human-gate clocks are durable rows fired by a poller:

```json
{
  "timer_id": "tmr_01HZ...",
  "feature_id": "feat_01HZ...",
  "kind": "GATE_SLA",
  "due_at": "2026-07-04T17:00:00Z",
  "business_calendar": "BANK_UK",
  "lease_owner": null,
  "idempotency_key": "feat_01HZ:gate:sla:1",
  "guard_state_version": 7,
  "on_fire": "ESCALATE",
  "status": "armed"   // armed | fired | cancelled
}
```

- **Escalation ladder**: SLA → reminder → escalation → **auto-park** (named owner, leaves active queues).
- **Business calendar / timezone**: `due_at` is resolved against a named calendar (weekends/holidays), not naive wall-clock.
- **Timer/response race**: a human answer and an SLA escalation can fire concurrently. Both attempt the transition with an **optimistic compare-and-swap** on `stream_version` (`guard_state_version`); the **first writer wins**, the loser becomes a no-op. A timer takes a **lease** while firing and is **cancelled** atomically when the gate is answered, so a fired-but-late timer cannot escalate an already-answered gate.

### 5.6 Retry semantics

Two distinct kinds, never conflated:

- **Delivery retry (infrastructure):** a message failed to process (transient DB/queue/version-conflict). Re-deliver with **backoff + jitter**, up to a per-message budget; on exhaustion → DLQ (§5.2). Does not change feature state.
- **Business repair loop (workflow):** a step produced a *valid failure* (e.g. compilation failed). The state machine routes to a failure state; a bounded number (`N`) of repair attempts is allowed, each recorded as an **attempt event**; on exhaustion the feature transitions to a human-routed terminal-for-now state. `manual_retry` (§4.4) can re-arm a repair loop.
- **Error classification:** handlers classify failures as **retryable** (transient) vs **permanent** (deterministic); permanent failures skip delivery retry and go straight to the business path. `max_elapsed_time` caps total delivery-retry duration.

### 5.7 Crash recovery

Automatic: the truth is in the event store and the outbox drives work, so a crashed worker/relay simply resumes from durable state — no in-memory state is lost.

---

## 6. Identity, authentication, authorization

### 6.1 Identity envelope (on every event/command)

`actor.id + role` is insufficient for governed SoD. The envelope:

```json
{
  "subject": "user:raj | service:intake-agent",
  "authenticated": true,
  "auth_method": "oidc",
  "actor_kind": "human | service",
  "role_claims": ["data_scientist"],          // claims AT TIME OF ACTION
  "groups": ["payments-ds"],
  "tenant": "retail-bank",
  "on_behalf_of": null,                        // delegated authority
  "impersonation": null,                       // set when an admin acts as another
  "break_glass": false,                        // emergency override flag (heavily audited)
  "source_of_authority": "iam-snapshot@2026-06-27T10:14Z"
}
```

- Role claims are captured **as of the moment of action** (not resolved later), so audit reflects the authority that actually applied.
- `service` actors (the intake agent, dispatcher) are first-class and distinguished from humans.
- `impersonation` and `break_glass` are recorded and alertable; break-glass actions are flagged for after-the-fact review.

### 6.2 Command / transition authorization (cannot be deferred)

Full *data-access* RBAC is deferred to SP-9, but **command-level write authorization** must live in SP-0 — otherwise `role` is self-asserted metadata. SP-0 enforces a coarse policy: which `actor_kind`/`role` may issue which command/transition, e.g.:

| Capability | Permitted |
|---|---|
| `create_feature`, `submit_human_signal` (clarify) | data scientist (owner of the request) |
| confirm **data** facts | registered **data owner** of the table |
| confirm **policy** facts | **Compliance** |
| approve (`→ APPROVED_*`) | approver role, **and** subject ≠ requester (§6.3) |
| register/modify handlers or the **transition table** | platform-admin (service-deploy identity) |
| read audit | auditor / compliance / owner (scoped) |
| `admin_correct`, `break_glass` | platform-admin, dual-control |

An unauthorized command is rejected and recorded as a `COMMAND_DENIED` event.

### 6.3 Structural segregation of duties

- **Requester ≠ approver** — the approval transition is rejected if the approving subject is the feature's requester (four-eyes).
- **Confirmation authority** — data facts → data owner; policy facts → Compliance (parent §6.5); a confirmation from the wrong role is rejected.
- **Dual control** for `admin_correct` and `break_glass`.

---

## 7. Human-gate API and task model

A human gate is a first-class, durable **task** with an explicit lifecycle — not just "pause and wait."

```json
{
  "task_id": "task_01HZ...",
  "feature_id": "feat_01HZ...",
  "gate": "CLARIFICATION | FINAL_APPROVAL | DATA_STEWARD | COMPLIANCE",
  "eligible_assignees": { "role": "data_owner", "scope": "core.transactions" },
  "allowed_responses": ["confirm", "edit", "reject"],
  "quorum": { "required": 1, "of_role": "data_owner" },
  "delegation_allowed": true,
  "sla": "7d",
  "state_version_at_open": 7,
  "status": "open"   // open | answered | expired | cancelled | superseded
}
```

Defined behaviors:
- **Eligible assignees** are role+scope; only an eligible (or validly delegated) subject may answer.
- **Allowed responses** are enumerated per gate; an out-of-set response is rejected.
- **Quorum** (default 1) supports multi-party approval where required (e.g. four-eyes compliance); the gate completes when quorum of *distinct* eligible subjects answer consistently.
- **Conflicting answers**: if quorum>1 and answers disagree, the task → `conflict` and routes to escalation, never silently picks one.
- **Delegation**: an eligible subject may delegate to another; the answering identity records `on_behalf_of`.
- **Duplicate submissions**: idempotent by `(task_id, subject)`; a second submission by the same subject updates, it does not double-count quorum.
- **Stale responses**: an answer carrying a `state_version_at_open` older than the feature's current stream version (the feature already advanced) is **rejected as stale**.
- **Cancellation**: if the feature advances or is cancelled, the open task is `cancelled`/`superseded` and a late answer is refused.
- **Timer/response race**: resolved by the optimistic CAS in §5.5.

---

## 8. Audit and reproducibility

The audit log is **a view over the event stream** — complete and immutable by construction, cannot drift from state. To satisfy parent §11.4 (reproduce a value for a regulator), the **provenance envelope** on every event/document records:

```json
{
  "artifact_type": "confirmed_contract",
  "schema_version": 2,
  "producing_component": "sp2-intake@1.4.0",
  "tool_versions": { "llm_model": "claude-…", "prompt_version": "p-2026-06-01",
                     "validator": "…", "compiler": "…" },
  "source_snapshots": ["delta:core.transactions@v8821"],   // time-travelable refs
  "registry_snapshot": "event-schema-registry@v37",
  "external_refs": ["llm_call:idem_…", "sandbox_run:…"]
}
```

Reproduction = **replay** to a `global_seq`, using the pinned `transition_table_version` and `registry_snapshot`, against the referenced immutable `source_snapshots`.

---

## 9. Privacy, retention, and the limits of immutability

"Never edited or deleted" must coexist with banking privacy law:

- **No raw PII/secrets in events or document bodies.** Events carry **references** (ids, hashes, snapshot pointers), not raw sensitive values; bodies live in the blob store with access control.
- **Encryption at rest** for payloads and bodies; **per-feature (or per-subject) keys** where feasible.
- **Crypto-shredding for erasure**: to honor a deletion/erasure obligation against immutable events, the encryption key for the affected data is destroyed, rendering the payload unrecoverable while the event *skeleton* (type, time, actor, hashes) remains for audit integrity.
- **Retention & legal hold**: retention policies and legal-hold flags govern when crypto-shred may run; held data is exempt from erasure until released.
- **Audit-read access control**: reading the audit/event log is itself an authorized, logged action (§6.2).
- **Key rotation** is supported without rewriting events (envelope references the key id/version).

---

## 10. The interfaces SP-0 exposes

- **Step-handler registration** — register a handler for a state; handler is **idempotent**, reads current documents + triggering event, returns new events and optionally a new document; declares any **external effects** (§5.4). SP-0 owns delivery, retries, version checks, the transaction, the audit write.
- **Human-gate API** — open a task (§7); SP-0 owns the task lifecycle, timers, escalation, and resume-on-signal; the sub-project owns task *content*.
- **Projection / query API** — read current-state and work-queues with as-of/lag semantics (§3.6). Read-only.
- **Command API** — issue lifecycle commands (§4.4), subject to authorization (§6.2).

---

## 11. What SP-0 deliberately does NOT decide

- The **content schemas** of Confirmed/Mapped/Plan documents (owned by SP-2/SP-3/SP-4; SP-0 owns the envelope + Draft schema + the registry they register into).
- Any **business rule** inside a handler or a guard's truth (the guard *contract* is SP-0's; the guard's domain logic is the owner's).
- The **data-access** control model (SP-9/SP-11).
- The **concrete technology** bindings (appendix — sample only).

---

## 12. Error handling and testing

Required test coverage (expanded for the event-sourced + roll-your-own design):

- **Replay determinism** — rebuilding state from events is identical every time, using pinned table + registry versions.
- **Event schema evolution** — an old-`schema_version` event upcasts correctly; a breaking change is caught by the compatibility rule.
- **Transition-table migration** — an in-flight feature on an old version is unaffected; an explicit migration maps correctly.
- **Document/event atomicity** — a failure mid-step leaves no orphan document and no event pointing at a missing document.
- **Idempotency** — duplicate message → one transition; duplicate **outbox publish** → one effect.
- **Crash/recovery** — kill worker/relay mid-step → resumes with no duplicate.
- **Optimistic-concurrency conflict** — two writers: one wins, the other retries against the new version.
- **External side-effect idempotency** — a retried side-effecting handler does not double-call the LLM/sandbox (cached by idempotency key).
- **Timer lifecycle & race** — SLA→reminder→escalation→auto-park fires across restarts; a human answer and escalation racing resolve to one outcome; a late timer cannot escalate an answered gate; business-calendar respected.
- **Bounded + classified retries** — transient failures retry with backoff; permanent failures skip to the business path; repair loop stops after N and routes to a human.
- **Stale/duplicate/conflicting human signals** — stale answers rejected; duplicate submissions don't double-count quorum; conflicting quorum answers escalate.
- **Authorization** — unauthorized command rejected (`COMMAND_DENIED`); wrong-role confirmation rejected; requester=approver rejected; break-glass flagged.
- **Projection lag/rebuild/migration** — lag is observable; a poison event is quarantined without stalling; rebuild from `global_seq=0` is identical; projection schema migration switches atomically.
- **Production lifecycle transitions** — `PRODUCTION → MONITORING_ALERT → REVALIDATION_REQUIRED → (PRODUCTION|DEPRECATED)` all valid.
- **Privacy** — no raw PII in events; crypto-shred renders a payload unrecoverable while preserving the event skeleton; legal hold blocks erasure.
- **Versioning model** — repair stays in one `feature_id`; approval mints a `feature_version_id`; change supersedes, prior stays immutable.

---

## Appendix A — Sample stack (non-binding)

| Concern | Sample choice | Capability required |
|---|---|---|
| Event store | Relational table, unique `(feature_id, stream_version)`, monotonic `global_seq` | Atomic append + optimistic concurrency + global ordering |
| Document bodies | Object/blob store, write-once, content-addressed | Write-once + hashing + encryption |
| Outbox + relay | `outbox` table written in-tx; leased relay | Transactional write + leased publisher |
| Worker queue | At-least-once queue, partitioned by `feature_id` | At-least-once + per-key ordering + DLQ |
| Timers | `timers` table + poller, business-calendar lib | Durable scheduled wake-ups |
| External commands | `external_commands` table + dispatcher | At-least-once with idempotency-key result cache |
| Projections | Tables / materialized views with checkpoints | Re-derivable read models with global position |
| Schema registry | Versioned registry store + upcaster library | Per-type schema versions + upcast-on-read |
| Identity/authz | OIDC + a policy store | Authenticated claims-at-time + command policy |
| Crypto | KMS with per-feature keys | Encryption + key destruction (crypto-shred) |

---

## Appendix B — End-to-end (plain language)

```
A request gets an id; it may produce one or more logical features; each approval freezes
  an immutable version. Everything is an append-only, globally-ordered event ledger.
Stage documents are frozen and linked as a branching graph (so candidates, repairs, and
  redesigns all fit), each stamped with the tool/model/prompt/snapshot versions that made it.
A versioned, declarative table says which moves are allowed and what must be true to move;
  guards are pure and audited, and each feature stays on the table version it started under.
A roll-your-own runtime advances it safely: one transaction writes the event, the document,
  the timers, and the "do next" message together; duplicates are harmless; external calls
  go through an idempotent command outbox so the LLM/sandbox is never paid for twice;
  timers chase the humans on a business calendar and never escalate an answered gate;
  bad steps retry (transient) or repair a bounded number of times (business) before calling a person.
Every action records WHO did it with claims-as-of-then; you can't approve your own feature,
  only the right expert confirms their facts, and admin overrides are dual-controlled and flagged.
The audit trail IS the ledger; "erase" is done by destroying keys, not editing history.
Other sub-projects just plug in handlers, human-gates, and queries; SP-0 handles the hard parts.
```
