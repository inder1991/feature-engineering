# The Feature Run Spine — design, revision 4

**Date** 2026-08-23 · **Code baseline** `feature/asset-detail-reapply` @ `302b8e9a` · **Plan
baseline** the four-stage gating plan at revision five (§0.1.0 included) and the recipe-to-code
child plan · **Status** design. Revision 1 (`9072f4d9`) was rejected with ten P0 gaps; revision 2
(`302b8e9a`) was rejected with eleven P0s and four P1s. Revision 3 folds that second review;
revision 3.1 **[R3.1]** folds the residuals of a 21-agent adversarial workflow pass (98 raw
findings, per-finding refutation): the §0.1.2 composite authorization/decision group spelled on
every durable attempt header, attach gated on input-identity equality, idempotency looked up
before the CAS, the phantom `renderer_contract_revision` FK replaced with real renderer identity
values, `preview_input_member` deleted as a second authority over the build set's own membership,
a pre-spine ownership rule so the foundation's only current content is not invisible, derived (not
static) availability with `GENERATE_PREVIEW` reason-coded during the foundation, and a total
domain→rail status mapping requirement.

**The standing verdicts this revision is written under:**

* **Read-only dashboard foundation — conditional GO**, the conditions folded here (§13).
* **Actionable runs, replay, retry, fork — NO-GO** until the amendments in §13 land.
* **The parent and child plans must themselves be amended** (§4.1) — implementing them as written
  would recreate the second lifecycle authority this spec forbids. That amendment is owner-gated
  work outside this document.

A durable identity for one feature-generation workflow, so a person can leave, come back tomorrow,
and run the next stage against exactly what they left — never against "latest".

**What revision 3 changes.** The child plan's `code_generation_job` is dissolved into an immutable
per-action *invocation* (§4.1); every input, subject, domain-attempt, authorization and decision
relationship becomes composite and subject-consistent (§6.2, §6.4); input sets get real relational
sealing on the 1006 snapshot pattern (§6.2); formula hashes move out of the immutable header into
attempt outputs (§6.4/§6.5); an atomic output-binding contract with recovery replaces the too-broad
"zero reconcilers" claim (§6.5); `AUTHOR_FORMULA` retry is withdrawn as impossible against the
current domain model (§8); fork is redesigned around typed, sealed, one-time-consumed pins and an
explicit cross-run adoption problem (§10); run identity becomes a composite-FK-enforced chain with
an explicit hash payload (§6.1); `TRAIN_MODEL` leaves the governed-action vocabulary for the rail
projection (§7); trigger authorization is aligned with read authorization, closing a write-IDOR
(§11); migration numbers are re-reserved through the parent's single ledger instead of claiming
1115+ (§13); and the dashboard narrows to what domain stores can honestly show — current state, on
two axes: historical outcome and current eligibility (§6.7, §12). Changes are marked **[R3]**.

---

## 1. The problem, stated in code rather than in prose

**The workflow's continuity currently lives in a URL query string.** `App.tsx:510` hands
`FeatureExecutionScreen` nine separate identity fragments, defaulting to `''`, `null` or a
hardcoded mode. Close the tab and the work is unrecoverable.

**Every stage already has a durable attempt record, and no two of them share a parent.**

| Stage | Store | Migration |
|---|---|---|
| recommendation generation | `feature_generation_run` | 1006 |
| generation input lineage | `contract_generation_input` | 1024 |
| candidate choice | `contract_gate1_choice_revision` | 1025 |
| formula authoring | `formula_draft` · `formula_authoring_run` | 1090 · 1022 |
| selection | `feature_selection_revision` | 1072 |
| build declaration + generation | `build_set_revision` · `generation_request` | 1092 |
| sandbox verification | `verification_request` · `verification_attempt` | 1094 · 1080 |
| sandbox publication | `publication_attempt` | 1081 |
| production materialization | `materialization_request` (legacy lane, outside the spine — §9) | 1053 |

**There is no feature-run list endpoint and no run-list authorization model.** (Collection
endpoints exist — `/contracts`, `/features` — what has never existed is a listing of *runs*.)

**A run/stage precedent works on the ingestion side only** (`ingestion_run` 0994 +
`ingestion_run_stage` 0996), and the feature workflow inherits its shape while rejecting its
bounded-request lifetime.

---

## 2. The invariants

> **A stage may run only when every input it consumes is named by immutable identity. If an exact
> identity is missing, the stage is UNAVAILABLE. The platform never substitutes "latest."**

> **One fact, one authority.** A lifecycle state, a lease, or an identity that exists in two tables
> is two answers. The spine points at domain facts and derives; it never copies them into a second
> mutable column.

> **[R3] A relationship the database cannot refuse is not recorded — it is asserted.** Every link
> between spine rows and domain rows is a composite foreign key that carries the facts both sides
> must agree on. This applies to inputs, subjects, domain attempts, authorizations, decisions, fork
> pins and the identity chain alike. (Revision 2 stated this principle and then violated it five
> times; §6 enumerates the repairs.)

---

## 3. Identity hierarchy

```
Contract intent                          "one retail current-account churn hypothesis"
│   contract_intent (0962)
│
├── Feature run  grun_1042               feature_generation_run (1006)
│   │
│   ├── Invocation                       one user gesture for ONE action [R3 — §4.1]
│   │   ├── Attempt header (subject: candidate 3)  ── links ONE domain attempt
│   │   └── Attempt header (subject: candidate 7)  ── links ONE domain attempt
│   └── Invocation  GENERATE_PREVIEW
│       └── Attempt header (subject: build set B)
│
└── Feature run  grun_1048               fork lineage — child of grun_1042 (§10)
```

**The run id is `feature_generation_run.generation_run_id`** — a bridge, not a promotion:
`generation_run_id` appears zero times in the execution lane (`build_sets.py`,
`feature_execution.py`, `formula_drafts.py`, migration 1092). Ids are opaque; both `grun_` and
`fgr_` prefixes are runs; nothing parses them. **Intent is the dashboard's grouping key** — a run
is minted per recommendation generation (`contract.py:790`), so one intent has many runs, and 7 of
12 live rows have no intent, which is a real "no hypothesis recorded" bucket, never an invented
header.

---

## 4. One lifecycle authority

The domain stores own their lifecycles: `formula_draft.state`, `generation_request.status`,
`verification_request.status`, `publication_attempt.outcome`. The spine's rules:

* The run-side attempt is an **immutable orchestration header**, created once in the trigger
  transaction, never updated, linked by composite FK to **exactly one** authoritative domain
  attempt. Status is **always derived** from the linked domain record — run-side/domain-side
  disagreement is unrepresentable, not validated away.
* **No run-side lease, no run-side fence.** The queue and domain stores own claims and crash
  recovery. The spine introduces no new *lifecycle*; §6.5 states the one durable-projection
  obligation it does carry (output binding), which revision 2's "zero reconcilers" claim glossed.
