# Phase 2 — Ranking, Confirmed Dimensions, Contextual Policy — Implementation Plan (v2)

> **For agentic workers:** REQUIRED SUB-SKILL: subagent-driven-development. Steps use `- [ ]` checkboxes.
> v2 folds in the Head-of-Architecture review: **split into four independently-shippable sub-phases** (2A ranking → 2B confirmed dimensions → 2C policy *shadow* → 2D policy enforcement), soft entity handling, event-sourced policy lifecycle, a closed predicate DSL, split justify/approve with authority, and "shadow-before-enforce" for policy.

**Goal:** On top of Phase-1B scoping, deliver — in de-risked order — (2A) deterministic **presentation-priority ranking** of the eligible set; (2B) two **human-confirmed** intent dimensions (`modelling_context`, `target_entity`, the latter *soft* until Phase 3); (2C) a **contextual-policy** foundation run in **shadow**; (2D) policy **enforcement + a governed approval workflow**.

**Architecture / final pipeline (once all sub-phases land):**
```
applicability → grounding → universal safety → contextual policy → rank(eligible only) → present
```
Policy sits *after* safety (additive-only; never rescues a safety-rejected recipe) and *before* ranking (a policy-blocked recipe never receives a rank). The recognizer gains two closed dimensions in one call; `target_entity` is a *soft* grain signal in Phase 2 (hard entity rejection needs Phase-3 join semantics).

**Tech stack:** Python 3.11 / FastAPI, psycopg + SQL migrations, React. Builds on Phase-1B `ApplicabilityResult`, `RecipeEvaluation`/`FinalDisposition`, `evaluate_dispositions`, the recognizer, `scope_records`, the disposition lens.

**Gating precondition:** Phase 1B live and trusted (its shadow-run bar cleared). Each sub-phase is separately flag-gated and shippable; 2D depends on a 2C shadow review by compliance.

## Global Constraints (bind every task)

**Neutrality & rollout**
- Flags default OFF → Phase-1B behaviour, byte-identical; prove it per sub-phase. Flags: `FEATUREGEN_INTENT_RANKING`, `FEATUREGEN_INTENT_CONTEXTUAL_POLICY` (backend) + `VITE_INTENT_RANKING`, `VITE_INTENT_POLICY` (frontend). Multi-dimension recognition rides the existing recognizer flag.
- **Shadow before enforce.** Contextual policy is evaluated and measured against compliance-reviewed expectations (2C) BEFORE it can change any disposition (2D) — the same discipline used for recognition (shadow-before-filter).

**Ranking = presentation priority**
- NEVER measured predictive utility (no data plane). Deterministic **tiered** order: relevance tier → exact modelling-context match → **binding quality → PIT-completeness** → explainability → stable id. No magic weights; any learned weight is derived from eval data, never asserted. If a signal (e.g. `pit_declared`) is in the contract it MUST be used or removed.
- Ranking is an **attribute** of an eligible recipe, NEVER a disposition.
- **Diversity affects `selected_for_initial_view` ONLY — it never rewrites `canonical_rank`.**
- Ranking runs **after policy, over the final eligible set only** (`POLICY_BLOCKED`/`POLICY_REVIEW_REQUIRED`/`OUT_OF_SCOPE`/`UNBUILDABLE` get no canonical rank).
- Rank reasons are **structured codes** (`RankReasonCode`), not hand-authored text; the UI maps codes → display.

**Dimensions**
- The recognizer PROPOSES `modelling_context`/`target_entity`; the human CONFIRMS at Gate #1. No recognized dimension becomes a constraint before confirmation — extend `ConfirmedScope` + `confirmed_generation_scope` + the Gate-#1 UI, retaining proposed-vs-confirmed (the 1B delta discipline).
- **`target_entity` is SOFT in Phase 2.** It's a *grain/groundability* signal, not a *relevance* one: `EntityCompatibility ∈ {EXACT, DERIVABLE, UNKNOWN}` → a rank nudge or a grain-mismatch WARNING, surfaced with a distinct reason (`entity_grain_mismatch`) — NEVER `OUT_OF_SCOPE`. A hard `INCOMPATIBLE` reject needs Phase-3 entity-graph join semantics and is **deferred to Phase 3**.

