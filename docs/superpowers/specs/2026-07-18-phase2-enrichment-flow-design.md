# Phase-2: Make Stored Enrichment Flow to Its Consumers — Design (rev. 2)

**Date:** 2026-07-18
**Status:** Draft for user review — **revised** to incorporate a 12-finding adversarial review (3 Critical, 9 Important). All 12 accepted; changes noted inline as `[Fn]`.
**Predecessor:** Phase-1 LLM-enrichment hardening (branch `phase1-llm-enrichment-hardening`, merge-ready).

## Problem

The same catalog column is represented across `CanonicalRow`, `GlossaryRecord`, the enrichment maps, and
the governed evidence, but never assembled into one record — so information exists yet isn't used. Three
consumption gaps remain after Phase-1: no shared column view; Pass B loses/accepts-unvalidated
table-synthesis fields; and the feature generator (`feature_assist._menu`, `feature_assist.py:117`) sends
the LLM only `object_ref/table/column/concept/domain`, discarding the definition it reads and never
selecting the governed fields.

## Architecture

**One authority-aware column view, assembled once — but the graph persists only a SUBSET of it [F4].**

The `ColumnMetadataView` is the in-memory ingest-time assembly. `graph_node` is a **lossy** projection of
it: it persists the operational + search fields, **not** the full structured sidecar. So the view is a
*superset*, and Slice 3 (which reads the graph) may only use what the graph actually persists, loading
governed/authority values through decision/fact readers rather than the ingest-time builder.

**Projection matrix [F4]** — where each field lives (`graph_node` column refs from migrations `0945`/
`0951`/`0953`/`0957`/`0986`/`1000`):

| Field | Ingest view | graph_node | Read for Slice 3 via |
|---|---|---|---|
| concept, domain, definition | ✓ | ✓ (flat) | graph (definition **sanitized on egress**, [F2]) |
| operational_type (`data_type`), declared_type | ✓ | ✓ (flat) | graph |
| semantic_terms | ✓ (structured) | ✓ **flattened, search-only** | graph (bounded, sanitized) |
| entity, additivity, unit, currency | ✓ (if known) | ✓ (flat) | **authority-qualified** read [F1] |
| is_grain, is_as_of (+ `*_fact_event_id`) | — (governed) | ✓ (flat + provenance) | fact-provenance read [F1] |
| table_role, primary_entity, event_or_snapshot | — (Pass B) | ✓ (flat, RECOMMENDATION-ceiling) | advisory only |
| term_name, term_type, process_path, synonyms, bian/fibo, logical_representation | ✓ | **NOT persisted** | not available graph-side |

