# Intent-Aware Feature Recipe Selection — Design & Delivery Plan

**Status:** Proposed (design agreed in dialogue; awaiting go-ahead to start Phase 0)
**Date:** 2026-07-09
**Author:** Architecture
**Related:** `docs/superpowers/specs/2026-07-08-banking-feature-template-library.md` (the 15-family library, PART F–L); the concept registry `src/featuregen/overlay/upload/concepts.py`

---

## 1. Thesis (what we are doing and why)

The template library now holds **153 recipes across 15 families**. The generator selects which recipes to show you by one test only — *can this recipe be built from the columns you uploaded?* It never asks *is this recipe relevant to what you are trying to model?* On a narrow upload that looks intelligent; on a real bank warehouse it is noisy, because a broad estate can build churn, credit, fraud, payments, cross-sell and liquidity recipes simultaneously.

We will insert an **intent layer in front of grounding**: recognise the modelling objective from the hypothesis, confirm it with the user, and let a deterministic *applicability* stage decide which recipes are in scope *before* grounding decides which are buildable.

The durable architecture we are committing to is a clean separation of **five decisions**, each with one home:

| Decision | Question | Where it lives |
|---|---|---|
| **Applicability** | Is this recipe relevant to the objective? | **NEW** — recognizer + applicability stage |
| **Buildability** | Can its ingredients bind to catalog columns? | Exists — the grounding engine |
| **Safety** | Is it universally safe & *structurally* point-in-time compliant (declared metadata — not empirically leakage-free)? | Exists — `_safe_to_bind` |
| **Policy** | Is it permitted in *this* governed context? | **NEW** (Phase 2) — ratified policy bundles |
| **Presentation priority** | How prominently should a structurally-eligible recipe be shown? (NOT measured predictive utility — no data plane) | **NEW** (Phase 2) — deterministic ranking |

The current defect is simply that **Applicability has no home**, so it collapses into Buildability. Everything below follows from giving it one.

The spine is unchanged from the rest of the platform: **the LLM proposes intent; deterministic code and the human dispose; regulatory policy is locally ratified before it enforces; universal safety is never weakened; there is no data plane, so all point-in-time / join / currency properties are declared, not executed.**

---

## 2. The problem, precisely

`gate1._template_candidates` calls `ground_all(ALL_TEMPLATES, catalog_source=…, roles=…)` **with no `use_case`**. `ground_all` already accepts a `use_case=` filter, and every `Template` already carries a `use_cases` tuple — but nothing turns "predict attrition" into "this is a churn task," so the filter is never fed and every family that grounds is surfaced.

Symptoms: **over-surfacing** on rich catalogs; **no relevance ranking** (grounded recipes are flat peers); **no contextual policy** (a `proxy`-tagged concept like geography grounds everywhere, legal in fraud, fair-lending-risky in underwriting); and in an entity-scoped **cross-catalog** run the deterministic template lens is skipped entirely (it only runs when a single `catalog_source` is in scope).

The primary current failure is **relevance noise**, not a bypass of the universal safety engine — leakage anchors and protected attributes are still refused on everything surfaced, and nothing is auto-decisioned (a human confirms every governed step; there is no data plane serving feature values). But **contextual mis-offering remains a governance risk**: universal safety cannot decide whether an otherwise-buildable, structurally-safe feature (e.g. a `proxy`-tagged concept like geography) is *appropriate* for a particular decision purpose or jurisdiction. That is exactly what the Phase-2 policy layer exists to catch. So this is a relevance-and-governance problem, not a safety-engine defect — and it was safe to defer until the library existed because the gap surfaces noise, never an unreviewed decision.

---

## 3. What already exists (the seams we build on)

This is deliberately *not* a greenfield build. The following are real today and we extend them:

