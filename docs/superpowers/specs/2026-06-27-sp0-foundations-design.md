# SP-0 — Foundations: Design Spec

**Status:** Design (sub-project spec)
**Date:** 2026-06-27
**Sub-project:** SP-0 (Phase A — Foundations)
**Parent:** [Reference architecture](./2026-06-27-feature-engineering-platform-design.md) · [Build roadmap](./2026-06-27-feature-engineering-platform-roadmap.md)
**Type:** Vendor-neutral design + a clearly-marked sample-stack appendix

---

## 1. Purpose and scope

SP-0 is the **backbone** every other sub-project plugs into. It provides the substrate on which a feature request is stored, advanced, audited, and governed — but it contains **no business logic** of its own. It is the rails, not the trains.

### 1.1 In scope

- The **feature aggregate** and its storage.
- The **event store** (event-sourced source of truth) and **projections** (read models).
- The **immutable staged document chain** (Draft → Confirmed → Mapped → Plan, and attached artifacts).
- The **state-machine engine** (declarative states, transitions, guards, failure routing).
- The **durable workflow runtime** (roll-your-own: transactional outbox, idempotent handlers, durable timers, bounded retries).
- The **audit log** (a view over the event store).
- **Identity capture** on every event + enforcement of the **structural segregation-of-duties** rules the workflow cannot work without.
- The **interfaces** later sub-projects implement against (step handlers, human gates, queries).

### 1.2 Out of scope (owned by later sub-projects, but they run *on* SP-0)

- LLM intake/normalization and the clarification logic (SP-2).
- Schema mapping, entity/grain/point-in-time resolution (SP-3).
- Validation packs, the Implementation Router, the DSL compiler (SP-4).
- Sandbox execution, data-quality checks (SP-5), evaluation/scoring (SP-5/SP-7).
- Rich access control — column-level permissions, purpose-based exposure, full RBAC (SP-9/SP-11).

### 1.3 Design decisions (ledger)

| Decision | Choice | Rationale |
|---|---|---|
| Output form | Vendor-neutral spec + sample-stack appendix | Consistent with the parent reference architecture; concrete enough to build from |
| Contract storage | **Immutable staged chain** | Immutability-by-construction; trivial point-in-time reproduction for audit (parent §11.4–11.5) |
| State & audit | **Event-sourced + projection** | Replayable, immutable audit trail (parent §9.3); audit cannot drift from state because the audit *is* the state |
| Runtime | **Roll-your-own** (DB + queue + poller) | Maximum control, commodity dependencies, vendor-neutral; correctness via well-known patterns |
| Identity / SoD | **Identity capture + structural SoD enforced**; rest → SP-9 | Four-eyes and confirmation-authority are intrinsic to the workflow's correctness, not optional security |

---

## 2. The data model

### 2.1 The feature aggregate

A **feature aggregate** is one feature request, identified by a stable `feature_id`. Everything about it hangs off that id:

```
feature_id ──┬── event stream      (truth — append-only, versioned)
             ├── document chain     (Draft → Confirmed → Mapped → Plan — all frozen)
             └── projections        (current-state, work-queues — derived from events)
```

### 2.2 Event stream (the source of truth)

The append-only, event-sourced record. Each event is immutable:

```json
{
  "event_id": "evt_01HZ...",
  "feature_id": "feat_01HZ...",
  "stream_version": 7,
  "type": "CONTRACT_CONFIRMED",
  "actor": { "id": "raj", "role": "data_scientist" },
  "timestamp": "2026-06-27T10:14:22Z",
  "payload": { "confirmed_contract_ref": "doc_01HZ..." },
  "caused_by": "evt_01HZ..."
}
```

- **Append rule (optimistic concurrency):** an append succeeds only if the stream is still at the expected `stream_version`. A stale append fails and the caller retries against the new version. This is the primary concurrency control.
- The stream is **never** edited or deleted. Corrections are new compensating events.

### 2.3 Immutable staged document chain

The contract documents (schemas defined in parent §4) are stored as **frozen** documents, each linked to its predecessor:

```json
{
  "doc_id": "doc_01HZ...",
  "feature_id": "feat_01HZ...",
  "stage": "CONFIRMED_CONTRACT",
  "previous_doc_id": "doc_01HZ...(the draft)",
  "created_at": "2026-06-27T10:14:22Z",
  "created_by": "raj",
  "content_hash": "sha256:...",
  "body": { /* the stage's contract content, per parent §4 */ }
}
```

