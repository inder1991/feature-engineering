# The Feature Run Spine — design, revision 2

**Date** 2026-08-22 · **Code baseline** `feature/asset-detail-reapply` @ `7a2c78b9` (server-owned
authorization landed AFTER revision 1) · **Plan baseline** the four-stage gating plan at revision
five, including §0.1.0's development authorization ruling · **Status** design; revision 1
(`9072f4d9`) was reviewed and its ten P0 gaps are closed here; not yet a plan

A durable identity for one feature-generation workflow, so a person can leave, come back tomorrow,
and run the next stage against exactly what they left — never against "latest".

**What revision 2 changes, in one paragraph.** Revision 1 mixed three concepts into one attempt
table (stage description, execution attempt, dashboard aggregation), created a second mutable
lifecycle authority beside the domain stores, specified an API its own schema could not implement,
froze inputs in JSON the database could not check, collapsed canonical output and re-execution
history into one row, under-specified run identity, over-claimed the considered-revision bridge,
under-counted the unavailable stages (two of five), shipped a lease with no reconciler, and
allocated migration numbers the parent plan had already taken. Each of those is corrected below,
and each correction is marked **[R2]**.

---

## 1. The problem, stated in code rather than in prose

**The workflow's continuity currently lives in a URL query string.** `App.tsx:510` hands
`FeatureExecutionScreen` nine separate identity fragments — `artifact_id`, `environment_id`,
`group`, `observation_id`, `authorization_id`, `check_set_hash`, `goal`, `target_mode`,
`target_ref` — each defaulting to `''`. Close the tab and the work is unrecoverable.

**Every stage already has a durable attempt record, and no two of them share a parent.**

| Stage | Store | Migration |
|---|---|---|
| recommendation generation | `feature_generation_run` | 1006 |
| candidate choice | `contract_gate1_choice_revision` | 1025 |
| formula authoring | `formula_draft` · `formula_authoring_run` | 1090 · 1022 |
| selection | `feature_selection_revision` | 1072 |
| build declaration + generation | `build_set_revision` · `generation_request` | 1092 |
| sandbox verification | `verification_request` · `verification_attempt` | 1094 · 1080 |
| sandbox publication | `publication_attempt` | 1081 |
| production materialization | `materialization_request` (legacy lane, see §9) | 1053 |

**[R2] There is no feature-run list endpoint and no run-list authorization model.** (Revision 1
said "no list endpoints anywhere", which is false — `GET /contracts` at `contract.py:590`,
`GET /features` at `features.py:95` and others are collection endpoints. What has never existed is
a listing of *runs*, or any answer to "which runs may this person see".)

**A run/stage precedent already works, on the ingestion side only.** `ingestion_run` (0994) +
`ingestion_run_stage` (0996): `UNIQUE (run_id, stage, attempt)`, a closed state vocabulary, an
append-only status-event history. The feature workflow inherits the shape and rejects the lifetime:
ingestion completes inside one bounded request; a feature run stays open for days.

---

## 2. The invariant

> **A stage may run only when every input it consumes is named by immutable identity. If an exact
> identity is missing, the stage is UNAVAILABLE. The platform never substitutes "latest."**

And its corollary, which revision 1 violated twice:

> **[R2] One fact, one authority.** A lifecycle state, a lease, or an identity that exists in two
> tables is two answers. Wherever the spine touches a fact a domain store already owns, the spine
> POINTS at it and derives; it never copies it into a second mutable column.

---

## 3. Three concepts, not one table **[R2 — closes P0-1]**

Revision 1 put `NOT_STARTED`, `UNAVAILABLE` and `NOT_APPLICABLE` into an *attempt* status column —
creating an attempt row to record that nothing was attempted — and keyed live-attempt uniqueness by
`(run, stage)`, which either serializes the authoring of independent candidates or forces them into
an artificial batch. The corrected model separates:

```
Feature run
├── MILESTONE            a human decision, evidenced by rows that already exist
│     CHOOSE_CANDIDATES    contract_gate1_choice_revision (1025)
│     BIND_SELECTIONS      feature_selection_revision + selection_formula_binding (§11.0.1)
│
├── EXECUTABLE ACTION ATTEMPT   an orchestration header over exactly one domain attempt
│     AUTHOR_FORMULA           subject = authoring_subject_revision   (per member)
│     GENERATE_PREVIEW         subject = build_set_revision           (per run)
│     EXECUTE_SANDBOX          subject = sealed artifact              (per artifact)
│     PUBLISH_SANDBOX · MATERIALIZE_PRODUCTION · PUBLISH_PRODUCTION · TRAIN_MODEL
│
└── STAGE-RAIL PROJECTION      derived, never stored as lifecycle
      "8 of 12 formulas ready, 1 blocked, 3 running"
```