- **Recipe tags** — every `Template` carries `use_cases: tuple[str, …]`. *But* across the six authoring passes this produced **107 distinct tags** at wildly mixed abstraction levels — `financial_crime` (a domain) beside `app_scam` (a typology); `credit_risk` beside `ifrs9_staging`/`frtb`/`xva` (frameworks and measures, not use-cases). This sprawl is the real Phase-0 input.
- **The use-case filter** — `ground_all(conn, templates, *, catalog_source, roles, use_case=None)` already filters by tag. Phase 1 feeds it; it does not build it.
- **Universal safety** — `_safe_to_bind` refuses leakage anchors (`default_flag`, `settlement_fail`, …) and `protected_attribute`/`special_category` concepts, on every candidate, structurally. Unconditional and unchanged by this work.
- **The rejections lens** — `ConsideredSet.rejections` already collects "what the gauntlet threw out + why" and the Workbench already renders it. The disposition model extends this surface; it is not new UI real estate.
- **The LLM seam** — `LLMClient` with a `FakeLLM` test double that matches by task-key. The recognizer is one more structured call on this seam.
- **Provenance** — features derive from `(catalog_source, object_ref)` pairs throughout; refs are already catalog-aware. Intent is persisted in `contract_intent` (hypothesis, definition, intake_mode, redacted_*, actor, target_ref) and the considered-set snapshot is persisted for reconstruction.
- **Intake modes** — the intent distinguishes **hypothesis** mode from **definition** mode (where the user brings their own feature as the anchor). Recognition applies to hypothesis-mode generation; definition-mode anchors bypass it.
- **No data plane** — grounding binds to graph-node metadata and *declares* PIT/currency; it cannot read fact rows. This bounds what Phase 3 can validate (declared graph metadata and join topology — yes; temporal coverage — no).

---

## 4. What we will build

### 4.1 A governed use-case taxonomy (the foundation)

A controlled registry, authored like `concepts.py`: stable IDs (`credit.early_warning`), a hierarchy (parent/child), a display name, a description, aliases, and **include/exclude boundary examples** (semantic boundaries matter more than label names). Framework/measure tags (`ifrs9_staging`, `frtb`, `xva`, `lcr`, `nsfr`, `lgd`) are **removed from the use-case taxonomy** into a separate *modelling-context* dimension — they are not objectives, and letting the recognizer "classify" a request as `frtb` is a category error. Every one of the 153 recipes is remapped onto this taxonomy with each association graded **primary / secondary / supporting**.

### 4.2 An LLM-only intent recognizer

Input is **only** the hypothesis + prediction goal (+ optional entity/horizon hints) — **never the catalog columns**, because buildability must not leak back into intent. It receives the **redacted** representation (`redacted_hypothesis`/`redacted_goal`, already produced by `redact_free_text` before every LLM call today), per the deployment's data-handling policy — raw banking hypotheses are not assumed safe to send to the configured provider. Output is structured and schema-validated against the *closed* taxonomy: up to one primary + two secondary use-cases, qualitative confidence (`high|medium|low`, never fake precision), evidence spans quoted from the input, a short rationale, and an explicit **`unscoped`** abstention when nothing clearly applies. No deterministic synonym router — a parallel keyword layer would duplicate the semantic judgment and drift out of sync.

### 4.3 Human confirmation at Gate #1

The recognised scope is shown with its evidence ("SME borrower" / "breach a covenant" / "next quarter" → *Credit Early Warning*), and the user confirms, edits, overrides, or chooses **"show all buildable recipes."** An explicit user-selected use-case takes precedence over recognition (the recognizer is not run to second-guess a deliberate choice). Recognition and confirmation are **append-only workflow events**, not mutable fields on the intent: `contract_intent` stays immutable (it already is — inserted once, `ON CONFLICT DO NOTHING`), and each recognition attempt and each confirmed scope is a separate append-only record (`intent_recognition_attempt`, `confirmed_generation_scope`) keyed to the intent. This preserves the full history — retries, the LLM proposal, a human override, a later broadened run, a taxonomy migration — for audit.

### 4.4 A deterministic applicability stage before grounding

