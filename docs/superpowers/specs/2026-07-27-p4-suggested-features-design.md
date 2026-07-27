# P4 — Suggested Features (design)

Date: 2026-07-27 · Status: design for review · Parent: `2026-07-22-scaled-ai-attestation-design.md` (§4A, §5a, §8-P4) · Design target: the user's validated mockup **"Workbench · Suggested Features"** (artifact `9bcf322f`)

> **The headline: this is largely an UN-BURYING, not a build.** The hypothesis-free, per-table
> feature-proposal engine already exists, is deterministic, needs no LLM, and is fully tested — it simply has
> exactly one call site, inside the hypothesis-driven flow, so nothing can reach it. P4 exposes it.

## Why this phase exists

The program's own design (§4A, §8) called for a **thin feature-ideation demonstrator pulled early "so the
program shows its payoff before the full P4."** It was scheduled to ride with P2 — and P2 is blocked on human
gold labels, so the demonstrator was deferred with it. The result: every phase since has built plumbing
(enrichment, governance, provenance, cascade) with **no surface where a user sees the payoff.** P4 is that
surface, and it is the honest answer to "where is the improvement?"

## The engine already exists

**`gate1._template_candidates(conn, *, catalog_source, roles, target_ref=None, now=None, templates=ALL_TEMPLATES, fresh_within=...)`** (`contract/gate1.py:146-198`):
- wraps `templates.ground_all` (`templates.py:332-343`) — *"a deterministic grounding engine — NO LLM"* — whose only required inputs are a connection, the template registry and a `catalog_source`;
- converts each `GroundedFeature` to a `FeatureIdea` deterministically (`_idea_from_grounded`, `:133-143`);
- runs **the same gauntlet** as LLM candidates (`feature_assist._validate_idea`, `:607`), returning ideas + typed rejections;
- accepts `target_ref=None` **and** `now=None` — **no intent, no hypothesis, no LLM, no use_case.**

**153 templates across 117 families** in 15 domain sets (`templates.py:4092-4097`), and *"grounding is the
router"* (`templates.py:16-17`) — a family surfaces only where its distinctive concepts exist in the catalog.
So the suggestions are already catalog-shaped, not a generic list.

Its **only** call site today is inside `build_considered_set`. That is the whole gap.

## What the mockup needs: FREE / CHEAP / NEW

**FREE (exists, verbatim):**
- **Descriptions** — `Template.intent` is a hand-authored SME sentence, already the mockup's prose (e.g. *"OLS slope of a customer's balance vs time over a trailing window — the core deposit-drain / attrition signal."*).
- **"clean & ready" vs "need review"** — the tri-state maps 1:1: `DESIGN_CHECKED` vs `NEEDS_EXTERNAL_VALIDATION`.
- **"leakage safe"** — by construction: `templates._safe_to_bind` (`:150-164`) refuses leakage-anchor/protected columns; `planner/safety.evaluate_column_safety` gives the reason-bearing view.
- **"time-safe (as-of)"** — `NO_POINT_IN_TIME` hard reject + the `TEMPORAL_IS_POPULATED` requirement + `Template.pit` (the human-readable rule).
- **The warning notes** — `Template.eligibility` already carries them: *"single currency — convert to base first"* IS the mockup's fils/minor-unit warning; `near_label` (*"⚠ NEAR-LABEL: if churn is defined as 'no activity in N days' this ≈ the label"*) IS the "needs review" chip.
- **Rejection vocabulary** — a closed 17-code `RejectCode` set with messages.

**CHEAP (small glue):**
- **Per-table filter** — `GroundedFeature.grain_table == table`.
- **Entity grouping** — `FeatureIdea.grain_ref` already *is* `cif_id` / `foracid` / `tran_id` (`feature_assist.py:735-744`).
- **The recipe line** — every part is on the validated idea (`operation_kind`, `measure_refs`, `grain_ref`, `time_ref`, `window`); a ~20-line renderer. *(Subtract `grain_ref`/`time_ref` from `measure_refs` to get the true measure column.)*
- **Summary counts**, a read-only `GET` route, and a route entry.

