# The Feature Run Spine — design

**Date** 2026-08-22 · **Baseline** `feature/asset-detail-reapply` @ `3c52a9de` (86 ahead of
`origin/main`) · **Status** design, approved in dialogue; not yet a plan

A durable identity for one feature-generation workflow, so a person can leave, come back tomorrow,
and run the next stage against exactly what they left — never against "latest".

---

## 1. The problem, stated in code rather than in prose

**The workflow's continuity currently lives in a URL query string.** `App.tsx:510` hands
`FeatureExecutionScreen` nine separate identity fragments — `artifact_id`, `environment_id`,
`group`, `observation_id`, `authorization_id`, `check_set_hash`, `goal`, `target_mode`,
`target_ref` — each defaulting to `''`. Close the tab and the work is unrecoverable. There is no id
that reconstitutes a workflow.

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
| production materialization | `materialization_request` | 1053 |

Eight stores, eight identity keys, eight lifecycles, no spine. Each answers *"what happened to this
attempt"*. Nothing answers *"what happened to my run"*.

**There are no list endpoints anywhere in the codebase.** `ingestion_runs.py` has exactly one route
— detail by id. `materialization_runs.py` has POST plus GET-by-id. Every read path requires the
caller to already know the id. A runs dashboard has zero backing today, and the question *"which
runs may this person see"* has never been asked in this system.

**A run/stage precedent already works, on the ingestion side only.** `ingestion_run` (0994) +
`ingestion_run_stage` (0996): a run header, child stage rows keyed `UNIQUE (run_id, stage, attempt)`
so a retry appends attempt N+1 rather than clobbering N, a closed fourteen-value state vocabulary
that distinguishes five different ways of not having happened, and an append-only status-event
history. The feature workflow inherits the shape and rejects the lifetime: ingestion completes
inside one bounded request and buffers its stage rows in memory; a feature run stays open for days.

---

## 2. The invariant

> **A stage may run only when every input it consumes is named by immutable identity. If an exact
> identity is missing, the stage is UNAVAILABLE. The platform never substitutes "latest."**

Every rule below is a consequence of this sentence. Where the platform cannot honour it today, the
answer is to withhold the action, not to approximate it.

---

## 3. Identity hierarchy

Four identities, and the fourth is a relationship rather than a thing:

```
Contract intent                     "one retail current-account churn hypothesis"
│   contract_intent (0962)
│
├── Feature run  grun_1042          "which frozen feature workflow is this?"
│   │   feature_generation_run (1006)
│   │
│   ├── Stage  GENERATE_PREVIEW     "which part of that workflow?"
│   │   ├── Attempt 1               "which execution of that stage?"
│   │   └── Attempt 2
│   └── Stage  EXECUTE_SANDBOX
│       └── (unavailable — no worker)
│
└── Feature run  grun_1048          forked_from_run_id = grun_1042
    reused unchanged candidates · corrected avg_balance_90d · different pinned build
```

**The run id is `feature_generation_run.generation_run_id`.** Minting a second root would leave the
platform with two answers to "which workflow is this", and the existing id already owns the intent,
the actor, the configuration, the catalog metadata snapshot, the considered revision and the input
lineage.

**But this is a bridge, not a promotion.** `grep generation_run_id` across `build_sets.py`,
`feature_execution.py`, `formula_drafts.py`, `suggestions.py` and migration 1092 returns **zero
occurrences in all five**. Its only writers are `contract.py:794`, `gate1.py:728` and the
compatibility path at `feature_metadata_snapshot.py:574`. Today the run spans the recommendation
half of the journey and nothing downstream of it. Connecting it to execution is most of this
project, and calling it a rename would hide that.

### The bridge already has a path — and it is unenforced

Every act in the execution lane can resolve its run **through the considered revision**:

```
formula_draft.considered_revision_id ─────────┐
feature_selection_revision.considered_revision_id ─┼─→ contract_considered_revision.generation_run_id
build_set_member → feature_selection_revision ┘        (NOT NULL, UNIQUE (intent_id, run))
```

That is what makes §3's bridge tractable rather than a re-keying of eight stores: the run is already
derivable everywhere, it is simply never carried.