* **Milestones are evidence-derived.** No milestone table holds a status; the projection reads the
  choice and binding rows. A milestone with no evidence simply has not happened.
* **Every executable action has a typed subject**, and uniqueness is scoped to it:
  `UNIQUE (generation_run_id, action, stage_subject_id, attempt_number)`. Twelve candidates author
  concurrently as twelve subjects; the preview is one subject per build set.
* **`NOT_STARTED` / `UNAVAILABLE` / `NOT_APPLICABLE` are projection vocabulary**, computed from
  workflow definition + deployed capability + prerequisite evidence. They never appear in an
  attempt row, because they describe the absence of one.

---

## 4. One lifecycle authority **[R2 — closes P0-2 and P0-9]**

The domain stores already own their lifecycles: `formula_draft.state`,
`generation_request.status`, `verification_request.status`, `publication_attempt.outcome`. Revision
1 added a fifth mutable `status` beside them, plus its own `lease_until` and `fence_token` beside
the queue's — permitting `feature_run_stage_attempt = SUCCEEDED` while
`generation_request = FAILED`, and recreating at the worker layer the very three-authority problem
§8 of revision 1 warned about.

**The correction: the run-side attempt is an immutable orchestration header.**

* Created once, in the trigger transaction. Never updated.
* Linked by typed FK to **exactly one** authoritative domain attempt (§6.4's per-action link
  columns, `NOT NULL` per action kind).
* **Status is always derived** from the linked domain record at read time. There is no run-side
  status column, so run-side and domain-side status cannot disagree — the disagreement is
  unrepresentable, not validated away.
* **No run-side lease, no run-side fence.** The queue and the domain stores own claims and
  recovery, including the crash-recovery machinery `7a2c78b9` just landed for the V2 lane. The
  spine adds **zero** new asynchronous lifecycles, and therefore owes zero new reconcilers.
  Any domain lane the spine exposes must already ship its producer + consumer + lease/fence +
  reconciler + operator status + crash tests as one unit — that is that lane's entry condition
  into the spine (§9.0's eight responsibilities for sandbox, for example), not the spine's debt.
* Consequently there is **no partial-unique "one live attempt" index on the header** (it has no
  status column to filter on). Double-trigger protection is the coordinator's CAS (§5) plus each
  domain store's own guard — `formula_draft`'s identity uniqueness,
  `generation_request_one_live_attempt`.
* If a common lifecycle vocabulary is ever wanted, the domain stores migrate to reference it.
  Both are never operated independently.

**Aggregation for batch acts:** an `AUTHOR_FORMULA` action over twelve members is twelve headers
over twelve drafts; the stage rail folds their derived states. Nothing copies a draft state
anywhere.

---

## 5. Coordination state and the trigger contract **[R2 — closes P0-3]**

Revision 1's API required `idempotency_key` and `expected_run_revision`, and its schema stored
neither; attempt-number allocation raced. The corrected substrate:

```
feature_run_state                       -- the ONLY mutable spine row per run
    generation_run_id    PK/FK
    state_version        bigint NOT NULL
```

Every run-centric mutation: `SELECT … FOR UPDATE` on this row → validate
`expected_state_version` → validate prerequisites → write immutable evidence (header, input
revision, links) → increment `state_version` → commit. Attempt numbers are allocated under this
lock, so they cannot race. `state_version` names the run's *coordination* state (attempts, links,
canonical outputs); the run's identity never moves.

The orchestration header stores idempotency relationally:

```
feature_run_action_attempt              -- immutable header, §4
    attempt_id                PK
    generation_run_id         FK -> feature_generation_run
    action                    closed CHECK (§7 vocabulary)
    stage_subject_id          typed per action (§6.3)
    attempt_number
    attempt_purpose           START · RETRY · RE_EXECUTE
    input_revision_id         composite FK (input_revision_id, generation_run_id, action)
                                -> feature_run_action_input   (§6.2)
    idempotency_key           NOT NULL
    request_content_hash      NOT NULL   -- refuses one key reused with different content
    action_authorization_revision_id     -- parent migration 1100
    action_decision_revision_id          -- parent migration 1106
    <exactly one per-action domain FK>   -- §6.4
    requested_by · requested_at

    UNIQUE (generation_run_id, action, stage_subject_id, attempt_number)
    UNIQUE (generation_run_id, action, stage_subject_id, idempotency_key)
```

