# Parent/Child Feature-Generation Production-Readiness Remediation

> **Status:** proposed; implementation is **NO-GO** until Task 0 is approved.
>
> **Authority:** this document amends the implementation sequence and completion criteria of:
>
> - `2026-08-22-four-stage-gating-and-production-certification.md` (parent), and
> - `2026-08-22-recipe-to-code-llm-fallback.md` (child).
>
> The parent still owns the six actions, blocker dispositions, certification and production
> boundary. The child still owns formula-method routing and recipe-to-code coordination. This plan
> owns the repairs required for those responsibilities to be true in running code.

> ▲ **SUPERSESSION RECORD — 2026-08-24 (Task P1 of the serving plan; owner-ruled R8 "narrowed
> supersession", recorded here and in the parent program's amended Stage-2 gate).**
> The cross-catalog serving plan — `2026-08-24-cross-catalog-serving-first-card.md` (Rev 13) —
> supersedes parts of this plan as follows; everything not named below stands unchanged:
>
> 1. **ABSORBED by the serving plan:** this plan's per-action fact-loader requirement (D2's
>    server-owned facts) — now the serving plan's R1 (one authority, six typed fact loaders) and
>    its A1 fact-loader framework — and the plan-envelope hash columns of Task 8's 1124 design
>    (catalog-snapshot / planning-request / binding-plan hashes on the frozen-plan row), together
>    with the EXISTING 1072 `selection_formula_binding` hash columns, which the serving plan
>    preserves as provenance pins; these land in its own identity-persistence and binding-chain
>    migrations (its T0-mapping rows 1134/1135), never in 1122/1124. Task 5's absorption is the
>    lineage-table overlap: its 1122 lineage design (target/selection/formula composite keys)
>    overlaps the serving plan's binding-chain migration (row 1135) — Task 5's written design
>    specifies no hash columns.
> 2. **OWNED by the serving plan's B0a/B0b (R8):** the preserved work of Task 4 (server-derived
>    principal/data scope — roles closed on both routes), Task 5 (target lineage), and Task 8
>    (frozen method identity). Those tasks are not re-executed here; their acceptance intent is
>    discharged by B0a/B0b under the serving plan's rigor.
> 3. **Reservation 1121 is DEAD.** The number was applied LIVE as
>    `1121_governed_telemetry_outbox.sql` (cross-catalog Stage 1B; cluster head verified 1121 on
>    2026-08-24). Task 2's governed-action-execution-binding design keeps its content but must take
>    a new number from this plan's remaining block at write time.
> 4. **Reservations 1122–1129 STAND** for this plan's OTHER tasks (the non-absorbed remainder of
>    the §3 table), subject to the registry re-check each migration task already requires.
> 5. **The authoritative 1130–1139 table is the serving plan's §T0-assigned migration mapping** —
>    numbers for the serving program live ONLY there; this plan allocates nothing above 1129.

**Goal:** A person can select recipe- or LLM-origin features, author the correct formulas, generate
inspectable Kedro/PySpark code, execute and publish it in a sandbox, and—only with current
method-specific certification—materialize and publish it in production. Every act must be bound to
one server-issued authorization and one allowed decision over the exact immutable inputs the worker
uses.

**Baseline reviewed:** `feature/asset-detail-reapply` at `09e8eca7` plus the uncommitted
retirement/config-composition changes present on 2026-08-23. The review ran 190 targeted backend
tests, 14 migration/ledger tests, 39 frontend tests and frontend typecheck successfully. Green tests
do not discharge the negative cases added below.

**Why this plan exists:** the implementation contains much of the intended substrate, but currently
has four dangerous shapes:

1. a persisted refusal can be attached to work and the worker still executes it;
2. identities that are checked together in Python are not always bound together relationally;
3. request facts are accepted or displayed and then discarded before execution; and
4. several routes say a worker will act although no worker can complete the lifecycle.

The programme is therefore a repair and completion programme, not a general refactor.

---

## 1. Non-negotiable product and architecture decisions

These are settled for this remediation. An implementation task must not reopen them locally.

### D1 — Six acts, never four and never one merged “materialization” act

The canonical vocabulary remains:

```text
AUTHOR_FORMULA
GENERATE_PREVIEW
EXECUTE_SANDBOX
PUBLISH_SANDBOX
MATERIALIZE_PRODUCTION
PUBLISH_PRODUCTION
```

Each act gets its own authorization, decision, immutable input identity, attempt lifecycle and
result. Verification is not publication; preview rendering is not execution; materialization is not
publication.

### D2 — One decision service; facts are server-owned

`ask`, `decide` and worker-time `recheck` are three modes of one implementation. Callers supply an
action subject, not blocker codes, readiness, formula method, certificate status, roles or policy
facts. Per-action fact loaders read those from durable server sources.

### D3 — A refusal is unexecutable, in schema and code

An action attempt can bind only an `allowed = true` decision for the same action, resource and
authorization. The worker independently checks all five facts immediately before external work:

```text
expected action
expected resource identity
expected authorization
allowed == true
evidence pins unchanged
```

Missing, mismatched, refused or drifted evidence terminalizes the domain request as `REFUSED`; it is
never retried as an outage.

### D4 — Development permission is broad; data scope is not client-constructed

Any authenticated development user may trigger an implemented non-production action. The server
still derives the actor, tenant and effective read/use roles from `IdentityEnvelope`. Request bodies
have no `roles` field. Broad trigger permission must never widen column read authority.

### D5 — Selection, formula and target are one relational lineage

A build member is valid only when all of these agree:

```text
target reading
selection revision
considered revision and option
planning/binding plan hashes
formula draft and content hash
```

Agreement is expressed with composite foreign keys, not only service checks.

### D6 — Formula strategy is chosen once per confirmed act

The plan endpoint may calculate a proposed route. The confirming write re-reads current facts,
compares the submitted plan hash, and persists an immutable authoring-plan revision. Workers consume
that revision. They never call the route selector again. A changed registry/review/override produces
`PLAN_STALE_REPLAN`; it never silently changes deterministic work into an LLM purchase.

### D7 — Reviewed means durable, current multi-person evidence

Static Python registry membership points to fixtures; it is not the review decision. A reviewed
recipe blueprint is eligible for deterministic authoring only when current approved
`recipe_review_event` evidence covers the exact recipe revision and blueprint/case hash for every
required role.

### D8 — Every physical LLM call is unique, reserved and auditable before egress

A duplicate `(logical_call_ref, physical_attempt_no)` never calls the provider again. A spend
reservation has exactly one dispatch parent. The strict pre-egress reservation is the maximum one
physical call can consume under the frozen provider contract—not the total budget divided by an
estimated call count.

### D9 — One canonical sandbox evidence chain

The canonical chain is:

```text
verification_request
  -> immutable verification request contract
  -> verification_attempt
  -> verified_output_revision
  -> sandbox publication attempt
  -> sandbox active pointer
```

`sandbox_output_revision` is not a second competing evidence model. Task 10 measures it and either
migrates fully provable rows or retires it from serving.

### D10 — No configured executor means unavailable, not queued-to-fail

If the deployment has no sandbox executor, the preflight and stage rail say
`EXECUTION_SUBSTRATE_UNAVAILABLE`. The API writes no request and queues no work. The same principle
applies to sandbox publication and both production acts.

### D11 — Certificates cover methods, not individual future feature ideas

Required certificate sets are method-specific:

| Sealed member method | Required for production |
|---|---|
| `LLM_AUTHORED` | current `AUTHORING_METHOD` **and** current `EXECUTION_STACK` |
| `REVIEWED_RECIPE_BLUEPRINT` | current `EXECUTION_STACK` |
| legacy/unrecorded/undecidable | ineligible; no backfill by inference |

A certificate is current only for the exact deployed contract, model/prompt or compiler/renderer
stack, corpus and policy versions it evaluated.

### D12 — Gold evaluation gates production, never formula visibility or preview

Missing or stale generator certification permits formula authoring, code preview, sandbox execution
and sandbox publication with a warning. It blocks `MATERIALIZE_PRODUCTION` and
`PUBLISH_PRODUCTION`. Feature-specific leakage, permission, renderer, policy and formula-validity
failures still block at their appropriate earlier act.

### D13 — A production route does not exist merely because it can insert `REQUESTED`

An action is “implemented” only when it has a producer, immutable request, queue, consumer, lease,
fence, crash reconciler, terminal result and UI projection. Until all exist, `action_available` is
false and the UI renders `UNAVAILABLE`.

### D14 — Idempotency means insert-or-verify

On every conflict the stored canonical payload is read and compared. Returning an existing id for
different work is corruption, not deduplication. Output writers return the stored winner, never a
newly calculated id that lost the insert.

### D15 — Applied migrations are immutable

All fixes use new migration files. Never edit 1100–1119 after any persistent application. Migration
numbers are provisional until execution rebases on the merged registry:

- 1117 remains reserved-unused by run-spine;
- 1118 and 1119 are already used;
- 1120 remains reserved for the cross-catalog programme;
- this programme provisionally owns 1121–1129.

▲ *Amended 2026-08-24: 1120 AND 1121 are applied live (cross-catalog Stage 1B; 1121 =
`governed_telemetry_outbox`) — this programme's 1121 reservation is dead and its block is
1122–1129; 1130–1139 belong to the serving plan's T0 mapping. See the supersession record at the
top of this document.*

If another branch has claimed one, renumber before writing; do not race the ledger.

### D16 — Pre-live permits a clean cutover, not fabricated backfills

The tool is not live, so incompatible development attempts may be marked `PRE_CONTRACT` and made
unexecutable, or deleted only under an owner-approved development-data reset. Never invent an
authorization, formula pin, verification contract or certificate for a row that never had one.

---

## 2. Corrected end-to-end journeys

### 2.1 Recipe-origin feature

```text
Hypothesis
  -> frozen recommendation/considered revision
  -> selection revision under exact target reading
  -> server builds FormulaAuthoringPlanV2
       current reviewed blueprint?  -> deterministic authoring, zero LLM calls
       otherwise authorable?         -> explicit cost-confirmed LLM authoring
       otherwise                     -> named refusal
  -> READY formula draft
  -> relational selection/formula/target binding
  -> immutable build set
  -> GENERATE_PREVIEW allowed decision
  -> restore exact formula -> admit -> compile -> render -> seal
  -> code preview available
  -> EXECUTE_SANDBOX decision over exact verification contract
  -> verification attempt -> verified output
  -> PUBLISH_SANDBOX decision -> sandbox active pointer
  -> optional production actions, requiring method-specific current certificates
```

### 2.2 LLM-origin feature

The journey is identical after strategy selection. Its immutable authoring plan says
`LLM_AUTHORED`, binds the provider contract and spend authorization, and records every author and
critic physical call. A novel feature is not compared to a per-feature gold answer. The certificate
says the exact authoring method is reliable on its reviewed programme; deterministic formula and
renderer checks still validate the individual result.

### 2.3 Operator evaluation journey