**But the join is by convention, not by constraint.** `considered_revision_id` is a bare
`text NOT NULL CHECK` in both `feature_selection_revision` (`1072:87`) and `formula_draft`
(`1090:50`) — **neither declares `REFERENCES contract_considered_revision`**. A draft or a selection
can name a considered revision that does not exist, and nothing stops it. The spine must add those
foreign keys before it depends on the path, or it inherits a lineage that can dangle. Both tables
are additive to check: `formula_draft` holds 7 rows and `feature_selection_revision` holds 0.

**Ids are opaque and the space has two prefixes.** `contract.py:790` mints `grun_`; `gate1.py:733`
mints `fgr_` when no run id is supplied. Both are runs. Nothing may parse a run id or assume a
prefix.

**Intent is the dashboard's grouping key.** `contract.py:790` mints a fresh run on *every*
recommendation generation — the comment says so outright: *"the run is born only NOW, when the human
commits to generate"* — and `contract_considered_revision` is `UNIQUE (intent_id,
generation_run_id)`, so one intent legitimately has many runs. A person who regenerated
recommendations three times while refining scope has three runs and one hypothesis. Rendering those
as three unrelated workflows is the wrong first impression.

`feature_generation_run.intent_id` is **NULLABLE by design** (1006: *"a run may exist before a
Gate-#1 choice is recorded against it"*), and seven of the twelve live rows have no intent. The
dashboard therefore needs a real *ungrouped* bucket — runs with no recorded hypothesis, shown as
that. It must never invent a hypothesis to fill a group header.

---

## 4. Stage vocabulary

The business phrase "select features" covers two distinct technical decisions and must be split.

| Stage | What it decides | Existing substrate |
|---|---|---|
| `CHOOSE_CANDIDATES` | which features are worth pursuing | `contract_gate1_choice_revision` (1025) |
| `AUTHOR_FORMULA` | what to calculate, for one candidate | `formula_draft` (1090) |
| `BIND_SELECTIONS` | this formula belongs to this selection | `feature_selection_revision` (1072) + `selection_formula_binding` (§11.0.1, unbuilt) |
| `GENERATE_PREVIEW` | produce the Kedro/PySpark project | `build_set_revision` · `generation_request` (1092) |
| `EXECUTE_SANDBOX` | run generated code against test data | `verification_request` (1094) — **no worker claims it** |
| `PUBLISH_SANDBOX` | make test results readable | `publication_attempt` (1081) |
| `MATERIALIZE_PRODUCTION` | store production values | none |
| `PUBLISH_PRODUCTION` | make those values available downstream | none |
| `TRAIN_MODEL` | train on this run's exact feature dataset | none |

`CHOOSE_CANDIDATES` already carries `generation_run_id NOT NULL REFERENCES feature_generation_run`
and `UNIQUE (generation_run_id, considered_revision_id, option_id)`. The spine's first plank exists.

**Authoring precedes selection, and that is the parent plan's ruling, not a preference.** §0.1.4 —
*"`AUTHOR_FORMULA`'s subject is a CANDIDATE, not a selection"* — with §0.1.1 correcting the evidence
table: *"`AUTHOR_FORMULA` carries an authoring-subject revision, never selections."* The subject is
`(considered_revision_id, option_id, planning_request_hash, catalog_snapshot_hash,
definition_revision)`. Selection binds the resulting READY formula afterwards.

### The stage graph is a DAG, and two of its nodes are sockets

```
CHOOSE_CANDIDATES → AUTHOR_FORMULA → BIND_SELECTIONS → GENERATE_PREVIEW
                                                            │
                                            ┌───────────────┴───────────────┐
                                      EXECUTE_SANDBOX          MATERIALIZE_PRODUCTION
                                            │                          │
                                      PUBLISH_SANDBOX          PUBLISH_PRODUCTION
                                            └───────────────┬───────────┘
                                                       TRAIN_MODEL  [socket]