- A document is **write-once**. Advancing a feature **creates a new document** referencing `previous_doc_id`; nothing is edited in place.
- SP-0 owns the **envelope** (id, stage, lineage, hash, provenance) and the **Draft** stage schema (parent §4.1). The bodies of later stages (Confirmed/Mapped/Plan) and attached artifacts (validation report, sandbox result, evaluation report, approval record) are produced by their owning sub-projects and stored through this same envelope.
- `content_hash` makes tampering detectable and supports reproducibility.

### 2.4 Projections (read models)

Derived, disposable views rebuilt from the event stream:

- **Current-state projection** — `feature_id → current_state, latest_doc_id, owner, risk_tier, sla_deadline`.
- **Work-queue projections** — e.g. "features in `NEEDS_DATA_STEWARD` by table owner," "features in `READY_FOR_APPROVAL`."
- Projections are **eventually consistent** and **always re-derivable** by replaying events. They are never a source of truth.

---

## 3. The state machine

### 3.1 Declarative definition

The states (parent §9.1, ~35) and transitions are **data, not code**. Each transition declares a **guard** (a hard gate) and the **event** it emits:

```json
{
  "from": "CONFIRMED_CONTRACT",
  "to": "SCHEMA_MAPPED",
  "trigger": "MAPPING_COMPLETED",
  "guard": "confirmed_contract_exists AND catalog_quality_passed",
  "emits": "FEATURE_MAPPED",
  "on_guard_fail": "MAPPING_REVIEW_FAILED"
}
```

The engine evaluates the guard before allowing a transition; a guard failure routes to the declared failure state (parent §9.2 failure routing is encoded this way). Because the table is declarative, the flow is auditable and changeable without code surgery.

### 3.2 Terminal and failure states

Terminal: `PRODUCTION`, `DEPRECATED`, `REJECTED`, `POLICY_BLOCKED`. Failure states (e.g. `VALIDATION_FAILED`, `SANDBOX_FAILED`) route per the parent's failure table — to a repair loop (bounded, §4.4) or to a human.

---

## 4. The durable runtime (roll-your-own, made safe)

The runtime advances features across days using commodity primitives (a database, a worker queue, a poller). Correctness comes from four mandated patterns.

### 4.1 Transactional outbox

When a step completes, the new domain event **and** an outbox message ("do the next step") are written in **one database transaction**. A relay process polls the outbox and publishes to the worker queue. This eliminates the dual-write bug where an event is saved but the next step never runs (or vice-versa).

### 4.2 Idempotent handlers + optimistic concurrency

The worker queue is **at-least-once**, so a handler may receive a message twice. Two defenses:

- Handlers append events with the **expected `stream_version`**; a duplicate loses the version check and becomes a **no-op**.
- A processed-message ledger (keyed by `event_id`/message id) is the secondary guard.

This also prevents two workers from double-advancing the same feature.

### 4.3 Durable timers

Human-gate clocks are **rows** with a `due_at`, fired by a poller — surviving restarts:

```json
{
  "timer_id": "tmr_01HZ...",
  "feature_id": "feat_01HZ...",
  "kind": "GATE_SLA",
  "due_at": "2026-07-04T10:14:22Z",
  "on_fire": "ESCALATE",
  "fired": false
}
```

The escalation ladder: **SLA → reminder → escalation → auto-park** (a parked feature has a named owner and leaves the active queues). Timer firing is idempotent.

### 4.4 Bounded retries

Per-step attempt counts are tracked; on exhaustion the feature transitions to a failure state routed to a human. **No infinite repair loops** (closes a parent §9 gap). Backoff between attempts.

### 4.5 Crash recovery

Automatic: the truth is in the event store and the outbox drives work, so a crashed worker simply resumes — no in-memory state is lost.

---

## 5. Identity and structural segregation of duties

- **Identity capture:** every event records `actor.id` and `actor.role`. Without this the audit log and SoD are meaningless.
- **Enforced structural rules** (the minimum the workflow cannot work without):
  - **Requester ≠ approver** — the approval transition (`→ APPROVED_*`) is rejected if the approver is the feature's requester (four-eyes).
  - **Confirmation authority** — data facts (availability time, grain, joins, SCD) may be confirmed only by the registered **data owner**; policy facts only by **Compliance** (parent §6.5). A confirmation event from the wrong role is rejected.
