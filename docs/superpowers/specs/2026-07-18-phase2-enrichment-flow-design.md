# Phase-2: Make Stored Enrichment Flow to Its Consumers — Design (rev. 3)

**Date:** 2026-07-18
**Status:** Draft for user review — **revised twice**. Rev. 2 incorporated a 12-finding review; rev. 3
incorporates a further 14-finding review (2 Critical, 12 Important), all accepted. Findings tagged `[Gn]`.
**Predecessor:** Phase-1 LLM-enrichment hardening (branch `phase1-llm-enrichment-hardening`, merge-ready).

## Problem

The same catalog column is spread across `CanonicalRow`, `GlossaryRecord`, the enrichment maps, and the
governed evidence, never assembled — so information exists yet isn't used. Three consumption gaps remain:
no shared column view; Pass B loses/accepts-unvalidated table-synthesis fields; and the feature generator
(`feature_assist._menu`, `:117`) discards the enrichment.

**Scope note (head-architect flag):** three review rounds deepened this materially. Slice 3 now carries a
tri-state result model, an expanded validator, deterministic relevance, and a real-provider eval — it is
large and may itself decompose at plan time. The **external-attestation ingestion far-end** (a signed
type/grain/temporal attestation flowing back in) is **defined as a contract here but its consuming endpoint
is deferred** to a follow-on; Phase-2 produces the requirements + result state, not the round-trip.

## Foundational model changes (used across slices)

### A. Tri-state feature result + external-validation contract [G2, Critical]

This platform cannot inspect physical data, and FTR deliberately stores `data_type=unknown` (declared type
is only a hint). `_is_numeric` gates numeric/ratio strategies on the **operational** type
(`feature_assist.py:577`), and `FeatureIdea.verification` is a single `"DESIGN-CHECKED"` stamp (`:133`) —
so a binary accept/reject validator would leave **every FTR numeric feature permanently rejected**. That is
a dead end, not honesty.

Replace the binary stamp with a **tri-state** result:
- **`DESIGN_CHECKED`** — structurally safe with the authority available now (no external checks needed).
- **`NEEDS_EXTERNAL_VALIDATION`** — structurally plausible, carrying a **machine-readable requirements
  list** the external execution platform must satisfy against real data. Requirement vocabulary (initial):
  `TYPE_IS_NUMERIC`, `GRAIN_IS_UNIQUE`, `TEMPORAL_IS_POPULATED`, `TEMPORAL_LAG_BOUNDED`,
  `JOIN_CONNECTIVITY`, `CURRENCY_CONSISTENT`. Each requirement names the column(s)/join it concerns.
- **`REJECTED`** — deterministic invalidity (missing column, target leakage, unmapped concept, additive
  aggregation over a governed-non-additive column).

"An unverified type is not a wrong type." The external platform consumes the requirements and returns a
**signed type/grain/temporal attestation**; on attestation a feature promotes out of
`NEEDS_EXTERNAL_VALIDATION`. Phase-2 defines the requirements + attestation **shape**; its ingestion
endpoint is the deferred far-end.

### B. `OperationalColumnFacts` authority adapter [G1, Critical]

"Present on `graph_node`" ≠ operational. Of the fields the validator wants, **only `additivity`** has a
decision policy + link (`additivity_decision_id`, migration `0984`); `entity`, `unit`, `currency`, and
`data_type` are **flat columns with no decision governance**, and `is_feature_eligible` returns a bare bool
(`field_resolution.py:360`) — not value + provenance.

Introduce a field-specific `OperationalColumnFacts(field) -> {value, authority, provenance}` adapter.
**Authority tiers:** `governed` (`additivity` via its decision; `is_grain`/`is_as_of` via a **non-null
`*_fact_event_id`**), `file_declared` (a flat CSV value with no decision), `hint` (`declared_type`), `none`.
**Validator rule (fail-closed):** any signal may *reject* or *tighten*; only a `governed` value may *clear*
a required check or yield `DESIGN_CHECKED`; a `hint`/`file_declared` value that a check depends on yields a
`NEEDS_EXTERNAL_VALIDATION` requirement instead. `declared_type` (hint) may reject a non-numeric operation
but **never approves** a numeric one. Either add evidence/decision support for a field or it is a hint —
never silently operational.

