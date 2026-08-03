# Phase G — Execution Wiring: design plan for approval

**Status: PLAN, awaiting user approval. No implementation has started.**
Baseline: `feature/phase-g` @ `3b0b7b01` (branched from `worktree-codegen-review-remediation`).
Charter: `docs/superpowers/plans/2026-08-03-phase-g-handoff.md`.
Evidence: four Opus scout reports, `/Users/ascoe/.claude/jobs/f47dd051/tmp/scout-{chain,lifecycle,trigger,track1}.md`, to be committed into the repo as an appendix before implementation.

---

## 0. Read this first: four charter premises moved

The charter is sound in intent. Four of its factual premises did not survive verification, and two of them change what Phase G can deliver. Nothing below is asserted from memory; every claim carries a file:line.

**0.1 — `main` already contains Track 2 (and this session put it there).** The charter says branch from the codegen branch, *not* `origin/main`, because "main lacks Track 2". True when written; false ~20 minutes later. `main` is now `f3424c36` = `Merge branch 'worktree-codegen-review-remediation'`, merged and pushed by this session with the user's approval after a 7588-green merged-tree suite. `feature/phase-g` @ `3b0b7b01` is a **strict ancestor of main** — 13 behind, 0 ahead. Both candidate baselines carry Track 2; `main` additionally carries 12 Track-1 commits already merged there. **Baseline choice is deferred to the user** (§7 Q1) because it interacts with the Gate A tag and with Track 1's ownership of the integration↔codegen merge. The plan below is baseline-agnostic.

**0.2 — `ExecutionTier` exists, but not where the charter thinks, and it means something else.** It is at `overlay/upload/bridge_realization.py:81-83` (`SANDBOX`/`PRODUCTION`) and scopes **bridge-realization applicability** — whether a cross-catalog join is approved for production data. `src/featuregen/materialize/` never imports it (grep: zero hits). The *run* path has no tier, deliberately: `binding.py:53` "There is no production path in this slice"; `derive_namespace()` takes no parameters by design (`identity.py:28-30`); and the sandbox name is baked into `sandbox_execution_hash` itself (`identity.py:34` "There is no production execution hash"). Decision 4 is therefore not "honor the existing tier" — see §3.4.

**0.3 — the chain order in the charter is wrong, and two stages are missing.** `select_publisher` must run **before** `render_project` (its selection is rendered into `catalog.yml`) and before `prepare_run` (which needs `capability_attestation_id`). The diagram also omits `resolve_physical_type` (`physical_types.py:375`) and `derive_requirement` (`inputs.py:271`). Corrected chain in §2.

**0.4 — the publish step does not exist at all** — not merely its pointer. The rendered `published` dataset writes parquet into `${staging_root}/published/<table>`; nothing in `src/` ever creates or points `sandbox_feature.<group>` (`render/publish.py:26-31`). Worse, its precondition is unbuilt: `probe_publication_capability` is *named* at `publish.py:326` and **does not exist**, so `select_publisher` can only ever return `CAPABILITY_UNPROVEN`. Consequence in §3.5 — and it is the single biggest reason this program cannot ship as one release.

---

## 1. What is actually true today

Established by the four scouts; the full evidence is in their reports.

