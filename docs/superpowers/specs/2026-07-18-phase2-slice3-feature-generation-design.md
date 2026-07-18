# Phase-2 Slice 3 — Honest Feature Generation (Half A) — Design

**Date:** 2026-07-18
**Status:** Draft for user review
**Scope:** **Half A only** (generation-side). Half B (the external-attestation round-trip) is explicitly deferred to its own spec.
**Predecessor:** Phase-2 Slices 1 & 2 (merged to `origin/main`). Slices 1–2 made *ingestion* store rich, honest per-column information; Slice 3 makes the *downstream feature-suggester* actually use it and answer honestly.

## Where this sits

Two separate stages, one-directional:
- **Ingestion** (Phase-1, Slices 1–2, done) — builds the **catalog** (`graph_node`: every table/column with its concept, declared type, grain, entity, additivity, etc.).
- **Feature suggestion** (Slice 3) — runs *later*, on a user request ("features for churn"), reads the catalog, and the LLM proposes concrete features. It never touches the upload path; it consumes what ingestion stored.

Slice 3 lives in `overlay/upload/feature_assist.py` (the suggester) — **unchanged by the recent merge**. The parallel "3C.1 gate" subsystem is a *different concern* (it gates the planner's classifier trustworthiness over shadow telemetry; it never touches `feature`/`FeatureIdea`) — Slice 3 does **not** build on it. The natural home is the existing **verification-stamp ladder** (`governance/attributes.py:14`: `DESIGN-CHECKED, DATA-CHECKED, USEFULNESS-CHECKED`) and the deterministic gauntlet `_validate_idea` (which the governed contract flow already re-runs at `confirm_contract`).

## Problem

1. **The suggester ignores the enrichment.** `_candidate_columns` selects only `concept/domain/definition`; `_menu` then discards even `definition`, sending the LLM just `object_ref/table/column/concept/domain`. Everything Slices 1–2 stored — declared type, grain, entity, additivity, unit, currency — is thrown away, so the LLM proposes vague or ungroundable features.
2. **The binary result dead-ends real features.** `_validate_idea` returns `(FeatureIdea, None)` (fine) or `(None, Rejection)` (rejected). Because this platform has **no data access**, FTR operational types are permanently `unknown`; a numeric feature like "customer total spend" can never be *proven* safe here, so it is silently rejected forever — even though it is structurally fine and only needs a real-data check.

## Architecture — the honest three-state result

Extend the **existing** gauntlet `_validate_idea` to classify a feature into three states (single source of truth; the `confirm_contract` MCV re-run inherits it automatically):

| State | Meaning | Existing analog |
|---|---|---|
| `DESIGN_CHECKED` | Structurally safe with the authority available now. | today's earned `DESIGN-CHECKED` |
| `NEEDS_EXTERNAL_VALIDATION` | Structurally plausible, but rests on facts this platform can't verify. Carries a machine-readable **requirements** list. | the empty gap between `DESIGN-CHECKED` and `DATA-CHECKED` (slot exists on `feature_versions`, nothing mints it) |
| `REJECTED` | Deterministically invalid / provably wrong / unauthorized. | today's gauntlet drop-with-`Rejection` |

**Half A emits this on the `FeatureIdea` proposal** (surfaced in the `POST /features/recommend` response); it does **not** persist a new state or mint `DATA-CHECKED` (that is Half B). `FeatureIdea.verification` is replaced/augmented so a proposal carries `{state, requirements}`.

### Classifying today's checks (REJECTED vs NEEDS_EXTERNAL_VALIDATION)

`_validate_idea`'s current failures split as:
- **REJECTED (broken / provably wrong / unauthorized):** `UNGROUNDED`, `AMBIGUOUS_CATALOG`, `UNKNOWN_COLUMN` (the column doesn't exist/resolve); `LEAKAGE` (derives from the target); `STALE` (the catalog's drift watermark already fails the freshness requirement — a real, verifiable fact); a **confirmed** non-additive column under a summing aggregation (`additivity` is governed — a confirmed value proves it wrong); an **unauthorized** join key.
- **NEEDS_EXTERNAL_VALIDATION (plausible, rests on unverified facts):** a numeric aggregation whose target column's **operational type is not confirmed numeric** (the FTR core case) → `TYPE_IS_NUMERIC`; a grain-dependent feature whose grain is **proposed-not-confirmed** → `GRAIN_IS_UNIQUE`; a windowed feature whose point-in-time column is **declared-but-unconfirmed** → `TEMPORAL_IS_POPULATED` (+ `TEMPORAL_LAG_BOUNDED` where a lag basis applies); mixed **unverified** unit/currency → `CURRENCY_CONSISTENT`; a cross-table feature needing a join that is not a `VERIFIED` `approved_join` (but the key IS authorized) → `JOIN_CONNECTIVITY`.

**Requirements vocabulary (initial, closed):** `TYPE_IS_NUMERIC`, `GRAIN_IS_UNIQUE`, `TEMPORAL_IS_POPULATED`, `TEMPORAL_LAG_BOUNDED`, `JOIN_CONNECTIVITY`, `CURRENCY_CONSISTENT`. Each requirement names the column(s)/join it concerns.

New checks Half A adds to the gauntlet: the **operational-type-for-numeric-op** check (today only `route_strategies._is_numeric` consults it, to disable strategies; the gauntlet doesn't), and the **grain-confirmed** / **as-of-confirmed** distinctions (via the authority adapter below).

**Deliberate boundary on additivity.** Additivity is already governed with a sensible default: the gauntlet rejects only a *confirmed* semi/non-additive column under a sum, and treats *unresolved* additivity as additive (fail-open). Half A keeps that behavior — a confirmed semi/non-additive sum stays `REJECTED`, and unresolved additivity stays `DESIGN_CHECKED` — rather than flipping every unresolved-additivity sum to `NEEDS_EXTERNAL_VALIDATION`, which would reclassify most of the existing safe set to needs-check. Half A's `NEEDS_EXTERNAL_VALIDATION` therefore targets the axes where the FTR gap is acute and the platform genuinely cannot verify (operational type, grain uniqueness, temporal population/lag, join connectivity, currency consistency). Tightening additivity to fail-closed is a deliberate follow-on, not Half A.

## The authority adapter — `OperationalColumnFacts{value, authority, provenance}`

"Present on `graph_node`" ≠ operational. New adapter reads a column field and returns its authority tier:
- **`governed`** — has an OPERATIONAL-ceiling field policy + a `*_decision_id` link, read via the decision (never the flat column): `additivity` (`additivity_decision_id`), the structural type `logical_representation`/`semantic_type` (`logical_type_decision_id`), `sensitivity`, `temporal_role`; and `is_grain`/`is_as_of` when backed by a non-null `*_fact_event_id` (a confirmed governed fact). *(Precedent: `route_strategies` already distinguishes operational vs display-only on join edges via `approved_join_status='VERIFIED'`.)*
- **`hint`** — flat display columns with no policy: `unit`, `currency`, column-level `entity`, raw `data_type`, and the glossary `declared_type`. Table-level `primary_entity` is RECOMMENDATION-ceilinged (structurally never operational) → also a hint.

**The rule (fail-closed): a hint may only TIGHTEN, never APPROVE.** A hint can trigger `REJECTED` or `NEEDS_EXTERNAL_VALIDATION`, but only a `governed` value may *clear* a check or yield `DESIGN_CHECKED`. `declared_type` (a hint) may reject a non-numeric operation but never approves a numeric one → that is precisely why the FTR numeric feature lands in `NEEDS_EXTERNAL_VALIDATION`, not `DESIGN_CHECKED`. This preserves the current gauntlet's safe behavior (it already *rejects* on flat unit/currency — a tightening) while making unverified-but-plausible cases honest instead of silently dropped.

## Menu enrichment (make the suggester use the rich info)

- **Widen `_candidate_columns`** to also select `data_type`, `declared_type`, `semantic_terms`, `entity`, `additivity`, `unit`, `currency`, `is_grain`, `is_as_of`, and the `*_fact_event_id` provenance links. Keep the existing read-scope filter (`allowed_sensitivities(roles)`).
- **Stop `_menu` discarding.** Emit per column: concept, domain, **sanitized** definition + semantic_terms, operational_type, `declared_type_hint`, entity, additivity, unit, currency, is_grain, is_as_of — each governed field wrapped by `OperationalColumnFacts` so the LLM sees `{value, authority}` (`governed` / `hint`), never a bare display value.
- **Per-table context** — one block per table (a table definition, the confirmed grain columns and as-of column, the primary entity tagged advisory), **assembled only from the authorized candidate rows** — never a second, unfiltered query. If every column of a table is read-scope-excluded, emit no context for it. "Confirmed grain/as-of" requires a non-null `*_fact_event_id`, not merely a true flag.

## Deterministic relevance selection (don't overload the prompt)

For a 126-column table, do not send every full description. A pure, deterministic selector:
- **Objective parsing:** the request's target entity + concepts/domains (reuse the recognizer / `known_entities()`). `roles` in `feature_assist` is an *authorization* role — do not overload it as a semantic role.
- **Mandatory set (always included):** confirmed grain columns, the as-of column, columns whose `entity` matches the objective's entity.
- **Scorer:** normalized match — entity > concept > domain — as an integer; stable tie-break by `object_ref`.
- **Hard bound:** a serialized **byte/token budget** is the real limit (column count is not). Select mandatory first, then by score until the budget; summarize the rest compactly.
- **Overflow:** if the *mandatory* set alone exceeds the budget, deterministically chunk or return `CONTEXT_TOO_LARGE` — never dispatch an oversized request.
- **Durable truncation stats:** `log()` + a recorded dropped/summarized count (no silent truncation).

## Authorization threading

The join-connectivity check needs the caller's `roles` to exclude restricted join keys (`find_join_path` requires them). Today `_validate_idea`/`_vet`/refinement/contract MCV do **not** carry roles. Thread authorization through every validation and revalidation call. Missing connectivity → a `JOIN_CONNECTIVITY` requirement (retained as `NEEDS_EXTERNAL_VALIDATION`); an *unauthorized* required key → `REJECTED`.

## Rollout + quality

- **Default-off `feature-context` flag** gates the whole enrichment; with it off, `_menu`/validation behave exactly as today (byte-for-byte). Bump the feature-gen prompt to a new version (`feature_recommend_v#`) for the changed request shape, across recommend / refine / recipe / feature-set paths.
- **Quality gate (thresholds, not just metrics):** keep the hermetic tests, then add a curated feature-gen gold set + a key-gated **real-provider baseline-vs-enriched** evaluation with delivery bars: **zero** unsafe-accepted features; **zero** restricted/unsanitized outbound fields; grounded-acceptance **non-regression**; a defined **relevance-improvement** target; **bounded** token/cost/latency regression; **pinned** model/settings + a **versioned** gold artifact.

## Cross-cutting invariants (must hold)

- **The deterministic validator is the sole safety authority** — richer prompts affect *generation* only; a hint never satisfies a safety check (only `governed` values do).
- **Sample-safety / field-aware egress** — every outbound free-text field (definition, semantic_terms, table definition) passes the field-aware sanitizer built in Slice 1 (`_redact_free_text_meta`: definition-keys sample-stripped + PII-redacted, structural fields allowlisted + bounded), with the audit reaching `llm_call.input_redaction`. Raw graph definitions are never sent as-is.
- **Read-scope preserved** — all column-derived context comes from the already-authorized candidate set; a restricted column never leaks via grain/summary/count.
- **Flag-off byte-for-byte.**

## Testing strategy

- Tri-state classification: an FTR numeric-sum feature → `NEEDS_EXTERNAL_VALIDATION` with `TYPE_IS_NUMERIC` (not `REJECTED`, not `DESIGN_CHECKED`); a target-leakage feature → `REJECTED`; a fully-governed safe feature → `DESIGN_CHECKED`; `declared_type` alone never yields `DESIGN_CHECKED` for a numeric op.
- Menu: the new fields appear with correct `governed`/`hint` tiers; a restricted column never appears in menu/grain/summary/count.
- Relevance: deterministic + stable; honors the byte budget; returns `CONTEXT_TOO_LARGE` rather than an oversized request when the mandatory set overflows; logs the dropped count.
- Authorization: an unauthorized join key → `REJECTED`; missing connectivity with an authorized key → `NEEDS_EXTERNAL_VALIDATION`.
- Flag off → the recommend output is unchanged.
- Quality: the real-provider baseline-vs-enriched eval passes its thresholds (key-gated; skips without a key).

## Global constraints

- Change `feature_assist.py` (+ the assist route + the contract MCV re-run) and reuse Slice-1's egress sanitizer, `known_entities()`, the field-authority kernel, and the RECOMMENDATION/OPERATIONAL ceiling — no parallel vocabulary, no unscoped query, no oversized dispatch.
- No governance regression; the gauntlet is *strengthened* (tri-state + authority-qualified reads), never bypassed.
- Implementers on **Fable**, reviews on **Opus**.

## Out of scope (Half B — deferred to its own spec)

- Consuming the requirements: an external execution platform verifying them against real data and returning a **signed attestation** (reuse the `authority_sign_gate` re-derive-then-sign template).
- **Minting `DATA-CHECKED`** — promoting a feature out of `NEEDS_EXTERNAL_VALIDATION` on a valid attestation (the `feature_versions` slot exists; the mechanism does not).
- `USEFULNESS-CHECKED` (backtest-proven predictive value).
- Persisting the tri-state as a durable per-feature state / a per-feature attestation table.
