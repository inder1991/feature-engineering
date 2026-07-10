# Phase 2 — Ranking + Confirmed Dimensions — Implementation Plan (v3)

> **For agentic workers:** REQUIRED SUB-SKILL: subagent-driven-development. Steps use `- [ ]` checkboxes.
> v3: **contextual policy has been split out into its own initiative** (`2026-07-10-governed-feature-policy.md`). This plan is now the two near-ready sub-phases only — ranking and confirmed dimensions — with the Head-of-Architecture review folded in.

**Goal:** On top of Phase-1B scoping, deliver (2A) deterministic **presentation-priority ranking** of the eligible set, and (2B) two **human-confirmed** intent dimensions (`modelling_context`, `target_entity` — the latter *soft*). Both flag-gated default-off.

**Architecture:** ranking consumes a **precomputed set of rankable recipe ids** (it never inspects dispositions itself), so it is independent of whether policy exists — today the rankable set = the Phase-1B `ELIGIBLE` recipes; when the policy initiative lands, the rankable set = the post-policy eligible recipes, with **no change to the ranker**. `target_entity` is a soft grain signal, never an applicability reject (hard entity rejection needs Phase-3 join semantics).

**Tech stack:** Python 3.11 / FastAPI, psycopg + SQL migrations, React. Builds on Phase-1B `ApplicabilityResult`, `RecipeEvaluation`/`FinalDisposition`, the recognizer, `scope_records`, the disposition lens.

**Gating precondition:** Phase 1B live and trusted.

## Global Constraints

- **Flags default OFF → Phase-1B byte-identical.** `FEATUREGEN_INTENT_RANKING` + `VITE_INTENT_RANKING`; dimensions ride the recognizer flag + `VITE_INTENT_CONFIRMATION_UI`.
- **Ranking = presentation priority, never predictive utility.** Deterministic, tiered, no magic weights. Ranking is an **attribute**, never a disposition.
- **The ranker consumes a precomputed `rankable_recipe_ids` set** — it does NOT read `FinalDisposition`. This keeps it stable across the future policy initiative.
- **Canonical rank vs initial view are separate projections.** Diversity affects `selected_for_initial_view` ONLY; it never rewrites `canonical_rank`. They carry **separate** structured reason codes (`rank_reasons` vs `initial_view_reasons`), including *negative* factors.
- **A binding-acceptability gate precedes context.** A structurally weak/ambiguous binding is never promoted into the initial view by a modelling-context match.
- **Signals are typed enums with a defined derivation** (no bare booleans, no undefined labels): `BindingQuality`, `PITCompleteness`, `ModellingContextFit`, `EntityCompatibility`. Any signal in the contract MUST be used.
- **Dimensions: recognizer proposes, human confirms at Gate #1** (extend `ConfirmedScope` + persistence + UI; retain proposed-vs-confirmed as a confirmation delta). **A valid use-case recognition is NEVER invalidated by an invalid *optional* dimension** — per-dimension failure semantics.
- **`target_entity` is SOFT** — a grain warning + rank nudge (`EXACT`/`DERIVABLE`/`UNKNOWN`), never `OUT_OF_SCOPE`. Hard `INCOMPATIBLE` reject deferred to Phase 3.
- **Determinism:** ranking is stable regardless of input dict/order; a `ranking_version` change never mutates an old projection.
- **Migration numbers in this plan are placeholders — resolve against repo head at execution time.**

---

# Phase 2A — Deterministic presentation priority