**Policy is a governed decision system, not a filter**
- **Missing/incomplete context never means unrestricted.** `PolicyContextStatus ∈ {COMPLETE, NOT_REQUIRED, INCOMPLETE, CONFLICTING}`. The trigger for review is a **policy-relevant bound concept** (e.g. `proxy`/sensitive), not the presence of context: incomplete context + a sensitive concept → `POLICY_REVIEW_REQUIRED (POLICY_CONTEXT_INCOMPLETE)`; incomplete context + no sensitive concept → `ELIGIBLE`.
- **Additive-only over universal safety** — policy can only ADD restrictions to an already-safe candidate; it never relaxes `_safe_to_bind` or rescues a `SAFETY_REJECTED` recipe.
- **Locally ratified before enforced.** Ship rules as **inactive draft templates**; a ratified rule exists only as a **test fixture** or via a local ratification event by an authorized authority. NEVER ship a production migration that marks a compliance rule ratified.
- **Event-sourced, immutable lifecycle.** Immutable `policy_rule_version` + append-only `policy_rule_event` (drafted/submitted/ratified/activated/suspended/retired); active-state is DERIVED, not a mutable column. First-class `policy_bundle_version`; a generation run **pins exactly one `policy_bundle_version_id`**; `active_rules` never infers "latest".
- **Closed predicate DSL** — enumerated fields + operators (`eq, in, contains, intersects, exists, not_exists, all, any, not`); NO arbitrary/executable/model-generated logic. The ratification UI shows both human-readable meaning and the normalized predicate.
- **Keep ALL matching rules** on the decision (`matched_rules`); `effective_action` = strongest (`exclude > require_approval > require_justification > warn > allow`) but the full set is auditable.
- **`review_due_at` ≠ `effective_until`.** Past `review_due_at` → **keep enforcing + emit a governance alert**; only `effective_until` expires a rule. Never silently disable an overdue-review rule.
- **Justify ≠ approve.** A justification may come from the feature author; an **approval requires an actor with the rule's authority** (role/authority check, no self-approval, separation-of-duties). Approvals are append-only and **bound to a decision fingerprint** (recipe id + binding-plan hash + rule-version + policy-context hash + catalog snapshot) so they can't be reused after anything changes.
- **Approval → an append-only evaluation revision + deterministic rerank.** Never patch a persisted disposition/snapshot in place; produce a new projection tied to the approval event and re-rank the (now larger) eligible set.

**Reproducibility**
- Pin `ranking_version` + `policy_bundle_version_id` on the run **before** evaluating (not "evaluate active, then record"). Persist per-policy-decision fingerprints (context/binding/concept-set hashes) for exact replay.

---

# Phase 2A — Deterministic presentation priority (ranking)

## Task A1: Controlled ranking metadata (journey-stage + semantic-group)
**Files:** `src/featuregen/overlay/upload/taxonomy/journey_stages.py` (new); tests.
- A per-family **controlled journey-stage vocabulary** mapping each template's existing free-form `stage` string → a controlled `journey_stage_id` under a `journey_model_id` (churn: engagement_decline/unbundling/primacy_loss/attrition; credit: early_stress/deterioration/delinquency/default; …). A `semantic_group` for near-duplicate detection: for parameter variants this is **just the recipe's `template_id`** (free — variants of `balance_trend` share it); a cross-recipe `semantic_group` tag is optional and left for later.
- **Test:** every template's `stage` resolves to a controlled journey stage; variants group by `template_id`.
- Commit `feat(2a): controlled journey-stage + semantic-group metadata (task A1)`.