```

**`EXECUTE_SANDBOX` is a socket today, exactly like `TRAIN_MODEL`.** Parent plan §9.0 —
*"The sandbox execution lane DOES NOT EXIST"* — and `POST /feature-execution/verifications` records
a row **no worker claims**. Reporting it as `QUEUED` would leave the first real user watching a
spinner that can never resolve, on the stage they care about most. It reports `UNAVAILABLE` with
`WORKER_NOT_IMPLEMENTED` until a worker exists.

`TRAIN_MODEL` must ultimately consume a *specific feature-dataset revision* produced by sandbox or
production, never "the latest feature table". Its declared required inputs are
`feature_dataset_revision_id` and `training_spec_revision_id`; triggering it returns a controlled
409 and creates no queue work.

---

## 5. Stage state vocabulary

Closed by CHECK, so an application bug cannot invent a value.

| State | Means |
|---|---|
| `NOT_STARTED` | could run; nobody triggered it |
| `UNAVAILABLE` | the stage exists; **no implementation is deployed** |
| `WAITING_FOR_USER` | a prerequisite is a human decision |
| `QUEUED` · `CLAIMED` · `RUNNING` | in flight, with a worker identity once claimed |
| `SUCCEEDED` | produced its output |
| `BLOCKED` | the platform worked; **governance or inputs refused** |
| `FAILED` | infrastructure or code failed |
| `CANCEL_REQUESTED` · `CANCELLED` | a person asked it to stop |
| `UNKNOWN` | an external effect may have landed; reconciliation required |
| `NOT_APPLICABLE` | this run's shape has no such stage |

`BLOCKED` and `FAILED` are different people with different remedies, which is why 1090 already draws
the same line for `formula_draft` (*"BLOCKED IS A PRODUCT RESULT, NOT A FAILURE"*). `UNKNOWN` exists
because the current publication read reports *blocking attempts only* and cannot distinguish
never-attempted from succeeded — a distinction `FeatureExecutionScreen` already refuses to fake.

---

## 6. Storage model

Migration numbers **1113 onward**; the two 2026-08-22 plans already reference numbers through 1112,
and allocation must be re-checked against the live ledger at implementation time.

### 6.1 Immutable identity, separate from mutable display

`feature_generation_run` is deliberately **not** write-once (1006: *"a run manifest may accrete
context"*), so identity gets an immutable companion.

```
feature_run_identity                        -- append-only, write-once trigger
    generation_run_id            PK/FK -> feature_generation_run
    workflow_definition_version           -- 'V1'
    root_generation_run_id
    parent_generation_run_id     NULL
    forked_from_stage_attempt_id NULL
    fork_reason                  NULL
    initial_snapshot_hash
    run_identity_hash
    created_by · created_at

feature_run_profile                         -- mutable, and identity-free
    generation_run_id · display_name · description · archived
```

Renaming a run must not re-key anything. That is the whole reason for the split.

**No identity row is written for the twelve pre-existing runs.** Five `grun_*` rows have intent,
considered revision and snapshot; seven `fgr_*` rows have snapshots but no intent or considered
revision; `feature_selection_revision`, `build_set_revision`, `generation_request` and
`verification_request` are all **zero**. None of the twelve has selection or build lineage. A
"conservative backfill" would compute `run_identity_hash` over absent fields — a fabricated identity,
in the identity table, on a platform whose stated rule is honest absence. They render as
`PRE_SPINE`, showing only the evidence that genuinely exists.

### 6.2 Stage attempts and their events

```
feature_run_stage_attempt
    stage_attempt_id             PK
    generation_run_id            FK
    stage_kind                            -- closed CHECK, §4
    attempt_number
    attempt_purpose                       -- INITIAL · RETRY_FAILED · RE_EXECUTE
    stage_input_revision_id      FK
    action_authorization_revision_id
    action_decision_revision_id
    status                                -- closed CHECK, §5
    reason_code
    requested_by · requested_at
    claimed_by · lease_until · fence_token
    started_at · completed_at
    UNIQUE (generation_run_id, stage_kind, attempt_number)

    -- at most one live attempt per run stage
    UNIQUE (generation_run_id, stage_kind)
        WHERE status IN ('QUEUED','CLAIMED','RUNNING','CANCEL_REQUESTED')

feature_run_stage_attempt_event             -- append-only
    event_id · stage_attempt_id · status · reason_code · detail · recorded_at
```

`attempt_purpose` says `RE_EXECUTE` rather than `REPLAY`, because replay is not one semantic across
stages (§8) and a uniform word would hide the difference between free and expensive.

The append-only event stream is what makes a GitHub-Actions-style timeline possible: *queued ·
claimed by worker-2 · resolving formulas · compiling 12 members · rendering · sealing · completed*.

### 6.3 The frozen input manifest

```
feature_run_stage_input_revision            -- immutable, content-addressed
    input_revision_id            PK
    generation_run_id · stage_kind
    upstream_output_refs                  -- typed refs to canonical outputs, never "latest"
    selection_formula_binding_ids         -- once §11.0.1 exists
    catalog_snapshot_hash
    configuration_versions
    environment_id
    input_content_hash