### C. Field-aware feature-egress projection [G12, from rev.2 G2]

`graph_node.definition` can be the **raw** technical-upload cell (`graph.py:248`), and the redactor
(`enrich_llm.py:79`) covers only selected keys. So a dedicated projection sanitizes every outbound field,
**by field type** (not one sanitizer for all strings, which would corrupt a column name):
- **Free-text** (`definition`, `table_definition`, semantic prose) → `sanitize_definition`/`redact_text`.
- **Structural** (`object_ref`, column/table names, types, enums, IDs) → **allowlist + bound**, never
  sample-clause stripped (`sanitize.py:96`).
Per-field **audit record**: `{path, sanitizer_version, state, removed_count}` (the sample stripper doesn't
emit redaction spans, so we record a count, not spans). Fail-closed: a planted token in a raw graph
definition, a nested field, and a table definition is absent from the payload **and** the recorded
`llm_call` input.

### D. Durable disposition store [G11]

An invalid LLM value never becomes evidence, and `field_evidence` has no disposition field — so counters/
logs are not a durable record. Persist per-field dispositions as **ingestion-run stage detail keyed by
`(table, field)`**. **Status vocab:** `accepted`, `abstained`, `dropped_invalid`, `staled`. **Reason-code
vocab:** `grain_col_not_in_table`, `grain_duplicate`, `grain_over_bound`, `role_off_vocab`,
`entity_not_registered`, `basis_not_allowed`, `as_of_col_not_in_table`. Reviewer-visible.

### E. Prompt/schema versioning seam [G13]

`audited_structured_call`/`audited_batch_call` **hardcode** prompt+schema version 1 (`enrich_llm.py:390`).
Add explicit `prompt_version`/`schema_version` parameters so a changed request shape ships a new version.
Pass B input+vocab change → `overlay_table_synth*` **v2**; feature-gen menu change → `feature_recommend`
**v2**, applied across recommendation, refinement, recipe, and feature-set paths, all under the flag.

## Architecture

The `ColumnMetadataView` is the in-memory ingest-time assembly; `graph_node` is a **lossy** projection of
it (rev.2's projection matrix stands: `term_name`/`term_type`/`process_path`/synonyms/BIAN-FIBO/structured
facets are **not** persisted, so Slice 3 uses only graph-persisted fields and reads governed values via the
adapter in §B). Binding is single-schema-per-source today (`field_resolution.py:26`); the view keys by
schema-preserving `logical_ref`, `(table,column)` valid **only under the FTR single-schema fence** [G-prev].

## Slice 1 — ColumnMetadataView + schema-safe attachable binding + egress foundation

**Gate:** schema-safe *attachable* binding and the field-aware egress projection exist and are tested.

- **`overlay/upload/column_view.py`** — `ColumnMetadataView` (operational_type + declared_type separate)
  and `TableMetadataView` (`table_definition` from `GlossaryRecord(is_table=True)`).
- **Attachable binding, not just keying [G7].** The builder consumes **validated** bindings and applies
  `may_attach`, and reuses the existing rule that **skips a table term whose schema disagrees with its
  columns**. Keying by `logical_ref` alone does not prevent a mismatched/unvalidated sidecar from attaching.
- **Reconciled facets [G-prev/G9].** The view carries `reconcile_profile(...)`-reconciled
  `semantic_type`/`logical_representation` (the same withholding the evidence layer applies,
  `ingest.py:767`) — never the raw contradictory facet.
- **Structured roster [G5].** Do **not** use a `column:operational/declared` string — column names may
  contain `:`/`/`. Roster entries are structured objects `{column, operational_type, declared_type}`. The
  narrow descriptor, the wide roster (`table_synth.py:251`), **and** the wide phase-2 final synthesis
  (`table_synth.py:275`, which must **explicitly propagate `table_definition`** — adding it to the initial
  item does not carry it through) all use the structured form.