**Endpoints are semantically distinct** — a generic `POST /attempts` cannot distinguish an
accidental second click from a deliberate re-execution after success:

```
GET   /feature-runs                                     §11 read policy
GET   /feature-runs/{run_id}
GET   /feature-runs/{run_id}/actions/{action}/attempts
GET   /action-attempts/{attempt_id}
GET   /action-attempts/{attempt_id}/events

POST  /feature-runs/{run_id}/actions/{action}/plan          read-only preflight
POST  /feature-runs/{run_id}/actions/{action}/start         first attempt for a subject
POST  /feature-runs/{run_id}/actions/{action}/retry         after FAILED
POST  /feature-runs/{run_id}/actions/{action}/re-execute    after SUCCEEDED (§8 semantics)

POST  /feature-runs/{run_id}/fork-plans                     preflight persists the plan (§10)
POST  /feature-runs/{run_id}/forks                          consumes ONE fork_plan_revision
POST  /feature-runs/{run_id}/archive
```

The body of every trigger carries `{ idempotency_key, expected_state_version, subject… }` and
nothing else. The client supplies **no** artifact id, formula draft id, build-set revision id,
role, certificate id, "latest" selection, or upstream output — the server resolves every one from
the run. This matches §0.1.0's safeguards verbatim: roles never come from the request body, the
triggering user is recorded, and a client may not supply an authorization belonging to somebody
else — the rule `7a2c78b9` just enforced on the build-set lane (`authorization_grantee`).

**There is no generic cancel endpoint. [R2]** Two runs may attach to the same live
`generation_request` (its idempotency is per build set + environment, deliberately), so "cancel my
stage" and "cancel the shared work" are different acts. Cancellation ships per action, with that
action's worker, under rules: detach this run's attachment; cancel the underlying request only when
no other live consumer exists; a publication past its external swap point cannot be assumed
cancellable; a paid provider call may finish, but no next turn starts. Until an action implements
those, it exposes no cancel.

**The plan endpoint reports pins, not predictions.** Which exact input identities would be used;
availability; prerequisites; blockers rendered with the server's own sentences; required
authorization. It does not predict byte-identical replay (unknowable without rendering, §8) and
returns **cost unavailable** until a governed estimator exists (the spend contract is P0-10,
undesigned; the authoring config hash is a measured constant).

---

## 6. Storage model **[R2 — closes P0-4, P0-5, P0-6]**

Migration numbers: **1115 onward** — the parent plan's allocation table (§17, lines 2898–2912)
reserves 1100–1114 inclusive, ending at `recipe_compiler_eval_attempt` = 1114. Revision 1's "1113
onward" collided with the compiler evaluation contract. Re-check the ledger when the first file is
actually written.

### 6.1 Identity — the complete business-input payload

`feature_generation_run` is deliberately mutable (1006: *"a run manifest may accrete context"*),
and its mutable row carries the very fields the dashboard groups by. So identity is an immutable
companion — and revision 1's version (`initial_snapshot_hash` + an unspecified
`run_identity_hash`) did not actually identify the run's business inputs. The complete payload:

```
feature_run_identity                    -- append-only, write-once trigger
    generation_run_id           PK/FK
    workflow_definition_version          'V1'
    intent_id                   FK -> contract_intent
    generation_input_content_hash        -- the sealed generation input
    confirmed_scope_id
    considered_revision_id      FK -> contract_considered_revision
    considered_content_hash
    metadata_snapshot_id        FK -> catalog_metadata_snapshot
    metadata_snapshot_content_hash       -- exact id AND hash, never a hash alone
    owner_subject               NOT NULL  -- immutable; the mutable base row cannot move ownership
    owner_tenant                NULL      -- from IdentityEnvelope.tenant (envelopes.py:26)
    root_generation_run_id      NOT NULL
    parent_generation_run_id    NULL
    forked_from_attempt_id      NULL FK
    fork_plan_revision_id       NULL FK   -- §10
    run_identity_hash                    -- JCS hash over EXACTLY the fields above
    created_by · created_at
```