## Task A2: Deterministic ranker
**Files:** `taxonomy/ranking.py` (new); tests.
- `class RankReasonCode(StrEnum)`; `RankSignals{relevance_tier, explainability, binding_quality, pit_declared, family, journey_stage_id, semantic_group, modelling_context_match}`; `RankedRecipe{recipe_id, canonical_rank, selected_for_initial_view, rank_reasons: tuple[RankReasonCode,...]}`.
- `rank_eligible(eligible: list[RecipeEvaluation], signals, *, ranking_version, initial_view_size=15, per_family_cap=3) -> list[RankedRecipe]`: order by **relevance_tier → modelling_context_match → binding_quality → pit_declared → explainability → stable id**; assign 1-based `canonical_rank`. A SEPARATE diversity pass sets `selected_for_initial_view` (≤ `per_family_cap` per family in the first `initial_view_size`; prefer distinct `journey_stage_id`s; one initial-view representative per `semantic_group`) — **it never changes `canonical_rank`**. Only `ELIGIBLE` recipes are ranked.
- **Test:** the ordering invariants (incl. binding-quality above explainability; `pit_declared` used); canonical rank immutable under diversity (a capped-out family recipe keeps its canonical rank); one initial-view rep per semantic_group with all variants retained in the full list; reasons are codes; deterministic; non-eligible unranked.
- Commit `feat(2a): deterministic presentation-priority ranker (task A2)`.

## Task A3: Wire ranking into considered-set (over the final eligible set)
**Files:** `api/routes/contract.py` + a signals helper; tests.
- Behind `FEATUREGEN_INTENT_RANKING`: after dispositions, build `RankSignals` per `ELIGIBLE` recipe (tier from disposition; explainability/binding_quality/pit/family/journey_stage from the Template; `modelling_context_match` = false in 2A, enriched in 2B), `rank_eligible`, attach `ranking` + `ranking_version` (pinned before ranking). Keep the **three layers separate**: `deterministic_rank` (this), the LLM `recommendation`, the human choice.
- **Test:** flag off → no `ranking` key (Phase-1B-identical); on → eligible ordered, initial-view respects the cap, LLM recommendation still present + distinct.
- Commit `feat(2a): rank the eligible set in considered-set (task A3)`.

## Task A4: (UI) ranked order + recommended band + reasons
**Files:** `frontend/src/api.ts`, `WorkbenchScreen.tsx`, test.
- Behind `VITE_INTENT_RANKING`: render eligible recipes in `canonical_rank` order; show the initial-view set + "show all"; a "why here" popover mapping `RankReasonCode`→text; the LLM "recommended starting set" as a separate labelled band. Flag off → unchanged.
- Gates: typecheck/vitest/lint. Commit `feat(2a): ranked order + recommended set UI (task A4)`.

---

# Phase 2B — Confirmed multi-dimensional intent

## Task B1: Multi-dimension recognition
**Files:** `taxonomy/recognition.py`, `recognizer.py` (+prompt), `enrich_llm.py` (schema), migration `0975` (attempt columns), `scope_records.py`; tests.
- Extend `RecognitionResult` with `modelling_contexts: tuple[str,...]` + `target_entity: str | None`; recognizer prompt + JSON schema gain both closed dimensions (validated vs `dimensions.MODELLING_CONTEXTS` + the entity vocabulary); one LLM call, fail-open, redacted-input-only; persist both on the recognition attempt.
- **Test:** an IFRS9 framing → `modelling_contexts` has `ifrs9`; entity framing → `target_entity`; unknown value rejected (that dim fails open); a body without them still validates.
- Commit `feat(2b): multi-dimension recognition (task B1)`.

