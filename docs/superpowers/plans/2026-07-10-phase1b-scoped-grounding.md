# Phase 1B — Scoped Grounding Live — Implementation Plan (v2)

> **For agentic workers:** REQUIRED SUB-SKILL: subagent-driven-development. Steps use `- [ ]` checkboxes.
> v2 folds in the Head-of-Architecture review: applicability computed once, recognition decoupled from generation runs, normalized scope storage, scope persistence in the API layer, versioned/timestamped stage evaluations.

**Goal:** Turn the shadow recognizer into a real, human-confirmed scoping step: recognise the objective → confirm at Gate #1 → compute applicability **once** into a concrete recipe subset → ground that subset → show a per-stage disposition lens. All behind flags that default to today's ground-everything behaviour.

**Architecture:** `Intent → Recognition Attempt → (human confirms) → Generation Run → Confirmed Scope → ApplicabilityResult → Grounding → Disposition`. `POST /contract/recognitions` runs the recognizer and persists an append-only attempt (no generation run yet). The UI confirms/overrides/broadens. `POST /contract/considered-set` mints the **generation run**, persists the confirmed scope (API layer), computes the `ApplicabilityResult` once, and `build_considered_set` grounds that subset and attaches dispositions. Definition-mode bypasses recognition/applicability (never safety/provenance/PIT).

**Tech stack:** Python 3.11 / FastAPI (`api/routes/contract.py`), psycopg + SQL migrations (`db/migrations/`), React + `frontend/src/api.ts` + `WorkbenchScreen.tsx`. Reuses Phase-1A `recognize`, `scope_from_recognition`, `in_scope_recipes`, `ConfirmedScope`, `ScopeExpansion`.

**Gating precondition:** Do NOT start until the Phase-1A **real-LLM shadow run** clears the exit bar (zero false-narrowing on the regulated gold subset; recall at target) and the gold set has had human review.

## Global Constraints

- **Flags default OFF → today's behaviour exactly.** `intent_scoped_applicability` off → `build_considered_set` grounds `ALL_TEMPLATES` unchanged; `intent_confirmation_ui` off → the frontend never calls `/contract/recognitions`. Prove flag-off is byte-identical.
- **Recognition is decoupled from generation.** A recognition attempt exists independently of any generation run; the **generation run is minted only when the user commits to generate** (the considered-set call), which references the confirmed scope. Re-recognising does NOT create runs.
- **Canonical linkage: `generation_run → scope_id`.** Each run has exactly one governing scope (`confirmed_generation_scope.generation_run_id` is UNIQUE); look it up by run id via `scope_for_run(run_id)`. `supersedes_scope_id` is lineage/history only — NEVER derive the governing scope from "latest" or from supersession.
- **Applicability is computed ONCE.** A single `applicability_result(scope) → ApplicabilityResult` yields the eligible recipe subset + a per-recipe relationship; grounding and disposition both consume that object. Grounding never rescans `ALL_TEMPLATES` to ask "is this applicable?". (This is also the scale story as the library grows past 153.)
- **Exactly one applicability decision per recipe** — every recipe is classified into exactly one of `{primary, supporting, out_of_scope}` before grounding. Grounding only evaluates the non-`out_of_scope` recipes.
- **Scope persistence lives in the API layer.** `build_considered_set` receives an already-persisted `ConfirmedScope` and stays computation-only (it still writes its own considered-set snapshot, as today). It does NOT own scope-record lifecycle.
- **Proposals AND choices are retained.** The recognizer's candidates persist on the attempt; the accepted use-cases persist as child rows with an `origin` (`llm_proposed`/`user_added`), linked by `recognition_id` — so the proposed-vs-accepted delta is queryable.
- **Fail-open asymmetry.** `unscoped` / `TECHNICAL_FAILURE` / an empty confirmed scope → full grounding; recognition never blocks generation, never returns 5xx.
- **The intent stays immutable** (`contract_intent` unchanged). Recognition attempts and confirmed scopes are separate append-only records with idempotency keys.
- **Recognition never sees catalog columns** (Phase-1A invariant). **Definition-mode** bypasses recognition + applicability but NOT grounding/safety/provenance/PIT.
- **Version the evidence.** Each recognition attempt stamps the version quintet; each stage evaluation stamps its `evaluation_version` (the mapping/taxonomy version it ran under) + `evaluated_at` (server clock) for replay.
- **use-case only.** Ranking/presentation-priority, contextual policy, and the `modelling_context`/`entity_context` dimensions are **Phase 2**.

---

## Task 1: DB — recognition + normalized confirmed-scope tables

