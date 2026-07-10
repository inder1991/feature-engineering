# Phase 1B — Scoped Grounding Live — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: subagent-driven-development. Steps use `- [ ]` checkboxes.

**Goal:** Turn the shadow recognizer into a real, human-confirmed scoping step: recognise the objective → confirm at Gate #1 → pass **concrete in-scope recipe IDs** to grounding → show a per-stage disposition lens. All behind flags that default to today's ground-everything behaviour.

**Architecture:** A two-call flow. `POST /contract/recognize` runs the recognizer (Phase 1A), persists an append-only recognition attempt, and returns the proposed scope + evidence. The UI lets the human confirm / override / broaden. `POST /contract/considered-set` then accepts a **confirmed scope** + a `generation_run_id`, persists an append-only confirmed-scope record, and `build_considered_set` grounds `in_scope_recipes(scope)` instead of `ALL_TEMPLATES`. Each recipe carries per-stage evaluation records + a derived disposition. Definition-mode bypasses recognition/applicability (never safety/provenance/PIT).

**Tech stack:** Python 3.11 / FastAPI (`api/routes/contract.py`), psycopg + SQL migrations (`db/migrations/`), React + `frontend/src/api.ts` + `WorkbenchScreen.tsx`. Reuses Phase-1A `recognize`, `scope_from_recognition`, `in_scope_recipes`, `ConfirmedScope`, `ScopeExpansion`.

**Gating precondition:** Do NOT start until the Phase-1A **real-LLM shadow run** clears the exit bar (zero false-narrowing on the regulated gold subset; recall at target) and the gold set has had human review.

## Global Constraints

- **Flags default OFF → today's behaviour exactly.** With `intent_scoped_applicability` off, `build_considered_set` grounds `ALL_TEMPLATES` (unchanged); with `intent_confirmation_ui` off, the frontend never calls `/contract/recognize`. Prove flag-off behaviour is byte-identical.
- **Fail-open asymmetry.** `unscoped` / `TECHNICAL_FAILURE` / an empty confirmed scope → full grounding. Recognition never blocks generation.
- **The intent stays immutable.** `contract_intent` is unchanged; recognition attempts and confirmed scopes are **separate append-only** records with `generation_run_id` + `supersedes_scope_id` lineage and idempotency keys. Never derive the governing scope with `ORDER BY confirmed_at DESC`.
- **Recognition never sees catalog columns** (Phase-1A invariant — `recognize` already enforces it).
- **Definition-mode** intake bypasses recognition + applicability narrowing but NOT grounding validation, universal safety, provenance, or PIT declarations.
- **Version quintet** persisted on every recognition attempt (`taxonomy_version`, `applicability_mapping_version`, `recognizer_model_id`, `prompt_version`, `recipe_registry_version`).
- **use-case only.** 1B scopes on the use-case dimension; `modelling_context` / `entity_context` / ranking / policy are Phase 2.

---

## Task 1: DB — append-only recognition + confirmed-scope tables

**Files:** Create `src/featuregen/db/migrations/0974_intent_scope_records.sql`; Test `tests/featuregen/db/test_migration_0974.py`.

**Interfaces — Produces:** two tables.
- `intent_recognition_attempt` — `recognition_id (pk)`, `intent_id`, `generation_run_id`, `input_hash`, `status`, `candidates jsonb`, `ambiguity_note`, `taxonomy_version`, `applicability_mapping_version`, `recognizer_model_id`, `prompt_version`, `recipe_registry_version`, `created_at`, `created_by jsonb`, `UNIQUE(intent_id, generation_run_id, input_hash)` (idempotency). No update path.
- `confirmed_generation_scope` — `scope_id (pk)`, `intent_id`, `generation_run_id`, `recognition_id (nullable)`, `supersedes_scope_id (nullable)`, `primary_use_case (nullable)`, `secondary jsonb`, `expansion`, `scope_mode ('scoped'|'unscoped')`, `confirmation_source`, `confirmed_by`, `confirmed_at`, `UNIQUE(generation_run_id)` (one governing scope per run). No update path.