Consequence: Slice 3 **cannot** compute "counts by term-type" from the graph (`term_type` isn't persisted)
[F4]. Its per-table summary uses only graph-persisted fields (domain/concept/entity). If term-type
grouping is later wanted, Slice 1 adds a small additive `term_type` column — flagged as an explicit,
optional sub-decision, not assumed.

**Binding scope [F5].** Graph identity is public-flattened and **single-schema-per-source** today
(`field_resolution.py:26`). The view is keyed internally by the schema-preserving `logical_ref`; the
within-upload `(table, column)` index is valid **only under the FTR single-schema fence** — the spec states
this scope explicitly. Multi-schema keying by `logical_ref` is deferred with the rest of multi-schema.

**Two honest verified tiers (unchanged, tightened by [F1]).** Operational fields (`is_grain`/`is_as_of`
with fact provenance; `entity`/`additivity`/`unit`/`currency`) can be genuinely operational; the three
table advisory fields are RECOMMENDATION-ceilinged (`field_policies.py:125`) and are **always** advisory —
they feed generation, never a safety check.

## Cross-cutting invariants (every slice)

- **Sanitized recursive feature-egress projection [F2] (Critical).** `graph_node.definition` can be the
  **raw** technical-upload cell (`graph.py:248`: `r.definition or draft`), and the current redactor
  (`enrich_llm.py:79`) only covers selected top-level keys + `column_profiles[].business_definition`. So
  **no field leaves for the LLM without passing a dedicated, path-aware, recursive sanitizer**
  (`strip_sample_values` + `redact_free_text`) with per-value bounds and an audit span. This covers the new
  `table_definition`, `columns[].definition`, and `semantic_terms`. Raw graph definitions are **never** sent
  as-is. Fail-closed tests: a planted PII/sample token in a graph definition, a nested field, and a table
  definition must all be absent from the outbound payload and from the recorded `llm_call` input.
- **Read-scope preservation [F3] (Critical).** All column-derived context (per-table grain columns, as-of
  column, summaries, counts) is assembled **from the already-authorized candidate set** returned by
  `_candidate_columns` (which filters by the caller's allowed sensitivities, `feature_assist.py:96`) — never
  a second unrestricted graph query. A restricted column excluded from the menu must not reappear via
  `grain_columns`/`availability_column`/summary/count.
- **Authority-qualified safety [F1] (Critical).** The deterministic validator must not treat a display-only
  graph value as operational truth. It reads governed values through the authority path
  (`is_feature_eligible` / decision + fact readers) and emits explicit rejection/unresolved codes for
  unverified operational type, absent grain, unverified temporal basis, unverified additivity, and missing
  join connectivity. `declared_type_hint` is a generation hint and **never** approves a numeric operation.
- **Rollout + replay versioning [F11].** Slice 3 changes feature-generation behavior, so it ships behind a
  **default-off** `feature-context` flag (the "flag-off byte-for-byte" claim applies only to Pass B/Pass C;
  corrected). Every changed request shape bumps its prompt/schema/config version (Pass B input+vocab change
  → new `overlay_table_synth*` prompt/schema version; feature-gen menu change → new
  `feature_recommend_v#`).
- **Metadata-only egress, operational_type ≠ declared_type, binding-only matching** — as before.

## Slice 1 — Shared ColumnMetadataView + schema-safe binding + egress foundation

**Deliverable gate:** schema-safe binding and the sanitized egress projection exist and are tested.

- **`overlay/upload/column_view.py`** — `ColumnMetadataView` (frozen; `operational_type` and
  `declared_type` as two fields) and `TableMetadataView` (carries `table_definition` from
  `GlossaryRecord(is_table=True)`). Not a DB identity.
- **Reconciled facets [F9].** The view carries the **reconciled** `semantic_type`/`logical_representation`
  — it applies Phase-1's `reconcile_profile(...)` against `declared_type` + column (the same reconciliation
  the evidence layer uses, `ingest.py:767`), so Pass B never sees a facet the evidence layer withheld.
  Raw facets, if ever carried, are tagged non-authoritative.
- **Builder** keyed by `logical_ref` (with the `(table, column)` FTR-scope index [F5]); respects the
  Phase-1 egress bounds.
- **Dual-type roster contract [F6].** Replacing the single `type` key breaks the wide-table path, which
  builds `name:type` from `d.get('type')` (`table_synth.py:251`). Define an explicit roster representation
  carrying **both** types — e.g. `column:operational/declared` — and update the narrow descriptor, the wide
  roster, the chunk-summary path, the egress allowlist (`_COLUMN_PROFILE_KEYS`), the Pass B prompt
  (`templates.py`), and their tests **together**.
- **Sanitized egress projection [F2]** (foundation used by Slices 1 & 3): a `feature_egress_view(...)` that
  recursively sanitizes + bounds every string field. Pass B item metadata gains a sanitized
  `table_definition`.

## Slice 2 — Per-field Pass B validation + stale-value lifecycle + durable dispositions

**Deliverable gate:** field validation, stale-value lifecycle, and durable per-field dispositions.

All in `table_synth.py` `make_ref_accept` (+ persistence in `_propose_table_facts`) and new versioned
vocab constants.

1. **Full grain decoupling.** A hallucinated grain column drops **only** grain (record a reason), keeping
   `table_role`/`primary_entity`/`event_or_snapshot`/as-of — today it discards the whole synthesis
   (`table_synth.py:126`). Empty grain stays an honest abstention.
2. **Versioned `table_role` vocabulary [F8].** The **current** live values are `fact` (13×), `dim` (2×),
   `reference` (2×). A new vocab must not silently drop them. Ship a **versioned** vocab with explicit
   aliases (`dim`→`dimension`; `fact`→`event_fact`/`snapshot_fact` resolved via `event_or_snapshot`, else
   retained as `fact`; `reference` kept), and update the **prompt (`templates.py`) + schema + tests in one
   change**. An unmapped value is treated as **abstention** (dropped), never written as active advisory
   evidence.
3. **`primary_entity` gated through `known_entities()`** (`taxonomy/dimensions.py`, 38 governed entities),
   clear-on-miss — the established pattern (`recognition.py:246`). Not the relationship `entity_registry.py`.
4. **`event_or_snapshot` normalization** on the synthesis path (`make_summary_accept` already does it).
5. **Stale-value lifecycle [F7].** Today advisory evidence is written only for a truthy value
   (`table_synth.py:422`), so a dropped/abstained field leaves the **previous** LLM value ACTIVE. On a
   successful drop/abstention (distinct from a whole-table synthesis failure), **producer-scope stale** the
   prior value (as Phase-1 does for parser fields via `_stale_absent_fields`).
6. **Durable per-field disposition [F7].** Persist each field's disposition + reason as reviewer-visible
   durable state (evidence/decision detail), not only counters/logs.

## Slice 3 — Authority-aware context + expanded validator + rollout flag + quality eval

**Deliverable gate:** authority-aware sanitized context, expanded deterministic validator, the rollout
flag, and a real-provider quality evaluation.

1. **Menu enrichment [from authorized set, sanitized].** Widen `_candidate_columns` (`:96`) to select the
   governed fields; stop `_menu` (`:117`) discarding them. Per column emit `concept`, `domain`,
   **sanitized** `definition` + `semantic_terms` [F2], `operational_type`, `declared_type_hint`, `entity`,
   `additivity`, `unit`, `currency`, `is_grain`, `is_as_of` — each governed field tier-tagged
   (`verified`/`file_declared`/`operational`/`none`; `declared_type_hint` always `hint`).
2. **Per-table context [F3, from authorized set].** One block per table, assembled **only from the
   authorized candidate rows**: `table_definition` (sanitized), the three advisory fields tagged
   `advisory`, and confirmed `grain_columns`/`availability_column` (derived from the authorized set's
   projected flags).
3. **Expanded deterministic validator [F1].** Add authority-qualified reads and explicit
   rejection/unresolved codes: unverified operational type, absent grain, unverified temporal basis,
   unverified additivity, missing join connectivity. `declared_type_hint` never satisfies a numeric-safety
   check. The validator remains the sole safety authority; richer prompts only affect generation.
4. **Deterministic relevance selection [F10].** Define it precisely: `roles` in `feature_assist` is an
   **authorization** role, not a semantic objective role — do not overload it. Specify (a) how the
   objective yields target entity/concepts/domains; (b) a mandatory set always included (confirmed grain
   cols, as-of col, objective-entity matches) even when it exceeds N; (c) a stable priority order with a
   deterministic tie-break (e.g. by `object_ref`); (d) a **token/byte budget** as the real bound (column
   count alone does not bound prompt size); (e) overflow behavior when the mandatory set exceeds the
   budget (send mandatory, summarize rest, never silently drop); (f) durable truncation statistics
   (`log()` + a recorded count).
5. **Rollout flag + versioning [F11].** Default-off `feature-context` flag; new `feature_recommend_v#`
   for the changed menu shape.

## Testing strategy

- **Slice 1:** operational/declared kept separate; reconciled facets in the view (a timestamp facet the
  evidence layer withheld is absent) [F9]; `table_definition` attaches; the dual-type roster is non-blank
  on the wide path [F6]; the sanitized egress projection strips a planted sample/PII token from a raw
  graph definition, a nested field, and a table definition — asserted absent from the payload AND the
  recorded `llm_call` input (fail-closed) [F2].
- **Slice 2:** bad grain drops only grain; `dim`/`fact`/`reference` still accepted via aliases and an
  off-vocab role abstains [F8]; non-registry entity cleared; a re-upload that drops a previously-proposed
  advisory value **stales** it (no stale ACTIVE value) [F7]; per-field disposition is queryable [F7].
- **Slice 3:** read-scope — a restricted column excluded from the menu never appears in grain/summary/count
  [F3]; the validator rejects a numeric aggregation grounded only on `declared_type_hint` and emits the
  right code [F1]; relevance selection is deterministic and honors the token budget with logged truncation
  [F10]; the flag defaults off (no behavior change when off) [F11].
- **Quality evaluation [F12] (not a scripted plumbing test).** Keep the hermetic FTR integration test, then
  add a **curated feature-generation gold set** and a key-gated **real-provider** evaluation comparing
  baseline vs enriched context on: grounded-acceptance rate, **unsafe-acceptance rate**, rejection-reason
  distribution, expert relevance, tokens, latency, cost. A scripted-fake pass is explicitly **not**
  evidence of improvement.

## Delivery gates (per the review's recommendation)

Order Slice 1 → 2 → 3, each gated:
1. **Slice 1:** schema-safe binding + sanitized egress projection (+ reconciled facets, dual-type roster).
2. **Slice 2:** per-field validation + stale-value lifecycle + durable dispositions + versioned vocab.
3. **Slice 3:** authority-aware context + expanded deterministic validator + rollout flag + quality eval.

## Global constraints

- Three independently-planned slices; each becomes its own implementation plan; order 1 → 2 → 3.
- No governance regression; the deterministic validator is **strengthened** (not bypassed) and stays the
  sole safety authority; advisory fields never satisfy a safety check.
- Reuse `known_entities()`, `reconcile_profile`, the RECOMMENDATION-ceiling policy, and the Phase-1 egress
  bounds — no parallel vocabulary, no new advisory status column, no second unscoped query.
- All subagent work on Opus 4.8.

## Out of scope / deferred

- Rewriting `build_graph`/search to route through the view (already receive the fields).
- Multi-schema binding by `logical_ref` (deferred with the rest of multi-schema) [F5].
- Persisting the full structured sidecar to the graph (only `term_type`, and only if term-type grouping is
  adopted) [F4].
- External data verification (uniqueness/population/freshness) — the downstream execution platform's job.