## Task B2: Confirmed-scope carries the dimensions (human-confirmed)
**Files:** `taxonomy/applicability.py` (`ConfirmedScope` +fields), migration `0976` (`confirmed_generation_scope` + `confirmed_scope_dimension` child), `scope_records.py`; tests.
- `ConfirmedScope` gains `modelling_contexts: tuple[str,...]` + `target_entity: str | None`. Persist confirmed dimensions (a normalized `confirmed_scope_dimension` child with `origin` `llm_proposed`/`user_added`/`user_overridden`) — proposed-vs-confirmed retained via `recognition_id`. `scope_for_run` rebuilds them.
- **Test:** round-trip a scope with a confirmed modelling-context + target-entity + origins; proposed-vs-confirmed delta derivable.
- Commit `feat(2b): confirmed-scope dimensions + persistence (task B2)`.

## Task B3: Soft entity (grain) signal + modelling-context rank boost
**Files:** `taxonomy/applicability.py`, ranking signals; tests.
- `class EntityCompatibility(StrEnum): EXACT; DERIVABLE; UNKNOWN` (NO `INCOMPATIBLE` in Phase 2). `entity_compatibility(recipe, target_entity) -> EntityCompatibility` using ONLY the recipe's declared grain vs the target (exact match → EXACT; a declared roll-up/derivable grain → DERIVABLE; otherwise UNKNOWN). It is a **grain signal, NOT applicability**: it never changes `by_recipe`/`out_of_scope`; instead it produces a `entity_grain_mismatch` warning reason and a rank nudge (EXACT ≥ DERIVABLE ≥ UNKNOWN as a tie-break BELOW relevance). `modelling_context_match` (recipe's framework tag ∈ confirmed contexts) feeds the Task-A2 ranker as a boost.
- **Test:** a customer-only recipe under `target_entity="account"` stays IN scope (not out_of_scope) but carries `entity_grain_mismatch` + a lower rank nudge; `target_entity=None` → no effect; a modelling-context match ranks above an equal-tier non-match; NO recipe is hard-rejected on entity in Phase 2.
- Commit `feat(2b): soft entity grain signal + modelling-context boost (task B3)`.

## Task B4: (UI) confirm/override the dimensions at Gate #1
**Files:** `api.ts`, `WorkbenchScreen.tsx`, test.
- The confirm panel lets the user remove/add/replace a modelling-context, correct or **clear** the target-entity, and see which recipes carry a grain-mismatch warning. Confirmed dimensions flow into the scoped considered-set. Flag off → unchanged.
- Gates: typecheck/vitest/lint. Commit `feat(2b): Gate #1 dimension confirmation UI (task B4)`.

---

# Phase 2C — Contextual policy foundation, in SHADOW

## Task C1: Closed predicate DSL + PolicyContext
**Files:** `overlay/upload/policy/predicate.py`, `policy/context.py` (new); tests.
- `PolicyContext{use_cases, modelling_contexts, jurisdictions, decision_purpose}` (+ reserved-empty legal_entity/product/lifecycle/automation_level) and `PolicyContextStatus{COMPLETE, NOT_REQUIRED, INCOMPLETE, CONFLICTING}` with `resolve_status(context, required_fields)`.
- A **closed predicate DSL**: `{all|any|not: [...]}` of leaf `{field, operator, value}` over an ENUMERATED field set (`context.jurisdictions`, `context.decision_purpose`, `candidate.concept_sensitivities`, `candidate.concepts`, …) + operators (`eq,in,contains,intersects,exists,not_exists`). `validate_predicate(p)` (reject unknown field/operator/shape) + `evaluate_predicate(p, context, candidate) -> bool`. No executable/model code.
- **Test:** the DSL evaluates the US+underwriting+proxy predicate correctly; `validate_predicate` rejects an unknown field/operator; `resolve_status` returns INCOMPLETE when jurisdiction/decision_purpose absent.
- Commit `feat(2c): closed policy predicate DSL + PolicyContext (task C1)`.