* There is no header status column, so no partial-unique live-attempt index; double-trigger
  protection is the CAS (§5) + each domain store's own guard + the START guard (§5) — and, where a
  domain attempt is *shared*, attaching is representable (§6.4) rather than prevented.

### 4.1 The child plan's `code_generation_job` is dissolved **[R3 — P0-1]**

> ▲ **SUPERSEDED by Revision 4 (Option B, owner ruling 2026-08-23 — see the final
> section).** The job aggregate was implemented and ratified while this section's premise
> (documentation-only) still held; the owner ruled it STANDS as the domain journey store. The
> text below is retained as the historical design record only.

The authoritative child plan (§3.5, migration 1109) still defines `code_generation_job` with a
mutable `status`, mutable member states, its own `code_generation_job_event`, per-action
`code_generation_job_action.state`, and a preview coordinator progressing
`REQUESTED → AUTHORING → PREVIEW_READY`. Implemented beside this spec, the platform would hold
**four** lifecycle authorities over one act — `code_generation_job.status`, the run's derived
status, `formula_draft.state`, `generation_request.status` — recreating exactly the
multiple-authority problem both documents exist to end.

**The replacement:**

```
feature_run_action_invocation            -- immutable; ONE user gesture, ONE action
    invocation_id            PK
    generation_run_id        FK
    action                                -- ActionV1 vocabulary only (§7)
    idempotency_key · request_content_hash
    requested_by · requested_at
    UNIQUE (generation_run_id, action, idempotency_key)
```

**[R3.1] The invocation groups; it never authorizes.** Authorization and decision are per
*resource*, and one gesture spans many resources — so the §0.1.2 composite group lives on each
attempt **header** (§6.4), minted per subject within the invocation's transaction. A single
authorization column here would be exactly the "one authorization link per job" defect the child's
revision five already corrected.

One click ("prepare formulas for 12 candidates") → **one invocation** → twelve per-subject attempt
headers → each header links one authoritative domain attempt → **invocation status is a read-time
fold, never stored**. This is also what makes the batch gesture one atomic operation instead of
twelve fragile HTTP calls. A journey spanning actions (author *then* preview) composes invocations,
each separately authorized — consistent with the child's own revision-five correction that one
authorization per job was wrong.

**Amendment obligation:** migration 1109 and the child plan's §3.5 must be rewritten to this model
before *either* coordinator is implemented. That is an owner-gated amendment to an authoritative
plan; this spec records the requirement and cannot discharge it.

---

## 5. Coordination state and the trigger contract

```
feature_run_state                        -- the only mutable COORDINATION row per run
    generation_run_id    PK/FK           -- (feature_run_profile is mutable too — display only,
    state_version        bigint NOT NULL --  never consulted by any coordination decision)
```

**[R3.1] Idempotency is consulted BEFORE the CAS.** A client that times out and resends its key
must get its original result back — but its first attempt already incremented `state_version`, so
a CAS-first protocol would answer the retry with a version conflict and the key could never fire
for the one case it exists for. Order: look up `(run, action, idempotency_key)` and return the
stored invocation if present → then `SELECT … FOR UPDATE` → validate `expected_state_version` →
validate prerequisites → write immutable evidence (invocation, headers, inputs, links) →
increment → commit. Attempt numbers are allocated under the lock. Rows are minted lazily on a
run's first spine mutation (foundation mints none), and the mint itself is
`INSERT … ON CONFLICT DO NOTHING` followed by the locked read, so "first mutation" cannot race.

**[R3] START gets a database-level one-time guard.** The CAS stops concurrent stale requests; it
does not stop a later `/start` with a fresh version and a fresh key. So:

```
UNIQUE (generation_run_id, action, stage_subject_id) WHERE attempt_purpose = 'START'
```

Retries and re-executions append; a second START for the same subject is refused by the index, not
by convention.

**Endpoints:**

```
GET   /feature-runs · /feature-runs/{run_id}
GET   /feature-runs/{run_id}/actions/{action}/attempts
GET   /action-attempts/{id} · /action-attempts/{id}/events

POST  /feature-runs/{run_id}/actions/{action}/plan          read-only preflight
POST  /feature-runs/{run_id}/actions/{action}/start
POST  /feature-runs/{run_id}/actions/{action}/retry         -- AUTHOR_FORMULA: Rev-4 gates (final section)
POST  /feature-runs/{run_id}/actions/{action}/re-execute    -- after SUCCEEDED (§8 semantics)
POST  /feature-runs/{run_id}/archive
```

Fork endpoints are deliberately absent until §10's design work lands. There is no generic cancel.

The trigger body carries `{ idempotency_key, expected_state_version, subject }` — **[R3.1] the
subject IS an identity the client names** (an authoring-subject revision, a build-set revision),
and it is the only one: the client supplies no *other* id — no artifact id, draft id, role,
certificate, "latest" selection or upstream output. The distinction is scope, not squeamishness
about ids: naming the subject picks *which* governed thing to act on; everything the act then
consumes is resolved by the server from the run. The server creates or resolves the authorization
for the current user — never accepts somebody else's; the grantee-equality rule `7a2c78b9`
enforced on the legacy table is re-expressed in 1100's `actor_subject` — and records who
triggered. The plan endpoint reports pins and preconditions, never predicted byte
equality and never an invented cost (the spend contract is P0-10, undesigned).

---

## 6. Storage model

