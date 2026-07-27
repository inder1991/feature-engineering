# E2 — Analytical Enrichment (design)

Date: 2026-07-27 · Status: design for review · Parent: `2026-07-26-llm-metadata-enrichment-design.md` (E2) · Builds on: E1a (built — `feature/e1a-llm-metadata-enrichment`, 9 commits, 2777 overlay green)

> **This spec is smaller than the E2 sketched in the parent, and deliberately so.** A code-grounding pass
> refuted three of the four fields the parent proposed. They are dropped here with the evidence, because
> shipping redundant metadata would be the opposite of "solid improvement in LLM enrichment".

## The question E2 must answer

E1a made the AI's existing work *governed, attributed and safe* — but it added almost no new **information**
(only synonyms and column-level domain overrides). E2 must add information the catalog genuinely does not
have, and be honest about whether it changes anything downstream.

## What the grounding refuted (dropped from E2, with evidence)

| Proposed field | Verdict | Evidence |
|---|---|---|
| **`analytical_role`** (key/measure/dimension/time) | **DROP — already exists, deterministically** | `binding_roles.py:10-33` defines `JoinRole{SOURCE_ENTITY_KEY, TARGET_ENTITY_KEY, INTERMEDIATE_ENTITY_KEY, MEASURE, TIME}` + `TemporalRole`, derived in `need_metadata._derive_one` (`:84-121`) from `concept.entity_link` + `concept.pit_role` + the template anchor. That IS a key/measure/time classifier — with zero LLM cost and zero free text. An LLM `analytical_role` would duplicate it and could *disagree* with it. |
| **`feature_role`** | **DROP — dead field** | It has a `_POLICIES` entry (`field_policies.py:197`) and **nothing else**: `grep -rn "feature_role" src/featuregen/` returns exactly two hits, both in `field_policies.py`. No writer, no reader, no consumer. Filling it would produce metadata nobody reads. |
| **entity PRIMARY KEY** | **DROP — already implicit** | The `(is_grain ∧ entity)` pair already *is* the entity's PK: `ingest.py:459-462` computes "the entity of the table's PK", and `catalog_realizations.object_grain()` derives a table's object grain from the `entity_link` of its `is_grain` column's concept. Proposing it again adds no information. |
| entity **TITLE** | **DEFER to E3** | Genuinely absent (`natural_key`/`primary_key`/`title` appear nowhere), but it belongs with E3's entity/link-type work, not with column enrichment. |

**The lesson:** most "obvious" analytical metadata is already derivable from the concept registry. The
registry is the asset; asking the LLM to restate it is waste.

## What E2 actually builds

### 2.1 `value_semantics` — the one genuinely new signal

**What it is.** A coded column's values are meaningless to feature generation until someone says what the
codes mean. That mapping is locked inside the customer's own prose today and reaches nothing. The LLM
**extracts and structures** it — it never guesses from data (it cannot: sample values are stripped before
egress).

*Example (real sample column):* `DPL_EIB_COMPLIANCE.COMP_FIN_TRAN.SETTLEMENT_DBL`, described as a
"legacy numeric settlement indicator…" → `{"1": "settled", "0": "unsettled"}` **only if the description
says so.** Where the description does not explain the codes, the field is **left empty, never invented.**

**Feasibility — VERIFIED, not assumed.** The grounding ran the real sanitizer against representative bank
code prose:
- `Statement indicator. Y = statement account, N = non-statement account.` → **unchanged**
- `Account status code: 01 = open, 02 = closed, 09 = charged off.` → **unchanged**
- `Product code. The sample profile is …, with representative values such as ABC; DEF. P = personal loan, M = mortgage.` → sample clause excised, **code meanings kept**

