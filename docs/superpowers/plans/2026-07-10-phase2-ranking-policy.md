# Phase 2 — Ranking + Dimensions + Contextual Policy — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: subagent-driven-development. Steps use `- [ ]` checkboxes.

**Goal:** Once recipes are scoped to the objective (Phase 1B), (A) **rank** the eligible set so the most relevant, most explainable recipes lead; wire the two deferred **dimensions** (`modelling_context`, `entity_context`) that Phase 1B recognised-but-didn't-use; and (C) add a **contextual policy** layer that can warn / require-approval / exclude a structurally-safe recipe that is *inappropriate* for a governed context — one locally-ratified rule proven end-to-end. All flag-gated default-off, additive over universal safety.

**Architecture:** Phase-1B pipeline is `applicability → grounding → safety → disposition`. Phase 2 inserts **policy** after safety (a structurally-safe recipe may still be context-inappropriate) and **ranking** over the eligible set (presentation-priority only). The recognizer gains two more closed dimensions in the SAME call; `entity_context` becomes a hard-incompatibility reject in applicability; `modelling_context` becomes a ranking-boost + a policy predicate.

**Tech stack:** Python 3.11 / FastAPI, psycopg + SQL migrations, React (`WorkbenchScreen.tsx`, `api.ts`). Builds on Phase-1B `ApplicabilityResult`, `RecipeEvaluation`/`FinalDisposition`, `evaluate_dispositions`, the recognizer, `contract/scope_records.py`, the disposition lens.

**Gating precondition:** Phase 1B enabled and its shadow-run exit bar cleared (scoping must be live and trusted before ranking/policy sit on top of it).

## Global Constraints