Migration numbers: **re-reserved through the parent plan's §17 allocation table — the single
ledger — in an amendment commit, with numbers matching intended apply order. [R3]** Revision 2's
"1115 onward" contradicted the parent's explicit write-order rule: 1113/1114 are *later*
certification work, and the foundation ships before them. The runner would technically tolerate a
lower-numbered file added later (`migrations.py:288` applies every missing file, checksum-ledgered)
— that tolerance is never relied on ACCIDENTALLY. **Overtaken by events (2026-08-23, ruled at
execution):** migrations 1100 and 1101 are now COMMITTED files (`75bdfe26`), so the foundation can
no longer slot before the parent's block; global number-order-equals-apply-order across two
parallel workstreams is unachievable without renumbering the parent's cited-everywhere 1101+. The
foundation therefore takes **1115/1116**, reserved in the §17 table with an explicit ordering note
— a DOCUMENTED interleaving of two independent blocks (the spine's FKs reach only ≤1024 tables),
which honours the owner's actual rule: never an *undocumented* out-of-order convention.

### 6.1 Identity — a composite-FK-enforced chain **[R3 — P0-8]**

Revision 2's identity row carried ids and hashes but proved only the root/fork shape — a row could
combine valid identifiers from unrelated runs and hash the false combination. The chain is now
enforced:

```
feature_run_identity                     -- append-only, write-once trigger
    generation_run_id           PK/FK
    workflow_definition_version          'V1'
    intent_id · confirmed_scope_id · generation_input_content_hash
    considered_revision_id · considered_content_hash
    metadata_snapshot_id · metadata_snapshot_content_hash
    owner_subject NOT NULL · owner_tenant NULL
    root_generation_run_id NOT NULL
    parent_generation_run_id NULL        -- [R3] fork columns DEFERRED: see below
    run_identity_hash
    created_by · created_at

    -- the chain, refused by the database rather than asserted:
    FOREIGN KEY (generation_run_id, intent_id, confirmed_scope_id)
        REFERENCES contract_generation_input (generation_run_id, intent_id, confirmed_scope_id)
    FOREIGN KEY (considered_revision_id, generation_run_id, intent_id)
        REFERENCES contract_considered_revision (considered_revision_id, generation_run_id, intent_id)
    FOREIGN KEY (metadata_snapshot_id, generation_run_id)
        REFERENCES catalog_metadata_snapshot (snapshot_id, generation_run_id)
```

Each composite target is a UNIQUE superset of an existing primary key
(`contract_generation_input` is keyed by `generation_run_id` with `intent_id` and
`confirmed_scope_id` NOT NULL, 1024; `contract_considered_revision` is PK + `UNIQUE (intent_id,
generation_run_id)`, 1021; `catalog_metadata_snapshot` is PK + run FK, 1006) — so every added
index is additive and cannot fail on existing data. All chain columns are NOT NULL, so the MATCH
SIMPLE trap does not disarm them.

**The hash payload is an explicit canonical function [R3]:** `run_identity_hash = jcs_sha256` over
exactly `{workflow_definition_version, generation_run_id, intent_id, confirmed_scope_id,
generation_input_content_hash, considered_revision_id, considered_content_hash,
metadata_snapshot_id, metadata_snapshot_content_hash, owner_subject, owner_tenant,
root_generation_run_id, parent_generation_run_id}` — the hash is **not** among its own inputs
(revision 2's "over exactly the fields above" was self-referential), and provenance timestamps are
excluded.

**The confirmed-scope ruling [R3]:** `generation_input_content_hash` hashes `confirmed_scope_id`,
not the normalized scope children — and the children (`confirmed_scope_use_case`, 0974) remain
insertable after the parent exists (`scope_records.py:422` reads the parent by id). Rather than
seal the scope set, the spine **declares: downstream actions consume only the immutable considered
revision and never dereference the confirmed scope again.** The scope children are pre-freeze
working state; no spine read touches them.

**Root/fork constraints** (each named with its mechanism): root points to itself and `parent IS
NULL ⇔ run = root` (CHECK — same-row, expressible); parent and root are workflow-V1 runs (FK to
`feature_run_identity`); **[R3] `forked_from_attempt_id` and `fork_plan_revision_id` are NOT in the
foundation's identity table at all** — revision 2 gave the foundation FKs to tables it explicitly
does not create. The actionable increment adds both columns (nullable, filled only for new forks —
legitimate on a write-once table because every pre-fork row is genuinely NULL), with
`(forked_from_attempt_id, parent_generation_run_id)` referencing the attempt table's
`(attempt_id, generation_run_id)` and the fork-plan FK carrying one-time consumption (§10).

**Written in the same transaction that completes run creation** (the `contract.py` flow creates
run, input lineage, considered revision and snapshot atomically). Runs whose creation path lacks
the chain get no identity row and render pre-spine. **No identity is fabricated for the twelve
existing runs** — owner re-measured: 12 runs / 12 snapshots / 5 considered revisions / 5
generation inputs / **0 choice revisions** / 7 drafts / 0 of everything downstream; they render
`PRE_SPINE` with only the evidence that exists.

```
feature_run_profile                      -- mutable display metadata, identity-free
    generation_run_id · display_name · description · archived
```

### 6.2 Frozen inputs: subject-consistent, and actually sealed **[R3 — P0-2, P0-3]**

Two revision-2 defects. First, the attempt's FK target was `(input_revision_id, run, action)` —
permitting an attempt whose subject is build set B to cite inputs frozen for build set A, same run,
same action, database content. The subject joins the key:

```
feature_run_action_input                 -- header; its existence IS the seal
    input_revision_id        PK
    generation_run_id · action · stage_subject_id
    member_count             NOT NULL    -- the sealed child-set size, 1006's item_count pattern
    input_content_hash       NOT NULL
    UNIQUE (input_revision_id, generation_run_id, action, stage_subject_id)
```

Second, "immutable" was a word, not a mechanism — nothing stopped a `preview_input_member` being
added after the hash was computed. The sealing follows the pattern **already implemented** for
catalog snapshots (1006): typed children INSERT first; the header INSERTs last in the same
transaction; the child→header FK is `DEFERRABLE INITIALLY DEFERRED` so child-before-parent commits;
write-once triggers refuse child UPDATE/DELETE always, and refuse child INSERT once the header
exists; the header records `member_count`; every loader recomputes `input_content_hash` over the
ordered child set and refuses a mismatch.

```
preview_input_catalog_snapshot
    input_revision_id PK/FK DEFERRABLE · snapshot_id FK · snapshot_content_hash
preview_input_renderer
    input_revision_id PK/FK DEFERRABLE · renderer_version · renderer_build_hash
```

**[R3.1] There is no `preview_input_member`, deliberately.** The preview's *subject* is a
`build_set_revision`, and — once parent 1101 lands — that revision already IS the sealed ordered
member set, binding ids included, inside its own `content_hash`. Re-recording members on the input
revision would be a second authority over the same fact, the §2 violation this spec polices
elsewhere. The input revision pins what the build set does **not**: the snapshot and the renderer.
**[R3.1] The renderer pin is two identity values, not an FK** — revision 3 referenced
`renderer_contract_revision`, a table that exists nowhere; the real renderer identity in this
codebase is `renderer_version` (1079) and `renderer_build_hash` (1091, in
`engine_operator_capability`'s primary key). Values pin; a phantom table does not. **[R3.1] Both
1:1 children carry `input_revision_id` as their PRIMARY KEY** — without it, a "frozen" input could
hold two snapshots.

Attempt headers reference inputs by the full four-column FK. Every retry/re-execution of a subject
references the same input revision; different inputs are a different question → fork.

### 6.3 Typed subjects

`stage_subject_id` is a typed FK per action: `AUTHOR_FORMULA` → `authoring_subject_revision`
(parent 1104); `GENERATE_PREVIEW` → `build_set_revision`; `EXECUTE_SANDBOX` → the sealed artifact.
One child table per action kind carries the typed column; a CHECK binds action ↔ child kind.

### 6.4 Domain links: relationship-level composite FKs **[R3 — P0-2, P0-4]**

Revision 2's links were single-column — a valid `generation_request_id` for build set B could be
attached to a header whose subject says build set A. Every link now carries the relationship:

```
preview_attempt_link
    attempt_id PK/FK
    build_set_revision_id · generation_request_id
    FOREIGN KEY (generation_request_id, build_set_revision_id)
        REFERENCES generation_request (request_id, build_set_revision_id)
    -- + the header's subject FK forces attempt.subject = build_set_revision_id

authoring_attempt_link
    attempt_id PK/FK
    formula_draft_id FK                   -- [R3 — P0-4] the id ONLY.
    -- formula_content_hash is NOT here: at trigger time the draft is REQUESTED and the hash
    -- does not exist; an immutable header cannot carry a value born later. The hash lives on
    -- the attempt OUTPUT (§6.5), written at terminal time.
```

The same rule — a composite FK carrying the facts both sides must agree on, backed by an additive
UNIQUE superset of the parent's PK — applies to verification and publication links.

**[R3.1] The authorization/decision group is spelled, on every durable attempt header, per parent
§0.1.2 (R6).** Revision 3 said "composite FK, §6.4" and never spelled it — the exact
under-specification whose enforced form the in-flight migration 1100 already carries
(`action_authorization_revision_act_key` on `(action, resource_identity_hash, authorization_id)`,
with the six-action CHECK and `resource_identity_hash NOT NULL`). Every header carries, as a
NOT NULL group:

```
    action
    resource_identity_hash               -- derived from the §6.3 typed subject child; 1100's own
                                         -- rule: "a hash cannot carry a foreign key, so the TYPED
                                         -- CHILD TABLE holds the real reference and this is
                                         -- derived from it" (1100:43)
    action_authorization_revision_id     NOT NULL
    action_decision_revision_id          NOT NULL
    FOREIGN KEY (action, resource_identity_hash, action_authorization_revision_id)
        REFERENCES action_authorization_revision (action, resource_identity_hash, authorization_id)
    FOREIGN KEY (action, resource_identity_hash, action_decision_revision_id,
                 action_authorization_revision_id)
        REFERENCES action_decision_revision (...)                    -- parent migration 1106
```

A `GENERATE_PREVIEW` header citing a `PUBLISH_PRODUCTION` authorization on another resource is
thereby unrepresentable — the false audit fact R6 exists to refuse.

**[R3.1] Attaching to a shared domain attempt is gated on input identity.** Where a domain attempt
is shared (a draft is idempotent on `formula_identity_hash` and crosses runs; a generation request
is one-per-build-set+environment), N headers legitimately link one domain row — each header is one
run's *attachment*, recorded as this typed row, never inferred later. But an attachment claims
"this execution answers MY frozen question", so it is lawful only when the header's
`input_content_hash` equals that of the input revision the live domain attempt was created from;
anything else records an output against inputs that were never used — a false input→output fact.
A mismatch refuses with `INPUT_IDENTITY_MISMATCH` → fork. Attachment is its own
`attempt_purpose = ATTACH`, and its attempt number is allocated normally — the number counts this
run's asks, not the world's executions.

### 6.5 Outputs: atomic binding, or honest incompleteness **[R3 — P0-5]**

```
feature_run_attempt_output               -- immutable, one per successful attempt
    attempt_id PK/FK
    <typed output reference per action>  -- for authoring: formula_draft_id · formula_content_hash
    output_content_hash
    compared_to_attempt_id NULL FK · reproduction_status NULL   -- REPRODUCED · DIVERGED

feature_run_canonical_output             -- the pointer; the row never moves
    generation_run_id · action · stage_subject_id · canonical_attempt_id FK
    UNIQUE (generation_run_id, action, stage_subject_id)
```

**Who writes these, and when — revision 2 never said.** The domain worker's terminal transaction
commits `SUCCEEDED` + the artifact; nothing then owned writing the output row, the canonical
pointer, the event, and the `state_version` bump — a crash between the two leaves a derived
SUCCEEDED with no resolvable output for the next stage.

The contract: **the domain terminal transaction writes the attempt output and establishes the
canonical pointer for every attached run attempt, atomically with the terminal state.** Where a
lane cannot make that transactional, it ships an outbox/projector **with a reconciler** — and
either way, a SUCCEEDED domain attempt whose binding is missing surfaces as
`OUTPUT_BINDING_INCOMPLETE` and **does not enable the next stage**. Revision 2's "the spine owes
zero reconcilers" is corrected to: zero *lifecycle* reconcilers — the durable output projection
still needs atomicity or repair, per lane, as that lane's entry condition.

Canonical rules: first accepted successful START/RETRY establishes canonical; a re-execution never
silently replaces it; its own output row records its own verdict permanently; adopting a diverged
output is a fork.

### 6.6 Events — and what a timeline can honestly show **[R3 — P1]**

```
feature_run_action_event                 -- append-only; the coordinator's stream only
    attempt_id · event_sequence · event_kind · actor_subject
    detail jsonb                         -- counts and codes; never formula bodies, provider
                                         -- payloads, credentials, row data
    recorded_at
    UNIQUE (attempt_id, event_sequence)
```

`event_sequence` orders; `recorded_at` does not. **The GitHub-Actions-style
queued/claimed/running/completed timeline is not derivable today for most lanes** —
`generation_request`, `formula_draft` and `verification_request` overwrite current status and keep
no transition events. The foundation UI therefore shows **current state**: requested time, current
status, last-updated, and authoring-trace details where they exist. Append-only domain transition
events are a **lane-entry requirement** for any lane that wants a timeline — not a spine table
that fakes one.

### 6.7 Historical outcome vs current eligibility **[R3 — P1]**

Two axes, never one field. A formula that succeeded and was later retired shows *Attempt outcome:
Succeeded* and *Current usability: Withdrawn (reason, replacement)*. Rewriting the old attempt to
BLOCKED destroys history; ignoring retirement lets an unusable output look current. Outcome is
immutable evidence; eligibility is derived at read time from retirement/supersession stores.

---

## 7. Vocabulary: six governed actions, and a rail that shows more **[R3 — P0-10]**

> ▲ Revision 4 corrects three socket reason codes below — see the final section.

**`ActionV1` — the executable-action and authorization vocabulary — is exactly the parent plan's
six:** `AUTHOR_FORMULA · GENERATE_PREVIEW · EXECUTE_SANDBOX · PUBLISH_SANDBOX ·
MATERIALIZE_PRODUCTION · PUBLISH_PRODUCTION`. Revision 2 put `TRAIN_MODEL` in the same vocabulary
whose attempts carry an authorization and decision — but the authorization tables cannot authorize
an action `ActionV1` does not contain. Corrected:

* **Workflow rail V1** = the six governed actions **plus** the `TRAIN_MODEL` capability socket and
  the two milestones.
* `TRAIN_MODEL` appears in the projection as `UNAVAILABLE`, and **no attempt row, invocation, or
  authorization for it can exist** until an explicit `ActionV2` migration introduces it.

Milestones stay evidence-derived: `CHOOSE_CANDIDATES` (`contract_gate1_choice_revision`, 1025 —
run-scoped already, **zero live rows**) and `BIND_SELECTIONS` (`feature_selection_revision` + the
1101 binding). Authoring's subject is a candidate (parent §0.1.4); selection binds afterwards.

**Sockets — five, honestly labelled:** `EXECUTE_SANDBOX` (no worker claims
`verification_request`, parent §9.0), `PUBLISH_SANDBOX` (`settle_attempt` has only test callers;
the route at `feature_execution.py:424` promises a worker that does not exist),
`MATERIALIZE_PRODUCTION` / `PUBLISH_PRODUCTION` (their new-workflow state machines, parent
1111/1112, are unbuilt — and production stays unavailable under §0.1.0 regardless), `TRAIN_MODEL`.
All render `UNAVAILABLE` with a reason code; triggering returns a controlled 409, creating no
queue work. *Not started* is reserved for stages that could actually run.

Projection vocabulary (rail, not rows): `NOT_STARTED · UNAVAILABLE · WAITING_FOR_USER ·
IN_PROGRESS · SUCCEEDED · BLOCKED · FAILED · CANCELLED · UNKNOWN · NOT_APPLICABLE ·
OUTPUT_BINDING_INCOMPLETE (§6.5)` — with 1090's `BLOCKED`-vs-`FAILED` distinction and `UNKNOWN`
for unreconciled external effects.

**[R3.1] Availability is DERIVED, never a static list.** "Five sockets" describes today's
deployment, not a constant: availability folds the deployment switches (the whole
`/feature-execution` and build-set surface 404s while `FEATUREGEN_MATERIALIZE_ENABLED` /
`FEATUREGEN_GENERATION_V2_ENABLED` are off), proven capability, and pin state. In particular,
**during the foundation `GENERATE_PREVIEW` itself renders `UNAVAILABLE` with
`BUILD_SET_DECLARATION_WITHHELD_PRE_PIN`** — the same reason code its §13 backend refusal returns
— because a stage whose declaration endpoint is refused is unavailable, and showing it "ready"
while its only entrance 409s would be the false rail this section exists to prevent.

**[R3.1] The domain→rail mapping must be TOTAL, per lane, written in the implementation plan** —
every value of `formula_draft.state`, `generation_request.status`, `verification_request.status`
and `publication_attempt.outcome` maps to exactly one rail value, proved exhaustive by test
against the CHECK constraints (the `ACTIVATION_BLOCKER_DISPOSITIONS` pattern). Two rulings taken
now: `generation_request.REFUSED` maps to `BLOCKED` (1092 calls REFUSED "a product result", 1090
draws the same line); and `WAITING_FOR_USER` derives only from milestone prerequisites — no
domain store carries a state that means it.

---

## 8. Re-execution is stage-specific — and authoring has no retry **[R3 — P0-6]**

> ▲ **The no-retry rows below are SUPERSEDED** — the Reconciliation addendum blessed
> retry-after-terminal under two gates, and Revision 4 (final section) carries the operative text.

| Action | The truthful act |
|---|---|
| `AUTHOR_FORMULA` | **Reuse existing formula** — a READ, not an attempt. `re-execute` here would mint a header that returns the cache; the spec forbids it |
| `AUTHOR_FORMULA` | **Request another opinion** — separately priced and authorized, and a fork; deferred with the spend contract and the constant config hash |
| `AUTHOR_FORMULA` | **Retry after failure — NOT OFFERED.** See below |
| `GENERATE_PREVIEW` | re-render the exact pinned inputs; compare canonically after rendering |
| `EXECUTE_SANDBOX` | run the exact sealed artifact again (once the lane exists) |

**Why authoring retry is withdrawn:** `formula_draft_identity` is globally UNIQUE on
`formula_identity_hash` (1090), and `request_draft` returns the existing row for that identity
**whatever its state**, with `created=False` — and the route enqueues only when a row was created
(`formula_draft_store.py:306`). A FAILED draft is a terminal cache entry: press retry → same
identity → same failed row → no queue message → nothing retries. Exposing `/retry` for
`AUTHOR_FORMULA` would be a button wired to a no-op. The real fix is a **formula-authoring attempt
identity beneath the draft/result aggregate** — a retry links a new attempt to the same draft
subject and consumes an explicit spend authorization. Until that domain model exists, the endpoint
is absent for this action, and the parent plan's failure-cache acknowledgment gains its concrete
consumer.

**Preview reproduction hash — the contract revision 2 left open [R3 — P1]:** the comparison is
over a **canonical semantic artifact hash** — the generated source / project-manifest identity,
excluding run ids, timestamps, packaging order and provenance — so a re-render that changes
archive bytes without changing the calculation reports `REPRODUCED`. The package byte hash is
retained separately on the output row. Defining the canonical function is part of the
`GENERATE_PREVIEW` lane's entry into re-execution, and no byte-equality is promised before
rendering.

Two labels, never one: *"Re-run — this run's frozen inputs"* vs *"Fork — a new run from an
approved fork plan"*.

---

## 9. Lineage: what the bridge proves, and what it does not

* Authoring and selection bridge to the run through the considered revision — but with
  **same-intent composite lineage**, not bare FKs (a bare FK still lets a selection combine a
  target reading from intent A with a considered revision from intent B). Drafts bind through
  `authoring_subject_revision` (parent 1104).
* The build lane bridges through `build_set_member → selection_formula_binding` (parent 1101).
* `publication_attempt` (1081) carries bare text ids with no FKs to the verified output or sealed
  artifact — its FKs must be added, or the table rebuilt, **after measuring its live rows** (it
  was not in the owner's count set; nothing here may assume it is empty).
* **Legacy `materialization_request` (1053) is outside the spine, permanently.** No spine surface
  renders it as a production-stage parent.

---

## 10. Fork: typed pins, one-time consumption, and an unsolved adoption problem **[R3 — P0-7]**

Revision 2's fork plan stored `retained_pins jsonb` / `replaced_pins jsonb` — repeating the exact
defect relational inputs were built to fix. Corrected shape:

```
feature_run_fork_plan_revision           -- header; sealed on the §6.2 pattern
    fork_plan_revision_id PK
    parent_generation_run_id FK · parent_state_version
    plan_content_hash · pin_count
    created_by · created_at

fork_plan_retained_pin                   -- typed children, write-once
    fork_plan_revision_id FK DEFERRABLE · position
    pin_kind                              -- choice · binding · snapshot · canonical_output …
    <typed composite FK per kind into the parent run's evidence>
fork_plan_replaced_pin
    fork_plan_revision_id FK DEFERRABLE · position
    pin_kind · <old typed ref> · <new typed ref>
```

One-time consumption: the child's identity row references the plan with a UNIQUE FK — one plan,
one child, ever. `POST /forks` consumes exactly one sealed plan and refuses if the parent's
`state_version` moved since preflight.

**The deeper problem revision 2 missed — and the reason fork stays NO-GO until designed:** the
records a fork must reuse are **run-scoped**. `contract_considered_revision` is
`UNIQUE (intent_id, generation_run_id)`; `contract_gate1_choice_revision` requires its considered
revision to belong to the same run; the snapshot is run-keyed. A child cannot simply point at the
parent's rows — and *duplicating* the considered revision changes `considered_revision_id`, which
sits **inside formula identity** (1090), so the "unchanged" formulas would be re-bought from the
provider. The fork design therefore needs an explicit **cross-run adoption record** for retained
choices, formulas and outputs — the child references the parent's evidence *as the parent's*,
with an `INHERITED` provenance (an attempt purpose, or inherited canonical outputs modelled
without claiming the child executed them) — and the run-scoped-revision vs
reusable-content-identity conflict must be resolved before any fork endpoint exists. Fork is
absent from the foundation **and** from the first actionable increment.

---

## 11. Authorization: trigger and read must agree **[R3 — P0-11]**

Revision 2 let any authenticated development user trigger while only the owner could read — a
write-IDOR: triggering work on a run the caller is forbidden to inspect. Aligned:

| Caller | May |
|---|---|
| run owner | read and trigger their run's implemented, non-production actions |
| `platform_admin` | read and trigger any run |
| other authenticated developer | neither, for that run |

**[R3.1] Pre-spine runs need their own ownership rule, or the foundation ships an empty
dashboard.** All twelve live runs are `PRE_SPINE` — none has an identity row, so a policy keyed
only on `feature_run_identity.owner_subject` renders the foundation's entire current content
invisible to everyone but `platform_admin`. The rule: a workflow-V1 run's owner is
`feature_run_identity.owner_subject` (immutable); a pre-spine run's owner is the `subject` inside
`feature_generation_run.actor` (the `identity_to_jsonb` envelope both writers store), read-only
and labelled as mutable-row-derived; a pre-spine run whose actor carries no subject is visible to
`platform_admin` only.

§0.1.0's ruling ("any authenticated development user may trigger any implemented non-production
stage") is honoured *within* object-level scope — its safeguards (server-side roles, recorded
actor, no borrowed authorizations) all stand. If the owner later rules that every developer may
trigger every run, the **read** policy broadens with it, consistently. It must never be possible
to trigger what one cannot read.

**The stub-identity detail [R3]:** development header-stub identities deliberately carry
`authenticated=False` (`deps.py:95`: "X-User/X-Roles → authenticated=False", off by default). A
literal `identity.authenticated` check would disable the entire development UI. The policy keys on
*a configured development stub* versus *a verified principal* — explicitly, in one place.

Authorization applies inside the query, before pagination and counts. Six RBAC role bundles exist
(`permissions.py`); `IdentityEnvelope.tenant` (`envelopes.py:26`) is captured into identity as
`owner_tenant`; the tenant-authority model stays deferred.

---

## 12. Dashboard

Grouped by hypothesis; a real "no hypothesis recorded" bucket; opaque truncated ids, never
re-numbered; `feature_run_profile` names, `—` until set; `PRE_SPINE` rows show only genuine
evidence. Route `#/runs/{run_id}` replaces the nine query-string fragments.

The folds, each with its honest source: per-member authoring progress (headers by subject, status
derived per linked draft); method mix (today every formula is LLM-authored — parent §0.2 fact 3 —
and the column says so); member counts **only from a sealed ordered set** (the build-set revision,
which post-1101 is that set — §6.2), otherwise "N candidates chosen (accumulating)"; overall status as a worst-of
fold with `BLOCKED` outranking `IN_PROGRESS`; **[R3] two axes everywhere an output can age —
outcome and current eligibility (§6.7)**; keyset pagination on a stable cursor; **[R3] a
current-state view, not a fabricated timeline (§6.6)**.

---

## 13. Increments and their gates

> ▲ **The actionable gates below are SUPERSEDED by Revision 4's re-scope (final section)** —
> the invocation-rewrite gate died with Option B, and the substrate closed most others.

### Foundation — read-only projection: **conditional GO**, conditions folded

* `feature_run_identity` **without fork columns** (§6.1) · `feature_run_profile` ·
  `feature_run_state` (table only; no rows minted) · the §9 lineage FKs
* Run list + detail at `#/runs/{run_id}`, projected from existing stores; `PRE_SPINE` for the
  twelve; **current-state** rendering only (§6.6); two-axis output display (§6.7)
* §11's aligned read policy — gates the endpoints' existence
* All five sockets `UNAVAILABLE`; `TRAIN_MODEL` as rail socket, not action (§7)
* Backend refusal on `POST /build-sets` until parent 1101 lands (`build_sets.py:80` registers the
  route whenever the V2 switch is on; one caller destroys 1101's zero-row no-backfill branch)
* Migration numbers assigned via the parent §17 amendment (§6 preamble), **before** any 1100+
  file is applied live
* No invocations, no headers, no triggers, no re-run, no fork, no cancel

The foundation does **not** wait for the rest of parent 2A. It coordinates with the in-flight
authorization work only where both touch `build_sets.py`.

### Actionable — **NO-GO** until, in order:

1. The parent §17 amendment (numbers) and the **child-plan rewrite of §3.5 / migration 1109 to the
   invocation model** (§4.1) — both owner-gated plan amendments;
2. Parent migrations 1100 · 1101 · 1104 · 1105 · 1106 (authorization, binding, authoring subject,
   spend, decisions);
3. The composite-FK web of §6.2–§6.4 and the sealing pattern;
4. The atomic output-binding contract per exposed lane (§6.5);
5. Then: invocations + headers for `AUTHOR_FORMULA` (start/reuse only — no retry, §8) and
   `GENERATE_PREVIEW`; existing direct routes become adapters onto the coordinator or are deleted
   with their lane (D2), each enumerated with a disposition in the implementation plan.

### Explicitly deferred beyond that

Formula-authoring attempt identity (unlocks retry and priced second opinions) · fork (needs §10's
adoption design) · `EXECUTE_SANDBOX` (§9.0's eight responsibilities as one shipped unit) ·
`PUBLISH_SANDBOX` (something must settle it) · production (1111/1112 + certificates + §0.1.0) ·
`TRAIN_MODEL` (`ActionV2` first) · per-action cancellation · domain transition-event tables
(timeline entry) · the canonical reproduction hash function.

---

## 14. Deferred decisions

| Decision | Why deferred |
|---|---|
| tenant-authority model | `owner_tenant` captured; enforcement has no substrate |
| governed cost estimator | spend contract P0-10 undesigned |
| "Request another opinion" | needs an identity-moving config hash + spend + fork |
| run retention / auto-archive | a run per recommendation generation accumulates; owner policy |
| pre-spine adoption | only with genuine lineage; owner decision |
| cross-run adoption model for forks | §10 — blocks fork entirely |
| every-dev-triggers-every-run broadening | owner call; read broadens with it if taken |
| `ActionV2` (TRAIN_MODEL) | when the subsystem is chartered |

---

## 15. Facts this design rests on

All opened at the code baseline; ® = verified in the revision-2 review or re-verified for R3.

| Fact | Where |
|---|---|
| nine query-string fragments | `App.tsx:510` |
| no `generation_run_id` in the execution lane | `build_sets.py` · `feature_execution.py` · `formula_drafts.py` · 1092 — zero |
| run minted per recommendation generation · `fgr_` second prefix | `contract.py:790` · `gate1.py:733` |
| many runs per intent | `1021:17` |
| ® child plan's mutable job lifecycle | child §3.5 (migration 1109): `code_generation_job` status, member states, events, per-action state |
| ~~a FAILED draft cannot re-enqueue~~ **SUPERSEDED by 1107 + R4.2**: the money-guard index covers only answers (`WHERE state NOT IN ('FAILED','CANCELLED')`); retry is governed per R4.2 (LLM: exception+spend; deterministic: free, Option 2 ruling) | `1107_money_guard_covers_only_answers.sql` |
| ® scope children mutable after the parent, id-only hash | `scope_records.py:422` · `confirmed_scope_use_case` (0974, ON DELETE CASCADE) |
| ® the runner applies any missing file, checksum-ledgered | `migrations.py:288` |
| ® dev stubs carry `authenticated=False` | `deps.py:95` |
| ® `contract_generation_input` chain target | 1024: PK `generation_run_id`, NOT NULL `intent_id` + `confirmed_scope_id` |
| ® the sealing pattern to copy | 1006: DEFERRABLE INITIALLY DEFERRED item FK, items-first, write-once triggers, `item_count` |
| fork cannot mint new selections · build-set identity excludes the formula · latest-wins resolve | `1072:97` · `1092:56,66` · `restore_formula_v3.py:90` |
| authoring idempotent on identity; ~~config hash a constant~~ **the constant-hash function was DELETED** — identity V2 is composed once in `formula_draft_service.py` (`{identity_version, formula_strategy, strategy_identity_hash, provider_contract_hash iff LLM}`) | 1090 · 1103/1109 companions |
| authoring subject is a candidate | parent §0.1.4 / §0.1.1 |
| ~~sandbox lane absent~~ **the §9.0 verification worker EXISTS** (executor seam awaits step 0b — posture-named FAILED, never a fake pass); publication read path repaired at `2a03a77b`; the PUBLISH_SANDBOX worker remains absent | `verification_lane.py` · 1110 |
| `publication_attempt` bare text ids | `1081:32` — live row count **unmeasured** |
| six role bundles · tenant on the envelope | `permissions.py` · `envelopes.py:26` |
| migrations 1100–1114 reserved; 1113/1114 = compiler evaluation | parent §17 allocation table (line numbers shift with the plan's in-flight edits — cite the section) |
| ® 1100's composite shape: six-action CHECK, `resource_identity_hash NOT NULL`, `act_key` unique index, typed-child rule | in-flight `1100_action_authorization_revision.sql:43,72-78` |
| ® `renderer_contract_revision` exists nowhere; real renderer identity = `renderer_version` (1079) + `renderer_build_hash` (1091 PK) | grep + `1091:77` |
| §0.1.0 development ruling · server-owned authorization landed | parent plan · `7a2c78b9` |
| `POST /build-sets` reachable whenever the V2 switch is on | `build_sets.py:80` |
| ® owner live re-measurement 2026-08-23 | ledger 195 → 1099 · runs 12 · snapshots 12 · considered 5 · generation inputs 5 · **choices 0** · drafts 7 · selections/build sets/generation requests/verifications 0 · action-authorization table not deployed |
| run/stage precedent | 0994 · 0996 |

---

## ▲ Reconciliation addendum, 2026-08-23 — from the substrate session, at the frozen remediation SHA

Written by the substrate remediation session as part of the owner-required three-document
reconciliation; the run-spine session owns this spec and the revised Stage I plan.

### The §5/§8 retry-withdrawal premise is now FALSE — by design, not by drift

This spec withdrew `AUTHOR_FORMULA /retry` on the premise that *"a FAILED draft is a terminal cache
entry; retry is a button wired to a no-op."* That was true, and migration **1107** made it false
deliberately: the money-guard index now covers only ANSWERS (`WHERE state NOT IN
('FAILED','CANCELLED')`), because a failed draft bought nothing and must not hold the identity slot
for ever.

**Retry-after-terminal is hereby BLESSED as a revised-Stage-I candidate act, under exactly these
gates — neither is optional:**

1. a **regeneration exception** bound to the EXACT formula identity being re-attempted (plus
   provider contract, strategy, actor, expiry, one-time consumption — 1103), which is what bounds
   the number of re-attempts; and
2. a **spend authorization** (1105), enforced per PHYSICAL provider call at `AuditingClient.call`,
   which is what bounds the money.

A retry button is therefore never a free re-spend and never a no-op: it is an approved,
cost-confirmed act whose refusals are typed (`DraftRetired` / `DraftNotAnAnswer` /
`SpendExhausted`). The failed draft's row survives as history — with multiple drafts per identity
now possible, any run-detail fold must separate ATTEMPT HISTORY from the current per-subject result
(the owner's P1; the run-spine session owns that fix in `runs/projection.py`).

### Schema facts a revised Stage I may rely on, as of this reconciliation

* `formula_draft_authoring_plan` (1104) is WRITTEN on every new draft and read by the worker —
  strategy is durable, never recomputed.
* Draft identity is **V2** (`{identity_version, formula_strategy, strategy_identity_hash,
  provider_contract_hash iff LLM}`); every pre-V2 draft carries an explicit V1 companion (1109)
  recording the constant era as the defect it was.
* `generation_request.action_decision_revision_id` (1108) is NULLABLE in the expand phase and
  ratchets to NOT NULL in 1100b; the worker refuses a missing decision at act time regardless.
* Migration numbering: substrate holds 1100–1114 (1107/1108/1109 now taken); run-spine holds
  1115–1117.


---

## Revision 4 — the Option B ruling and the Stage I re-scope (owner, 2026-08-23)

Ruled at frozen SHA `e5c4f581` after three verified interface maps. This section is OPERATIVE and
supersedes §4.1 wholly, §8's authoring rows, §13's actionable gates, and three §7 socket labels.

### R4.1 The job aggregate STANDS — Option B

`code_generation_job` (+`_member`, `_event`, `_action`; migration 1111, store, coordinator, route,
worker tick, frontend screen) was implemented under the child plan's authority while this spec's
dissolution obligation sat unexecuted, and the reconciliation ratified it. The owner ruled:

> **The job is a DOMAIN JOURNEY AGGREGATE — the same standing as `formula_draft` and
> `generation_request`. The spine derives from it, links to it, and triggers through it. Nothing
> named `feature_run_action_invocation` is ever built.**

The §2/§4 principles survive intact and are RE-AFFIRMED against the job: one lifecycle authority
per act (the job is now the only coordinator — the invocation was never built, so there are not
two); status derived at read time wherever the spine renders it; immutable evidence linked, never
copied. §4's "no spine lease, no spine reconciler" holds unchanged. `feature_run_state` remains a
shipped, writerless table — reserved for future spine-owned mutations; the job store owns the
journey's idempotency and lifecycle.

The run→job bridge needs no new identity: `code_generation_job` carries `considered_revision_id`,
and `contract_considered_revision.generation_run_id` is NOT NULL — the same §9 bridge every other
domain store uses.

### R4.2 Retry-after-terminal — the operative §8 rows

| Action | The truthful act |
|---|---|
| `AUTHOR_FORMULA` | **Reuse existing formula** — a READ; never an attempt |
| `AUTHOR_FORMULA` | **Retry after FAILED/CANCELLED — OFFERED, and the gates are LANE-AWARE** (owner ruling 2026-08-23, Option 2, stated sixteen lines below and shipped). **LLM lane:** a regeneration exception bound to the exact formula identity (1103; one-time consumption; carries its own NOT NULL spend authorization) AND spend enforced per physical call at the audited seam (1105) — the store gates on `provider_contract_hash is not None`. **Deterministic lane:** free by construction — no provider contract is folded, no call is dispatched, nothing is spent, so no exception exists to require (1103's NOT NULL columns make one unrepresentable). **Both lanes:** a covering tombstone refuses, because withdrawal is a decision rather than a cost. Typed refusals: `DraftRetired` / `DraftNotAnAnswer` / `SpendExhausted` / `DraftCeilingExhausted` (the request seam's pre-consumption check that an approved ceiling can cover ONE per-call worst-case reservation → 409 `COST_AUTHORIZATION_EXHAUSTED`) |
| `AUTHOR_FORMULA` | "Request another opinion" on a LIVE draft — still deferred (identity must move; a live draft's slot is held) |

Three unreachability gaps stood between the blessing and a working button. (1) CLOSED (Task 6,
substrate chain accepted at `b35d3249` after a five-round adversarial loop): the
exception-creation act exists — POST regeneration-exceptions behind `governance:confirm`. Its
governing law, which replaced three rounds of precedence patches: **every covering withdrawal
must be individually NAMED by a valid, then consumed, coupon — at request AND at advance**; the
writer binds the FULL covering set in one approval act (one coupon per withdrawal, one shared
spend ceiling) under the same scope lock the mint and the withdrawal hold; the coupon identity
folds a per-exact-binding regeneration ordinal, so a post-exhaustion re-approval mints a fresh
generation while replays of a live approval converge on it. (2) CLOSED (same chain):
`DraftNotAnAnswer` is translated to `NotAnAnswerAtRequest` in the service and caught by BOTH
callers — the route answers a typed 409, the coordinator blocks the one member by name
(`FORMULA_DRAFT_NOT_AN_ANSWER`), never the whole job; (3) the
deterministic lane cannot be covered by an exception at all (1103's `provider_contract_hash` NOT
NULL + 1105's spend NOT NULL make it unrepresentable) — a FAILED reviewed-lane draft is
permanently stuck at its identity. (3) RULED (owner,
2026-08-23, Option 2): **deterministic-lane retries are free by construction.** The exception
mechanism gates SPEND — its own wording is "re-authoring spends again" — and the reviewed lane
provably spends nothing (1104's CHECK forbids a provider contract on REVIEWED; 1118's
certification programme asserts zero provider dispatch by CHECK). A FAILED/CANCELLED
reviewed-lane draft may therefore be re-requested WITHOUT an exception; worker-time needs no
approval. Tombstones (deliberate withdrawal) still refuse BOTH lanes — governance keeps its teeth
exactly where a human decided something. The honest concession, recorded: "every retry is an
approved act" narrows to "every retry THAT SPENDS is an approved act." The regeneration-exception
surface (gap 1) is accordingly LLM-lane-only.

### R4.3 Socket corrections (§7)

Availability stays DERIVED; three stored reason codes are now false and must derive instead:

| Stage | Truth at `e5c4f581` |
|---|---|
| `EXECUTE_SANDBOX` | worker EXISTS (§9.0 lane); still honestly UNAVAILABLE, derived from TWO INDEPENDENT SWITCHES surface-first (`FEATUREGEN_MATERIALIZE_ENABLED` gates the whole route surface at the router; `FEATUREGEN_VERIFICATION_V2_ENABLED` gates the lane) — shipped as `MATERIALIZATION_DISABLED` then `VERIFICATION_DISABLED` then `NOT_STARTED` (Task 1, deviation upheld). `_EXECUTOR is None` is deliberately unread: its absence surfaces as a posture-named FAILED attempt, not unavailability. Never `WORKER_NOT_IMPLEMENTED` |
| `MATERIALIZE_PRODUCTION` / `PUBLISH_PRODUCTION` | state machines BUILT (1113/1114) behind `action_available()` — the true reason is `ACTION_UNAVAILABLE` under §0.1.0, derivable |
| `PUBLISH_SANDBOX` | still genuinely `WORKER_NOT_IMPLEMENTED` — and its READ path repair (the substrate's `2a03a77b`) is the lane's own affair |
| `TRAIN_MODEL` | unchanged — `SUBSYSTEM_NOT_BUILT` |

### R4.4 The Stage I re-scope — what the revised plan builds

`AUTHOR_FORMULA` is the one action with no authorization, no decision, and no evidence assembler
anywhere (the coordinator records `PERFORMED` with NULLs, deliberately deferring per-candidate
governance to this increment). Stage I therefore is:

1. **Projection honesty** (run-spine files only): derived socket reasons per R4.3; the
   history-vs-current split (1107 makes multiple drafts per identity real — attempt history and
   the current per-subject result are two readings, §6.7's axes applied to drafts); plus the
   still-parked BIND_SELECTIONS accumulating count and the switch-precedence test.
2. **The run→job trigger bridge**: the run detail's start gesture mints/attaches a
   code-generation job scoped to the run's candidates, through the existing coordinator — the
   server resolves everything from the run; the §11 read/trigger policy applies; PRE_SPINE runs
   are not actionable (`PRE_SPINE_NOT_ACTIONABLE`).
3. **AUTHOR_FORMULA governance closed at the service seam** (`request_draft_for_candidate`):
   per-draft `authorize_action` + `decide` (an authoring evidence-pin assembler must be built —
   none exists) + the spend authorization already threaded on the job path made MANDATORY there,
   and the ungoverned direct route (`formula_drafts.py` — parent §0.1.3's named bypass, still
   open, still ceiling-less) becomes an adapter or dies. Ownership of these files is coordinated
   with the substrate session before execution.
4. **The retry chain** per R4.2's three gaps, including the owner ruling request for (3).
5. **Corrections carried**: `§15`'s two stale fact rows; migration numbering (1117 free; 1118
   taken by the compiler-certification programme and 1119 taken by
   `formula_draft_authoring_decision`, shipped by Stage I Task 5 — Stage I expected ZERO new
   migrations and shipped one; Stage II re-reserves from 1120).

Everything else in the frozen NO-GO plan (`2026-08-23-run-spine-actionable-stage1.md`) that
described invocation tables, CAS-minted headers, or migration 1117 content is dead; that document
remains banner-frozen as the historical record.