- [ ] **Step 1: Failing test** — apply migrations; assert both tables exist with the columns + unique constraints; assert an `UPDATE` is not needed (append-only by convention) and the uniqueness rejects a duplicate `(generation_run_id)`.
- [ ] **Step 2: Run — expect fail.**
- [ ] **Step 3: Write the migration** (follow the style of an existing `db/migrations/*.sql`; include the WORM/RLS grants the repo applies to append-only tables — see `0971_worm_truncate_revoke.sql`).
- [ ] **Step 4: Run — expect pass.**
- [ ] **Step 5: Gates + commit** `feat(1b): recognition + confirmed-scope append-only tables (task 1)`.

---

## Task 2: Scope-record persistence

**Files:** Create `src/featuregen/overlay/upload/contract/scope_records.py`; Test `tests/featuregen/overlay/upload/contract/test_scope_records.py`.

**Interfaces — Produces:**
- `def record_recognition_attempt(conn, *, intent_id, generation_run_id, input_hash, result: RecognitionResult, actor) -> str` — INSERT (idempotent on the unique key), returns `recognition_id`.
- `def record_confirmed_scope(conn, *, intent_id, generation_run_id, recognition_id, scope: ConfirmedScope, confirmation_source, confirmed_by, supersedes_scope_id=None) -> str`.
- `def scope_for_run(conn, generation_run_id) -> ConfirmedScope | None` — the governing scope for a run (by `generation_run_id`, never latest-by-time).

- [ ] **Step 1: Failing test** — round-trip a recognition attempt (stamps the version quintet from the result) and a confirmed scope; `scope_for_run` returns exactly the scope written for that run; a duplicate `(generation_run_id)` confirmed-scope INSERT is rejected/idempotent.
- [ ] **Step 2–4:** implement to green.
- [ ] **Step 5: Gates + commit** `feat(1b): scope-record persistence (task 2)`.

---

## Task 3: The per-stage disposition model

**Files:** Create `src/featuregen/overlay/upload/taxonomy/disposition.py`; Test `tests/featuregen/overlay/upload/taxonomy/test_disposition.py`.

**Interfaces — Produces:**
- `class StageStatus(StrEnum): COMPLETED; FAILED; NOT_EVALUATED`
- `class FinalDisposition(StrEnum): OUT_OF_SCOPE; UNBUILDABLE; SAFETY_REJECTED; ELIGIBLE` (policy/ranking dispositions are Phase 2).
- `@dataclass RecipeEvaluation: recipe_id; applicability: {status, decision, reason_codes}; grounding: {status, reason_codes}; safety: {status, reason_codes}; final_disposition; relevance_tier` (`primary|secondary|None`).
- `def evaluate_dispositions(scope: ConfirmedScope, grounded_ids: set[str], rejected: list[dict]) -> list[RecipeEvaluation]` — for every recipe in `ALL_TEMPLATES`: applicability from `in_scope_recipes(scope)` (in primary/supporting → in-scope; else `out_of_scope`, downstream stages `not_evaluated`); grounding from whether it grounded / was `rejected`; safety from the reject reason-code if present. Derive the terminal disposition. Ranking is NOT a disposition.

- [ ] **Step 1: Failing test** — an out-of-scope recipe has `applicability.decision=out_of_scope` and `grounding.status=not_evaluated` (never a bare null); an in-scope recipe that grounded is `ELIGIBLE`; an in-scope recipe that failed safety is `SAFETY_REJECTED`; an in-scope recipe that didn't ground is `UNBUILDABLE`; `unscoped` scope → no recipe is `out_of_scope`.
- [ ] **Step 2–4:** implement to green.
- [ ] **Step 5: Gates + commit** `feat(1b): per-stage recipe disposition model (task 3)`.

---