```text
Governance -> Formula quality
  -> choose LLM authoring programme or recipe compiler programme
  -> server resolves exact deployed configuration
  -> cost preview (LLM only)
  -> explicit start
  -> durable evaluation run walks governed cases
  -> real author/compiler/renderer/executor paths run
  -> per-case evidence and cost recorded
  -> score
  -> issue certificate only if certifiable
  -> changing any identity-bearing component makes the certificate stale
```

---

## 3. Migration programme

The filenames below are provisional reservations, not permission to skip the registry check.

▲ *Amended 2026-08-24: the 1121 row below is DEAD as a number (applied live as
`governed_telemetry_outbox`) — its content re-numbers from the 1122–1129 block at write time; the
1124 design's plan-envelope hash columns and the 1122 design's lineage-table overlap are absorbed
by the serving plan (its 1134/1135 rows). See the supersession record at the top.*

| Migration | Purpose | Key guarantees |
|---|---|---|
| 1121 | governed action execution bindings | only an allowed decision for the same action/resource/authorization can bind to an attempt; server principal snapshot |
| 1122 | target/selection/formula lineage V2 | target reading agrees across job, selection binding and build-set member |
| 1123 | physical LLM dispatch/spend integrity | one reservation per dispatch, real FK, insert-or-verify identities |
| 1124 | frozen authoring plan and method identity V2 | strategy is not recomputed; review revision and expectation generation are identity-bearing |
| 1125 | verification contract and canonical attempt chain | check set, inventory, ordinal and run parameters survive to execution and output |
| 1126 | sandbox publication V2 | decision-bound request, worker lifecycle, output/group composite integrity, active pointer |
| 1127 | certificate binding V2 | certificate subject tuple is relational; method-specific required-set bindings |
| 1128 | durable evaluation operation | operator run, case attempts, budgets, status/events and issued-certificate link |
| 1129 | production execution integrity V2 | artifact/env/group/verification/cert bindings, full-work idempotency and active-pointer composite FK |

Every migration task must include:

1. live and restored-database row counts before deciding nullability;
2. a scratch restore and migration dry run;
3. `verify_migration_ledger.py` before and after;
4. FK violation tests for both mismatch directions;
5. append-only/update/delete tests where evidence is immutable;
6. no edits after application to any persistent database.

### 3.1 Delivery gates and atomic increments

Tasks are ordered below. Within that order, the following changes deploy atomically:

| Gate | Tasks | What may be enabled afterwards |
|---|---|---|
| A — control-plane integrity | 0–5 | existing formula/preview paths, now with allowed-decision, server-role and target-lineage enforcement |
| B — authoring integrity | 6–9 | costed recipe/LLM authoring and the durable preview coordinator |
| C — sandbox | 10–12 | sandbox execution and sandbox publication as two working actions |
| D — certification | 13–14 | operator evaluations and current method certificates; production remains unavailable |
| E — production | 15–16 | isolated production smoke tests; public production actions still require an owner opening decision |
| F — product cutover | 17–19 | mounted end-to-end UI and removal of the legacy path |

Do not deploy Task 2’s schema without Tasks 3–4’s writers and worker readers. Do not deploy Task 6
without Task 7’s reservation calculation. Do not activate strategy V2 without Task 9 consuming it.
Do not advertise sandbox until both Task 11 and Task 12 have consumers. Do not expose production
because Task 15’s constraints exist; Task 16 and Gate E’s fault tests are mandatory.

---

## 4. Implementation sequence

### Task 0 — Freeze the truth and amend both source plans

**Owner:** shared documentation/architecture.

**Files:**

- Modify the parent plan status block and sequence.
- Modify the child plan status blocks for steps 4–10.
- Add an implementation ledger beside this plan.

**Work:**

1. Coordinate with the current retirement/config session and land or isolate its eight dirty files.
2. Pin one clean baseline SHA; record backend/frontend suite counts and migration ledger state.
3. Amend the parent’s claims:
   - step 3 = `PARTIAL — worker enforcement and relational attempt binding open`;
   - step 6 = `PARTIAL — verification request/worker skeleton; executor and sandbox publication open`;
   - step 7 = `SCHEMA/API SKELETON — unavailable and no workers`;
   - step 9 = `COMPARATORS/STORAGE ONLY — no trusted runner or operator journey`.
4. Amend the child’s claims:
   - step 4 = `PARTIAL — current review and frozen strategy consumption open`;
   - step 5a = `PARTIAL — fence, target lineage, action evidence and object policy open`;
   - step 5b = `UNMOUNTED — no selection host and broken execution handoff`.
5. Add a link from both plans to this remediation and state that its completion tests supersede
   their “done” prose.
6. Measure counts in every table touched by 1121–1129. No cleanup is performed in this task.

**Acceptance:** the documents no longer call schemas/helpers “complete journeys”; baseline is clean;
all later tasks name the same SHA and migration ledger.

### Task 1 — Add adversarial characterization tests before repairs

**Owner:** shared test infrastructure.

**Files:** existing decision, generation, formula worker, verification, coordinator, production,
compiler-certification and route test modules; new `test_parent_child_journey_invariants.py`.

Write failing tests proving every defect, not source-inspection tests:

1. an `allowed=false` decision with unchanged pins is refused by each worker;
2. a decision for the wrong action/resource cannot attach to every attempt type;
3. roles in either generation request are rejected at the wire;
4. selection under target A cannot enter a job/build set for target B;
5. a duplicate physical dispatch identity causes zero second provider calls and zero second
   reservation;