```

Every attempt of a stage references **the same** input revision. A different input revision is a
different question and requires a fork.

### 6.4 Typed links to existing evidence

One table per stage, not a JSON bag of downstream ids — so a reader can join rather than parse, and
so a link cannot claim a relationship the FK would have refused.

```
feature_run_formula_attempt       stage_attempt_id · formula_draft_id · formula_content_hash · member_name
feature_run_selection_binding     stage_attempt_id · selection_revision_id · selection_formula_binding_id
feature_run_preview_attempt       stage_attempt_id · build_set_revision_id · generation_request_id · sealed_artifact_id
feature_run_sandbox_attempt       stage_attempt_id · verification_request_id · verified_output_revision_id
feature_run_sandbox_publication   stage_attempt_id · publication_attempt_id
feature_run_production_material   stage_attempt_id · materialization_attempt_id · materialized_output_revision_id
feature_run_production_publication stage_attempt_id · publication_attempt_id
```

Each associates existing records without replacing their specialised identities. **Every reuse is
represented by one of these rows** — never inferred afterwards from hashes or timestamps.

### 6.5 The canonical output binding

```
feature_run_stage_output_binding
    generation_run_id · stage_kind · stage_attempt_id
    output_revision_id · output_content_hash · reproduction_status
    UNIQUE (generation_run_id, stage_kind)