## Task 4: Wire scoped grounding into `build_considered_set`

**Files:** Modify `src/featuregen/overlay/upload/contract/gate1.py`; Test `tests/featuregen/overlay/upload/contract/test_gate1_scoped.py`.

**Interfaces — Consumes:** Tasks 2–3, `in_scope_recipes`, `ConfirmedScope`. **Produces:** `build_considered_set(..., confirmed_scope: ConfirmedScope | None = None, generation_run_id: str | None = None)`; `ConsideredSet` gains `dispositions: list[RecipeEvaluation]`.

- [ ] **Step 1: Failing tests** —
  - Flag `intent_scoped_applicability` **off** (or `confirmed_scope=None`): `_template_candidates` grounds `ALL_TEMPLATES` — alternatives identical to today (reuse the existing gate1 churn fixture).
  - Flag **on** + a churn-scoped `ConfirmedScope`: `_template_candidates` grounds only `in_scope_recipes(scope)` — the credit/fraud template candidates are absent; the churn ones remain; the considered-set `dispositions` mark credit/fraud recipes `out_of_scope`.
  - `unscoped` scope (flag on) → full grounding (fail-open).
  - **Definition-mode** intent → grounding + anchor unchanged regardless of scope (bypass), but safety/provenance still applied.
  - The confirmed scope is persisted (`record_confirmed_scope`) bound to `generation_run_id`.
- [ ] **Step 2: Run — expect fail.**
- [ ] **Step 3: Implement** — replace the log-only `_shadow_recognition` hook: when `intent_scoped_applicability` is on and a `confirmed_scope` is supplied and not `unscoped` and the intent is hypothesis-mode, pass `in_scope_recipes(scope)`'s template subset to `_template_candidates`/`ground_all` (the existing signature already takes an explicit template list — do NOT use `use_case=`); persist the scope; attach `dispositions`. Otherwise ground `ALL_TEMPLATES` (today). Keep the snapshot write.
- [ ] **Step 4: Run — expect pass**, then the FULL overlay/contract/governance suite (flag-off neutrality).
- [ ] **Step 5: Gates + commit** `feat(1b): scoped grounding + dispositions in build_considered_set (task 4)`.

---

## Task 5: `POST /contract/recognize` endpoint

**Files:** Modify `src/featuregen/api/routes/contract.py`; Test `tests/featuregen/api/test_contract_recognize.py`.

**Interfaces — Produces:** `POST /contract/recognize` (body: `hypothesis`, `objective`) → submits/loads the intent (idempotent), redacts, calls `recognize(conn, client, redacted_hypothesis=…, redacted_goal=…)`, `record_recognition_attempt`, and returns `{intent_id, generation_run_id, status, candidates:[{use_case_id, display_name, relationship, confidence, evidence_spans}], proposed_in_scope_count, unscoped}`. Mints a fresh `generation_run_id`.

- [ ] **Step 1: Failing test** (FakeLLM scripted for the recognizer + db) — a classified hypothesis returns the proposal with the primary + evidence + a non-null `generation_run_id`, and an `intent_recognition_attempt` row exists; an unscoped hypothesis returns `unscoped=true`; a recognizer failure returns `status=technical_failure` (never a 5xx — fail-open).
- [ ] **Step 2–4:** implement to green (reuse `submit_intent`, the `_LLM`/`_Conn`/`_Identity` deps, `require_feature_generate`).
- [ ] **Step 5: Gates + commit** `feat(1b): /contract/recognize endpoint (task 5)`.

---

## Task 6: Extend `POST /contract/considered-set` for a confirmed scope

**Files:** Modify `src/featuregen/api/routes/contract.py`; Test `tests/featuregen/api/test_contract_scoped.py`.