`build_considered_set` gains a stage that maps the confirmed scope → a concrete set of in-scope recipe IDs, then grounds **only those**. Matching is **explicit, not automatic tree-expansion** — automatic bidirectional hierarchy inheritance would recreate over-surfacing (scoping to `credit.early_warning` must NOT drag in every `credit.risk` recipe: underwriting, pricing, collections, capital). A recipe is in scope when its own primary/secondary/supporting tag *exactly* matches a confirmed use-case, or matches an ancestor **only when the recipe declares `applies_to_descendants: true`**; a descendant tag never auto-includes a recipe against a broader confirmed scope. Multi-use-case tiering: confirmed **primary** → full applicability; confirmed **secondary** → included but placed in a lower relevance tier; **supporting** → **included in full at the applicability stage** — a hard cap here would drop structurally-relevant recipes and violate "nothing disappears silently"; volume is managed at the *presentation* stage (collapsed by default, with "show all supporting"). **Broad-parent selection is explicit, never automatic:** an LLM-recognised narrow use-case defaults to `EXACT`; when a user *deliberately* selects a broad parent (e.g. `credit.risk`) the UI offers "include all sub-use-cases?" → `INCLUDE_DESCENDANTS`, so selecting a parent never yields fewer recipes than selecting its children one by one. `scope_mode == "unscoped"` → today's ground-everything behaviour (fail-open). Applicability is its own stage producing explicit recipe IDs — grounding receives that set and stays responsible only for *can this bind*.

### 4.5 A stage-decision model with a derived disposition (transparency)

Every recipe carries a **per-stage evaluation record** — applicability, grounding, safety, policy (Phase 2), ranking (Phase 2) — each with its own decision and reasons, plus **one derived terminal disposition** for the UI: `out_of_scope` / `unbuildable` / `safety_rejected` / `policy_blocked` / `policy_review_required` / `eligible`. Ranking is an *attribute* of an eligible recipe (`canonical_rank`, `relevance_tier`, `selected_for_initial_view`), **not** a disposition — folding "eligible-but-lower-ranked" into the status field would mix five dimensions into one lossy value and quietly collapse the five-decision separation. These records live inside the existing considered-set snapshot and extend the `rejections` lens the Workbench already renders, so a user always sees whether the remedy is *broaden scope*, *upload data*, *fix metadata*, *seek approval*, or *accept manually*. Each stage record carries an **execution status** (`completed` / `failed` / `not_evaluated`) with reason codes, so a downstream stage skipped on an out-of-scope recipe reads as `not_evaluated (PRIOR_STAGE_OUT_OF_SCOPE)` — never an ambiguous `null`. Nothing disappears silently.

### 4.6 (Phase 2) Presentation-priority ranking + contextual policy

Ranking sets **presentation priority** — how prominently a structurally-eligible recipe is shown. It is emphatically **not** measured predictive utility, information value, lift, or stability (no data plane can know those). It orders eligible recipes by applicability strength, binding quality, PIT validity, explainability, and a redundancy/diversity pass — starting as **tiered ordering** (primary > secondary > supporting, then explainability), with any weighted scoring **derived from evaluation data, not asserted up front.** Policy adds *locally ratified* bundles keyed by (use-case, jurisdiction, decision purpose) that can warn / require-justification / require-approval / exclude — additive-only over universal safety, every suppressed recipe shown with its reason. The execution-implying actions (fairness-test, block-export) stay out until there is a training/export plane to enforce them on.

### 4.7 (Phase 3) Cross-catalog grounding

Teach grounding to assemble one recipe's needs across catalogs via the entity graph, so the template lens survives an entity-scoped multi-catalog run. Reframed for the no-data-plane reality: we emit a **declared** binding-and-join *plan* validated against graph metadata and **detect ambiguous join paths** (a graph-topology question, legitimately deterministic) — we do **not** claim to validate temporal coverage or "prevent future information," because that needs data we never read.

**Prerequisite, stated now so Phase 3 is not a surprise recipe-schema rewrite:** cross-catalog grounding needs a **richer `needs` contract** than today's `(role, concept)` — each need must declare required grain, join role (entity-anchor vs measure), required aggregation, temporal semantics, and unit/currency compatibility, and the entity graph must declare edge cardinality, allowed traversal direction, and whether an edge is authoritative or inferred. None of this is Phase-0 work, but Phase 3's design assumes it exists.