## Task A1: Typed ranking signals + their derivations
**Files:** `taxonomy/ranking_signals.py` (new: the enums + derivations), `taxonomy/journey_stages.py` (new: optional journey vocab); tests.
- `class BindingQuality(StrEnum): EXACT; STRONG; ACCEPTABLE; AMBIGUOUS` — **derived in a grounding-side helper** from the grounded feature (all roles bind exact single-candidate → EXACT; aliases/inherited → STRONG; optional metadata incomplete → ACCEPTABLE; multiple viable/weak resolution → AMBIGUOUS). If grounding already rejects ambiguous bindings, `AMBIGUOUS` simply won't appear in the rankable set.
- `class PITCompleteness(StrEnum): COMPLETE; NOT_APPLICABLE; PARTIAL; UNKNOWN` (from the template's PIT declaration; `NOT_APPLICABLE` for non-time-dependent recipes).
- `class ModellingContextFit(StrEnum): REQUIRED_MATCH; COMPATIBLE; NEUTRAL; CONFLICT` (2A always `NEUTRAL` — no confirmed context yet; 2B supplies the real fit).
- `class EntityCompatibility(StrEnum): EXACT; DERIVABLE; UNKNOWN` (2A always `UNKNOWN`; 2B supplies it). **No `INCOMPATIBLE` — deferred to Phase 3.**
- **Optional** journey metadata: `journey_model_id: str | None`, `journey_stage_id: str | None` (both nullable). A registry maps a template's existing free-form `stage` → a controlled `(model, stage)` **where one exists**; recipes with no meaningful journey (pricing, actuarial, custody-holdings, capital/liquidity, ops) keep both null. `semantic_group = the source recipe's `template_id`` (verified: variants like `balance_trend_90d` share `template_id="balance_trend"`).
- **Test:** every derivation is total; a journey-relevant family resolves to a valid `(model, stage)`; a non-journey recipe has nulls (NOT forced); an invalid stage id / a stage without a model is rejected; `semantic_group` groups `balance_trend` variants.
- Commit `feat(2a): typed ranking signals + optional journey metadata (task A1)`.

## Task A2: Deterministic ranker (canonical rank + separate initial view)
**Files:** `taxonomy/ranking.py`; tests.
- `class RankReasonCode(StrEnum)` (positive AND negative: `PRIMARY_USE_CASE_MATCH`, `EXACT_BINDING`, `PIT_COMPLETE`, `LOW_BINDING_QUALITY`, `PIT_METADATA_INCOMPLETE`, `ENTITY_GRAIN_UNKNOWN`, …) and `class InitialViewReasonCode(StrEnum)` (`DUPLICATE_VARIANT_NOT_IN_INITIAL_VIEW`, `FAMILY_CAP_NOT_IN_INITIAL_VIEW`, `STAGE_DIVERSITY`).
- `RankSignals{relevance_tier, binding_quality, modelling_context_fit, pit_completeness, explainability, family, journey_model_id, journey_stage_id, semantic_group, entity_compatibility}`.
- `RankedRecipe{recipe_id, canonical_rank, selected_for_initial_view, rank_reasons, initial_view_reasons}`.
- `rank_eligible(rankable_recipe_ids: Sequence[str], signals: Mapping[str, RankSignals], *, ranking_version, initial_view_size=15, per_family_cap=3) -> list[RankedRecipe]`:
  - **Binding-acceptability gate:** `AMBIGUOUS`-binding recipes are ranked but never `selected_for_initial_view`.
  - **Canonical order:** relevance_tier → modelling_context_fit (REQUIRED_MATCH>COMPATIBLE>NEUTRAL; CONFLICT is a 2B warning, not a rank change here) → binding_quality → pit_completeness → explainability → stable `recipe_id`. Assign 1-based `canonical_rank`.
  - **Initial-view (separate projection, never mutates canonical_rank):** deterministic **relaxation** — Pass 1 enforce semantic-group (one variant), family cap, prefer distinct `journey_stage_id` *within a shared journey model*; Pass 2 relax stage diversity; Pass 3 relax family cap incrementally; never > one semantic variant unless the set can't fill the size; return fewer than requested only if fewer eligible exist. Stamp `initial_view_reasons`.
- **Test:** the ordering invariants (binding-quality above explainability, `pit_completeness` used); canonical rank immutable under diversity (a capped-out family recipe keeps its canonical rank); ambiguous binding never initial-view; the backfill relaxation is deterministic; separate rank vs initial-view reasons; deterministic under shuffled input; a `ranking_version` bump doesn't mutate a prior projection; 0 eligible / fewer-than-size / all-one-family / all-one-semantic-group / missing-journey / missing-binding-signal all handled.
- Commit `feat(2a): deterministic ranker — canonical + initial-view projections (task A2)`.

## Task A3: Wire ranking into considered-set (over a precomputed rankable set)
**Files:** `api/routes/contract.py` + a signals helper; tests.
- Behind `FEATUREGEN_INTENT_RANKING`: compute the **rankable set** = the `ELIGIBLE` recipe ids from the disposition (a small `rankable_recipe_ids(evaluations)` helper — the ONLY place `FinalDisposition` is read; the ranker stays disposition-agnostic). Build `RankSignals`, pin `ranking_version` **before** ranking, `rank_eligible`, attach `ranking` to the response. Keep the three layers separate: `deterministic_rank`, LLM `recommendation`, human choice.
- **Test:** flag off → no `ranking` key (Phase-1B-identical); on → eligible ordered, initial-view respects the cap, LLM recommendation still present + distinct; `rankable_recipe_ids` excludes non-eligible.
- Commit `feat(2a): rank the eligible set in considered-set (task A3)`.

## Task A4: (UI) ranked order + recommended band + reasons
**Files:** `frontend/src/api.ts`, `WorkbenchScreen.tsx`, test.
- Behind `VITE_INTENT_RANKING`: render eligible recipes in `canonical_rank` order; the initial-view set + "show all"; a "why here" popover mapping `RankReasonCode`→text AND a distinct "why not shown initially" from `initial_view_reasons`; the LLM "recommended starting set" as a separate labelled band. Flag off → unchanged.
- Gates: typecheck/vitest/lint. Commit `feat(2a): ranked order + recommended set UI (task A4)`.

## Phase 2A exit
Deterministic across shuffled/repeated input; all signals sourced + versioned; no non-eligible recipe ranked; canonical rank unchanged by diversity; initial-view backfill matches the documented relaxation; rank vs initial-view reasons separate; product review confirms explanations are understandable.

---

# Phase 2B — Confirmed multi-dimensional intent

## Task B1: Multi-dimension recognition + per-dimension failure semantics
**Files:** `taxonomy/recognition.py`, `recognizer.py` (+prompt), `enrich_llm.py` (schema), migration `NNNN_recognition_dims`, `scope_records.py`; tests.
- `RecognitionResult` gains `modelling_contexts: tuple[str,...]`, `target_entity: str | None`, `warnings: tuple[str,...]`. **Per-dimension validation:** invalid **primary use-case** → whole result unscoped; invalid **optional** `modelling_context` → drop that value, keep the use-case result, add `UNKNOWN_MODELLING_CONTEXT` warning; invalid `target_entity` → clear it, keep the result, warn; invalid whole schema → technical-failure/unscoped. One LLM call, redacted-input-only; persist all + warnings on the attempt.
- **Test:** the failure matrix above (esp. an invalid optional dim does NOT invalidate a valid use-case recognition); IFRS9 framing → `ifrs9` context; entity framing → `target_entity`; a body without them still validates.
- Commit `feat(2b): multi-dimension recognition + per-dimension failure semantics (task B1)`.

## Task B2: Confirmed-scope dimensions + rich provenance
**Files:** `taxonomy/applicability.py` (`ConfirmedScope` +fields), migration `NNNN_confirmed_dims` (`confirmed_scope_dimension` child), `scope_records.py`; tests.
- `ConfirmedScope` gains `modelling_contexts` + `target_entity`. Persist a normalized `confirmed_scope_dimension` child per confirmed value with `source ∈ {accepted_llm_proposal, user_added, user_replacement, project_default, organization_default}` + `replaces_value`. Persist a **confirmation delta** (`accepted / rejected / added / replaced`) derivable against the attempt's proposals via `recognition_id`.
- **Test:** round-trip a scope where the user accepts one context, rejects another, adds a third, replaces the entity; the confirmation delta reconstructs correctly; `scope_for_run` rebuilds the confirmed dimensions.
- Commit `feat(2b): confirmed-scope dimensions + provenance delta (task B2)`.

## Task B3: Soft entity grain signal + modelling-context fit
**Files:** `taxonomy/applicability.py`, ranking signals; tests.
- `entity_compatibility(recipe, target_entity) -> EntityCompatibility` (EXACT/DERIVABLE/UNKNOWN) from the recipe's declared grain — a **grain/groundability** signal that produces an `entity_grain_mismatch` warning + a rank nudge (EXACT ≥ DERIVABLE ≥ UNKNOWN as a low tie-break), and **never** changes `by_recipe`/`out_of_scope`. `modelling_context_fit(recipe, confirmed_contexts) -> ModellingContextFit` (REQUIRED_MATCH/COMPATIBLE/NEUTRAL/CONFLICT); feeds the Task-A2 ranker; `CONFLICT` → a warning surfaced in the lens, NOT a hard reject in Phase 2.
- **Test:** a customer-only recipe under `target_entity="account"` stays IN scope with an `entity_grain_mismatch` warning + lower nudge; `target_entity=None` → no effect; a REQUIRED_MATCH context recipe ranks above an equal-tier NEUTRAL one; a CONFLICT recipe carries a warning but is not rejected; NO recipe is hard-rejected on entity.
- Commit `feat(2b): soft entity grain + modelling-context fit (task B3)`.

## Task B4: (UI) confirm/override dimensions at Gate #1
**Files:** `api.ts`, `WorkbenchScreen.tsx`, test.
- The confirm panel lets the user accept/remove/add/replace a modelling-context, correct or **clear** the target-entity, and see grain-mismatch + context-conflict warnings on affected recipes. Confirmed dimensions + provenance flow into the scoped considered-set. Flag off → unchanged.
- Gates: typecheck/vitest/lint. Commit `feat(2b): Gate #1 dimension confirmation UI (task B4)`.

## Phase 2B exit
Proposed-vs-confirmed deltas persisted; an invalid optional dimension never invalidates use-case recognition; NO entity-based hard rejection; grain warnings verified against real recipes; per-dimension override metrics available.

---

## Self-review
- **Ranking** consumes a precomputed rankable set (disposition-agnostic → survives the policy initiative unchanged); typed signals with defined derivations; canonical rank vs initial-view are separate projections with separate reason codes; binding-acceptability gate; deterministic backfill.
- **Dimensions** are human-confirmed with rich provenance; per-dimension failure preserves a valid use-case; `target_entity` strictly soft; hard entity reject deferred to Phase 3.
- **Contextual policy is a separate initiative** — see `governed-feature-policy.md`; its pipeline slot (`safety → policy → rank`) is honoured by the ranker already consuming a post-policy-ready rankable set.