- **Nothing is wired.** Zero `src/` callers for `record_generation`, `append_run_event`, `record_run_manifest`, `record_validation_report`, `record_attestation`, `select_publisher`, `prepare_run`, `submit`. `grep materialize src/featuregen/api/` returns nothing. `compile/chain.py` (the orchestrator's reserved home, DEFERRED-WORK A.24) does not exist; `compile/__init__.py:18` says so in as many words.
- **The plane is genuinely durable and genuinely good**: 7 tables, 14 guard triggers, one-terminal partial index, plus Task 15's `1044` BEFORE-INSERT ordering trigger. Its logic (`fold_run_status`, `may_regenerate_for`, `select_publisher`, `check_completeness`) is tested and correct — and uncalled.
- **A run has no DB identity until someone appends an event**, and `fold_run_status` raises on an empty stream (`control_plane.py:344`). A crash between `spark-submit` and the first append leaves **zero trace**. After `RUN_SUBMITTED` a run stays `submitted` forever: no lease, no heartbeat, no staleness rule, no `UNKNOWN`, no persisted `SubmissionOutcome`.
- **The compile-side artifacts are not persisted** — the group plan, contract body, IRs, the Gate-2 authorization token and the prepared parameters have no writer. Only their hashes survive (`GENERATED.lock` + the plane). A crash after render loses the packing list §9 gates against.
- **Run evidence exists but is unreadable**: everything `RunManifestV1` promises is computed on the cluster and written as per-feature `StagingManifestV1` JSON under `staging_root` (`nodes_compute.py:3273-3328`). No `src/` code reads it back. Gate outcomes reach a caller only as the leading token of a `RuntimeError` transported in `SubmissionOutcome.detail`'s last 1500 stderr chars; `COMPUTATION_COMPLETED` vs `GATES_PASSED` is unobservable.
- **A cross-catalog run cannot be submitted at all**: the join-gate node needs `params:bridge_predicate_values`, `prepare_run` supplies it, and `submit.check_run_parameters` (`submit.py:115-116`) rejects it as "unexpected" against `REQUIRED_RUN_PARAMETERS`.
- **Nothing builds the Kedro `nodes` sequence.** The only reference wiring is a test fixture (`test_render_nodes_compute.py:1882` `_wired_nodes`) and it omits the join gate — no full cross-catalog project has ever been assembled.
- **Admission's input is unreachable from any durable identity.** `admit_artifacts(conn, inputs: Iterable[ResolvedFeatureInput])`; `ResolvedFeatureInput(intent: AuthoringIntent, result: AuthoringResult)` has **zero constructors in `src/`**. `AuthoringResult` is by design "Pure in-memory: no DB, no execution, no durable artifact" (`formula/result.py:22`), and the docstring forbids the obvious workaround: the intent "must be THE intent the run was opened for, not a look-alike rebuilt from its parts" (`admission.py:115-117`). The only restorer that exists is the replay lane's `_restore_terminal_result(checkpoint, run_id) -> AuthoringResult` (`replay_authoring.py:179`), and the two trace stores differ: admission's proof reads `authoring_trace_event` (migration 1020), while only `formula_authoring_trace_event` (1022) carries a recoverable `payload jsonb`.

**The honest summary:** every stage works and is well tested; what is missing is not glue but *four load-bearing pieces of machinery* — a durable path into admission, a persisted compile-side record, a readable run-evidence channel, and a publish step that exists at all.

---

## 2. The corrected chain

```text
trigger (queue job, per §3.1)
  └─ resolve   : durable feature identity → ResolvedFeatureInput          [NEW — §3.1/T2]
     admit_artifacts                                                       admission.py:140
     compile_ir            → FormulaExecutionIRV1                          ir.py
     authorize_compilation → AuthorizedCompilation (Gate 2)                ir.py:565
     classify_read_set / resolve_physical_type / derive_requirement        classify.py, physical_types.py:375, inputs.py:271
     derive_group_contract → contract, then build_group_plan               contract.py, group_plan.py
     bind_group            → physical target                               binding.py
     select_publisher      → PublisherSelection  ← BEFORE render           publish.py  (blocked: §3.5)
     render_project / materialize_to → the Kedro project on disk           render/project.py
     seal_project          → generated_project_hash + GENERATED.lock       identity.py
     run_l0                → build-verify in a separate interpreter        validation.py
     prepare_run           → snapshots + parameters + execution hash       runprep.py
     run_l1                → live-metastore checks                         validation.py
     submit                → LocalClusterSubmitter                         submit.py   [LIVE — gated]
     reconcile             : read staging manifests → run manifest         [NEW — §3.3/T7]
     publish                                                               [DOES NOT EXIST — §3.5]
```

---

## 3. The six decisions

### 3.1 Trigger surface — **a fenced queue lane, with a thin route as producer; per-group; `feature:generate`, hardened to platform-admin for release 1**

*What is true.* `LocalClusterSubmitter.timeout_seconds = 3600.0` (`submit.py:162`) and the API's `get_conn` holds one transaction for the whole request (`api/deps.py:100-116`). A route that ran the chain would hold a DB transaction open for up to an hour and, on client disconnect, orphan a Spark process writing into `staging_root` — precisely the failure Task 14 hardened the submitter against. The generic `HandlerRegistry` path is unusable as-is: `_build_context` (`runtime/dispatch.py:111-133`) requires `payload["event_id"]` and a `run` stream, and a job without one is dead-lettered.

*Decision.* Copy the **`recipe_formula_shadow` fenced-lane pattern** verbatim in shape: a dedicated handler map (`queue.py:33`), a claim function with a lease and a monotonic `lease_fence` (`queue.py:202`, 300s), a worker drain stage (`worker.py:436-446`), and an outbox producer (`insert_outbox_message_checked`, `recipe_formula_shadow.py:793`). The HTTP surface is two thin routes: `POST /materialization-runs` validates preconditions, mints the request, enqueues, and returns `202` with a run-id header (`uploads.py:157` precedent); `GET /materialization-runs/{id}` folds `control_plane.run_status` (`ingestion_runs.py:27-33` precedent).

*Unit: per-group, not per-feature.* Forced by the code: `authorize_compilation` raises on an empty group and authorizes the group-wide read set (`ir.py:565-637`); `build_group_plan` demands exactly the group's members (`group_plan.py:317-325`); publication is atomic per group. A per-feature trigger would have to invent a group of one and would still publish a group.

*Authorization.* The closest-fit primitive is `FEATURE_GENERATE = "feature:generate"` — "run the feature-generation workflow + govern contracts" (`identity/permissions.py:27`) — reachable via the existing `require_feature_generate` (`api/deps.py:78`), bundled to `feature_engineer` + `platform_admin`. **Recommendation: gate release 1 on `require_confirmer`** (the raw platform-admin claim, `deps.py:81-91`) instead. Rationale: this is the first path in the product that spends cluster resources and writes outside the governed catalog, and the charter forbids live submission without per-action approval — matching the authorization to that reality costs nothing now and can be relaxed to `feature:generate` once the path is proven.

### 3.2 Minimal run lifecycle — **mint durable identity at REQUEST time; ship four states; add a lease; do not build the state machine**

*What is true.* The plane's terminal discipline is excellent but it starts too late: identity begins at the first appended event, `fold_run_status` raises on an empty stream, `seq` is caller-supplied and 1044 refuses any non-extending value — and the numbering convention a resumer would need is **written down nowhere**. `RunEventKind` has no `REQUESTED`; the request→accepted→running states are a recorded deferral (DEFERRED-WORK A-head `:20`).

*Decision.* Migration **1053** adds `materialization_request` — request id, logical group, actor, roles snapshot, idempotency key, flag/interlock state at accept time, `accepted_at`, `lease_expires_at`, and the resolved-input digest. This is the smallest change that closes the "crash before the first append leaves zero trace" hole, because the row exists *before* any work begins. The event stream stays exactly as built; the request row is its anchor, not its replacement.

Ship these states only: **REQUESTED → ACCEPTED → RUNNING → COMMITTED | FAILED**, mapped onto existing event kinds where they exist (`RUN_PREPARED`, `RUN_SUBMITTED`, `COMPUTATION_COMPLETED`, `GATES_PASSED`, and the terminal four) — **no new event kinds, so no CHECK-constraint change**. Write the `seq` convention down as a module constant with a docstring (`seq = 0` at prepare, monotonic per append, single writer per run enforced by the lease).

*Durability line.* Durable: the request, the event stream, the run manifest, validation reports, group binding, plan revision. Not durable, by decision: the in-memory `AuthorizedCompilation` token and the rendered project tree (both re-derivable from the request + the hashes; re-render is free and byte-identical by construction, which Task 26's golden guard now protects).

### 3.3 Mid-chain failure — **reconcile from evidence; never resume a submission; a new generation, not a retry**

*What is true.* Three facts force this. (a) The plane is append-only with a one-terminal index and 1044's ordering trigger — a mis-sequenced or duplicate write bricks the fold permanently, with no repair path. (b) The run's real evidence is on disk: per-feature `StagingManifestV1` JSON under `staging_root`, plus the in-pipeline gate raises. (c) Task 14's submitter kills the process group on timeout but explicitly cannot reach a descendant that called `setsid` — so "the orchestrator died" never proves "the cluster stopped".

*Decision.* A **reconciler**, not a retry loop. On an expired lease with no terminal event, the reconciler reads the staging manifests for that generation and decides: manifests complete and consistent with the sealed plan → append `COMPUTATION_COMPLETED`/`GATES_PASSED` and record the run manifest; manifests absent or partial → append `RUN_FAILED` with the evidence. **Re-running means a new `generation_id` and a new `staging_root`, never a re-submission into the existing one** — that is Task 14's orphan lesson stated as policy, and it is what makes the whole thing safe under the append-only constraint.

For the charter's exact case — *cluster ran but publish didn't* — see §3.5: publish does not exist, so in G-1/G-2 this case reduces to "staged but not published", which is the **normal, honest terminal state** of every run this program can currently produce.

### 3.4 Sandbox vs production tier — **do not introduce a run execution tier now; fix the realization-tier bug instead**

*What is true.* Two different tier notions, and the charter conflates them. The *realization applicability* tier is real, governed, and **misused**: both materialize entry points hard-code `ExecutionTier.PRODUCTION`, so a SANDBOX-scoped bridge is unusable and there is no parameter to change it. The *run execution* tier does not exist, and its absence is load-bearing: `derive_namespace()` takes no arguments by design and the sandbox namespace is inside `sandbox_execution_hash`, so introducing a production namespace **forks execution identity** — every existing hash would describe a run that could no longer be reproduced under the new scheme.

*Decision.* (a) Fix the hard-coded `PRODUCTION` into a passed-through parameter so realization scope is honored — a real bug, cheap, Phase-G-owned. (b) Introduce **no run execution tier**; one namespace, as designed. (c) Record the production-tier decision as an explicit deferral **with the identity consequence stated**: a production tier requires either a second execution-hash scheme or an accepted identity migration, and that is a governance decision, not a wiring one.

### 3.5 The publish pointer — **out of scope, and the chain must be forbidden from claiming otherwise**

*What is true.* Not just the pointer: the whole publish step is absent, and its precondition is unbuilt (`probe_publication_capability` named at `publish.py:326`, does not exist). Today, after a "successful" run a reader gets a per-generation parquet directory; `sandbox_feature.<group>` — the name in `group_binding.physical_target` and in the rendered README — **is not a queryable object**, and nothing records which generation is current. Implementing it needs all of: a metastore *write* seam (none exists), a chosen DDL form (view swap / `SET LOCATION` / atomic rename — none attested), the live 16b probe, an active-revision record the plane does not have, and an orchestrator publish step.

*Decision.* Out of scope for G-1/G-2 — but the important half is the **honesty constraint**: the orchestrator must never append `RunEventKind.PUBLISHED`, because it would be a lie the plane can never retract (append-only, one terminal). The natural and *truthful* terminal is what the code already produces: `select_publisher` returns `CAPABILITY_UNPROVEN` → the run terminates `PUBLICATION_REFUSED` carrying that code. No new event kind, no migration, and the control plane says exactly what happened. Migration **1055 stays unused and reserved** for the active-revision pointer when G-3 builds it.

### 3.6 Which durability deferrals come forward

**Forward** (each is required for the chain to be operable at all, not a nicety):
1. Run identity at request time + lease — §3.2, migration 1053. Without it, runs are losable.
2. Compile-side artifact retention — migration **1054**: the group plan and contract body as JSONB keyed by `generation_id`. Without it a crash after render loses the packing list §9 gates against, and no human can audit *what* a run intended to read.
   **Amended 2026-08-03 (Task 5, structural finding — not a judgement call):** prepared parameters are NOT part of this table and never can be. Two independent proofs: (a) `prepare_run` is keyed on `run_id`/`business_dt` (`runprep.py:831`), so parameters are *run*-scoped while this table is `generation_id PRIMARY KEY` — one row could hold only one run's set; (b) 1034's append-only guard function has no escape branch, so a nullable column reserved for G-2 could never be filled by an UPDATE. G-2 needs its own run-keyed table. **1055 stays reserved for G-3's active-revision pointer; 1056 is numerically free but claiming it requires appending to the Track-1-owned D7 reservation table in the same commit — a coordination step, not a unilateral one.**
3. A readable run-evidence channel — §1's staging-manifest reader, needed by the reconciler; no migration (reads disk, writes the existing `materialization_run_manifest`).

**Stay deferred, with the consequence stated in `DEFERRED-WORK`** (not silently): atomic multi-write publish (A-head `:21`); the outbox/reconciliation generalization (`:22`); content-addressed inputs (A.1 `:38` — "one of the reasons Spec A publishes to sandbox"); the full run state machine (`:20`); the publish pointer (A.26 `:484`); recording actually-read partitions (A.31). Each keeps its trigger; none is required for a first end-to-end path that stops at "staged, publication refused for unproven capability".

---

## 4. Scope: three stages, and only the first is proposed for approval now

The charter asks for trigger → published. **That cannot ship as one release**, because publish does not exist and its precondition is a live cluster probe that requires per-action approval. Proposed staging:

- **G-1 — the spine (this plan's implementation scope).** Trigger → resolve → admit → compile → Gate 2 → contract/plan → bind → render → seal → **L0** → durable run record, terminating truthfully at `PUBLICATION_REFUSED (CAPABILITY_UNPROVEN)`. Entirely local Python; **no cluster, no submission, no live anything**. This is the piece that has never existed and it is independently valuable: it makes a governed feature produce a verified, sealed, auditable Kedro project on demand.
- **G-2 — execution.** `prepare_run` → L1 → submit → reconcile from staging manifests → run manifest. Requires the cross-catalog `check_run_parameters` fix (P1 below) and a **user-approved** local-Spark smoke; cluster submission is a separate approval.
- **G-3 — publication.** `probe_publication_capability`, the metastore write seam, the pointer/active-revision record (migration 1055), atomic-publish decisions. Gated on a live probe and on the governance decision in §3.4/§3.5.

**Prerequisite bugs found while scouting** (all Phase-G-owned, all in `materialize/`): **P1** `check_run_parameters` rejects `params:bridge_predicate_values`, making cross-catalog runs unsubmittable (`submit.py:115-116`); **P2** both entry points hard-code `ExecutionTier.PRODUCTION`; **P3** gate outcomes are only reachable as a stderr tail (mitigated in G-2 by reading manifests instead of parsing text); **P4** no durable path into admission (T2 below). P1/P2 land in G-1 as small fixes with tests; P3's mitigation is G-2.

---

## 5. Task list (G-1), with ownership and gates

Every task: TDD, implementer→adversarial-reviewer cycle, commit at each green boundary, named-suite count gates (never repo-wide), no silent command over 4 minutes. All files below are **Phase-G-owned** unless marked.

| # | Task | Files | Notes |
|---|---|---|---|
| 1 | Migration **1053** `materialization_request` + store | `db/migrations/1053_*.sql`, `materialize/request_store.py` | legacy-replay-tested against seeded pre-migration rows |
| 2 | **Resolution seam**: durable id → `ResolvedFeatureInput` | `materialize/resolve.py` (+ read-only use of `formula/replay_authoring.py`) | the P4 blocker; decides between restoring via the 1022 replay lane vs persisting at authoring time — **argued in the task brief, not improvised**; must preserve admission's "THE intent, not a look-alike" invariant |
| 3 | `compile/chain.py` — the orchestrator, stages 1→seal, pure functions + explicit refusal branching | `materialize/compile/chain.py` | A.24's reserved home; `compile/__init__.py` stays import-free (`identity.py:52` cycle) |
| 4 | Kedro `nodes` assembly incl. the join gate | `materialize/compile/chain.py` or a `wiring.py` sibling | nothing in `src/` does this today; the test fixture omits the join gate |
| 5 | Migration **1054** compile-artifact retention + writer/reader | `db/migrations/1054_*.sql`, `materialize/control_plane.py` (additive) | §3.6 item 2 |
| 6 | L0 in the chain + the truthful terminal (`PUBLICATION_REFUSED`) | `materialize/compile/chain.py` | never append `PUBLISHED` (§3.5) |
| 7 | Queue lane + worker stage + outbox producer | `runtime/queue.py`, `runtime/worker.py` (additive, Track-1-adjacent — flag in report) | copy `recipe_formula_shadow` shape incl. lease + fence |
| 8 | Routes: `POST /materialization-runs`, `GET /materialization-runs/{id}` | `api/routes/materialization_runs.py`, `api/app.py` (**shared-risk: additive `include_router` appended at end, away from `profiles`**) | 202 + run-id header; status folds `run_status` |
| 9 | Flag + interlock, default OFF (D8 style) | the new modules, `.env.example`, `deploy/kind/k8s/20-backend.yaml` as `"0"` | flag-off ⇒ routes 404 and every existing payload byte-identical |
| 10 | P1 + P2 fixes with tests | `materialize/submit.py`, the two entry points | small, independent |
| 11 | Acceptance e2e | `tests/featuregen/api/test_materialization_e2e.py` | template: `tests/featuregen/api/test_full_ingestion_e2e.py` |
| 12 | Deferral bookkeeping | `docs/DEFERRED-WORK.md` | §3.6's "stay deferred, consequence stated" list + the §3.4 identity note |

**Seam parameter for Track 1** (no task of its own; lands in T3): `decision_pins: DecisionPinSet | None = None`, keyword-only, modeled exactly on the existing `bridge_authorization: BridgeExecutionAuthorization | None = None` (`runprep.py:842`, refusal logic `:859-880`). Its no-op form is free today: Track 1's reserved vocabulary already filters to empty by construction (`feature_metadata_snapshot.py:72-83`), so Phase G runs without Release B and consumes pins when they land. **`materialize/` will not import `feature_metadata_snapshot.py`** — the projection happens in the wiring layer above both, per D6 Seam 4.

**Migrations: 1053 and 1054 used; 1055 reserved** for G-3's active-revision pointer. Three is enough; no coordination needed.

**Acceptance (G-1).** One feature, one fixture catalog, flag ON: `POST` → 202 → worker claims → resolve → admit → compile → Gate 2 → plan → render → seal → L0 green → terminal `PUBLICATION_REFUSED (CAPABILITY_UNPROVEN)`; `GET` returns that status; the request row, event stream, retained plan/contract and validation report are all readable afterwards, and every stage's decision is visible in the run's lineage. Flag OFF: routes 404, existing payloads byte-identical.

---

## 6. What this plan deliberately does not do

Merge any trees (Track 1 owns that). Touch `analysis/**`, `data_agent/**`, `overlay/upload/**`, `api/routes/analysis.py`, or the policy stores. Reshape `runprep.py` or `identity.py` (additive only; `identity.py` is byte-identical across both trees and will stay so). Submit anything to a cluster, deploy, upload a catalog, or call a live LLM. Claim a feature was published.

---

## 7. Decisions taken (user, 2026-08-03)

**Q1. Baseline — REBASE ONTO `main`. Done.** `feature/phase-g` replayed onto `f3424c36`; now 0 behind / 1 ahead. The charter's "branch from the codegen branch, not main" is superseded: main already carries Track 2.

**Q3. Scope — G-1 ONLY, approved for implementation.** G-2 (execution) and G-3 (publication) stay designed-but-unapproved; each needs its own approval, and G-2's first cluster contact — including a local-Spark smoke — is a separate per-action approval.

**Q4. Authorization — PLATFORM-ADMIN.** Release 1 gates on `require_confirmer` (the raw platform-admin claim, `deps.py:81-91`), not `feature:generate`. Relaxation is a later, deliberate act.

### Still open — not blocking G-1, but owed

**Q2. Sequencing (D12.8).** Unanswered. The controlling doc still sequences Phase G after Release B; this handoff plausibly amends that, but the amendment is the Track-1 session's to record. Phase G proceeds on the handoff's authority and does not edit the controlling doc.

**Q5. Ownership of the resolution seam (`formula/`).** Unanswered, so **T2 takes the ownership-safe route by default**: the new `materialize/resolve.py` *reads* the existing replay-lane restorer (`replay_authoring._restore_terminal_result`, `replay_authoring.py:179`) and the 1022 trace store; it does **not** modify anything under `formula/`. If the implementer finds that route cannot preserve admission's "THE intent, not a look-alike rebuilt from its parts" invariant (`admission.py:115-117`) without a `formula/`-side change, the task stops as BLOCKED and returns here rather than reaching across an unassigned ownership line.