- **Egress projection (§C)** is delivered here as the shared foundation.

## Slice 2 — Per-field Pass B validation + stale-value lifecycle + durable dispositions

**Gate:** field validation, a stale-value lifecycle that clears the *graph*, and durable dispositions.

Implementation map [G6]: **prompt** = `table_synth.py:290` `_INSTRUCTION` (+ `_SUMMARY_INSTRUCTION`,
`_SYNTH_WIDE_INSTRUCTION`); **schemas** = `enrich_llm.py` `_SCHEMAS` (`overlay_table_synth`/`_batch`/
`_summary_batch`, `:269/289/307`); **egress allowlist** = `_COLUMN_PROFILE_KEYS`; plus tests — all changed
together.

1. **Grain validation (complete) [G8].** Accept only when: every column exists in the table; **no duplicate
   normalized columns**; **within the bounded grain size**; an empty list is abstention; **any violation
   drops grain only**, keeping the other fields.
2. **Versioned `table_role` vocab with aliases [G-prev/G8].** Live values are `fact` (13×), `dim` (2×),
   `reference` (2×). Ship a versioned vocab: `dim`→`dimension`; `fact`→`event_fact`/`snapshot_fact` via
   `event_or_snapshot`, else retained `fact`; `reference` kept. Co-update prompt + narrow & wide schemas +
   tests. Unmapped → **abstention** (dropped, `dropped_invalid`), never active advisory evidence.
3. **`primary_entity` gated through `known_entities()`** (`taxonomy/dimensions.py`, 38 entities),
   clear-on-miss (`recognition.py:246`).
4. **`event_or_snapshot` normalization** on the synthesis path.
5. **Stale-value lifecycle that clears the graph [G3].** Producer-scope staling alone is insufficient:
   `resolve_and_project` skips fields with no active evidence (`field_resolution.py:315`), so a staled
   advisory field leaves the previous `graph_node.table_role`/`primary_entity` visible. Add an explicit
   **touched-field resolver** that records a `STALED` decision, **clears the display column + decision
   link**, and rebuilds search where relevant. Test the **graph projection + eligibility**, not only the
   evidence lifecycle.
6. **Durable dispositions (§D)** for every field.

## Slice 3 — Authority-aware context + tri-state validator + relevance + rollout + eval

**Gate:** authority-aware sanitized context, the tri-state validator (§A/§B), deterministic relevance, the
rollout flag, and a threshold-gated real-provider eval.

1. **Menu enrichment (authorized, sanitized, authority-tagged).** Widen `_candidate_columns` (`:96`); stop
   `_menu` (`:117`) discarding. Each column carries concept, domain, sanitized definition + semantic_terms
   (§C), operational_type, `declared_type_hint`, and the governed fields — each wrapped by
   `OperationalColumnFacts` (§B) so the LLM and validator both see `{value, authority}`.
2. **Per-table context [G4].** `_candidate_columns` returns column rows only, so table definitions/advisory
   fields need a **scoped read of the parent table node restricted to exactly the tables in the authorized
   candidate set** (or a scoped join) — never a second unrestricted query. If every column of a table is
   restricted, **emit no context** for it. Confirmed grain/as-of require a **non-null `*_fact_event_id`**,
   not merely a true flag [G4].
3. **Tri-state validator (§A/§B) + authorization threading [G10].** The gauntlet emits `DESIGN_CHECKED` /
   `NEEDS_EXTERNAL_VALIDATION(requirements)` / `REJECTED`. `find_join_path` needs caller **roles** to
   exclude restricted join keys, but `_validate_idea`/`_vet`/refinement/contract MCV don't thread roles
   today (`feature_assist.py:278`, `contract/review.py:31`) — the plan threads authorization through
   **every** validation and revalidation call. Missing join connectivity → a `JOIN_CONNECTIVITY`
   external-requirement (retained), not a hard reject, unless a required key is unauthorized (reject).