- **Deferred to SP-9/SP-11:** column-level permissions, purpose-based exposure, full RBAC. SP-0 leaves a clean enforcement hook point for these.

---

## 6. The interfaces SP-0 exposes

These boundaries let later sub-projects be isolated, independently-testable units.

### 6.1 Step-handler registration

A sub-project registers a handler for a state. Contract of a handler:

- **Input:** the feature's current documents + triggering event.
- **Must be idempotent.**
- **Returns:** zero or more new domain events, and optionally one new document (stored via the §2.3 envelope).
- SP-0 owns everything around it: delivery, retries, the version check, the outbox, the audit write.

```
register(state="READY_FOR_MAPPING", handler=fn)
   // SP-3 supplies fn; SP-0 runs it durably when a feature enters that state
```

### 6.2 Human-gate API

A sub-project requests a pause: *"present this task to this role, with this SLA."* SP-0 owns the pause, the timer, the escalation ladder, and the resume-on-signal; the sub-project owns only the task **content**.

```
await_human(feature_id, role="data_owner", task=<payload>, sla="7d",
            on_timeout="escalate")
   // resumes when a matching signal (the human's answer) is appended as an event
```

### 6.3 Projection / query API

Read current state and work-queues (the §2.4 projections). Read-only; never a write path.

---

## 7. Audit log

The audit log is **not a separate store** — it is a **view over the event stream**. Because every transition is an event carrying actor + timestamp + payload, the audit trail is complete and immutable by construction and cannot drift from the system state. Reproducing "what did this feature look like and where was it on date D" is an event **replay** to that point.

---

## 8. Error handling and testing

SP-0's correctness rests on the event-sourced + roll-your-own choices, so the spec **requires** these tests:

- **Replay determinism** — rebuilding state from events yields an identical result every time.
- **Idempotency** — delivering the same message twice produces exactly one transition.
- **Crash/recovery** — killing a worker mid-step leaves the feature resumable with no duplicate effect.
- **Optimistic-concurrency conflict** — two concurrent writers: one wins, the other safely retries against the new version.
- **Timer lifecycle** — SLA → reminder → escalation → auto-park fires correctly, including across restarts.
- **Bounded retries** — a perpetually-failing step stops after N attempts and routes to a human.
- **Structural SoD** — requester = approver is rejected; a wrong-role confirmation is rejected.
- **Projection rebuild** — dropping and rebuilding a projection from events yields an identical view.

---

## 9. What SP-0 deliberately does NOT decide

- The **content schemas** of Confirmed/Mapped/Plan documents beyond the envelope (owned by SP-2/SP-3/SP-4, per parent §4).
- Any **business rule** inside a step handler or guard implementation (owned by the relevant sub-project).
- The **rich access-control** model (SP-9/SP-11).
- The **concrete technology** bindings (see appendix — sample only).

---

## Appendix A — Sample stack (non-binding)

This appendix names a concrete, commodity stack so SP-0 is buildable. It is illustrative; the spec body depends only on capabilities, not these products.

| Concern | Sample choice | Capability required |
|---|---|---|
| Event store | A relational database table (append-only, unique `(feature_id, stream_version)`) | Atomic append with optimistic concurrency |
| Document chain | Rows/objects keyed by `doc_id`, write-once | Write-once storage + content hashing |
| Outbox + relay | An `outbox` table written in the same transaction; a relay process | Transactional write + a publisher |
| Worker queue | Any at-least-once queue | At-least-once delivery |
| Timers | A `timers` table + a poller | Durable scheduled wake-ups |
| Projections | Database tables/materialized views | Re-derivable read models |
| Service language | One service language for handlers + runtime | — |

---

## Appendix B — End-to-end (plain language)

```
A feature request gets an id; its whole life is an append-only event ledger.
Each stage produces a frozen document that points back to the previous one.
A declarative table says which moves are allowed and what must be true to move (the guards).
A roll-your-own runtime advances it safely: one transaction writes the event AND the
  "do next" message; duplicates are harmless; timers chase the humans; bad steps give up
  after N tries and call a person.
Every action records who did it; you can't approve your own feature, and only the right
  expert can confirm their facts.
The audit trail isn't a side-log — it IS the ledger, so it can never disagree with reality.
Other sub-projects just plug in handlers and human-gates; SP-0 handles all the hard parts.
```