**Files:** Create `src/featuregen/db/migrations/0974_intent_scope_records.sql`; Test `tests/featuregen/db/test_migration_0974.py`.

**Produces three append-only tables:**
- `intent_recognition_attempt` — `recognition_id (pk)`, `intent_id`, `input_hash`, `status`, `candidates jsonb` (the recognizer's PROPOSALS), `ambiguity_note`, `taxonomy_version`, `applicability_mapping_version`, `recognizer_model_id`, `prompt_version`, `recipe_registry_version`, `created_at`, `created_by jsonb`, `UNIQUE(intent_id, input_hash)` (idempotent — same intent+redacted input → same attempt). **No `generation_run_id`** (recognition precedes generation).
- `confirmed_generation_scope` — `scope_id (pk)`, `intent_id`, `generation_run_id`, `recognition_id (nullable fk)`, `supersedes_scope_id (nullable)`, `expansion`, `scope_mode ('scoped'|'unscoped')`, `confirmation_source`, `confirmed_by`, `confirmed_at`, `UNIQUE(generation_run_id)` (one governing scope per run — the canonical linkage). No `primary_use_case`/`secondary jsonb`.
- `confirmed_scope_use_case` (child) — `scope_id (fk)`, `use_case_id`, `relationship ('primary'|'secondary')`, `origin ('llm_proposed'|'user_added'|'user_overridden')`, `display_order`, `PK(scope_id, use_case_id)`. One row per confirmed use-case (normalized; queryable; extensible).

- [ ] **Step 1: Failing test** — apply migrations; assert the three tables + their columns + the two UNIQUE constraints + the child FK; assert a duplicate `(generation_run_id)` scope INSERT is rejected and a duplicate `(intent_id, input_hash)` attempt INSERT is idempotent/rejected.
- [ ] **Step 2–4:** write the migration (WORM/RLS grants per `0971_worm_truncate_revoke.sql`) to green.
- [ ] **Step 5: Gates + commit** `feat(1b): recognition + normalized confirmed-scope tables (task 1)`.

---

## Task 2: Scope-record persistence

**Files:** Create `src/featuregen/overlay/upload/contract/scope_records.py`; Test `tests/featuregen/overlay/upload/contract/test_scope_records.py`.

**Produces:**
- `record_recognition_attempt(conn, *, intent_id, input_hash, result: RecognitionResult, actor) -> str` — INSERT (idempotent on `(intent_id, input_hash)`), stamps the version quintet + `candidates` from the result; returns `recognition_id`.
- `record_confirmed_scope(conn, *, intent_id, generation_run_id, recognition_id, scope: ConfirmedScope, use_case_origins: dict[str,str], confirmation_source, confirmed_by, supersedes_scope_id=None) -> str` — writes the parent + one `confirmed_scope_use_case` child per primary/secondary (with `origin`, `display_order`).
- `scope_for_run(conn, generation_run_id) -> ConfirmedScope | None` — the governing scope for a run (parent + children → `ConfirmedScope`), by run id only.

- [ ] **Step 1: Failing test** — round-trip an attempt (quintet stamped, candidates preserved); round-trip a scope with a primary + a secondary → two child rows with correct `relationship`/`origin`/`display_order`; `scope_for_run` reconstructs the exact `ConfirmedScope`; the proposed-vs-accepted delta is derivable (attempt.candidates vs child rows via `recognition_id`); duplicate `(generation_run_id)` rejected.
- [ ] **Step 2–4:** implement to green.
- [ ] **Step 5: Gates + commit** `feat(1b): scope-record persistence (task 2)`.

---

## Task 3: `ApplicabilityResult` — compute applicability once

**Files:** Modify `src/featuregen/overlay/upload/taxonomy/applicability.py`; Test extends `test_applicability.py`.

**Produces:**
- `@dataclass(frozen=True) class ApplicabilityResult: by_recipe: dict[str, str]` (every recipe id → exactly one of `"primary"|"supporting"|"out_of_scope"`), plus `eligible_ids: frozenset[str]` (primary ∪ supporting) and `reason_codes: dict[str, tuple[str,...]]`.
- `def applicability_result(scope: ConfirmedScope) -> ApplicabilityResult` — one pass over `ALL_TEMPLATES` using the existing `in_scope_recipes` logic; asserts the exactly-one invariant (every recipe classified). `unscoped` → all recipes `primary`.

- [ ] **Step 1: Failing test** — every recipe id in `ALL_TEMPLATES` appears exactly once in `by_recipe`; a churn scope → churn recipes `primary`, credit/fraud `out_of_scope`; `eligible_ids == primary_scoped | supporting_scoped` from `in_scope_recipes`; `unscoped` → all `primary`, none `out_of_scope`.
- [ ] **Step 2–4:** implement to green (reuse `in_scope_recipes`; this is the single source of truth downstream consumes).
- [ ] **Step 5: Gates + commit** `feat(1b): ApplicabilityResult — one decision per recipe (task 3)`.

---

## Task 4: Wire scoped grounding into `build_considered_set`

**Files:** Modify `src/featuregen/overlay/upload/contract/gate1.py`; Test `tests/featuregen/overlay/upload/contract/test_gate1_scoped.py`.

**Produces:** `build_considered_set(..., applicability: ApplicabilityResult | None = None)` — when `intent_scoped_applicability` is on, `applicability` is supplied, its scope is not `unscoped`, and the intent is hypothesis-mode, `_template_candidates` grounds only the `applicability.eligible_ids` template subset (the existing signature already takes an explicit template list — do NOT use `use_case=`). Otherwise ground `ALL_TEMPLATES` (today). The builder does NOT persist the scope (the API already did). `ConsideredSet` carries the `ApplicabilityResult` through for the disposition stage.

- [ ] **Step 1: Failing tests** — flag off / `applicability=None` → alternatives identical to today (existing churn fixture); flag on + churn `ApplicabilityResult` → only churn candidates ground, credit/fraud absent; `unscoped` → full grounding; definition-mode → grounding/anchor unchanged (bypass) but safety still applied; the builder writes NO scope row.
- [ ] **Step 2–4:** implement; replace the log-only `_shadow_recognition` hook with this path. Run the FULL overlay/contract/governance suite (flag-off neutrality).
- [ ] **Step 5: Gates + commit** `feat(1b): scoped grounding via ApplicabilityResult (task 4)`.

---

## Task 5: Per-stage disposition model (consumes grounded results)

**Files:** Create `src/featuregen/overlay/upload/taxonomy/disposition.py`; Test `test_disposition.py`.

**Produces:** `StageStatus{COMPLETED,FAILED,NOT_EVALUATED}`; `FinalDisposition{OUT_OF_SCOPE,UNBUILDABLE,SAFETY_REJECTED,ELIGIBLE}`; `RecipeEvaluation{recipe_id, applicability, grounding, safety, final_disposition, relevance_tier}` where each stage carries `{status, reason_codes, evaluation_version, evaluated_at}`; `evaluate_dispositions(result: ApplicabilityResult, grounded_ids, rejected, *, evaluation_version, now) -> list[RecipeEvaluation]`. Consumes the SAME `ApplicabilityResult` (no recompute). An `out_of_scope` recipe → downstream stages `NOT_EVALUATED` (never bare null). Ranking is an *attribute* added in Phase 2 — NOT a disposition.

- [ ] **Step 1: Failing test** — out-of-scope → `applicability.decision=out_of_scope`, `grounding.status=NOT_EVALUATED`; eligible+grounded → `ELIGIBLE`; in-scope+failed-safety → `SAFETY_REJECTED`; in-scope+not-grounded → `UNBUILDABLE`; every stage carries `evaluation_version`+`evaluated_at`; `unscoped` → no recipe `out_of_scope`.
- [ ] **Step 2–4:** implement to green.
- [ ] **Step 5: Gates + commit** `feat(1b): per-stage disposition model (task 5)`.

---

## Task 6: `POST /contract/recognitions` endpoint

**Files:** Modify `src/featuregen/api/routes/contract.py`; Test `tests/featuregen/api/test_contract_recognitions.py`.

**Produces:** `POST /contract/recognitions` (body `hypothesis`, `objective`) → submits/loads the intent (idempotent), redacts, `recognize(conn, client, …)`, `record_recognition_attempt`, returns `{intent_id, recognition_id, status, candidates:[{use_case_id, display_name, relationship, confidence, evidence_spans}], unscoped}`. **No `generation_run_id`, no `proposed_in_scope_count`** — recognition returns recognised use-cases only; applicability owns any recipe count (computed later, after generate).

- [ ] **Step 1: Failing test** (FakeLLM + db) — a classified hypothesis returns the proposal + `recognition_id` and writes an attempt row (no run row); an unscoped hypothesis → `unscoped=true`; a recognizer failure → `status=technical_failure`, HTTP 200 (fail-open, never 5xx); the response has no run id or recipe count.
- [ ] **Step 2–4:** implement to green.
- [ ] **Step 5: Gates + commit** `feat(1b): /contract/recognitions endpoint (task 6)`.

---

## Task 7: Extend `POST /contract/considered-set` — mint run, persist scope, generate

**Files:** Modify `src/featuregen/api/routes/contract.py`; Test `tests/featuregen/api/test_contract_scoped.py`.

**Produces:** the endpoint accepts optional `intent_id`, `recognition_id`, `confirmed_scope:{primary, secondary, expansion, unscoped, use_case_origins, confirmation_source}`. When present: **mint a `generation_run_id`**, `record_confirmed_scope(... generation_run_id, recognition_id ...)` (API layer, BEFORE the builder), compute `applicability_result(scope)`, call `build_considered_set(... applicability=…)`, then `evaluate_dispositions` and return `{... dispositions, in_scope_count}`. When absent → today's `(hypothesis, objective)` path unchanged. **Broaden** = client re-calls with `confirmed_scope.unscoped=true`, a NEW `generation_run_id` server-side, and `supersedes_scope_id` = the prior scope → a fresh unscoped run + snapshot; persistence keeps both.

- [ ] **Step 1: Failing test** — a scoped call mints a run, persists a scope (+ child rows) before generation, returns fewer candidates + a disposition lens + `in_scope_count` (from applicability, not recognition); a broaden call persists a second scope superseding the first under a new run and returns full grounding; the no-scope call is byte-unchanged; `scope_for_run(run)` returns the governing scope.
- [ ] **Step 2–4:** implement to green.
- [ ] **Step 5: Gates + commit** `feat(1b): considered-set mints run + persists scope + broaden (task 7)`.

---

## Task 8: Gate #1 confirmation UI + disposition lens

**Files:** Modify `frontend/src/api.ts`, `frontend/src/screens/WorkbenchScreen.tsx`; Test `WorkbenchScreen.test.tsx` (vitest).

- [ ] **Step 1:** `api.ts` — `contractRecognitions(hypothesis, objective)` → `POST /contract/recognitions`; extend `contractConsideredSet` to pass `intentId?`, `recognitionId?`, `confirmedScope?` (incl. `useCaseOrigins`).
- [ ] **Step 2:** `WorkbenchScreen` (behind `intent_confirmation_ui`) — on generate, first call `contractRecognitions`, render the proposed scope (primary + secondary + evidence spans + confidence) with **confirm / remove-secondary / change-primary / broaden ("show all buildable")**, and an "include all sub-use-cases?" toggle (`EXACT` vs `INCLUDE_DESCENDANTS`) when a broad parent is chosen. Track each confirmed use-case's `origin`. Then call `contractConsideredSet` with the confirmed scope.
- [ ] **Step 3:** render the **disposition lens** (behind `intent_disposition_lens`) — group results: *Recommended/eligible*, *Relevant but missing data*, *Rejected by safety*, *Outside confirmed scope* — each recipe showing its reason; broaden one click away.
- [ ] **Step 4:** flag OFF → the screen behaves exactly as today (one-shot generate, no recognitions call). Assert in a test.
- [ ] **Step 5:** `npm run typecheck` + `npx vitest run` + `npm run lint`; commit `feat(1b): Gate #1 confirmation UI + disposition lens (task 8)`.

---

## Task 9: Feature flags + rollback + neutrality proof

**Files:** Modify the flag/config module (follow `_auth_stub_enabled` in `api/deps.py`); Test `tests/featuregen/api/test_1b_rollout.py`.

- [ ] **Step 1: Failing test** — all three flags off → considered-set byte-identical to pre-1B (same alternatives, no recognition row, no scope row); **emergency rollback** (disable `intent_scoped_applicability` only) → full grounding while `intent_recognition_attempt` rows still write (telemetry retained).
- [ ] **Step 2–4:** implement the three flags (default off) to green.
- [ ] **Step 5: Gates + commit** `feat(1b): 1B feature flags + emergency rollback (task 9)`.

---

## Self-review
- **Applicability once:** Tasks 3→4→5 share one `ApplicabilityResult`; grounding never rescans (scale note satisfied); exactly-one decision per recipe asserted.
- **Recognition decoupled:** run minted only at generate (Task 7); attempts idempotent on `(intent_id, input_hash)` (Task 1).
- **Canonical linkage:** `generation_run → scope_id` unique; `scope_for_run` by run id; supersedes = lineage only.
- **Separation:** scope persisted in the API (Task 7), builder computation-only (Task 4).
- **Auditability:** proposals on the attempt + accepted child rows w/ origin → delta queryable; each stage stamps `evaluation_version`+`evaluated_at`.
- **Neutrality/fail-open/immutability/definition-mode** covered per the global constraints; flags default off (Tasks 4/7/9).
- **Deferred to Phase 2 (stated, not built):** ranking, contextual policy, `modelling_context`/`entity_context`.