**Interfaces — Produces:** the endpoint accepts optional `intent_id`, `generation_run_id`, and `confirmed_scope: {primary, secondary, expansion, unscoped, confirmation_source}`. When present → load the intent, thread the `ConfirmedScope` + run id into `build_considered_set`; the response includes the `dispositions` lens. When absent → today's `(hypothesis, objective)` path unchanged. **Broaden-scope** = the client re-calls with `confirmed_scope.unscoped=true` + a NEW `generation_run_id` and `supersedes_scope_id` = the prior scope → a new unscoped run + snapshot (persistence keeps both).

- [ ] **Step 1: Failing test** — a scoped call returns fewer template candidates + a disposition lens; a broaden call (`unscoped`, new run, `supersedes`) returns full grounding and persists a second scope superseding the first; the no-scope call is unchanged.
- [ ] **Step 2–4:** implement to green.
- [ ] **Step 5: Gates + commit** `feat(1b): considered-set accepts a confirmed scope + broaden (task 6)`.

---

## Task 7: Gate #1 confirmation UI + disposition lens

**Files:** Modify `frontend/src/api.ts`, `frontend/src/screens/WorkbenchScreen.tsx`; Test `frontend/src/screens/WorkbenchScreen.test.tsx` (vitest).

- [ ] **Step 1:** `api.ts` — add `contractRecognize(hypothesis, objective)` → `POST /contract/recognize`; extend `contractConsideredSet` to pass `intentId?`, `generationRunId?`, `confirmedScope?`.
- [ ] **Step 2:** `WorkbenchScreen` — behind `intent_confirmation_ui`: on generate, first call `contractRecognize`, render the proposed scope (primary + secondary + evidence spans + confidence), with controls to **confirm**, **remove a secondary**, **change the primary**, **broaden ("show all buildable recipes")**, and — when the user picks a broad parent — an "include all sub-use-cases?" toggle (`EXACT` vs `INCLUDE_DESCENDANTS`). Then call `contractConsideredSet` with the confirmed scope.
- [ ] **Step 3:** render the **disposition lens** — group the results: *Recommended / eligible*, *Relevant but missing data* (unbuildable), *Rejected by safety*, *Outside confirmed scope* — each recipe showing its reason; broaden stays one click away.
- [ ] **Step 4:** flag **off** → the screen behaves exactly as today (one-shot generate, no recognize call). Assert this in a test.
- [ ] **Step 5:** `npm run typecheck` + `npx vitest run` + `npm run lint`; commit `feat(1b): Gate #1 confirmation UI + disposition lens (task 7)`.

---

## Task 8: Feature flags + rollback + neutrality proof

**Files:** Modify the flag/config module (follow `_auth_stub_enabled` in `api/deps.py`); Test `tests/featuregen/api/test_1b_rollout.py`.

- [ ] **Step 1: Failing test** — with all three flags (`intent_confirmation_ui`, `intent_scoped_applicability`, `intent_disposition_lens`) **off**, a considered-set call is byte-identical to pre-1B (same alternatives, no recognition row written); the **emergency rollback** (disable `intent_scoped_applicability` only) returns to full grounding while `intent_recognition_attempt` rows are still written (telemetry retained).
- [ ] **Step 2–4:** implement the three flags (default off) to green.
- [ ] **Step 5: Gates + commit** `feat(1b): 1B feature flags + emergency rollback (task 8)`.

---

## Self-review
- **Neutrality:** flags default off; Tasks 4/6/8 each assert flag-off == today.
- **Fail-open:** unscoped/technical/empty scope → full grounding (Task 4); recognizer failure → no 5xx (Task 5).
- **Lineage:** every scope binds to `generation_run_id` + `supersedes_scope_id`; governing scope read by run id, never by time (Tasks 1/2/6).
- **Immutability:** `contract_intent` untouched; recognition/scope are append-only (Tasks 1/2).
- **Definition-mode** bypass covered (Task 4). **Dispositions** never bare-null; ranking is an attribute, not a disposition (Task 3).
- **Deferred to Phase 2** (stated, not built): ranking/presentation-priority, contextual policy, `modelling_context`/`entity_context` dimensions.