```

* The first successful `INITIAL` or `RETRY_FAILED` attempt establishes the canonical output.
* A re-execution of an already-successful stage **never silently replaces it**.
* Identical output content → `REPRODUCED`. Different → `DIVERGED`; the original stays canonical.
* Adopting a diverged output requires a fork.

---

## 7. Fork, advance and re-execute

**Advance** means running the *next* stage using the exact recorded outputs of the stages before it.
It never means advancing the same run onto today's formula. Where an exact input identity is
missing, the stage is `UNAVAILABLE` — never best-effort.

**Fork** means a new run derived from this one: `parent_generation_run_id`, `forked_from_stage_attempt_id`
and a `fork_reason`. The parent stays completely reproducible.

**Two labels, never one.** *"Re-run stage — uses this run's frozen inputs"* and *"Fork using current
inputs — creates a new run and picks up approved changes"*. Labelling both "Run again" is
prohibited, because the two differ in cost, in audit meaning, and in which run the answer belongs to.

### Why the fork is unrepresentable until §11.0.1 lands

This is the finding that sets the whole sequence, and it is worth writing out because it is not
visible from any single table.

1. `feature_selection_revision` is `UNIQUE (target_reading_revision_id, considered_revision_id,
   option_id)` (`1072:97`). A fork that carries forward unchanged candidates **cannot mint new
   selection revisions** — it must reuse the parent's.
2. `build_set_revision.content_hash` is identity over *(target reading, ordered members,
   declaration)* (`1092:56`) with `UNIQUE (content_hash)` at `:66`. **The formula is not in it.**
3. Therefore a run and its fork hash to the **same build set**; the unique index hands the fork its
   parent's row.
4. And when either generates, the formula is resolved by `restore_formula_v3.py:90` — `ORDER BY
   updated_at DESC`.

So today, *re-run* does not replay (it silently picks up whatever formula is newest) and *fork*
produces a run that resolves to the same build set and the same newest formula. **Two buttons, one
behaviour.** `feature_run_stage_output_binding` cannot catch it either — it compares outputs *within*
a run, and the collision happens beneath it.

Parent plan §11.0.1 closes this exactly: `selection_formula_binding` (migration 1101) with composite
FKs to both parents, `build_set_member.selection_formula_binding_id NOT NULL`, and — the load-bearing
clause — *"The BINDING enters `build_set_revision.content_hash`, not the loose pair. Pinning that does
not change identity is not pinning."* Once a member's binding is inside build-set identity, a
corrected formula yields a different binding, a different build set, and a genuinely distinct fork.

**A closing window.** §11.0.1 lands `NOT NULL` with **no backfill** precisely because
`feature_selection_revision` and `build_set_revision` are measured at zero. Every run that reaches a
build set before it lands forces a nullable column and a backfill of exactly the rows the pin exists
to constrain. The foundation increment therefore withholds build-set *declaration*
(`POST /build-sets`, `build_sets.py:128`) as well as generation.

---

## 8. Re-execution is stage-specific

A single "replay" concept is misleading, because the stores beneath the stages have deliberately
different semantics.

| Stage | The truthful action |
|---|---|
| `AUTHOR_FORMULA` | **Reuse existing formula** — no provider call |
| `AUTHOR_FORMULA` | **Request another opinion** — a distinct, separately authorized, priced act that forks |
| `GENERATE_PREVIEW` | **Re-render the exact pinned formula** |
| `EXECUTE_SANDBOX` | **Run the exact sealed artifact again** |
| `TRAIN_MODEL` | unavailable |

**Why authoring has no generic replay.** `formula_draft.formula_identity_hash` carries a UNIQUE
index, and 1090 states the reason without ambiguity: *"DOUBLE-CLICK MUST NOT BUY TWO ANSWERS… the
idempotency is on the IDENTITY rather than on a caller-supplied key… and the thing being protected is
money."* Re-executing with byte-identical inputs finds the existing draft and returns it. There is no
second provider call, so a divergence can never be observed here — observing one would mean
defeating the money guard on purpose.

It is currently worse than neutral: `_authoring_config_hash()` calls `getattr()` on a **dict**, so
every field defaults and the hash is a constant across providers and models. Formula identity cannot
today distinguish one model from another, so even a model swap re-executes as a cache hit. *"Request
another opinion"* therefore requires a server-issued revision that actually moves identity, plus
spend authorization — which is why it is not in either increment below.

**Preview reproduction is reported honestly, in two steps.** Before execution, the platform states
what it *knows*: formula, catalog snapshot and renderer versions are unchanged, so a reproducible
output is expected. After execution it compares artifact hashes and records `REPRODUCED` or
`DIVERGED`. **It never promises byte-identical output before rendering** — that is only knowable by
rendering, and predicting it would be a fabricated certainty on a platform whose rule is honest
absence.

---

## 9. How three deduplication authorities coexist

They are not interchangeable; they protect different resources.

| Guard | Protects |
|---|---|
| `formula_draft.formula_identity_hash` UNIQUE | paying twice for the same authoring question |
| `generation_request_one_live_attempt (build_set_revision_id, environment_id)` | duplicate builds of one build set in one environment |
| `feature_run_stage_attempt` live-attempt index | two live attempts of one run stage |

**The stage coordinator is the single user-facing authority**, and only it may invoke the subordinate
stores. Its rules:

| Situation | Action |
|---|---|
| an exact existing result | bind and reuse it — and record the reuse as a typed link row |
| an exact request already live | attach to it; never launch a second |
| same stage, different input | **refuse**; require a fork |
| a finished retryable stage | append attempt N+1 |

---

## 10. Trigger contract

```
GET   /feature-runs                                    -- intent-grouped, server-authorized
GET   /feature-runs/{run_id}
GET   /feature-runs/{run_id}/stages/{stage}/attempts
GET   /stage-attempts/{attempt_id}
GET   /stage-attempts/{attempt_id}/events

