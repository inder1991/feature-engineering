# Run Spine Actionable Stage I — REVISED Implementation Plan (rev 2, Option B)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The run page becomes actionable and honest: its stage rail derives every reason from reality, its history survives retries, and its one trigger — start formula authoring for chosen candidates — flows through the existing job coordinator under full governance (authorization, decision, spend), with the blessed retry-after-failure reachable end to end.

**Architecture:** Option B (spec §R4.1): `code_generation_job` is the domain journey aggregate; the spine derives from it, links to it, and triggers through it. Zero new migrations — every table this plan touches exists at the baseline. Two tasks are SUBSTRATE-EXECUTED (the other session builds, this session reviews) because they live inside files carrying that lane's invariants.

**Tech Stack:** Python/FastAPI/psycopg · React/TS/Vitest.

**Spec:** `docs/superpowers/specs/2026-08-22-feature-run-spine-design.md` **revision 4** (`a819294d`) — §R4.1–R4.4 are the operative sections; §§1–15 as amended by the supersession pointers.

## Global Constraints

- **Baseline:** `feature/asset-detail-reapply` at `2a03a77b` or later (the frozen `e5c4f581` + the substrate's defect fixes — the journey test relies on the typed `FORMULA_DRAFT_NOT_AN_ANSWER` 409). Execution in a NEW worktree branched from the then-current tip, coordinated with the substrate session (it remains active on this branch). `git add <exact path>` only; never `-A`; never `git stash`.
- **Zero new migrations.** 1117 stays reserved-unused. If any task believes it needs DDL, it stops and reports — that is a plan defect, not an improvisation site.
- **Ownership split (spec §R4.4, agreed with the substrate session, one owner flag pending):** Tasks 5 and 6 are SUBSTRATE-EXECUTED — dispatched to `feature-engineering-4b` as briefs, reviewed by this session before merge. All other tasks touch only run-spine-owned files (`src/featuregen/runs/*`, `api/routes/feature_runs.py`, `frontend/src/screens/Run*`, `frontend/src/api.ts` additions) plus tests.
- **Owner gates inside this plan:** Task 0 (approve spec rev 4 + this plan — one pass); Task 6's design ruling (deterministic-lane retry shape).
- The §11 read/trigger policy stands: owner or `platform_admin`; 404 hides denial; PRE_SPINE runs are never actionable (`PRE_SPINE_NOT_ACTIONABLE`).
- Reason codes are closed-vocabulary; any new code is the full multi-part commit (`semantic_eligibility_reasons.py` + `action_dispositions.py` all-six-actions row + evaluator registry if emitted + the emitted-count literal). This plan adds exactly one: `PRE_SPINE_NOT_ACTIONABLE`.
- API tests never `conn.commit()`. Baselines at plan time: backend 13480/20/0 (frozen-SHA full run), frontend 1051 — re-measure at Task 1.

---

### Task 0: Approval gate + baseline pin (OWNER)

- [ ] **Step 1:** Owner approves spec rev 4 (`a819294d`) and this plan in one pass. The two owner flags to confirm explicitly: (a) Tasks 5/6 substrate-executed with run-spine review; (b) the Task 6 design question will be answered when Task 6 starts, not now.
- [ ] **Step 2:** Pin the execution baseline: `git -C <worktree> rev-parse HEAD` recorded in the ledger; full suites re-run once against it; counts recorded as the regression reference.
- [ ] **Step 3:** Create the execution worktree from that SHA; confirm with the substrate session that its in-flight work (if any) does not touch `src/featuregen/runs/`, `api/routes/feature_runs.py`, or the two Run screens for the plan's duration.

---

### Task 1: Derived socket reasons (spec §R4.3)

**Files:**
- Modify: `src/featuregen/runs/projection.py` (`_SOCKETS` literal at `:90-98` dies; derivation replaces it)
- Test: `tests/featuregen/runs/test_projection_detail.py`

**Interfaces:**
- Consumes: `action_available(action: ActionV1) -> bool` (`materialize/action_authorization.py:79` — False exactly for the two production acts); `verification_enabled()` (find its definition the way `generation_enabled` was found — the switch the verification route/worker read; if none exists, the worker-tick gate at `runtime/worker.py:623-639` names the authoritative check — read it first); `_EXECUTOR is None` is NOT importable state — the honest derivable facts are the deployment switch and, when on, the posture-named FAILED outcomes; derive from the switch only and keep the reason code accurate.
- Produces (rail entries, replacing three false codes):
  - `EXECUTE_SANDBOX`: switch off → `UNAVAILABLE`/`VERIFICATION_DISABLED`; switch on → `NOT_STARTED` (the §9.0 lane exists; a claimed run may still FAIL with the posture named — that is the attempt's honest outcome, not unavailability).
  - `MATERIALIZE_PRODUCTION`/`PUBLISH_PRODUCTION`: `UNAVAILABLE`/`ACTION_UNAVAILABLE` derived from `action_available(...)` — never a stored string.
  - `PUBLISH_SANDBOX`: unchanged `WORKER_NOT_IMPLEMENTED` (still true). `TRAIN_MODEL`: unchanged `SUBSYSTEM_NOT_BUILT`.

- [ ] **Step 1: Failing tests** — extend the socket test: assert the five entries' `{stage: (state, reason_code)}` map under (a) all switches off (default): EXECUTE_SANDBOX `UNAVAILABLE`/`VERIFICATION_DISABLED`, both production `UNAVAILABLE`/`ACTION_UNAVAILABLE`, PUBLISH_SANDBOX/TRAIN_MODEL unchanged; (b) verification switch on (monkeypatch the same env/setting the worker gate reads): EXECUTE_SANDBOX `NOT_STARTED`/None. Mutation check in-test comment: a revert to the static tuple fails (a).
- [ ] **Step 2:** Red → implement (mirror `_generate_preview_stage`'s switch-first shape; one function `_sandbox_stage(conn)` + inline derivations for the production pair) → green; `uv run pytest tests/featuregen/runs -q`.
- [ ] **Step 3:** Commit: `feat(runs): socket reasons derive from reality — three false codes die`

---

### Task 2: History vs current — the fold that survives retries (spec §R4.4.1, owner P1)

**Files:**
- Modify: `src/featuregen/runs/projection.py` (`run_detail`'s authoring query + fold), `frontend/src/screens/RunDetailScreen.tsx` (render both readings)
- Test: `tests/featuregen/runs/test_projection_detail.py`, `frontend/src/screens/RunDetailScreen.test.tsx`

**Interfaces:**
- The premise: 1107 allows many drafts per `formula_identity_hash`; the per-candidate question splits into two readings. Group drafts by candidate `(considered_revision_id, option_id)`:
  - `authoring_history`: every draft row, ordered `requested_at`, each with `state`, `rail_state`, `eligibility` (existing two-axis derivation kept per row);
  - `authoring_current`: per candidate, THE current answer — the unique non-terminal draft when one exists (1107 guarantees at most one), else the most recent terminal row flagged `"resolved": false`.
- `run_detail` output change: `authoring` becomes `{"current": [...], "history": [...]}` — a breaking shape change to the detail dict; `frontend/src/api.ts` types follow; the rail's `AUTHOR_FORMULA` worst-of fold reads **current only** (an old BLOCKED attempt can no longer shadow a later success — the exact defect the owner named).

- [ ] **Step 1: Failing tests** — seed one candidate with a FAILED draft then (1107 path) a READY draft for the same identity: assert `current` shows READY/`SUCCEEDED`, `history` shows both rows in order, and the RAIL folds to `SUCCEEDED` (this is the mutation that dies: fold-over-history returns `FAILED`). Frontend: history table renders both rows; the current row is visually primary; no trigger buttons appear from this task.
- [ ] **Step 2:** Red → implement → green (runs suite + both screen tests + `tsc -b`).
- [ ] **Step 3:** Commit: `feat(runs): attempt history and the current answer are two readings`

---

### Task 3: BIND_SELECTIONS accumulating + switch-precedence test (parked debts)

**Files:** `src/featuregen/runs/projection.py`, `tests/featuregen/runs/test_projection_detail.py`

- [ ] **Step 1: Failing tests** — (a) with 2 bindings over a run whose choices count 5, the milestone reads `state IN_PROGRESS` with `detail "2 of 5 bound — accumulating"`; never `SUCCEEDED` while bindings < choices; `SUCCEEDED` only when counts equal and > 0 (the denominator is the run's choice count — the only frozen-set-free honest denominator; when choices are 0 and bindings exist, render the count with no denominator). (b) The precedence cell: switch OFF + pin ABSENT (delenv + `_drop_the_pin`) → `GENERATE_PREVIEW` reads `GENERATION_DISABLED` (the branch-swap mutant dies).
- [ ] **Step 2:** Red → implement → green.
- [ ] **Step 3:** Commit: `feat(runs): binding milestone counts honestly; switch precedence pinned`

---

### Task 4: The run→job trigger bridge (spec §R4.4.2)

**Files:**
- Modify: `src/featuregen/api/routes/feature_runs.py` (one new POST), `src/featuregen/runs/projection.py` (jobs surfaced on the detail), `frontend/src/api.ts`, `frontend/src/screens/RunDetailScreen.tsx`
- Test: `tests/featuregen/api/test_feature_run_trigger.py`, screen test

**Interfaces:**
- Consumes (read these before writing — the maps are the guide, the files the authority): `api/routes/code_generation_jobs.py`'s job-creation entry (its request contract, how it takes considered revision + option ids, its idempotency) and `materialize/code_generation_coordinator.py`'s public seam; the §11 policy helpers already in `feature_runs.py`.
- Produces:
  - `POST /feature-runs/{run_id}/prepare-code` — body `{"option_ids": [...]}` ONLY. The server: §11 object policy (owner/admin; 404 hides denial) → **`PRE_SPINE_NOT_ACTIONABLE` 409 when the run has no `feature_run_identity` row** (the new reason code, full multi-part vocabulary commit) → resolves the run's considered revision server-side → delegates to the SAME job-creation path the code-generation route uses (import the shared service function; if only route-level code exists, extract the shared function into the coordinator module as part of this task — flag to the substrate session before touching `code_generation_jobs.py`) → returns the job id + the run detail's refreshed `state`.
  - `run_detail` gains `jobs: [{job_id, requested_action, status, requested_at}]` derived via the considered-revision bridge (read-only join; the job store's status is authoritative and rendered verbatim).
  - Frontend: candidates in `authoring_current` with no draft get checkboxes + ONE `Prepare formulas` button (onClick only); PRE_SPINE runs render the disabled control with the server's sentence; job rows link to `#/code-generation` when `VITE_CODE_GENERATION` is on, else render inline.

- [ ] **Step 1: Failing tests** — owner triggers 2 candidates → 202, a job row exists, the run detail lists it; non-owner → 404 byte-identical to absent; PRE_SPINE run → 409 `PRE_SPINE_NOT_ACTIONABLE`; replay of the same gesture → the job path's own idempotency answer (assert no second job); `data_owner` → 403 (`feature:generate` gate on the POST only).
- [ ] **Step 2:** Red → implement → green; `uv run pytest tests/featuregen/api -q` once.
- [ ] **Step 3:** Commit: `feat(runs): the run page prepares code through the job coordinator`

---

### Task 5 (SUBSTRATE-EXECUTED): AUTHOR_FORMULA governance at the service seam (spec §R4.4.3)

Dispatched as a brief to `feature-engineering-4b`; this session reviews the diff before merge.

**Brief requirements (acceptance criteria for the review):**
- An authoring evidence-pin assembler exists (`authoring_evidence_pins(...)` beside the two existing assemblers), pinning at minimum the candidate facts hash (`retirement_scope_key`), `catalog_snapshot_hash`, and the strategy identity.
- `request_draft_for_candidate` binds, per draft, in its one transaction: `authorize_action(AUTHOR_FORMULA, resource_identity_hash=retirement_scope_key(...))` → `decide(...)` (refusal → typed member-level answer, never a 500) → the spend authorization made MANDATORY on every path that can reach the LLM lane (the HTTP route's `spend_authorization_id=None` hole closes — either the route mints the dev-envelope spend like the job path, or the route dies as §0.1.3 demands; the substrate chooses, states which, and the parent plan's bypass row is updated in the same commit).
- The decision id is persisted on `formula_draft_authoring_plan` or an equivalent durable place the worker re-reads (not only in memory) — worker refuses a missing decision at act time for governed drafts, mirroring the generation lane's shape.
- No behaviour change for the coordinator path beyond the new bindings; suites green; the §8.3 one-composition invariant and identity-V2 composition untouched in meaning.

- [ ] **Step 1:** Dispatch the brief (this plan section verbatim + file anchors). **Step 2:** Review the returned diff against the acceptance criteria; iterate once if needed. **Step 3:** Merge on green.

---

### Task 6 (SUBSTRATE-EXECUTED, OWNER-GATED): the regeneration-exception surface + the deterministic ruling

- [ ] **Step 1 (OWNER):** the design ruling recorded verbatim in the parent plan: **zero-spend exception kind vs deterministic-retry-is-free-by-construction.** Decide when this task starts.
- [ ] **Step 2:** Substrate builds §11.1's approval surface per the ruling (the exception-creator act: owner/admin, cost-confirmed, one-time-consumption semantics already in 1103). Acceptance: a production route/act exists that writes `formula_draft_regeneration_exception` under the ruling's shape; `valid_exception_for`'s three bindings respected; suites green.
- [ ] **Step 3:** Run-spine review, then merge.

---

### Task 7: The retry gesture on the run page (spec §R4.2)

**Files:** `frontend/src/api.ts`, `frontend/src/screens/RunDetailScreen.tsx` + tests; `src/featuregen/runs/projection.py` (retryability facts)

**Interfaces:**
- Depends on Tasks 2, 5, 6. `authoring_history` rows gain `retryable: bool` + `retry_blockers: [...]` derived server-side: terminal state AND a valid exception exists (`valid_exception_for`) AND spend remains — each absence a typed blocker rendered verbatim (`NO_REGENERATION_EXCEPTION`, `SpendExhausted`'s code, `DraftRetired`'s).
- The terminal row's `Retry` control (onClick only) POSTs through the SAME governed path Task 5 hardened (a new draft mint for the same candidate — the service path already handles the 1107 fresh-row semantics); refusals render the server's sentences; success refreshes the detail, where Task 2's fold shows the new attempt as current and the old as history.

- [ ] **Step 1: Failing tests** — a FAILED draft with no exception renders a disabled Retry with `NO_REGENERATION_EXCEPTION`; with a seeded exception + spend it POSTs once and the detail refresh shows two history rows and a new current; a second click while live is refused by the money guard (the 1107 index) and rendered verbatim.
- [ ] **Step 2:** Red → implement → green; full frontend suite + `tsc -b`.
- [ ] **Step 3:** Commit: `feat(runs): retry-after-failure — approved, priced, and visible`

---

### Task 8: Spec/plan corrections + journey test + full suites

- [ ] **Step 1:** Spec §15's two stale fact rows corrected (the 1107-era index row; the deleted `_authoring_config_hash` row); the frozen NO-GO plan's banner gains one line: `SUPERSEDED by 2026-08-23-run-spine-actionable-stage1-rev2.md (Option B)`.
- [ ] **Step 2:** The journey test (`tests/featuregen/api/test_run_prepare_journey.py`): seed a chain + identity → run detail (honest rail per Task 1) → prepare-code for 2 candidates → drive the worker (FakeLLM) → detail shows current READY with attempt history → force a FAILED draft → assert the typed 409 on re-request — per the substrate's stated contract, assert `code == "FORMULA_DRAFT_NOT_AN_ANSWER"` and the PRESENCE of `remedy`, never the message's exact words (the store's sentence is its own to change) → with a seeded exception, retry succeeds and history holds both.
- [ ] **Step 3:** Full suites against the final tree; counts ≥ Task 0's baseline; 0 failed; summary lines read verbatim.
- [ ] **Step 4:** STOP — no deploy. The maintenance-cutover runbook governs anything live.

---

## Self-review notes (applied)

- Spec R4 coverage: R4.3→T1 · R4.4.1→T2+T3 · R4.4.2→T4 · R4.4.3→T5 · R4.2 gaps→T6+T7 · R4.4.5→T8. Struck as already-done: `DraftNotAnAnswer` handling (substrate `2a03a77b`; asserted in T8's journey instead).
- Known execution risks, named: the job-creation shared-function extraction (T4) may need a substrate handshake; `verification_enabled`'s real name (T1 reads the worker gate first); the T2 shape change breaks `api.ts` consumers — both screens updated in the same task; T5/T6 review loops depend on the substrate session's availability.
- Deliberately absent, unchanged from §13: fork, cancel, TRAIN_MODEL rows, Stage II (preview re-execute, output binding, verification/publication mappings, route retirements beyond T5's).