**NEW (real build — defer out of v1):**
- **The relevance %** — see §"Honesty" below.
- **"blocked · missing input — needs a governed `account_balance` from another source"** — `ground_template` returns bare `None` on an unmet required need (`templates.py:287`), discarding *which* need and concept failed, so the template is silently absent. Capturing it is a small additive change (~30 lines) but it is a real change.
- **"Find source"** action and **durable Dismiss** (today's `avoid` list is transient, per-generation-round).

## Honesty: do NOT render a fake relevance percentage

The mockup shows a relevance bar. **No percentage scorer exists anywhere.** A ranker does exist —
`taxonomy/ranking.rank_eligible` (`ranking.py:334`) — but it yields an **ordinal** `canonical_rank`, and its
top axis (`relevance_tier`) requires a recognized use case, i.e. a hypothesis. Four of its five axes *are*
hypothesis-free (binding quality, PIT completeness, modelling-context fit, explainability).

**v1 renders rank/tier, not a %.** Inventing a percentage would be exactly the kind of confident-looking
fabrication this program exists to prevent. (A calibrated score can come later, with the hypothesis-bearing
axis, if it earns its place.)

## Accept: make it a funnel INTO governance, not a bypass

`POST /contract/confirm` hard-requires `intent_id` + a recorded Gate-#1 choice + a **server-persisted**
considered set (`api/routes/contract.py:672-687`), and `submit_intent` refuses a blank hypothesis
(`intake.py:59-60`). So a suggestion generated without an intent **cannot** be governed as-is. Two options:

- **(a) Synthesize a system intent** at Accept-time — the chain works unchanged, but the hypothesis is a
  fiction recorded as a governance artifact. **Rejected.**
- **(b) Accept PROMPTS for the hypothesis**, then runs the existing governed flow with the suggestion
  pre-selected. **Recommended.** Discovery-first: the suggestion is what makes the user *want* a feature; the
  hypothesis is captured at the moment they commit. The governance artifact stays truthful, and P4 becomes a
  funnel into the existing flow rather than a second, weaker path around it.

## Surface: a new screen, not a mode

`WorkbenchScreen.tsx` is **2549 lines**, one component, three feature flags, with a scope-invalidation state
machine (scope edits clear candidates, sequence guards, feedback rounds). Adding a hypothesis-free mode inside
it would fight that machine. Follow the **`asset` detail-sheet precedent** (`App.tsx:138-145` — a route
deliberately excluded from the left rail): either a new `'suggested'` hash route, or a section on the existing
per-asset detail sheet, which already builds a bounded per-asset read model.

## V1 — the thin slice (ship this first)

**One read-only endpoint** — `GET /catalogs/{source}/tables/{table}/suggestions`:
calls `_template_candidates(conn, catalog_source=source, roles=…, target_ref=None, now=None)`, filters to
`grain_table == table`, groups by `grain_ref`, and returns per card:
`{name, description (= Template.intent), recipe_parts, validation_status, requirements, near_label,
eligibility, uses[]}` plus the summary counts and the typed rejections.

**One read-only surface** rendering the mockup minus the deferred pieces: grouped by entity, chips from the
tri-state + near-label, warning notes from `eligibility`, **rank/tier instead of a %**, and **no
Accept/Dismiss in v1**.

That ships *"AI proposed 14 features from your upload — 11 clean, 3 need review"* on day one, and defers every
genuinely new mechanism.

## V2 (after v1 proves the surface)
Blocked-reason capture (the unmet `Need`), Accept → hypothesis prompt → existing governed flow, durable
Dismiss, "Find source" (cross-catalog), and a calibrated relevance score if justified.

## How this connects to the other phases
- **E1a (built)** — richer, governed metadata is what these suggestions are grounded on; this is where it becomes visible.
- **E4** — its gating fields are exactly the mockup's amber chips ("confirm scale" = `unit`). E4 turns amber cards green; P4 is where anyone can *see* that happen.
- **P2/P3** — unaffected; P2 stays blocked on gold labels.

## Success criteria
- After an upload, a user opens a table and sees **grouped, catalog-grounded suggestions with honest statuses** — no hypothesis typed.
- Counts are real: "clean & ready" = `DESIGN_CHECKED`, "need review" = `NEEDS_EXTERNAL_VALIDATION`.
- Every chip and warning traces to a real code path (no invented text, no invented %).
- **Zero new authority**: v1 is strictly read-only and cannot govern, accept, or write anything.
- Families that don't ground on this catalog do not appear (grounding is the router).

## Risks
- **Fabricated confidence** — the relevance %; mitigated by rendering rank/tier.
- **Suggestion flood** — 153 templates could ground widely; cap per entity/table and rank, and report what was truncated.
- **Accept-path fiction** — mitigated by prompting for the hypothesis rather than synthesizing one.
- **The mockup implies actions v1 won't have** — ship it visibly read-only rather than with dead buttons.

## Deferred NFRs
Caching/precomputation of suggestions, dashboards, bulk accept, per-suggestion telemetry, cost controls.