POST  /feature-runs/{run_id}/stages/{stage}/plan       -- read-only preflight
POST  /feature-runs/{run_id}/stages/{stage}/attempts
POST  /stage-attempts/{attempt_id}/cancel
POST  /feature-runs/{run_id}/forks
POST  /feature-runs/{run_id}/archive
```

### The plan endpoint reports pins, not predictions

It answers: is the stage available; are prerequisites complete; **which exact input identities will
be used**; which blockers and warnings apply, each rendered with the sentence the server sent; what
authorization is required; and which stages a fork would invalidate.

It does **not** answer "will replay be byte-identical" — unknowable without rendering (§8). And it
returns **cost unavailable** until a governed estimator exists: the ordinary LLM spend-authorization
contract is P0-10 and undesigned, and the only config hash in the authoring path is the constant
described above. A cost figure here would be invented at the exact moment someone decides whether to
spend.

### The attempt body carries a key and a guard, nothing else

```json
{ "idempotency_key": "...", "expected_run_revision": "..." }
```

The client supplies **no** artifact id, formula draft id, build-set revision id, authorization role,
certificate id, "latest" selection, or upstream stage output. The server resolves every one from the
run. `expected_run_revision` guards against the run's *stage-attempt and output-binding state* having
moved since the client read it — the identity itself never moves.

This directly closes a live escalation: `GenerationIn.roles: list[str]` (`build_sets.py:122`) flows
to `roles=tuple(body.roles)` (`:259`) and into the read-scope predicate, letting a caller widen its
own read scope.

**The trigger transaction is atomic**: revalidate prerequisites → create action authorization →
create the durable action decision → create the stage attempt → enqueue outbox work → commit. The
worker revalidates the same decision immediately before executing. This is the discipline §0.1.3
found missing at `formula_drafts.py:117`, which writes a draft and an outbox message with no action
authorization, no decision and no spend authorization, after which `formula_draft_worker.py:239`
trusts `formula_draft.requested_by` off the row.

---

## 11. Authorization

`GET /feature-runs` must not be a table scan filtered in the UI. The caller's identity is derived
server-side and authorization is applied **inside the query, before pagination and counts** — a count
computed over rows the caller may not see leaks the shape of other people's work.

**Scoped against what exists.** `grep -rln "tenant_id\|tenancy" src/featuregen/` returns nothing:
there is no tenant model and no collaboration-grant model in this codebase. Visibility is therefore
derived from the five RBAC functional roles, the caller's permitted catalog read scope, and
`feature_generation_run.actor` as owner. Tenancy and explicit collaboration grants are recorded in
§14 as deferred decisions; the run list is not blocked on them.

Client-supplied roles play no part in listing runs or triggering stages.

---

## 12. Dashboard

### Runs list — grouped by hypothesis

```
Retail current-account churn                                        3 runs
  grun_01M02SAZ…  Retail churn — August   ascoe      12   Kedro generated     Ready for sandbox
  grun_01M01RTB…  —                       ascoe       8   Formula authoring   1 blocked
  grun_01M01Q7K…  —                       ascoe       0   Candidates chosen   —

No hypothesis recorded                                              7 runs
  fgr_01LZK4M2…   —                       analyst-2   —   Pre-spine           —
```

Run ids are rendered truncated and copyable, never re-numbered into a friendlier sequence. A display
label derived from an opaque id would be a second identity for the same run, and the first thing
someone would paste into a support ticket. A run's human name lives in `feature_run_profile` and is
absent — shown as `—` — until somebody sets one.

Filters: my runs / all authorized runs · created date · status · environment · hypothesis · stage ·
authoring method. Method filtering is honest only once methods differ — §0.2 fact 3 records that
`formula_draft_worker` passes no `reviewed_blueprint`, so **every formula today is LLM-authored**
whatever its recommendation's origin.

### Run detail

Header: run id, name, creator, date, member count, and fork lineage when present. Then the stage
rail, each stage opening onto its inputs, attempts, event timeline, output artifacts, blockers with
the server's own sentences, duration, and the two clearly distinct actions.

```
Candidates chosen     ✓
Formulas              ✓
Selections bound      ✓
Kedro code            ✓  2 attempts
Sandbox execution     Unavailable — worker not implemented
Sandbox publish       Not started
Production            Blocked — certification pending
Model training        Unavailable — subsystem not built
```

The route becomes `#/runs/{run_id}`, replacing the nine query-string fragments at `App.tsx:510`. The
backend reconstructs everything else from the run.

---

## 13. Increments

Two development increments. The first is honest and shippable; it is **observability, not a workflow
you can drive**, and should be named that way.

### Foundation increment — read-only spine

* Intent-grouped run dashboard and run detail, at `#/runs/{run_id}`
* Opaque run ids, both `grun_*` and `fgr_*`
* Server-scoped list/read authorization (§11) — **P0, and it gates the endpoint's existence**
* `feature_run_identity` · `feature_run_profile` · stage attempts · attempt events · fork lineage
* Stage attempts, events and typed links **written by the existing stage writers** — the routes that
  already create formula drafts and choices record a stage attempt against the run in the same
  transaction. The history is real from day one; what is withheld is the run-centric *trigger*, not
  the run-centric *record*.
* The twelve existing runs shown as `PRE_SPINE`, no fabricated identity hashes
* `EXECUTE_SANDBOX` and `TRAIN_MODEL` visibly `UNAVAILABLE`
* **No re-run button. No build-set declaration. No run-centric AUTHOR_FORMULA trigger.**