## Task C2: Event-sourced policy model (rules, lifecycle, bundles)
**Files:** migration `0977_policy_model.sql`, `policy/model.py`; tests.
- Immutable `policy_rule_version` (rule_version_id, rule_id, bundle_id, predicate jsonb [DSL], action, reason_code, message, effective_from, **effective_until**, **review_due_at**, authored_by/at, version). Append-only `policy_rule_event` (event_id, rule_version_id, event_type ∈ drafted/submitted/ratified/activated/suspended/retired, actor, authority, occurred_at, reason). `policy_bundle_version` (bundle_version_id, bundle_id, version, effective_from/until). WORM grants per 0971.
- `draft_rule(...)` → a `drafted` event (NOT active). `ratify_rule(conn, rule_version_id, *, authority, actor)` → a `ratified`+`activated` event. `active_rules(conn, bundle_version_id, *, now)` → rules whose DERIVED state is active AND `effective_from ≤ now < effective_until` — **past `review_due_at` still returns the rule** (enforcing) and flags it for a governance alert; never infers "latest bundle".
- **Test:** a drafted rule is not active; after ratify it is; a rule past `review_due_at` (but before `effective_until`) is STILL active (with a review-overdue flag); a rule past `effective_until` is not; active-state is derived from events (no mutable status column).
- Commit `feat(2c): event-sourced policy rule/bundle model (task C2)`.

## Task C3: Policy evaluation (shadow — computes, does not change dispositions)
**Files:** `policy/evaluate.py`; tests.
- `PolicyDecision{matched_rules: tuple[MatchedRule,...], effective_action, effective_reason_codes, context_status, context_hash, binding_fingerprint, concept_set_hash, bundle_version_id}`; `MatchedRule{rule_version_id, bundle_version_id, action, reason_code, message}`.
- `evaluate_policy(candidate_concepts_by_recipe, context, rules, *, now) -> dict[str, PolicyDecision]`: match ALL ratified rules whose predicate holds over `(candidate concept sensitivities, context)`; `effective_action` = strongest; **context asymmetry** — if `context_status == INCOMPLETE` AND the candidate binds a policy-relevant (sensitive/proxy) concept AND no rule already decides it → synthesize a `require_approval`-equivalent with reason `POLICY_CONTEXT_INCOMPLETE`; a non-sensitive candidate with incomplete context → `allow`. Fingerprints stamped.
- **Test:** the proxy+US+underwriting recipe → strongest action `require_approval`, all matched rules retained; a recipe matching two rules keeps both, effective = strongest; incomplete-context + sensitive concept → `require_approval (POLICY_CONTEXT_INCOMPLETE)`; incomplete-context + non-sensitive → `allow`; fingerprints present.
- Commit `feat(2c): policy evaluation + context asymmetry (task C3)`.

## Task C4: Shadow run — persist + compare to expectations (no UX change)
**Files:** `policy/shadow.py`, migration `0978_policy_shadow_decision.sql`; a policy-expectation fixture; tests.
- In the scoped considered-set path, when a policy shadow flag is on, compute `evaluate_policy` and **persist the decisions** (append-only `policy_shadow_decision`, pinned bundle_version + fingerprints) — but DO NOT change dispositions or the response's user-visible fields. A `compare_to_expectations(decisions, expected)` helper + a compliance-review fixture (like the recognizer gold set) measures false-block / false-allow before enforcement.
- **Test:** shadow decisions persisted with versions/fingerprints; the considered-set response is byte-identical to 2A/2B (no disposition change); the comparison flags a mismatch.
- Commit `feat(2c): policy shadow persistence + expectation comparison (task C4)`.

---

# Phase 2D — Policy enforcement + governed approval