---

## 5. Design invariants (non-negotiable)

1. **Recognition never sees catalog columns.** Enforced at the interface.
2. **Fail-open is asymmetric.** Relevance uncertain → broaden. Policy context unresolved → do *not* assume unrestricted. Ambiguous join → fail explicitly. Never one blanket "when unsure, allow."
3. **Universal safety is unconditional and additive-only.** The policy gate can only *add* restrictions; it can never relax `_safe_to_bind`.
4. **The LLM proposes; it never disposes.** It cannot invent taxonomy values, silently suppress recipes, enforce policy, or define canonical rank.
5. **Everything is inspectable.** Scoped-out and policy-suppressed recipes surface with reasons.
6. **No data-plane assumptions.** All PIT/join/currency properties remain declarations.
7. **Shadow before filter.** Scoping does not go live until the false-narrowing rate is defensible.

### Recognizer runtime contract

Bounded timeout; at most one structured retry; strict enum validation (no dynamic taxonomy creation); deterministic model settings where supported; caching keyed by (normalised-input-hash, taxonomy version, model, prompt version); telemetry on latency / errors / retries / abstention. **Every failure mode resolves uniformly** — timeout, invalid or empty structured response, unknown taxonomy ID, service unavailable, prompt-injection attempt, repeated low-confidence: record a failed recognition attempt, set `scope_mode = unscoped`, continue with full grounding, and show a non-blocking explanation. The recognizer never blocks generation.

### Feature flags & rollback

Each phase maps to a runtime flag — `intent_recognition_shadow`, `intent_confirmation_ui`, `intent_scoped_applicability`, `intent_disposition_lens`, `intent_deterministic_ranking`, `intent_context_policy`, `cross_catalog_template_grounding` — so recognition can run while filtering is off, the confirmation UI can be toggled independently, and policy rollout is isolated from ranking. Canary by user / workspace / project. **Emergency rollback for 1B:** disable `intent_scoped_applicability` → recognition records are retained → return to full grounding, telemetry intact.

---

## 6. Phased delivery