6. exhausted spend is non-transient;
7. a single physical call cannot settle above its reservation or approved ceiling;
8. verification worker receives the exact check set, inventory, ordinal and run parameters selected;
9. nonexistent and zero-member artifacts cannot pass production preflight;
10. stale coordinator fence cannot release, advance or mutate members;
11. output insert conflict returns the stored output or raises disagreement;
12. certificate A cannot be bound while claiming subject B;
13. active pointer cannot pair publication A with output B;
14. a compiler certificate cannot be minted from caller-supplied comparison payloads;
15. the frontend generation-to-execution journey submits a complete valid request.

For each test, temporarily reintroduce the defect after the fix and record that the named test turns
red. A grep of pytest output is not evidence; read the actual failing assertion and summary.

### Task 2 — Migration 1121: make allowed action execution relational

**Owner:** parent.

**Schema:**

1. Add a unique parent key to `action_decision_revision` over:

   ```text
   action, resource_identity_hash, decision_id, authorization_id, allowed
   ```

2. Create `action_execution_binding` with:

   ```text
   binding_id PK
   action
   resource_identity_hash
   authorization_id
   decision_id
   decision_allowed BOOLEAN NOT NULL CHECK (decision_allowed)
   evidence_hash
   bound_by
   bound_at
   UNIQUE(action, resource_identity_hash, binding_id)
   composite FK -> action_decision_revision(..., allowed)
   composite FK -> action_authorization_revision(action, resource, authorization)
   append-only trigger
   ```

3. Add domain-specific companion bindings for:

   - formula authoring plan;
   - generation request;
   - verification request;
   - sandbox publication attempt;
   - production materialization attempt;
   - production publication attempt.

   Each companion has a constant action CHECK and a composite FK to both the domain row’s real
   resource and `action_execution_binding`. A generic `(domain_type, domain_id)` JSON/polymorphic
   link is forbidden because PostgreSQL could not enforce its parent.

4. Add `action_authorization_principal_binding`, content-addressed over authenticated subject,
   tenant, effective roles and policy version. The authorization references this server-written
   snapshot. This is the only role source a worker reads.

Legacy rows without a companion are `PRE_CONTRACT` and unexecutable. Do not backfill allowed
decisions or principals.

**Code:** add one `bind_allowed_action()` service used in the same transaction that creates the
domain request and queue row.

**Acceptance:** every mismatch produces `ForeignKeyViolation`; no request can bind a refused
decision; a transaction rollback removes authorization, decision, binding, request and queue work
together.

### Task 3 — Make the shared decision service authoritative at worker time

**Owner:** parent.

**Files:** `materialize/action_decision.py`, per-action fact loaders, all workers and their tests.

1. Change worker API to:

   ```python
   recheck(
       conn,
       decision_id,
       expected_action,
       expected_resource_identity_hash,
       expected_authorization_id,
       current_pins,
   ) -> AllowedActionDecisionV2
   ```

2. Add typed failures: `DecisionRefused`, `DecisionForWrongAct`, `DecisionDrift`,
   `DecisionMissing`.
3. Validate exact pin-key sets, not merely a hash: missing and unexpected pins are drift.
4. Check `allowed` before changing a domain request to `RUNNING` and before external egress.
5. Store a terminal per-action refusal containing decision id, per-member verdicts and remedies.
6. Provide one server fact loader per action. No route assembles its own blocker tuple.
7. Each loader returns one typed `EvidenceBundle` containing facts, pins and the durable dependency
   revisions it read. Every fact source must contribute a revision/hash pin; an unpinned current-state
   read is a plan defect, not an acceptable convenience.
8. Actions over a member collection require a non-empty exact member set. Empty `all([])` is never
   permission. Single-subject actions explicitly carry their one subject.
9. Have `ask` and `decide` call the same fold over the same loader output; `ask` remains read-only.

**Acceptance:** request-time and worker-time outcomes agree for unchanged facts; changed pins refuse;
a persisted denied decision refuses; a queue message with a decision from another act refuses even
if all hashes happen to match.

### Task 4 — Remove client roles and bind authoritative data scope

▲ *Superseded 2026-08-24: preserved work owned by the serving plan's B0a (R8) — see the
supersession record at the top.*

**Owner:** parent.

**Files:** build-set and code-generation request models, coordinator, generation job codec, compile
lane, frontend API types.

1. Delete `roles` from `GenerationIn` and `execution_parameters`.
2. Reject old callers sending it via `extra="forbid"`.
3. Resolve the principal snapshot from authenticated identity inside the write transaction.
4. Queue only `action_execution_binding_id`; worker loads roles from its principal binding.
5. Recheck read/use entitlement against the frozen catalog and policy revisions before compilation.
6. Keep broad development trigger permission, but never infer PII/restricted read permission from it.

**Acceptance:** adding `pii_reader` to a body returns 422; the same caller gets only server roles;
restricted data cannot be compiled without the corresponding server entitlement.

### Task 5 — Migration 1122: enforce target/selection/formula lineage