- **Flags default OFF → Phase-1B behaviour exactly.** New flags: `FEATUREGEN_INTENT_RANKING`, `FEATUREGEN_INTENT_CONTEXTUAL_POLICY` (backend); `VITE_INTENT_RANKING`, `VITE_INTENT_POLICY` (frontend). Off → the eligible set is unordered-as-today and no policy stage runs. Prove flag-off neutrality.
- **Ranking is PRESENTATION PRIORITY, never measured predictive utility** (no data plane). Start **tiered** (primary > secondary > supporting, then explainability, then binding quality) with deterministic, stable tie-breaks. Any weighted score is **derived from evaluation data, not asserted** — do NOT hardcode magic weights. Ranking is an *attribute* of an eligible recipe, NEVER a disposition.
- **Policy is additive-only over universal safety.** The policy stage can only ADD restrictions to an already-safe candidate; it NEVER relaxes `_safe_to_bind`. Every policy rule is **locally ratified before it enforces** (the LLM may *draft* a rule; a human ratifies; only a ratified bundle evaluates). No execution-implying actions (no fairness-test/block-export — there is no training/export plane).
- **Deterministic action → disposition.** `allow`→`ELIGIBLE`; `warn`→`ELIGIBLE` + a policy-warning reason code; `require_justification`/`require_approval`→`POLICY_REVIEW_REQUIRED`; `exclude`→`POLICY_BLOCKED`. The policy stage **retains the original action** even after an approval (`action=require_approval, approval_status=approved`) — an approval doesn't erase that approval was needed.
- **Predicates over a versioned `PolicyContext`**, not a hard-coded key. Phase-2B populates only use-cases + modelling-contexts + jurisdiction + decision-purpose; the schema leaves room for legal-entity/product/lifecycle/automation-level (empty for now).
- **The LLM proposes; deterministic code + humans dispose.** Recogniser proposes the extra dimensions (human-confirmed at Gate #1); ranking is deterministic; policy rules are human-ratified.
- **Version the evidence.** Persist `ranking_version` + `policy_bundle_version` on the generation run; each policy decision stamps the ratified rule id + bundle version it fired under (replay).
- **Transparency.** A policy-blocked / review-required / down-ranked recipe stays visible in the disposition lens with its reason; nothing disappears silently.

---

# Part A — Ranking (presentation priority)

## Task 1: Deterministic ranker

**Files:** Create `src/featuregen/overlay/upload/taxonomy/ranking.py`; Test `test_ranking.py`.

**Interfaces — Produces:**
- `@dataclass(frozen=True) class RankSignals: relevance_tier: str; explainability: str; binding_quality: str; pit_declared: bool; family: str; funnel_stage: str; modelling_context_match: bool`
- `@dataclass(frozen=True) class RankedRecipe: recipe_id: str; canonical_rank: int; selected_for_initial_view: bool; rank_reasons: tuple[str,...]`
- `def rank_eligible(evaluations: list[RecipeEvaluation], signals: dict[str, RankSignals], *, ranking_version: str, initial_view_size: int = 15, per_family_cap: int = 3) -> list[RankedRecipe]` — order the `ELIGIBLE` recipes by **tier** (primary→supporting), then explainability (H>M>L), then `modelling_context_match`, then binding quality, then a stable id tie-break; assign `canonical_rank` (1-based); run a **diversity pass** for `selected_for_initial_view` (at most `per_family_cap` from one family in the first `initial_view_size`; prefer covering distinct funnel stages; drop near-duplicates that differ only by a minor window param). No magic weights — pure ordered tiers + tie-breaks.

- [ ] **Step 1: Failing test** — a primary-tier eligible recipe ranks above a supporting-tier one; within a tier a high-explainability recipe ranks above a low one; the diversity pass never puts >`per_family_cap` recipes of one family in the initial view and prefers distinct funnel stages; `rank_reasons` explains each (e.g. `("primary_match","high_explainability","covers balance-runoff stage")`); ranking is deterministic (same input → same order). Non-eligible recipes are NOT ranked.
- [ ] **Step 2–4:** implement to green.
- [ ] **Step 5: Gates + commit** `feat(2a): deterministic presentation-priority ranker (task 1)`.

## Task 2: Wire ranking into the considered-set + keep the three layers separate

**Files:** Modify `contract.py` (the scoped considered-set route) + a signals helper (from the grounded features' template metadata + disposition); Test `test_contract_ranked.py`.

**Interfaces:** when `FEATUREGEN_INTENT_RANKING` is on and a scoped response exists, compute `RankSignals` per eligible recipe (relevance_tier from the disposition; explainability/family/funnel_stage from the `Template`; `modelling_context_match` from the confirmed modelling-context, Part B) and attach `ranking: [{recipe_id, canonical_rank, selected_for_initial_view, rank_reasons}]` + `ranking_version` to the response. Persist the three layers **separately**: `deterministic_rank` (this), the existing LLM `recommendation` (`SetRecommendation`), and the human Gate-#1 choice — never conflate them.

- [ ] **Step 1: Failing test** — flag off → response has no `ranking` key (Phase-1B-identical); flag on → `ranking` orders the eligible recipes, `selected_for_initial_view` respects the family cap, and the LLM `recommendation` is still present and distinct from `deterministic_rank`.
- [ ] **Step 2–4:** implement to green (full suite for neutrality).
- [ ] **Step 5: Gates + commit** `feat(2a): rank the eligible set in considered-set (task 2)`.

## Task 3: (UI) ranked order + recommended set + rank explanation

**Files:** Modify `frontend/src/api.ts`, `WorkbenchScreen.tsx`; Test `WorkbenchScreen.test.tsx`.

- [ ] Behind `VITE_INTENT_RANKING`: render the eligible recipes in `canonical_rank` order, show the "initial view" (the `selected_for_initial_view` set) with a "show all" expander, a small "why ranked here" popover from `rank_reasons`, and the LLM "recommended starting set" as a SEPARATE labelled band. Flag off → unordered as Phase 1B. Tests: order rendered; recommended band distinct; flag-off unchanged. Gates: typecheck/vitest/lint. Commit `feat(2a): ranked order + recommended set UI (task 3)`.

---

# Part B — The two deferred dimensions

## Task 4: Multi-dimension recognition (modelling_context + target_entity)

**Files:** Modify `taxonomy/recognition.py` (contract + validator), `recognizer.py` (+ prompt), `enrich_llm.py` (schema), `scope_records.py` (persist); Test extends `test_recognition_contract.py`, `test_recognizer.py`.

**Interfaces:** extend `RecognitionResult` with `modelling_contexts: tuple[str,...]` and `target_entity: str | None`; the recognizer prompt + JSON schema gain those closed dimensions (validated against `dimensions.MODELLING_CONTEXTS` and the entity vocabulary); the recognition attempt persists them (add columns to migration — a new `0975` migration). One LLM call, still fail-open, still redacted-input-only.

- [ ] **Step 1: Failing test** — a body naming an IFRS9 framing → `modelling_contexts` contains `"ifrs9"`; a customer-vs-account framing → `target_entity` set; unknown context/entity → validation rejects (fail-open to unscoped for those dims); the recognition attempt row stores both. Backward-compat: a body without them still validates.
- [ ] **Step 2–4:** implement to green.
- [ ] **Step 5: Gates + commit** `feat(2b-dim): multi-dimension recognition (task 4)`.

## Task 5: Wire target_entity (hard reject) + modelling_context (rank boost)

**Files:** Modify `taxonomy/applicability.py` (entity incompatibility), pass `modelling_contexts` into the Task-1 `RankSignals`; Test extends `test_applicability.py`.

**Interfaces:** `applicability_result(scope, *, target_entity=None)` — when `target_entity` is set, a recipe whose grain/entity is **incompatible** with it (a recipe that can only be built at a different, non-joinable entity) is forced `out_of_scope` with reason `("entity_incompatible",)` — a HARD reject, distinct from "no use-case match". `modelling_context_match` in `RankSignals` is true when a recipe declares a `required_modelling_context` (or `use_cases` framework tag) matching a confirmed context → a rank boost (Task 1), never a hard filter.

- [ ] **Step 1: Failing test** — a scope with `target_entity="account"` forces a customer-only recipe `out_of_scope` with the `entity_incompatible` reason; with `target_entity=None` nothing changes (backward-compat); a recipe matching a confirmed `modelling_context` gets `modelling_context_match=True` and ranks above an equal-tier non-matching one (Task 1 consumes it). Entity reject is NOT applied under `unscoped` (fail-open).
- [ ] **Step 2–4:** implement to green.
- [ ] **Step 5: Gates + commit** `feat(2b-dim): entity hard-reject + modelling-context boost (task 5)`.

---

# Part C — Contextual policy (one ratified rule end-to-end)

## Task 6: PolicyContext + ratified policy-bundle model

**Files:** Create `src/featuregen/db/migrations/0976_policy_bundles.sql`, `src/featuregen/overlay/upload/policy/bundles.py`; Test `test_policy_bundles.py`.

**Interfaces — Produces:**
- `@dataclass PolicyContext: use_cases: tuple[str,...]; modelling_contexts: tuple[str,...]; jurisdictions: tuple[str,...]; decision_purpose: str | None` (+ reserved-but-empty legal_entity/product/lifecycle/automation_level).
- DB: `policy_rule` (rule_id, bundle_id, predicate jsonb, action, reason_code, message, status, effective_from, review_date, references, version) and `policy_ratification` (append-only: rule_id, ratified_by, ratified_at, authority) — a rule only EVALUATES when it has a ratification row and `status='active'`. WORM grants per 0971.
- `def active_rules(conn, bundle_version) -> list[PolicyRule]` — only ratified + active + effective rules.
- `def draft_rule(...)` (LLM-proposed candidate, status `draft`, NOT evaluated) vs `def ratify_rule(conn, rule_id, *, authority, ratified_by)` (writes the ratification → active).

- [ ] **Step 1: Failing test** — a `draft` rule is NOT returned by `active_rules`; after `ratify_rule` it is; the ratification is append-only + WORM-protected; a rule past `review_date`/before `effective_from` is excluded.
- [ ] **Step 2–4:** implement to green.
- [ ] **Step 5: Gates + commit** `feat(2b): PolicyContext + ratified policy-bundle model (task 6)`.

## Task 7: The policy evaluation stage

**Files:** Modify `taxonomy/disposition.py` (extend enum + a policy stage), create `policy/evaluate.py`; Test `test_policy_evaluate.py` + `test_disposition.py`.

**Interfaces — Produces:**
- Extend `FinalDisposition` with `POLICY_BLOCKED`, `POLICY_REVIEW_REQUIRED` (and represent `warn` as `ELIGIBLE` + a `policy_warning` reason code).
- `def evaluate_policy(candidate_concepts: dict[str, frozenset[str]], context: PolicyContext, rules: list[PolicyRule], *, now) -> dict[str, PolicyDecision]` — per eligible recipe, match ratified rules whose predicate holds over `(the recipe's bound concepts' sensitivities, context)`; the STRONGEST action wins (`exclude` > `require_approval` > `require_justification` > `warn` > `allow`); returns `PolicyDecision{action, rule_id, bundle_version, approval_status}`.
- Extend `evaluate_dispositions` to run policy AFTER safety, BEFORE the final ELIGIBLE: a `SAFETY_REJECTED`/`OUT_OF_SCOPE`/`UNBUILDABLE` recipe skips policy (`NOT_EVALUATED`); an otherwise-eligible recipe's `final_disposition` comes from the policy action mapping. **Additive-only:** a safety-rejected recipe is never rescued to eligible by policy.

- [ ] **Step 1: Failing test** — a recipe binding a `proxy`-sensitivity concept under `(jurisdiction=US, decision_purpose=underwriting)` with the ratified proxy rule → `POLICY_REVIEW_REQUIRED`, stamping the rule id + bundle version + `approval_status='pending'`; the same recipe with NO ratified rule / different context → `ELIGIBLE`; an `exclude` rule → `POLICY_BLOCKED`; a safety-rejected recipe stays `SAFETY_REJECTED` (policy `NOT_EVALUATED`); a warn rule → `ELIGIBLE` + `policy_warning`; the original action is retained after an approval.
- [ ] **Step 2–4:** implement to green.
- [ ] **Step 5: Gates + commit** `feat(2b): contextual policy evaluation stage (task 7)`.

## Task 8: Proof rule end-to-end + approval flow (API)

**Files:** Modify `contract.py` (thread `PolicyContext` from the confirmed scope + the modelling-context/jurisdiction/decision-purpose; run policy in the scoped path; add an approval endpoint), a ratified proof-rule seed; Test `test_contract_policy.py`.

**Interfaces:** the scoped considered-set builds a `PolicyContext` (use-cases + modelling-contexts from the scope; jurisdiction + decision_purpose from the request/project config) and, when `FEATUREGEN_INTENT_CONTEXTUAL_POLICY` is on, runs `evaluate_policy` → the dispositions carry the policy outcome + `in_scope_count`/counts by disposition. `POST /contract/policy/approve` (body: generation_run_id, recipe_id, justification) records an approval (append-only) → the recipe's disposition flips `POLICY_REVIEW_REQUIRED`→`ELIGIBLE` while the stage retains `action=require_approval, approval_status=approved`. Seed ONE ratified proof rule (`fair_lending_us_v1`: US + underwriting + proxy-sensitivity → `require_approval`).

- [ ] **Step 1: Failing test** — flag off → no policy in the response (Phase-1B/2A-identical); flag on + the proof rule ratified + a US-underwriting context + a proxy-binding recipe → that recipe is `policy_review_required` in the dispositions with the rule id; `POST /contract/policy/approve` → it becomes `eligible` (action retained); a run without the context → all `eligible`.
- [ ] **Step 2–4:** implement to green (full suite for neutrality).
- [ ] **Step 5: Gates + commit** `feat(2b): policy proof rule + approval endpoint (task 8)`.

## Task 9: (UI) policy dispositions + approval + minimal ratification surface

**Files:** Modify `api.ts`, `WorkbenchScreen.tsx` (+ optionally a small admin view); Test `WorkbenchScreen.test.tsx`.

- [ ] Behind `VITE_INTENT_POLICY`: the disposition lens gains *Blocked by policy* and *Needs approval* groups (each with the rule message + reason), an **approve** action (calls `/contract/policy/approve` with a justification) that moves the recipe to eligible, and a policy-warning badge on warned recipes. Flag off → no policy groups. (Ratification itself is an admin/governance action — a minimal read-only "active policy rules" panel is enough for the proof.) Tests: review-required renders + approve flips it; flag-off unchanged. Gates: typecheck/vitest/lint. Commit `feat(2b): policy dispositions + approval UI (task 9)`.

---

## Self-review
- **Ranking:** presentation-priority only (Task 1 asserts non-eligible are unranked; tiers + tie-breaks, no magic weights); three layers persisted separately (Task 2).
- **Dimensions:** one recognizer call (Task 4); `target_entity` is a hard reject, `modelling_context` a soft boost (Task 5); both fail-open under `unscoped`.
- **Policy:** ratified-before-enforced (Task 6); additive-only over universal safety, deterministic action→disposition, original action retained after approval (Task 7); ONE proof rule end-to-end (Task 8).
- **Neutrality/fail-open/immutability** hold; all four flags default off; the no-scope and flag-off paths stay Phase-1B-identical.
- **Deferred to Phase 3 (stated, not built):** cross-catalog grounding + the richer recipe `needs` contract.