## Task D1: Fold policy into the disposition pipeline (after safety, before rank)
**Files:** `taxonomy/disposition.py` (+enum), `api/routes/contract.py`; tests.
- Extend `FinalDisposition` with `POLICY_BLOCKED`, `POLICY_REVIEW_REQUIRED` (`warn` → `ELIGIBLE` + a `policy_warning` reason). Behind `FEATUREGEN_INTENT_CONTEXTUAL_POLICY`: run `evaluate_policy` AFTER safety, map action→disposition; **additive-only** (a `SAFETY_REJECTED`/`OUT_OF_SCOPE`/`UNBUILDABLE` recipe skips policy → `NOT_EVALUATED`). Pin `policy_bundle_version_id` on the run BEFORE evaluating. **Ranking (2A) now runs over the POST-policy eligible set** — blocked/review-required recipes get NO canonical rank.
- **Test:** flag off → 2A/2B-identical; on + ratified proxy rule + US-underwriting context → the proxy recipe `POLICY_REVIEW_REQUIRED` (with rule id + bundle version) and NOT ranked; an `exclude` rule → `POLICY_BLOCKED`, unranked; safety-rejected stays safety-rejected; incomplete-context asymmetry enforced.
- Commit `feat(2d): policy enforcement in the disposition pipeline (task D1)`.

## Task D2: Justify + authorized approve (append-only revision + rerank)
**Files:** `api/routes/contract.py`, migration `0979_policy_approval.sql`, `policy/approval.py`; tests.
- `POST /contract/policy/justify` (author supplies a justification for `require_justification`) — append-only. `POST /contract/policy/approvals` (an **authorized** actor for `require_approval`): checks role/authority for the rule, **no self-approval**, separation-of-duties; records an append-only `policy_approval{approval_id, generation_run_id, recipe_id, rule_version_id, decision_fingerprint, requested_by, decided_by, decision, rationale, decided_at}` bound to the **decision fingerprint** (recipe+binding+rule-version+context+snapshot). An approval whose fingerprint no longer matches the current decision is rejected (stale).
- Approval → an **append-only evaluation revision** (a new projection referencing the approval; the original decision + snapshot are NOT mutated); the eligible set is **re-ranked deterministically** (the approved recipe enters, canonical ranks/initial-view recompute). The stage retains `action=require_approval, approval_status=approved`.
- **Test:** the author cannot self-approve (403); an authorized approver flips the recipe to `ELIGIBLE` via a NEW revision (original snapshot intact) and the eligible set re-ranks; a stale-fingerprint approval is rejected; the retained action is visible.
- Commit `feat(2d): justify + authorized approval, append-only revision + rerank (task D2)`.

## Task D3: (UI) policy dispositions + role-aware approval
**Files:** `api.ts`, `WorkbenchScreen.tsx` (+ read-only rules panel), test.
- Behind `VITE_INTENT_POLICY`: the lens gains *Blocked by policy* and *Needs approval* groups (rule message + reason); the author sees **Request approval / Justify**, an actor with authority sees **Approve / Reject** (role-gated render); a policy-warning badge on warned recipes; a read-only "active policy rules" panel (human-readable + normalized predicate). Approval renders the new reranked revision. Flag off → no policy groups.
- Gates: typecheck/vitest/lint. Commit `feat(2d): policy dispositions + role-aware approval UI (task D3)`.

---

## Self-review
- **Sequencing/shadow:** 2A→2B→2C(shadow)→2D(enforce) — policy is measured against compliance expectations before it blocks anything (shadow-before-enforce), matching recognition's discipline.
- **Ranking:** presentation-priority only; canonical rank immutable under diversity; runs after policy over the eligible set; structured reason codes; binding-quality/PIT ordered above explainability; `pit_declared` used.
- **Dimensions:** recognizer proposes, human confirms (scope + persistence + UI); `target_entity` is SOFT (grain warning + rank nudge, never out_of_scope); hard entity reject deferred to Phase 3.
- **Policy:** context-incomplete never = unrestricted (sensitive-concept trigger); additive-only over safety; ratified-before-enforced with draft templates (never shipped ratified); event-sourced immutable rule/bundle versions with derived state; closed predicate DSL; all matched rules retained; `review_due_at` ≠ `effective_until`; justify ≠ authorized approve; approvals fingerprint-bound + append-only revision + rerank; versions pinned before evaluation + fingerprints persisted for replay.
- **Deferred to Phase 3 (stated, not built):** cross-catalog grounding, the richer recipe `needs` contract, and the hard `INCOMPATIBLE` entity reject.