Constraints, enforced rather than narrated:

* a root points to itself: `parent IS NULL ⇔ generation_run_id = root_generation_run_id`;
* parent and root must themselves have `feature_run_identity` rows (workflow-V1 runs);
* `forked_from_attempt_id` must belong to the parent (composite FK through the attempt's run id);
* a fork inherits its parent's `intent_id` — crossing intents mints a new root, never a fork.

**Written in the same transaction that completes run creation** — the `contract.py` flow already
creates run, considered revision and snapshot atomically, so the identity row joins that
transaction. A run whose creation path does not produce a considered revision and snapshot gets
**no identity row** and renders as pre-spine. `catalog_metadata_snapshot` does not enforce one
snapshot per run (the schema allows more; live happens to hold 1:1) — the identity pins the exact
`snapshot_id`, so multiplicity in the base table cannot blur which one this run is.

**Grouping reads through identity when it exists.** For workflow-V1 runs the dashboard's owner and
intent come from `feature_run_identity`, so no edit to the mutable base row can move a run between
groups or owners. Pre-spine runs group by the mutable row, labeled as such.

```
feature_run_profile                     -- mutable display metadata, identity-free
    generation_run_id · display_name · description · archived
```

**No identity row is fabricated for the twelve existing runs** (5 `grun_*` with intent + considered
revision + snapshot; 7 `fgr_*` with snapshots only; selections, build sets, generation requests and
verifications all measured zero). Computing `run_identity_hash` over absent fields would put a
fabricated identity in the identity table. They render `PRE_SPINE`, showing only what exists.
Whether a pre-spine run can ever be *adopted* is a deferred owner decision (§13) — adoption would
require its genuine lineage, never a backfill.

### 6.2 Frozen inputs are relational, not JSON **[closes P0-4]**

Revision 1 froze `upstream_output_refs` and `selection_formula_binding_ids` as JSON — which the
database cannot check for existence, ownership, or stage provenance, contradicting the spec's own
typed-link principle. Corrected: a parent revision plus typed children per action.

```
feature_run_action_input                -- immutable
    input_revision_id       PK
    generation_run_id · action · stage_subject_id
    input_content_hash
    UNIQUE (input_revision_id, generation_run_id, action)   -- the attempt's composite FK target

-- per-action children, e.g. for GENERATE_PREVIEW:
preview_input_member                    -- the frozen ORDERED member set (§12 needs it)
    input_revision_id · position
    selection_formula_binding_id  FK -> selection_formula_binding        (migration 1101)
    UNIQUE (input_revision_id, position)
preview_input_catalog_snapshot
    input_revision_id · snapshot_id FK · snapshot_content_hash
preview_input_renderer
    input_revision_id · renderer_contract_revision_id FK
```

An attempt references its input by the composite FK `(input_revision_id, generation_run_id,
action)`, so it cannot cite an input revision minted for a different run or action. Every retry and
re-execution of a subject references the **same** input revision; a different input is a different
question and requires a fork.

### 6.3 Typed subjects

`stage_subject_id` is not a free string. Per action it is a typed FK: `AUTHOR_FORMULA` →
`authoring_subject_revision` (parent migration 1104); `GENERATE_PREVIEW` → `build_set_revision`;
`EXECUTE_SANDBOX` → the sealed artifact; `TRAIN_MODEL` → a feature-dataset revision. One child
table per action kind carries the typed column; a CHECK on the header binds action ↔ child.

### 6.4 Typed links to the authoritative domain attempt

One link column set per action, `NOT NULL` for that action's headers — the header exists only
because the domain attempt does, created in the same trigger transaction:

```
AUTHOR_FORMULA         -> formula_draft_id · formula_content_hash (when READY)
GENERATE_PREVIEW       -> generation_request_id  (thence build_set_revision_id, sealed_artifact_id)
EXECUTE_SANDBOX        -> verification_request_id
PUBLISH_SANDBOX        -> publication_attempt_id
MATERIALIZE_PRODUCTION -> (parent migration 1111's attempt)
PUBLISH_PRODUCTION     -> (parent migration 1112's attempt)
```

Every *reuse* (attaching to an already-live domain request, binding an already-existing draft) is
one of these rows — never inferred afterwards from hashes or timestamps.

### 6.5 Canonical output, separated from re-execution history **[closes P0-5]**

Revision 1 held both in one `UNIQUE (run, stage)` row with a `reproduction_status` column — so
attempt 2's `REPRODUCED` and attempt 3's `DIVERGED` could not both survive. Corrected:

```
feature_run_attempt_output              -- immutable, one per successful attempt
    attempt_id              PK/FK
    <typed output reference per action>          -- never bare text
    output_content_hash
    compared_to_attempt_id  NULL FK              -- the canonical attempt at comparison time
    reproduction_status     NULL                 -- REPRODUCED · DIVERGED; NULL for the first

feature_run_canonical_output            -- the pointer; the row never moves
    generation_run_id · action · stage_subject_id
    canonical_attempt_id    FK
    UNIQUE (generation_run_id, action, stage_subject_id)
```

The first accepted successful `START`/`RETRY` attempt establishes canonical. A re-execution never
silently replaces it; its own output row records its own comparison verdict, permanently. Adopting
a diverged output requires a fork.

### 6.6 Attempt events **[R2 — ordering and hygiene]**

```
feature_run_action_event                -- append-only; the COORDINATOR's stream only
    attempt_id · event_sequence · event_kind
    actor_subject                        -- who/which worker
    detail jsonb                         -- counts and codes; NEVER formula bodies, provider
                                         -- payloads, credentials or row data
    recorded_at
    UNIQUE (attempt_id, event_sequence)
```

`recorded_at` alone is not an ordering key; `event_sequence` is. This stream records orchestration
facts (requested, authorized, decision recorded, domain attempt created, attached, output bound).
Domain execution facts live in the domain stores and are **merged at read time** — the timeline is
a projection over both streams, each internally ordered by its own sequence.

---

## 7. Stage vocabulary and the milestone/action split

| Kind | Name | Substrate today |
|---|---|---|
| milestone | `CHOOSE_CANDIDATES` | `contract_gate1_choice_revision` — run-scoped already |
| action | `AUTHOR_FORMULA` | `formula_draft` + worker (live) |
| milestone | `BIND_SELECTIONS` | `feature_selection_revision` + 1101 binding (unbuilt) |
| action | `GENERATE_PREVIEW` | `generation_request` + V2 lane (live behind flag) |
| action | `EXECUTE_SANDBOX` | **socket** — `verification_request` claimed by no worker (§9.0) |
| action | `PUBLISH_SANDBOX` | **socket** — see below |
| action | `MATERIALIZE_PRODUCTION` | **socket** — new-workflow state machine does not exist (1111 unbuilt) |
| action | `PUBLISH_PRODUCTION` | **socket** — 1112 unbuilt |
| action | `TRAIN_MODEL` | **socket** — no subsystem |

**[R2 — closes P0-8] Five sockets, not two.** Revision 1 reported `EXECUTE_SANDBOX` and
`TRAIN_MODEL` unavailable and rendered "Sandbox publish — Not started" and "Production — Blocked:
certification pending". Both renderings were false. `publication_attempt` is settled by **no
runtime code** — `settle_attempt` is exported by `publication_attempt_store.py` and called only
from `test_publication_attempt_s10.py`; the route at `feature_execution.py:424` records STARTED
and promises "a worker performs the swap", and there is no such worker. And production is not
"blocked pending certification" — its new-workflow state machines (parent migrations 1111/1112)
do not exist. *Not started* means "could run, nobody asked"; *blocked* means "the platform worked
and governance refused". Neither is true of an unimplemented lane. All five render `UNAVAILABLE`
with a reason code (`WORKER_NOT_IMPLEMENTED`, `STATE_MACHINE_NOT_BUILT`), and triggering any of
them returns a controlled 409 creating no queue work.

Authoring precedes selection (parent §0.1.4 / §0.1.1: the subject is a **candidate**;
`feature_selection_revision` binds the READY formula afterwards). The projection vocabulary for the
rail: `NOT_STARTED · UNAVAILABLE · WAITING_FOR_USER · IN_PROGRESS (queued/claimed/running, from
the domain record) · SUCCEEDED · BLOCKED · FAILED · CANCELLED · UNKNOWN · NOT_APPLICABLE` — with
`BLOCKED` vs `FAILED` keeping 1090's distinction (different people, different remedies), and
`UNKNOWN` for external effects pending reconciliation.

---

## 8. Re-execution is stage-specific (unchanged from revision 1, restated)

| Action | The truthful re-execution |
|---|---|
| `AUTHOR_FORMULA` | **Reuse existing formula** — no provider call. `formula_identity_hash` is UNIQUE to protect money (1090); byte-identical inputs return the existing draft, so a divergence is unobservable here by design |
| `AUTHOR_FORMULA` | **Request another opinion** — a separately authorized, priced act that forks; requires a config hash that actually moves identity (the current one is a measured constant) — deferred with the spend contract |
| `GENERATE_PREVIEW` | **Re-render the exact pinned formula**; compare artifact hashes after rendering → `REPRODUCED` / `DIVERGED`. Never promise byte-equality before rendering |
| `EXECUTE_SANDBOX` | **Run the exact sealed artifact again** (once the lane exists) |

Two labels, never one: *"Re-run — uses this run's frozen inputs"* vs *"Fork — creates a new run
from an approved fork plan"*. Labelling both "Run again" is prohibited.

---

## 9. Lineage: what the bridge proves, and what it does not **[R2 — closes P0-7]**

Revision 1 claimed *"every act can resolve its run through the considered revision."* False in the
execution tail. The corrected statement, act by act:

* **Authoring and selection** can bridge — but bare FKs are not enough. Live data permits adding
  them (0 orphan drafts, 0 orphan selections against `contract_considered_revision`), yet a simple
  FK still lets a selection combine a target reading from intent A with a considered revision from
  intent B, and lets a draft cite a real considered revision beside mismatched planning or
  snapshot facts. The spine therefore requires **same-intent / same-run composite lineage**:
  `feature_selection_revision` gains composite FKs tying its target reading and considered revision
  to the same intent, and drafts bind through `authoring_subject_revision` (parent 1104) — the
  subject row carries the matched facts, and the draft references the subject, not loose columns.
* **The build lane** bridges through `build_set_member → feature_selection_revision` once 1101's
  binding exists.
* **`publication_attempt` (1081) carries bare text ids with no FKs** to the verified output or the
  sealed artifact. Its FKs must be added (or the table rebuilt — it is empty on live) before
  publication joins the spine, so that output, artifact, environment and group cannot disagree.
* **Legacy `materialization_request` (1053) is OUTSIDE the spine, permanently.** It has no
  considered-revision bridge, and the parent plan's D2 deletes its whole lane. The dashboard never
  renders it as a production-stage parent; its history is legacy evidence, not a spine stage.

---

## 10. Fork is a consumed plan, not a mood **[R2]**

"Fork using current inputs" is unimplementable as stated — *current* could mean: keep the old
snapshot and change one formula; refresh the snapshot; regenerate recommendations; change the
candidate set; refresh renderer versions. Each is a different fork.

```
feature_run_fork_plan_revision          -- immutable, minted by the preflight
    fork_plan_revision_id   PK
    parent_generation_run_id FK
    parent_state_version                 -- the coordination state the plan was computed against
    retained_pins  jsonb                 -- every pin kept, by exact identity
    replaced_pins  jsonb                 -- every pin replaced, old identity -> new identity
    invalidated_actions                  -- what the replacement invalidates downstream
    created_by · created_at
```

`POST /forks` consumes exactly one plan revision; the child's `feature_run_identity` records
`fork_plan_revision_id`. No implementation ever resolves an unconstrained "current". The plan is
recomputed-and-refused if the parent's `state_version` moved since preflight.

**Fork creation is absent from the foundation increment entirely** — revision 1 listed "fork
lineage" as foundational while proving a meaningful fork is unrepresentable before the formula pin.
The identity columns exist from day one; the endpoint does not.

---

## 11. Authorization **[R2 — corrected to §0.1.0]**

The parent plan's owner ruling (§0.1.0) supersedes revision 1's role-scoped trigger model:

```
any authenticated DEVELOPMENT user
    -> may trigger any IMPLEMENTED, NON-PRODUCTION stage
    -> and the server records WHO triggered it
```

Safeguards are not relaxed: roles never come from the request body; the triggering user is
recorded; a client may not supply an authorization belonging to somebody else (the server creates
or resolves it from the run and the current user — the rule `7a2c78b9` enforces via
`authorization_grantee`). `MATERIALIZE_PRODUCTION` and `PUBLISH_PRODUCTION` are UNAVAILABLE, which
§7 already says for the stronger reason that they do not exist.

**Reading is not triggering.** "Anyone may trigger" does not mean everyone reads every hypothesis.
The development read policy:

| Surface | Rule |
|---|---|
| `GET /feature-runs` | `owner_subject == current subject`; `platform_admin` may list all |
| run detail | same rule, plus the existing catalog-read enforcement on catalog-derived content |
| production actions | unavailable |

Authorization applies **inside the query, before pagination and counts** — a count computed over
rows the caller may not see leaks the shape of other people's work.

Two facts revision 1 got wrong, corrected: there are **six** RBAC role bundles
(`catalog_viewer · data_owner · feature_engineer · access_admin · audit_reader · platform_admin`,
`permissions.py`), not five. And tenancy is not "absent from the codebase" —
`IdentityEnvelope.tenant` exists (`envelopes.py:26`) and flows through JWT verification and event
serialization; what is absent is a **persisted run-tenancy and tenant-authority model**. The
identity row stores `owner_tenant` from the envelope now, so a future tenant-authority model has
real data; enforcement policy is deferred (§13).

---

## 12. Dashboard **[R2 — aggregation specified]**

Grouped by hypothesis via `contract_intent`; a run is minted per recommendation generation
(`contract.py:790`), so one intent has many runs, and 7 of 12 live runs have no intent — a real
"No hypothesis recorded" bucket, never an invented header. Run ids are rendered truncated and
copyable (`grun_01M02SAZ…`), never re-numbered into a friendlier sequence; the human name lives in
`feature_run_profile` and shows `—` until set. Pre-spine runs show only genuine evidence.

The folds the UI needs, each with its source named:

| Fold | Derivation |
|---|---|
| per-member authoring progress | headers for `AUTHOR_FORMULA` grouped by subject, status derived from each linked draft |
| method mix | honest only when methods can differ; today every formula is LLM-authored (parent §0.2 fact 3), so the column says so rather than inventing a mix |
| member count and order | **only from a frozen ordered set** — `preview_input_member` (§6.2) or the build set itself. Before one exists, choices accumulate individually with no stable order, and the dashboard says "N candidates chosen (accumulating)", not "N members" |
| overall run status | worst-of fold over milestone evidence + derived action states, with `BLOCKED` outranking `IN_PROGRESS` |
| partially blocked | "8 ready · 1 blocked · 3 running" — counts, never a single dishonest word |
| pagination | grouped by intent, keyset cursor on `(intent recency, run recency, run_id)` — stable under concurrent inserts |
| fork invalidation | from the fork plan's `invalidated_actions`, once forks exist |

Route: `#/runs/{run_id}`, replacing the nine query-string fragments at `App.tsx:510`.

---

## 13. Increments **[R2 — restructured around the drift and bypass findings]**

### Foundation increment — the spine as a read projection

**Decision: domain lifecycles remain authoritative, and the foundation writes no lifecycle at
all.** Revision 1 had existing writers dual-writing stage rows "in the same transaction" — which is
not read-only, requires changing every transition writer (authoring progresses across multiple
worker transactions), and invites drift. Corrected: the foundation is **derivation, not
recording**.

Ships:

* `feature_run_identity` (written by run creation from now on) · `feature_run_profile` ·
  `feature_run_state` · the lineage FK hardening of §9 (simple FKs now, composite lineage with the
  actionable increment)
* Run list + run detail, at `#/runs/{run_id}`, entirely **projected** from existing stores through
  the bridge: choices (1025, already run-scoped), drafts (via considered revision), and for the
  twelve existing runs, `PRE_SPINE`
* The §11 read policy — P0, it gates the endpoints' existence
* All five sockets visibly `UNAVAILABLE`; milestones derived from evidence
* **No orchestration headers, no run-centric triggers, no re-run, no fork, no cancel**

**Backend closure of the bypass, not UI absence [R2]:** `POST /build-sets` remains registered
whenever `FEATUREGEN_GENERATION_V2_ENABLED` is on (`build_sets.py:80` registers the router behind
`require_generation_enabled` only). Hiding its button does not protect 1101's
zero-rows/no-backfill branch — **one caller destroys it**. The foundation adds a server-side
refusal on build-set declaration (`409 UNAVAILABLE_PRE_PIN`, reason named) until 1101 lands. The
other direct routes (formula drafts, generations, verifications, publications) keep working — the
projection reads whatever they write — but each is enumerated in the implementation plan with its
disposition: *adapter onto the coordinator*, *refuse-until-pin*, or *delete with its lane (D2)*.
None may quietly remain a second door after the coordinator exists.

**Dependencies, stated honestly:** the foundation depends only on its own migrations (identity,
profile, state, FKs — 1115+). It does **not** depend on 1100–1106, because it creates no headers.

### Actionable increment — the pinned, triggerable journey

* Parent migrations first: **1100** (action authorization) · **1101** (the binding, `NOT NULL`,
  inside `build_set_revision.content_hash`) · **1104** (authoring subject) · **1105** (spend) ·
  **1106** (decision revisions). This increment is sequenced **behind** the parent plan's 2A — it
  was never an independent first step, and revision 1 presented it as one.
* The coordinator: `feature_run_state` CAS · orchestration headers · typed inputs · typed domain
  links · canonical outputs · events
* `/start` · `/retry` · `/re-execute` for `AUTHOR_FORMULA` and `GENERATE_PREVIEW`; the existing
  direct routes become adapters invoking the same coordinator contract or are deleted
* Reproduction/divergence reporting per §6.5
* Fork plans + fork creation
* Cancellation per action, as each action's worker earns it

### Later, in order

`EXECUTE_SANDBOX` when §9.0's eight responsibilities exist as one shipped unit · `PUBLISH_SANDBOX`
when something settles it · production stages on 1111/1112 with their certificates · `TRAIN_MODEL`
through its socket, consuming a specific feature-dataset revision.

---

## 14. Deferred decisions

| Decision | Why deferred |
|---|---|
| tenant-authority model | `IdentityEnvelope.tenant` is captured into identity now; enforcement policy has no substrate yet |
| governed cost estimator | spend contract P0-10 undesigned; plan reports cost unavailable |
| "Request another opinion" | needs an identity-moving config hash; the current one is a constant |
| run retention / auto-archive | a run per recommendation generation accumulates abandoned runs; owner policy |
| pre-spine adoption | only with genuine lineage, never backfill; owner decision |
| `catalog_metadata_snapshot` UNIQUE(run) | live is 1:1 but 1006 anticipated accretion; identity pins the exact snapshot either way |

---

## 15. Facts this design rests on

All opened at the code baseline; review-verified items marked ®.

| Fact | Where |
|---|---|
| nine query-string fragments carry the workflow | `App.tsx:510` |
| no `generation_run_id` in the execution lane | `build_sets.py` · `feature_execution.py` · `formula_drafts.py` · migration 1092 — zero |
| run minted per recommendation generation | `contract.py:790` |
| many runs per intent · second prefix `fgr_` | `1021:17` · `gate1.py:733` |
| candidate choice already run-scoped | `1025:4-18` |
| fork cannot mint new selections · build-set identity excludes the formula · formula resolved latest-wins | `1072:97` · `1092:56,66` · `restore_formula_v3.py:90` |
| authoring idempotent on identity; config hash a constant | `1090` · `_authoring_config_hash` |
| authoring subject is a candidate | parent §0.1.4 / §0.1.1 |
| sandbox lane does not exist | parent §9.0; `verification_request` unclaimed |
| ® `settle_attempt` has only test callers — sandbox publication is a socket | `publication_attempt_store.py:183`; callers only in `test_publication_attempt_s10.py`; route promise at `feature_execution.py:424` |
| ® `publication_attempt` has bare text ids, no FKs | `1081:32` |
| ® six RBAC role bundles | `permissions.py` — counted |
| ® `IdentityEnvelope.tenant` exists; persisted run-tenancy does not | `envelopes.py:26` |
| ® collection endpoints exist; a feature-run list does not | `contract.py:590` · `features.py:95` |
| ® migrations 1100–1114 reserved by the parent plan; 1113/1114 = compiler evaluation | plan §17 lines 2898–2912 |
| ® 0 orphan drafts / selections against considered revisions; counts 12/12/5/7/0/0/0/0 | owner-measured, live, 2026-08-22 |
| development authorization ruling | parent §0.1.0 (folded at revision five) |
| server-owned authorization + V2 crash recovery landed | `7a2c78b9` |
| `POST /build-sets` reachable whenever the V2 switch is on | `build_sets.py:80` |
| run/stage/attempt precedent | `0994` · `0996` |
