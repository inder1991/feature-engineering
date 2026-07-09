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
| **Safety** | Is it universally safe & point-in-time honest? | Exists — `_safe_to_bind` |
| **Policy** | Is it permitted in *this* governed context? | **NEW** (Phase 2) — ratified policy bundles |
| **Utility** | How useful/prominent should an eligible recipe be? | **NEW** (Phase 2) — deterministic ranking |

The current defect is simply that **Applicability has no home**, so it collapses into Buildability. Everything below follows from giving it one.

The spine is unchanged from the rest of the platform: **the LLM proposes intent; deterministic code and the human dispose; regulatory policy is locally ratified before it enforces; universal safety is never weakened; there is no data plane, so all point-in-time / join / currency properties are declared, not executed.**

---

## 2. The problem, precisely

`gate1._template_candidates` calls `ground_all(ALL_TEMPLATES, catalog_source=…, roles=…)` **with no `use_case`**. `ground_all` already accepts a `use_case=` filter, and every `Template` already carries a `use_cases` tuple — but nothing turns "predict attrition" into "this is a churn task," so the filter is never fed and every family that grounds is surfaced.

Symptoms: **over-surfacing** on rich catalogs; **no relevance ranking** (grounded recipes are flat peers); **no contextual policy** (a `proxy`-tagged concept like geography grounds everywhere, legal in fraud, fair-lending-risky in underwriting); and in an entity-scoped **cross-catalog** run the deterministic template lens is skipped entirely (it only runs when a single `catalog_source` is in scope).

The failure mode is **noise, not danger** — universal safety still refuses leakage anchors and protected attributes on everything surfaced. That is why this is a relevance problem, not a safety problem, and why it was safe to defer until the library existed.

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

Input is **only** the hypothesis + prediction goal (+ optional entity/horizon hints) — **never the catalog columns**, because buildability must not leak back into intent. Output is structured and schema-validated against the *closed* taxonomy: up to one primary + two secondary use-cases, qualitative confidence (`high|medium|low`, never fake precision), evidence spans quoted from the input, a short rationale, and an explicit **`unscoped`** abstention when nothing clearly applies. No deterministic synonym router — a parallel keyword layer would duplicate the semantic judgment and drift out of sync.

### 4.3 Human confirmation at Gate #1

The recognised scope is shown with its evidence ("SME borrower" / "breach a covenant" / "next quarter" → *Credit Early Warning*), and the user confirms, edits, overrides, or chooses **"show all buildable recipes."** An explicit user-selected use-case takes precedence over recognition (the recognizer is not run to second-guess a deliberate choice). The confirmed scope is persisted.

### 4.4 A deterministic applicability stage before grounding

`build_considered_set` gains a stage that maps the confirmed scope → in-scope recipe IDs using the taxonomy hierarchy (exact / parent / child / supporting), then grounds **only those**. `scope_mode == "unscoped"` → today's ground-everything behaviour (fail-open). Applicability is its own stage, not a flag buried in grounding — grounding stays responsible only for *can this bind*.

### 4.5 A disposition model (transparency)

Every recipe gets exactly one inspectable outcome, extending the existing rejections lens: **out-of-scope / relevant-but-unbuildable / universal-safety-rejected / (Phase 2) policy-restricted / (Phase 2) eligible-lower-ranked / eligible.** A user can always tell whether the remedy is *broaden scope*, *upload data*, *fix metadata*, *seek approval*, or *accept manually*. Nothing disappears silently.

### 4.6 (Phase 2) Deterministic ranking + contextual policy

Ranking orders eligible recipes by applicability strength, binding quality, PIT validity, explainability, and a redundancy/diversity pass — starting as **tiered ordering** (primary > secondary > supporting, then explainability), with any weighted scoring **derived from evaluation data, not asserted up front.** Policy adds *locally ratified* bundles keyed by (use-case, jurisdiction, decision purpose) that can warn / require-justification / require-approval / exclude — additive-only over universal safety, every suppressed recipe shown with its reason.

### 4.7 (Phase 3) Cross-catalog grounding

Teach grounding to assemble one recipe's needs across catalogs via the entity graph, so the template lens survives an entity-scoped multi-catalog run. Reframed for the no-data-plane reality: we emit a **declared** binding-and-join *plan* validated against graph metadata and **detect ambiguous join paths** (a graph-topology question, legitimately deterministic) — we do **not** claim to validate temporal coverage or "prevent future information," because that needs data we never read.

---

## 5. Design invariants (non-negotiable)

1. **Recognition never sees catalog columns.** Enforced at the interface.
2. **Fail-open is asymmetric.** Relevance uncertain → broaden. Policy context unresolved → do *not* assume unrestricted. Ambiguous join → fail explicitly. Never one blanket "when unsure, allow."
3. **Universal safety is unconditional and additive-only.** The policy gate can only *add* restrictions; it can never relax `_safe_to_bind`.
4. **The LLM proposes; it never disposes.** It cannot invent taxonomy values, silently suppress recipes, enforce policy, or define canonical rank.
5. **Everything is inspectable.** Scoped-out and policy-suppressed recipes surface with reasons.
6. **No data-plane assumptions.** All PIT/join/currency properties remain declarations.
7. **Shadow before filter.** Scoping does not go live until the false-narrowing rate is defensible.

---

## 6. Phased delivery

### Phase 0 — Governed taxonomy + recipe remap *(foundation; no user-visible change)*
**Build:** the taxonomy registry (hierarchical, boundary examples, frameworks split out); remap all 153 recipes to `primary/secondary/supporting`; validation tests (every recipe tag resolves; taxonomy has no dup/dangling parent; coverage report of taxonomy-leaf → recipes).
**Why first:** recognition and applicability are both only as good as this mapping. It is the critical path and it changes no behaviour (grounding still grounds everything until Phase 1B feeds the filter), so it ships safely on its own.
**Exit:** 153/153 recipes mapped; taxonomy validates at import; a coverage report exists.

### Phase 1A — Shadow recognition *(measured, still no filtering)*
**Build:** the recognizer (LLM-only, structured, abstaining) on the `LLMClient` seam with `FakeLLM` fixtures; a gold evaluation set of representative hypotheses; run recognition on every request but **still ground everything**; log proposed vs expert scope.
**Why:** de-risk the only thing that can hurt — hiding a relevant recipe — before it can hide anything.
**Exit:** **false-narrowing rate** (how often the proposed scope would drop an expert-relevant recipe) is measured and defensible; abstention behaves on exploratory inputs.

### Phase 1B — Scoped grounding live *(first user-visible value)*
**Build:** Gate #1 confirmation UI (evidence + override + "show all"); the applicability stage feeding `ground_all(use_case=…)`; the disposition model + extended rejections lens; the `unscoped` fallback; persist recognition + confirmed scope (new scope fields on the intent). Definition-mode intake bypasses recognition.
**Exit:** a churn request stops surfacing interchange/cross-sell candidates; broaden-scope always available; every recipe carries a disposition.

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