### Phase 0 — Governed taxonomy + recipe remap *(foundation; no user-visible change)*
**Build:** (1) the **use-case taxonomy registry** (hierarchical, stable IDs, boundary include/exclude examples); (2) a separate **modelling-context registry** for the framework/measure tags (`ifrs9_staging`/`frtb`/`xva`/`lcr`/`nsfr`/`lgd`) split out of the use-case dimension; (3) a **legacy-tag crosswalk** — every one of the 107 current tags → {dimension, target ID, recipe remap, status} — proving no tag vanished (the same backward-compat discipline the concept registry's legacy aliases already follow); (4) remap all 153 recipes to `primary/secondary/supporting` with `applies_to_descendants` set where inheritance is intended; (5) a **governance contract** (taxonomy owner, mapping approver, version-bump / deprecation / alias rules, review cadence, unknown-ID resolution).
The crosswalk `dimension` is **multi-valued**, not just use-case-vs-framework: `use_case | modelling_context | domain | typology | product | measure | deprecated`. First deliverable is a **dimension-classification report** for all 107 tags — so cleanup *reclassifies* category errors rather than moving them around.
**Validation:** 153/153 mapped; every recipe has ≥1 primary-or-secondary use-case; no recipe scoped only through an over-broad root; every legacy tag appears in the crosswalk with a dimension; **no framework/measure/typology remains in the use-case dimension**; deprecated IDs resolve deterministically; taxonomy validates at import (no dup / dangling parent); a taxonomy-leaf → recipes coverage report flags any accidental zero-recipe leaf.
**Why first:** recognition and applicability are only as good as this mapping. It is the critical path and behaviour-neutral (grounding still grounds everything until Phase 1B feeds the filter), so it ships safely on its own.
**Exit:** all validation checks green; crosswalk + governance contract reviewed by the taxonomy owner.

### Phase 1A — Shadow recognition *(measured, still no filtering)*
**Build:** the recognizer (LLM-only, structured, abstaining) on the `LLMClient` seam with `FakeLLM` fixtures; a gold evaluation set of representative hypotheses; run recognition on every request but **still ground everything**; log proposed vs expert scope.
**Why:** de-risk the only thing that can hurt — hiding a relevant recipe — before it can hide anything.
**Exit (objective; thresholds co-owned with the gold-set owner and fixed *before* shadow starts):** the hard gate is **zero false-narrowing on the designated regulated/high-risk test set**; the softer target is **≥98% recall** of expert-relevant recipes on the general set. Plus: **100%** rejection of unknown taxonomy values; **100%** fallback-to-`unscoped` on any recognizer failure; correct **abstention ≥90%** on deliberately-unscoped examples; repeated-run scope **stability ≥98%** under pinned config. Expert override rate on the proposed primary is tracked (not gated).

### Phase 1B — Scoped grounding live *(first user-visible value)*
**Build:** Gate #1 confirmation UI (evidence + override + "show all"); the applicability evaluator producing **concrete in-scope recipe IDs**, passed directly to grounding (`ground_all(in_scope_templates, …)` — the existing signature already takes an explicit template list, so `use_case=` is never the boundary); the per-stage evaluation records + derived-disposition lens; the `unscoped` fallback; **append-only** recognition + confirmed-scope records (the intent stays immutable). **Definition-mode** intake bypasses recognition *and* applicability narrowing — but NOT grounding validation, universal safety, contextual policy (Phase 2), provenance, or PIT declarations; where policy context is required it is supplied explicitly or inherited from the governed project context.
**Exit:** a churn request stops surfacing interchange/cross-sell candidates; broaden-scope always available; every recipe carries per-stage decisions + a terminal disposition.

### Phase 2A — Deterministic ranking
Tiered relevance ordering + redundancy/diversity + rank explanations; the LLM "recommended starting set" persisted *separately* from the canonical rank and the human selection. Weights (if any) derived from the gold set, not hardcoded.

### Phase 2B — Contextual policy proof
**One** locally-ratified rule end-to-end (recommended: proxy-concept → require-approval in a US underwriting context), with the ratification workflow and the policy disposition surfaced. Not a rulebook — a proven pattern.

### Phase 3 — Cross-catalog grounding
Entity-graph join assembly with ambiguous-path detection and a declared binding-and-join plan; keeps provenance exact and PIT/currency declared. Largest lift, sequenced last.

---

## 7. What we are deliberately NOT building (the architect's filter)

- **No weighted ranking formula up front.** Inventing weights before the gold set is false precision; start tiered, derive weights from data.
- **No UUID column-ref migration.** `(catalog_source, object_ref)` is already catalog-aware; we don't pay a migration for forward-compat we largely have.
- **No execution-implying policy actions** (fairness-test, block-export) — there is no training/export plane to enforce them on.
- **No temporal/coverage join *validation*** — the platform cannot read rows; Phase 3 validates topology and declares the rest.
- **No parallel synonym/keyword router** — the LLM plus a closed taxonomy plus human confirm covers it; a second router would only drift.
- **No full version matrix in Phase 1** — persist the recognition record + taxonomy version now; version policy/ranking when those layers exist.

---

## 7b. Detailed-design decisions carried into Phase 1/2 (review round 2)

These sharpen implicit runtime semantics; they do not change direction.

- **Multi-dimension recognition (one LLM call).** The recognizer outputs several *closed* dimensions — primary/secondary use-cases, `modelling_contexts`, `target_entity`, `prediction_horizon` — not one merged taxonomy. **Phase-1B wiring:** use-case drives applicability; `target_entity` is a hard-incompatibility reject; `modelling_context`/`horizon` are recognised and persisted but wired into ranking-boost + policy in **Phase 2**. Framework-specific recipes may declare a `required_modelling_context`.
- **Scope expansion is explicit.** `EXACT` (default for a recognised narrow use-case) vs `INCLUDE_DESCENDANTS` (a user-selected broad parent, UI-confirmed). Selecting a parent never returns fewer recipes than selecting its children.
- **Supporting recipes are never capped at applicability** — only collapsed at presentation.
- **Stage records carry `execution_status`** (`completed`/`failed`/`not_evaluated`) so skipped stages are self-explanatory, not ambiguous nulls.
- **Run lineage + idempotency.** Every `ConfirmedGenerationScope` binds to a `generation_run_id` and a `supersedes_scope_id`; the considered-set points to `scope_id` + `generation_run_id`. The governing scope is **never** derived via `ORDER BY confirmed_at DESC` (fragile under retries/concurrency). Recognition and confirmation writes carry idempotency keys.
- **"Show all buildable recipes" is a new run, not a reveal.** Out-of-scope recipes weren't grounded, so broadening appends an `unscoped` scope (`supersedes_scope_id` = the original) + a new generation run + a new snapshot; the UI may merge visually, but persistence keeps both evaluations. This also enables a later precise "add `credit.collections` to this scope" without a full unscoped run.
- **Recognition status taxonomy:** `CLASSIFIED` / `AMBIGUOUS` / `UNSCOPED` / `TECHNICAL_FAILURE`. Low confidence is `AMBIGUOUS` (semantic uncertainty → offer alternatives), *not* a failure. Prompt injection: the hypothesis is untrusted data isolated in-prompt, output is schema-validated, invalid → fallback; the recognizer has no tools/authority so injection impact is bounded — detection is telemetry, not a correctness dependency.
- **Version quintet persisted from Phase 1** (not the full matrix, but these five are load-bearing for reproducibility): `taxonomy_version`, `applicability_mapping_version`, `recognizer_model_id`, `prompt_version`, `recipe_registry_version`. (Same recognition + changed recipe mapping = different candidate set — the mapping version is what explains it.)
- **Evaluation gate (Phase 1A) measures two things.** *Recognition accuracy* (did the LLM identify the use-cases) is necessary but not sufficient; **applicability recall** (after mapping + inheritance, were expert-relevant recipes retained) is the true 1B release gate — a perfect recognizer + a wrong mapping still causes false narrowing. Gate design requires per-leaf/per-parent example minimums, explicit ambiguous/unscoped/multi-use-case/regulated examples, ≥2 labelers + adjudication on the high-risk set, and **per-use-case** metrics (not just aggregate — 98% recall is meaningless if one framework has two examples).
- **Policy contract shape (Phase 2).** Rules evaluate predicates over a **versioned `PolicyContext`** (use-cases, modelling-contexts, jurisdiction, decision-purpose, legal-entity, product, lifecycle, automation-level), not a hard-coded 3-column key; Phase-2B populates only the first three. Action→disposition is deterministic (allow→eligible; warn→eligible-with-warning; require-justification/approval→`policy_review_required` until satisfied; exclude→`policy_blocked`), and the stage record **retains the original action** after an approval (`action=require_approval, approval_status=approved`).

---

## 8. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Recognition hides a relevant recipe | Human confirm + `unscoped` fallback + one-click broaden + visible out-of-scope + shadow-measured false-narrowing before go-live |
| Taxonomy incomplete/overlapping | Hierarchy + boundary examples + abstention + secondary use-cases + gap capture; frameworks split out to avoid category errors |
| Recognizer drifts on model/prompt change | Versioned prompt + gold-set regression gate + preserved historical recognitions |
| Users rubber-stamp the proposal | Show evidence spans; require explicit confirm for governed use-cases; monitor immediate broaden/override rates |
| Recognition used as a policy bypass | Policy context confirmed separately; unresolved context ≠ unrestricted; universal safety independent |

---

## 9. Immediate next step

Start **Phase 0**: reconcile the 107 real tags into the governed hierarchy (frameworks split out), remap all 153 recipes with primary/secondary/supporting, and land the validation tests — a self-contained, behaviour-neutral change that unblocks the recognizer, the applicability stage, and ranking. On approval, this becomes a task-by-task implementation plan under `docs/superpowers/plans/`.