4. **Deterministic relevance selection [G9] (a real algorithm, not "specify X").**
   - **Objective parsing:** the objective's target entity + concepts/domains (reuse the recognizer /
     `known_entities()`); `roles` in `feature_assist` is an **authorization** role — do not overload it.
   - **Scorer:** normalized (lowercase, entity via `known_entities`) match — entity match > concept match >
     domain match, summed to an integer score.
   - **Mandatory set:** confirmed grain columns, the as-of column, objective-entity columns.
   - **Ordering:** score desc, then `object_ref` asc (stable, deterministic tie-break).
   - **Hard bound:** a serialized **byte/token budget** is the real limit (column count is not). Select
     mandatory first, then by score until the budget; the rest become a compact per-table summary.
   - **Overflow [G9]:** if the **mandatory** set alone exceeds the budget, **deterministically chunk** or
     return `CONTEXT_TOO_LARGE` — **never dispatch an oversized request**.
   - **Durable truncation stats:** `log()` + a recorded dropped/summarized count.
5. **Rollout + versioning (§E).** Default-off `feature-context` flag; `feature_recommend` v2 across all
   feature-gen paths.

## Testing strategy

- **Slice 1:** operational/declared separate; a schema-mismatched table term does **not** attach [G7]; a
  withheld (reconciled-away) facet is absent from the view [G9]; the wide phase-2 carries `table_definition`
  and the structured roster is intact for a column name containing `:` [G5]; the egress projection strips a
  planted token from a raw graph definition + nested field + table definition (absent from payload **and**
  `llm_call` input) and emits the `{path, sanitizer_version, state, removed_count}` audit record; a
  structural field (column name) is **not** sample-stripped [G12].
- **Slice 2:** grain rejects duplicates and over-bound and drops grain only [G8]; `dim`/`fact`/`reference`
  accepted via aliases, off-vocab abstains [G8]; non-registry entity cleared; a re-upload dropping a prior
  advisory value **stales the graph column + decision link** and `is_eligible`/display both clear [G3]; the
  disposition row exists with the right status + reason-code [G11].
- **Slice 3:** a restricted column never appears in menu/grain/summary/count [G4]; the validator returns
  `NEEDS_EXTERNAL_VALIDATION` with `TYPE_IS_NUMERIC` for an FTR sum-of-amount (not `REJECTED`, not
  `DESIGN_CHECKED`) [G2]; `declared_type_hint` alone never yields `DESIGN_CHECKED` for a numeric op [G1/G2];
  authorization is threaded so an unauthorized join key rejects [G10]; relevance is deterministic, honors
  the byte budget, and returns `CONTEXT_TOO_LARGE` rather than an oversized request when mandatory overflows
  [G9]; the flag defaults off (no behavior change) [E].
- **Quality gate [G14] (thresholds, not just metrics).** Hermetic FTR integration test **plus** a curated
  feature-gen gold set + a key-gated real-provider baseline-vs-enriched eval, with **delivery thresholds**:
  **zero** unsafe-accepted features; **zero** restricted/unsanitized outbound fields; grounded-acceptance
  **non-regression**; a defined **expert-relevance improvement** target; **bounded** token/cost/latency
  regression; **pinned** model + settings + a **versioned** gold artifact.

## Global constraints

- No governance regression; the validator is **strengthened** (tri-state + authority-qualified), never
  bypassed; advisory fields never satisfy a safety check; hints may only tighten, never approve.
- Reuse `known_entities()`, `reconcile_profile`, `may_attach`, the RECOMMENDATION-ceiling policy, and the
  Phase-1 egress bounds — no parallel vocabulary, no unscoped query, no oversized dispatch.
- All subagent work on Opus 4.8.

## Delivery gates & out of scope

- Order 1 → 2 → 3; slice 3 may decompose at plan time (tri-state model / validator / relevance / eval).
- **Deferred:** the external-attestation **ingestion** endpoint (Phase-2 defines the requirements +
  attestation shape only) [G2]; multi-schema binding by `logical_ref`; persisting the full structured
  sidecar to the graph (`term_type` only, and only if term-type grouping is adopted); rewriting
  `build_graph`/search to route through the view (already receive the fields).