▲ *Superseded 2026-08-24: target-lineage work owned by the serving plan's B0b, and the 1122
lineage design overlapped by its binding-chain migration (R8; this design specifies no hash
columns — the absorbed hashes are Task 8's) — see the supersession record at the top.*

**Owner:** child with parent schema review.

1. Add target-reading composite keys to `feature_selection_revision` and
   `selection_formula_binding`.
2. Add `target_reading_revision_id` to `code_generation_job_member` and `build_set_member` only if
   Task 0 proves the append-only parents are empty. If rows exist, create append-only V2 lineage
   companion tables instead; do not UPDATE through evidence-table write-once triggers or fabricate
   target values.
3. Bind job member simultaneously to:
   - its job’s target and considered revision;
   - its selection’s target and considered revision.
4. Bind build-set member simultaneously to:
   - its build set’s target;
   - its selection/formula binding’s target.
5. Change `member_authoring_plans()` to select and compare target reading before loading candidates.
6. Make direct `/build-sets` declaration perform the same service validation; the composite FK is
   the final authority.

**Acceptance:** both mismatch directions fail at service and database levels; a correct multi-member
set round-trips in selected order; retry identity changes when any formula binding or target changes.

### Task 6 — Migration 1123: repair physical-dispatch spend integrity

**Owner:** child authoring substrate.

1. Add `UNIQUE(dispatch_ref)` and a real FK from reservation to `llm_dispatch`.
2. Make `dispatch_ref NOT NULL` for the new contract. Legacy null reservations remain explicitly
   pre-contract or are reset after measurement.
3. Reorder pre-egress transaction:
   - insert/verify the physical dispatch identity;
   - if the identity already exists, return `AlreadyDispatched` and do not call the provider;
   - for a new dispatch, reserve strict worst case against the locked authorization;
   - commit dispatch and reservation together;
   - only then call the provider.
4. A crash with an unresolved dispatch is reconciled to `UNKNOWN_SPEND`/worst case. A retry mints a
   new physical attempt number; it never reuses the old identity.
5. `reservation_for_dispatch` must return exactly one row or raise corruption.
6. Settlement is insert-or-verify; different actuals for one reservation are corruption.
7. Re-raise `SpendExhausted` before the broad audit exception and map it to a permanent
   authorization refusal.

**Acceptance:** concurrency tests with two real connections prove one egress; crash/retry proves two
physical attempt identities and two honest reservations; exhaustion makes zero provider calls.

### Task 7 — Make the displayed and enforced cost ceiling strict

**Owner:** child, with provider adapter owners.

1. Extend the frozen provider contract with maximum input tokens, maximum output tokens, maximum
   physical attempts and worst-case cost under a pinned pricing version.
2. Derive one physical-call reservation from that contract. Do not divide the total approval by
   estimated calls.
3. Validate at confirmation that the requested ceiling can cover at least one complete call and the
   declared retry envelope. Offer the user a smaller retry envelope rather than weakening one-call
   safety.
4. Remove automatic production-capable development-envelope minting. Direct formula authoring must
   either go through plan/confirm or run under `FAKE_TEST` with no provider egress.
5. Make UI copy match the actual guarantee and show maximum calls, tokens and cost separately.
6. Record estimated versus actual calls/tokens/cost on the job result.

**Acceptance:** the provider request is capped so every conforming response is within its
reservation and cumulative spend is within approval. If a provider reports usage above its declared
maximum, persist the true actual as a typed provider-contract-violation/overage incident, stop future
calls and fail the run; never discard or reduce actual usage to keep the ledger cosmetically within
the ceiling. The UI ceiling and backend reservation are derived from one response contract.

### Task 8 — Migration 1124: freeze strategy, governed review and method identity V2

▲ *Superseded 2026-08-24: frozen-method-identity work owned by the serving plan's B0b, and the
1124 design's plan-envelope hash columns absorbed by its identity-persistence migration (R8) — see
the supersession record at the top.*

**Owner:** child routing; parent owns certificate identity review.

Create the immutable companion `formula_authoring_plan_revision_v2`, keyed to the existing
`formula_draft_authoring_plan`, carrying:

```text
plan_revision_id and content hash
candidate/considered/option/target identities
catalog snapshot, planning request and binding-plan hashes
formula strategy and strategy identity
recipe id and canonical recipe revision hash
review-event set/content hash
expectation ref and expectation generation
reviewed blueprint revision/hash when deterministic
provider contract hash and spend authorization when LLM
method override revision when used
all server facts used by the decision
```

Then:

1. Replace static-registry currentness with a batched durable review-validity reader.
2. Verify recipe canonical hash equals the frozen candidate’s recipe revision.
3. `POST /code-generation-jobs/plan` returns an opaque plan content hash and writes nothing.
4. Confirmation recomputes once in its transaction; mismatch returns `PLAN_STALE_REPLAN` before
   spend authorization or job creation.
5. Persist the V2 plan on each member/draft. Worker calls `author_from_frozen_plan`; it cannot invoke
   `resolve_formula_strategy`.
6. Deterministic derivation failure is a named refusal; never an LLM fallback.
7. Add `expectation_generation` and authoring-plan hash to reviewed method identity; bump
   `METHOD_IDENTITY_VERSION` to 2.
8. Pre-V2 method identities remain visible history and production-ineligible.

**Acceptance:** change review, registry, override or provider contract between plan and confirm →
stale response and zero spend; change it after confirmation → worker uses the frozen plan or refuses
pin drift, never changes method.

### Task 9 — Repair coordinator lifecycle, evidence and object policy

**Owner:** child.

1. Pass `worker_id` and `lease_fence` into `release_job`, `advance_job`, `update_member`, action
   updates and job-link updates. Every write includes both in its `WHERE` clause.
2. A fence loss stops the stale worker without terminalizing another worker’s job.
3. Persist per-member `AUTHOR_FORMULA` action bindings; derive the aggregate display state. Remove
   linkless aggregate `PERFORMED` claims.
4. `GENERATE_PREVIEW` becomes `PERFORMED` only after the generation request and queue row are
   durably recorded; terminal result derives from the generation request.
5. Include the frozen plan/evidence hash in job content identity. A corrected external fact mints a
   new plan/job; an unchanged repeat returns the old terminal answer.
6. Add explicit `/retry` and `/re-execute` semantics only where the run-spine spec permits them.
7. GET and cancel enforce owner-or-platform-admin, using indistinguishable 404 for denied reads.
8. Add a reconciler test proving a released message is not abandonment and a genuinely unreachable
   live job is recoverable.

**Acceptance:** reverse-order concurrency tests prove stale workers cannot mutate; all member
authorizations and decisions are traceable; one user cannot read or cancel another user’s job.

### Task 10 — Migration 1125: preserve the complete verification contract

**Owner:** parent.

1. Introduce an immutable verification request contract containing:
   - sealed artifact and its server-resolved environment/group/authorization;
   - versioned check-set identity and hash;
   - pinned inventory observation FK;
   - attempt ordinal/purpose;
   - runtime profile and enforced-read policy;
   - contract content hash.
2. The public request accepts an artifact and a server-recognized check profile/id—not arbitrary
   hashes, environment or authorization. The server resolves the exact revisions.
3. Live-request idempotency includes the complete contract hash, not only artifact/environment.
4. Bind `verification_request` to its action execution binding and contract.
5. Worker loads the contract and creates the existing rich `verification_attempt`, then
   `verified_output_revision`. It receives no security-critical fact from the queue payload.
6. Add composite FKs proving request, attempt, artifact, authorization, check set and inventory agree.
7. Stop writing `sandbox_output_revision`. If Task 0 finds rows:
   - migrate only rows with enough evidence to populate the canonical chain exactly;
   - otherwise label them pre-contract and exclude them from publication.
8. Output insertion uses insert-or-verify, and request terminal state references the stored output.

**Acceptance:** the exact user-visible verification plan reaches the executor byte-for-byte; a
different check set or inventory mints different work; output disagreement is loud.

### Task 11 — Configure and prove the sandbox executor

**Owner:** operator plus parent integration.

1. Define the executor interface over `VerificationContractV2`, not loose ids.
2. Implement the Kind adapter using the real rendered project, pinned input inventory, isolated
   namespace/path and enforced read scope.
3. Record external operation id before submission or in the same durable handoff that authorizes it.
4. Add lease renewal/fencing around long-running work and reconcile unknown submit outcomes.
5. Configure authoritative inventory, Spark/Kedro runtime, durable staging and output manifest store.
6. When absent, the action fact loader returns `EXECUTION_SUBSTRATE_UNAVAILABLE`; route writes
   nothing.

**Acceptance:** a known banking fixture executes in Kind and produces the expected grain, dates,
values and manifest; process death at every external boundary recovers without duplicate execution
or a permanently live request.

### Task 12 — Migration 1126: build `PUBLISH_SANDBOX` as a real action

**Owner:** parent.

1. Use the canonical `verified_output_revision` as the subject.
2. Add immutable sandbox publication request/attempt evidence with:
   - verified output, verification contract and artifact;
   - environment and logical group derived server-side;
   - capability revision;
   - expected active pointer/fence from a server-issued publication plan;
   - action execution binding;
   - lease, fence, attempts and explicit uncertain outcome where the external swap requires it.
3. Add a queue handler and reconciler. `STARTED` may never be written without a consumer.
4. Add a composite active-pointer FK proving pointer output, attempt, environment and group agree.
5. `PUBLISH_SANDBOX` never inherits the `EXECUTE_SANDBOX` decision.
6. Replace the old publication route with an adapter to this service during one cutover; remove the
   legacy independent gate after journey tests.

**Acceptance:** execution can pass without publication permission; publication of output A under
attempt B is unrepresentable; stale active-pointer plans refuse without swapping.

### Task 13 — Migration 1127: correct certificate identity and required sets

**Owner:** parent.

1. Add a unique certificate subject key:

   ```text
   certificate_revision_id
   certificate_kind
   subject_identity_kind
   subject_identity_hash
   contract_hash
   corpus_hash
   outcome
   ```

2. Bind attempt certificate rows to that complete key.
3. Define `ExecutionStackIdentityV1` from compiler, IR, renderer, template, Kedro/PySpark, physical
   type, output policy and runtime-profile versions.
4. Implement `required_certificates_for_member()` using D11’s table.
5. Implement currentness against the exact deployed evaluation contract and corpus. Newest-by-time
   alone is removed.
6. Store all required certificate bindings on materialization decision. Publication compares those
   exact bindings against current validity; it never substitutes newer certificate rows silently.
7. Make missing, stale and mismatched distinct codes and UI remedies.

**Acceptance:** LLM and reviewed members require different sets; mixed-method build requires the
union per member; certificate A cannot be relabelled as subject B; any identity component change
makes the old certificate `STALE`.

### Task 14 — Migration 1128: build both trusted evaluation operations

**Owner:** shared; governance owns case approval.

#### LLM authoring programme

1. Build a durable evaluation run over the existing V2/V3 evaluation contract and approved corpus.
2. Resolve the deployed author/critic/model/schema/policy identities server-side.
3. Obtain explicit cost confirmation, then queue the run.
4. For every case, invoke the real audited authoring and critic lane, require
   `qualifies_as_v3_evidence_for_run == (True, ())`, compare with expert expectation and record cost.
5. Issue `AUTHORING_METHOD` only when the corpus is sufficient and every safety/currentness rule
   passes.

#### Recipe compiler programme

1. Replace caller-supplied `produced_ir`, `executed_rows` and dispatch count with a trusted runner.
2. Load one approved governed case revision.
3. Invoke the real deterministic blueprint producer, formula validation, compiler and renderer.
4. Execute against the pinned synthetic/approved dataset using Task 11.
5. Read provider dispatch count from audit storage; any dispatch fails the deterministic case.
6. Derive grain keys and decimal policies from the governed case/formula; never accept them as
   caller assertions. Compare normalized IR and exact banking values using the existing comparators.
7. Issue `EXECUTION_STACK`, never the default `AUTHORING_METHOD`.

Case approval is its own governed operation: authenticated reviewer identity and role are resolved
server-side, required distinct roles must approve the exact combined case revision, and no API
accepts an `approved_by` list. Certificate subject hashes are derived from the actual audited
authoring run or deployed execution stack; they are never request fields.

#### Operator API/UI

Add:

```text
GET  /formula-evaluations/programmes
POST /formula-evaluations/plan
POST /formula-evaluations
GET  /formula-evaluations/{run_id}
GET  /formula-evaluations/{run_id}/cases
```

The UI shows deployed identity, corpus coverage, calls/tokens/cost ceiling, progress, per-case
expected/actual differences, final status and certificate/currentness. It never accepts hashes the
server can resolve.

The programme keeps an `evaluation_programme_current` pointer to its latest accepted evaluation run.
A newer accepted evaluation failure makes the previous certificate non-current;
`current_method_certificate` may not skip over a failure and select an older success merely because
it is the newest `CERTIFIED` row.

**Acceptance:** comparator helpers alone cannot issue a certificate; empty/unapproved corpus cannot
certify; changing a deployed identity or advancing the programme head to a failed run changes
currentness without rewriting history.

### Task 15 — Migration 1129: repair the production data model before building workers

**Owner:** parent.

1. Bind production materialization to the real sealed artifact using a composite key that includes
   environment, group and generation authorization.
2. Require a non-empty sealed member set.
3. Bind the exact current verification evidence and all required certificates at decision time.
4. Derive environment/group from the artifact; remove them from the public body.
5. Make materialization idempotency cover the full work:

   ```text
   artifact + target revision + environment/group + verification + certificate set + execution plan
   ```

   The environment/group live-work exclusion remains a concurrency lock, not an idempotency answer.
6. On a live-group conflict, return `GROUP_BUSY` naming the existing attempt; never `created=false`
   as if unrelated work were identical.
7. Output insert is insert-or-verify and returns the stored identity.
8. Add the composite production active-pointer FK proving the publication attempt owns the named
   output in the same environment/group.
9. Strengthen the invariant sweep to verify output, attempt, environment, group and status—not only
   `PUBLISHED`.
10. Production publication reads certificate bindings from the materialization attempt and compares
    current validity. It never re-derives a replacement set.

**Acceptance:** nonexistent/empty/mismatched artifacts and outputs fail at the database boundary;
unrelated live work cannot be returned as idempotent success; active pointer cross-pairing is
unrepresentable.

### Task 16 — Implement production materialization and publication workers

**Owner:** parent plus operator substrate.

1. Add queue producers and consumers for both production actions.
2. Every worker loads its action execution binding and calls hardened `recheck` before external work.
3. Materialization stages through the declared production runtime, records external operation id,
   verifies the manifest, writes output identity, handles unknown outcome, quarantine and recovery.
4. Publication claims its attempt, rechecks stored certificate bindings/currentness and performs
   active-pointer CAS plus terminal `PUBLISHED` in one transaction.
5. Lease/fence every mutation. A stale worker cannot stage, terminalize or swap.
6. Reconcilers distinguish a healthy message awaiting redelivery from unreachable work.
7. Only after the full Kind journey passes does `action_available` expose these actions.

**Acceptance:** fault-injection at every external boundary yields one of the designed states and an
operator remedy; no request remains indefinitely `REQUESTED`/`CLAIMED`; no duplicate data or pointer
swap occurs.

### Task 17 — Complete and correct the frontend journey

**Owner:** child UI for selection/code; parent UI for execution/governance.

1. Mount `PrepareCodeGenerationAction` on the real selection/recommendation surface.
2. Server returns the exact request object or opaque plan token required to start; browser does not
   assemble roles, hashes or policy facts.
3. Navigate code workspace to execution using an artifact id only. Execution page asks the backend
   for the current verification plan/check profiles rather than receiving empty query parameters.
4. Disable unavailable sandbox/production actions with server reason codes. Never show an infinite
   “queued” spinner for an absent worker.
5. Show per-member origin, authoring method, formula, assumptions, pins, decisions, code files,
   execution evidence and certificate state.
6. Display cost ceiling language from Task 7 and actual spend after completion.
7. Apply owner/admin read and cancel policy to job/run screens.
8. Add the Formula Quality operator page from Task 14.
9. Accessibility: keyboard action order, focus on error/result, live progress region, table labels and
   non-colour state indicators.

**Acceptance:** hypothesis → selections → plan → confirm → browser reload → code → sandbox execution
→ sandbox publication works through mounted UI without manually editing a URL.

### Task 18 — Atomically relocate gold and delete the legacy path

**Owner:** shared.

This is one cutover commit after Tasks 2–17 pass.

1. Remove gold evaluation from any formula/preview executable gate. Preserve its code as an owned
   warning for historical rows until old projections are removed.
2. Make production facts use only the method-specific certificate service.
3. Delete `POST/GET /materialization-runs`, its route registration, producer, queue handler and
   independent activation-policy entry.
4. Delete or retire all alternate paths that enqueue generation, verification or publication
   without an action execution binding.
5. Replace the old readiness response used by current UI/API consumers with three explicit
   projections: `formula_status`, `engine_status` and `production_certification_status`. Keep the
   old fold only in a named compatibility reader until those consumers are migrated, then delete the
   compatibility reader. No caller may authorize from the aggregate label.
6. Update OpenAPI and frontend clients; route absence is asserted.

**Acceptance:** one selected feature cannot receive different code-generation answers from different
APIs; gold absent still yields formula, preview and sandbox; production remains blocked.

### Task 19 — Decisive journeys, mutation suite and maintenance cutover

**Owner:** shared engineering plus operator.

Run these through public APIs and real workers:

1. reviewed recipe → zero provider calls → V3 formula → preview → sandbox run/publish;
2. unreviewed recipe → explicit LLM author/critic → preview → sandbox run/publish;
3. LLM-origin feature through the same downstream chain;
4. mixed deterministic/LLM build with exact per-member provenance;
5. missing grain/ambiguous event time/currency/reversal/status/target leakage before egress;
6. denied and wrong-act decision direct-queue attacks;
7. target-reading cross-binding attack;
8. client role-escalation attack;
9. duplicate dispatch, worker crash and retry under one cost ceiling;
10. verification check-set/inventory mutation between request and worker;
11. sandbox execution permitted while publication is refused;
12. gold/certification absent: sandbox succeeds, production refuses;
13. correct certificate sets allow the two production actions;
14. stale/mismatched certificate refuses both production actions;
15. nonexistent/empty artifact and output-forgery attacks;
16. stale worker after lease reclamation in every lane;
17. unrelated live work in the same group returns `GROUP_BUSY`, not idempotent success;
18. browser closure/reload and run-spine continuation;
19. owner/admin visibility and cross-user denial;
20. old route absent and legacy-shaped queue messages dead-lettered.

For every P0 defect, keep a mutation test that fails when the old line/constraint is restored.

Operational order:

```text
stop intake and workers
verify clean migration ledger
take and verify backup
restore backup to scratch
apply all new migrations in lexical order
run schema probes and adversarial smoke tests on scratch
apply migrations to the branch-owned development database
deploy API and workers together
deploy frontend
smoke recipe and LLM preview journeys
smoke sandbox
keep production unavailable
run and certify both evaluation programmes
smoke production in an isolated namespace
owner explicitly opens production actions
```

Rollback is image plus forward repair migration or database restore. Never edit an applied file or
move a checksum merely to make the ledger green.

---

## 5. Finding-to-task closure matrix

| Review finding | Closed by |
|---|---|
| refused decision executes | Tasks 1–3 |
| attempt has plain decision FK | Tasks 2–3 |
| verification contract discarded | Tasks 10–11 |
| two verification authorities | Tasks 3, 10, 18 |
| client-supplied roles | Task 4 |
| duplicate dispatch/orphan reservation | Task 6 |
| spend exhaustion transient | Task 6 |
| ceiling can overshoot | Task 7 |
| target reading can disagree with selections | Task 5 |
| wrong method-specific certificate set | Tasks 13–14 |
| certificate can claim another subject | Task 13 |
| production empty/nonexistent artifact fail-open | Task 15 |
| production routes have no workers | Task 16 |
| production active pointer cross-pairing | Task 15 |
| production/output conflicts not verified | Task 15 |
| compiler runner trusts caller assertions | Task 14 |
| sandbox publication has no worker/decision | Task 12 |
| verification executor/evidence gaps | Tasks 10–11 |
| static registry treated as current review | Task 8 |
| method identity omits expectation generation | Task 8 |
| strategy recomputed after confirmation | Task 8 |
| coordinator action audit/fence/idempotency | Task 9 |
| job read/cancel object policy | Task 9 |
| old and new paths disagree | Task 18 |
| prepare action not mounted | Task 17 |
| code-to-execution handoff is incomplete | Task 17 |
| no evaluation operator journey | Task 14 |
| UI overpromises cost control | Tasks 7, 17 |

No finding is deferred beyond this programme. Operator-owned infrastructure is an explicit task and
an entry condition, not an unowned footnote.

---

## 6. Definition of done

The parent and child plans may be marked complete only when all statements below are true:

1. All six actions are represented by one decision vocabulary and one server fact-loading service.
2. Every implemented action has complete producer-to-terminal lifecycle machinery.
3. Every attempt is relationally bound to an allowed same-action/same-resource authorization and
   decision.
4. No request body can widen roles, submit readiness/blockers, choose formula method, or supply a
   certificate identity the server can resolve.
5. Strategy, formula, selection, target, build, verification and publication inputs are immutable and
   relationally connected.
6. Every physical LLM egress has one dispatch, one reservation and one outcome; approved ceilings
   cannot be crossed.
7. Reviewed recipes use the deterministic lane only on current durable review evidence; all other
   authorable formulas take the explicit costed LLM lane.
8. Formula and code preview never wait for gold certification.
9. Production requires the correct current certificate set for every sealed member plus current
   feature-specific verification.
10. Both evaluation programmes can be triggered, monitored and audited from the UI.
11. The old materialization route and all queue bypasses are absent.
12. The full backend, frontend, migration, concurrency, fault-injection, mutation and Kind journey
    suites pass from a clean checkout.
13. The migration ledger has zero unexplained rows or checksum drift.
14. Plan status text, API behavior, worker behavior and UI wording all describe the same reality.

Until then, production actions remain unavailable and the programme reports partial completion
without weakening the already-working preview capabilities.