The carrier already exists: `business_definition` is allowlisted (`enrich_llm.py:922-932`),
definition-kind-classified (`_DEFINITION_META_KEYS`, `:87`) and 600-char windowed; and
`ColumnMetadataView.business_definition` (`column_view.py:164,187`) already supplies *curated-else-AI-drafted*
text **without** breaching the M4 invariant (an uploader's technical free-text definition must never egress).

**Three honest constraints (state them in the UI, don't hide them):**
1. A description containing `observed values` / `sample values` / `values such as` **outside** a strippable
   clause blanks the whole field (fail-closed marker scan, `sanitize.py:121-128`).
2. The 600-char cap truncates long code lists (measured: 13 of 29 codes kept on a 1424-char description).
3. **No curated glossary definition ⇒ nothing to extract.** An AI-drafted one-liner has no codes. On a
   technical CSV with no sidecar, this yields nothing.

**Storage.** `field_evidence` with `field_name="value_semantics"`, `producer=llm`, `strength=proposed`, via
E1a's existing `_write_llm_field_evidence` + `_reconcile_llm_field_evidence` (already `field_name`-
parameterized — reused verbatim). `proposed_value` is typed `object` and round-trips from jsonb, so the
value is a **structured `{code: meaning}` map**, not a string. (`_write_llm_field_evidence`'s
`items: dict[str, str]` hint must widen.)

### 2.2 Concept coverage + quality — the only lever on actual feature OUTPUT

**The grounding's most important finding:** the deterministic planner grounds **purely** on
`concept` equality plus `is_grain`/`is_as_of`/`entity` (`templates._match:167-203`); it never reads
`definition`, `domain`, or `semantic_terms` (`templates._load_columns:134-147`). So **no new descriptive
field can change which features are buildable.** The only lever on real output is *how many columns carry a
correct concept*.

E2 therefore includes the concept work E1a deferred:
- **Broaden concept-evidence coverage** to classified columns that currently get a display concept but no
  governed evidence (E1a Task 5, deferred because the sample is glossary-backed — revisit with the real
  target sources).
- **Measure and improve concept accuracy** on the sample (the P0 harness already scores this).
Every column that moves out of `unclassified` is a column templates can ground on.

### 2.3 Reaching the consumer (the migration decision)

Evidence-only fields need **no migration** (`semantic_terms` and `leakage_anchor` prove it). But the
feature-gen menu (`feature_assist._candidate_columns:146-176`) and the planner
(`templates._load_columns`) read `graph_node` **only** — never `field_evidence`. So for `value_semantics`
to reach the LLM's proposal prompt it needs **either** a `graph_node.value_semantics` column
(**migration 1031** — 1021-1030 are taken on origin/main) **or** a new `field_evidence` join in
`_candidate_columns`. Recommend the column + adding it to `_MENU_DEFINITION_FIELDS`, matching how
`semantic_terms` reaches the menu today.

## What E2 does NOT claim

- **It does not change what the deterministic planner can ground.** `value_semantics` lands exactly where
  `definition` lands: LLM prompt context. It should make the *proposals* better; it cannot make more
  templates bind.
- It does not unblock a gated feature (unit/currency/additivity stay source/human — that is E4).
- It is **unmeasured** by construction: unlike E1a there is no deterministic consumer to assert against.
  Hence §4.

## Measurement (cheap, do it FIRST — it may cancel §2.1)

Before building, run a read-only count on the real sample: **what fraction of coded columns have a
description that actually explains their codes?** If it is low (say <15-20%), `value_semantics` is not worth
a migration and E2 collapses to §2.2 alone. This is a few minutes of work and it decides the phase.
Then, post-build: coverage (columns yielding extractable codes) and an A/B on proposal quality.

## Build order
1. **Coverage measurement** (§4) — gate. May cancel §2.1.
2. **§2.2 concept coverage/quality** — the only lever on real output; no new field, no migration.
3. **§2.1 `value_semantics`** — drafter + evidence + reconciliation (reuse E1a's writer verbatim) +
   `graph_node` column (migration 1031) + menu wiring.

## Touch points for §2.1 (grounded — 9, all established patterns)
`_SCHEMAS` flat+batch (`enrich_llm.py:424`) · `enrich_config` **all three** dicts (`mode`/`max_items`/
`max_input_tokens` — the latter two `KeyError` on an unregistered task) · task constant + accept gate +
`draft_value_semantics` in `enrich.py` · `_write_value_semantics_evidence` (~35 lines, copy
`_write_synonym_evidence:787-798`) · `CANONICAL_STAGES` in order (`stage_report.py:50`) · the ingest stage
block (~30 lines, pattern `ingest.py:2063-2094`) · the `skipped_no_client` tuple (`ingest.py:1944`) ·
`_POLICIES` entry reusing `_MEANING` · migration 1031 + `_DISPLAY_COLUMN` (only because the menu needs it).

## Deferred NFRs
Measurement dashboards, review/governance UI, bulk correction, caching, cost controls, per-field confidence
(P2), ops/cold-start, compliance reporting. Functional enrichment only.

## Risks
- **§2.1 yields little on sparse descriptions** → the §4 gate exists precisely to catch this before spend.
- **Marker-phrase blanking** silently drops good descriptions → count and report blanked fields, don't hide them.
- **Duplicating deterministic knowledge** (the trap that killed three fields) → any future field must name a
  consumer that reads it and prove the concept registry doesn't already derive it.