The only stage a person can trigger here is `CHOOSE_CANDIDATES`, whose store already exists and is
already run-scoped. `AUTHOR_FORMULA` cannot be triggered run-centrically until action authorization,
a durable decision and spend authorization bind before the outbox row (§10); the spend contract is
undesigned. Build-set declaration is withheld to keep §11.0.1's no-backfill branch alive (§7).

### Actionable increment — the pinned journey

* §11.0.1 `selection_formula_binding` (migration 1101), composite FKs to both parents
* The binding inside `build_set_revision.content_hash`
* Exact-input stage authorization end to end
* `BIND_SELECTIONS` and `GENERATE_PREVIEW` from a persisted run
* Stage-specific re-execution, and reproduction/divergence reporting

**Entry condition:** `selection_formula_binding` exists, `build_set_member` references it `NOT NULL`,
and the binding participates in build-set content identity. Until then `GENERATE_PREVIEW` reports
`UNAVAILABLE`, because a re-run button without the pin is an audit trail that lies.

### Later, in order

`EXECUTE_SANDBOX` when a real worker exists (§9.0's eight responsibilities) · production stages with
their own state machines · `TRAIN_MODEL` through its declared socket, consuming a specific
feature-dataset revision.

---

## 14. Deferred decisions

| Decision | Why it is deferred |
|---|---|
| Tenancy and collaboration grants | no substrate anywhere in `src/`; the run list ships scoped to roles + catalog scope + owner |
| Governed cost estimator | P0-10 spend contract undesigned; plan reports cost unavailable |
| "Request another opinion" pricing and authorization | needs a config hash that actually moves identity — the current one is a constant |
| Run retention and auto-archive | a run is minted per recommendation generation, so abandoned runs accumulate; policy is an owner decision, not a default |
| Whether `PRE_SPINE` runs can ever be adopted into workflow V1 | only if they later acquire genuine selection lineage; no fabricated adoption |

---

## 15. Facts this design rests on

Every one opened in the baseline worktree rather than recalled.

| Fact | Where |
|---|---|
| nine query-string fragments carry the workflow | `frontend/src/App.tsx:510` |
| no `generation_run_id` in the execution lane | `build_sets.py` · `feature_execution.py` · `formula_drafts.py` · `suggestions.py` · `1092` — zero |
| run minted per recommendation generation | `contract.py:790` and its comment |
| second id prefix `fgr_` | `gate1.py:733` |
| many runs per intent | `1021_contract_considered_revision.sql:17` |
| candidate choice already run-scoped | `1025_contract_option_choice.sql:4-18` |
| selection uniqueness blocks a fork's new selections | `1072:97` |
| `binding_plan_hash` is a selection fact, not a formula fact | `selection_revisions.py:307-315` |
| build-set identity excludes the formula | `1092:56,66` |
| one live generation attempt per build set + environment | `1092` partial unique index |
| formula resolved by `ORDER BY updated_at DESC` | `restore_formula_v3.py:90` |
| authoring idempotent on identity, to protect money | `1090_formula_draft.sql` |
| authoring config hash is a constant | `_authoring_config_hash()` — `getattr()` on a dict |
| authoring's subject is a candidate, not a selection | plan §0.1.4 · §0.1.1 |
| draft route binds no authorization, decision or spend | plan §0.1.3 · `formula_drafts.py:117` · `formula_draft_worker.py:239` |
| sandbox lane does not exist | plan §9.0 · `verification_request` unclaimed |
| the §11.0.1 binding and its identity clause | plan §11.0.1, lines 1806–1858 |
| client-supplied roles reach the read-scope predicate | `build_sets.py:122` → `:259` |
| no tenancy model | `grep tenant_id\|tenancy src/featuregen/` — empty |
| run/stage/attempt precedent | `0994_ingestion_run.sql` · `0996_ingestion_run_stage.sql` |
| no list endpoints anywhere | `ingestion_runs.py` 1 route · `materialization_runs.py` 2 routes |
| live counts (owner-measured) | `feature_generation_run` 12 · `catalog_metadata_snapshot` 12 · `contract_considered_revision` 5 · `feature_selection_revision` 0 · `formula_draft` 7 · `build_set_revision` 0 · `generation_request` 0 · `verification_request` 0 |
