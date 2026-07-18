# Phase-2: Make Stored Enrichment Flow to Its Consumers — Design

**Date:** 2026-07-18
**Status:** Draft for user review
**Predecessor:** Phase-1 LLM-enrichment hardening (branch `phase1-llm-enrichment-hardening`, merge-ready). Phase-1 fixed the enrichment↔provider blocker and the ingestion-side gaps (MF-1..MF-8). This is the consumption-side follow-on.

## Problem

The same catalog column is represented in several places that never assemble into one record:
`CanonicalRow` (source/table/column/operational-type/definition), `GlossaryRecord` (schema/term/declared
type/domain/term-type/synonyms/BIAN-FIBO/parser facets), the enrichment maps (concept/definition/domain),
and the governed evidence (entity/additivity/unit/currency, grain/as-of). Different stages see different
pieces, so **information exists in the system but isn't used**. Three concrete consequences remain after
Phase-1:

1. **No shared column view.** Each stage re-assembles its own partial picture. Phase-1 point-fixed the
   Pass B seam (MF-2 threaded a records map), but the pattern persists and the operational-vs-declared
   type distinction is collapsed.
2. **Pass B loses valid table-synthesis fields.** MF-3 made abstention valid, but a single hallucinated
   grain column still discards the whole synthesis (`table_synth.py:126`), and `table_role`/
   `primary_entity` are written to advisory evidence **unvalidated** — the LLM can invent an operational
   entity even though a governed vocabulary (`known_entities()`, 38 entities) already exists.
3. **The feature generator ignores most of the enrichment.** `feature_assist._menu` (`feature_assist.py:117`)
   sends the LLM only `object_ref/table/column/concept/domain`; it reads `definition` then discards it,
   and never selects `declared_type/semantic_terms/entity/additivity/unit/currency/is_grain/is_as_of`.

## Architecture

The organizing idea: **one authority-aware column view, assembled once, with `graph_node` as its durable
projection.**

```
parse+validate ─┐
GlossaryRecord ─┤
Pass A concepts ┼─► ColumnMetadataView (in-memory, ingest-time)  ──► Pass B (slice 2)
Pass A defs   ──┤                          │
Pass A domains ─┘                          └─► build_graph  ──► graph_node (durable projection)
                                                                   │
                                                 feature context ──┘ (slice 3 reads the projection)
```

- The **view** is the in-memory assembly at ingest time; `graph_node` is its persisted form. Pass B is the
  one consumer that was bypassing the assembly (it got a thin dict) — slice 1 fixes that and subsumes
  Phase-1's ad-hoc records map into a first-class view. `build_graph`, search, and feature context already
  receive the same fields via the graph, so we do **not** rewrite them; we formalize the assembly and let
  Pass B adopt it now.
- **Two honest verified tiers** (this shapes slice 3):
  - **Operational fields** — `is_grain`/`is_as_of` (projected from CONFIRMED facts, provenance via
    `grain_fact_event_id`/`availability_fact_event_id`), and column-level `entity`/`additivity`/`unit`/
    `currency`. These can be genuinely *verified* and the deterministic feature validator already relies
    on them.
  - **Advisory table fields** — `table_role`/`primary_entity`/`event_or_snapshot` are RECOMMENDATION-
    ceilinged (`field_policies.py:125`); they can never be operational. In the feature menu they are
    always tagged *advisory*, feed idea generation only, and must never satisfy a safety check.

**Binding invariant (all slices):** a column's metadata is matched by its validated FTR binding
(`source + schema + table + column`, via the same normalization Pass C / MF-2 use) — **never** by LLM or
value-shape inference.

## Slice 1 — Shared ColumnMetadataView (the spine)

**New module `overlay/upload/column_view.py`.** A frozen, temporary, in-memory view — not a DB identity,
does not replace `CanonicalRow`.

```python
@dataclass(frozen=True, slots=True)
class ColumnMetadataView:
    # binding (the only join key; never inferred)
    source: str; schema: str; table: str; column: str
    logical_ref: str            # schema-preserving
    # types — DELIBERATELY separate (the platform cannot inspect the physical table)
    operational_type: str       # CanonicalRow.type — stays "unknown" until confirmed
    declared_type: str          # GlossaryRecord.declared_type — an LLM HINT only
    # glossary sidecar
    term_name: str; business_definition: str; domain: str; term_type: str
    process_path: str; synonyms: tuple[str, ...]; bian_path: str; fibo_path: str
    semantic_type: str; logical_representation: str
    # enrichment (Pass A)
    concept: str | None; drafted_definition: str | None; classified_domain: str | None
    # column-level governed (populated when known; None at first ingest)
    entity: str | None; additivity: str | None; unit: str | None; currency: str | None

@dataclass(frozen=True, slots=True)
class TableMetadataView:
    source: str; schema: str; table: str; logical_ref: str
    table_definition: str | None   # from GlossaryRecord(is_table=True) — the missing table term
    term_name: str | None
    columns: tuple[ColumnMetadataView, ...]
```

**Builder** `build_table_views(rows, *, glossary, concepts, definitions, domains) ->
dict[str, TableMetadataView]` (keyed by table):
- index `glossary.records` by normalized `(table, column)` for columns and by `table` for the
  `is_table=True` term (its `definition` becomes `table_definition` — closes Phase-1's missing-table-def gap);
- for each accepted `CanonicalRow`, assemble its `ColumnMetadataView`, keeping `operational_type` and
  `declared_type` **separate**;
- `business_definition` = the sanitized glossary definition, else the Pass A draft — never `r.definition`;
- respect the metadata-egress bounds already in place (per-value ≤200, `business_definition` ≤600).

**Consumer (this slice): Pass B.** `assemble_table_items` takes the views instead of the MF-2 records map;
each column profile is derived from the view (so it carries `operational_type` + `declared_type` as two
keys, plus the sidecar), and the item gains a `table_definition` field. The egress allowlist
(`_COLUMN_PROFILE_KEYS`) adds `operational_type`/`declared_type` (replacing the conflated `type`), and the
batch item metadata adds `table_definition`. This retires the Phase-1 records-map threading.

**Not in this slice:** rewriting `build_graph`/search to route through the view (they already receive the
same fields). The view is the assembly spine; adopting it elsewhere is incremental and out of scope here.

## Slice 2 — Per-field Pass B validation

All changes are in `table_synth.py` `make_ref_accept` (`:104-147`) plus new vocab constants; governance
destinations are unchanged (grain/availability → governed fact proposals; the three advisory fields →
field evidence via `_write_producer_field`).

1. **Full decoupling of grain.** Today `if any(c not in cols for c in grain_cols): return None,
   "grain_col_not_in_table"` discards the *entire* synthesis — including a valid `table_role`/
   `primary_entity`. New rule: validate grain independently — accept only when every column is real, there
   are no duplicates, and it is within the size bound; **on any violation, drop grain (set `None`) and
   record a reason, but keep every other field.** An empty grain list stays an honest abstention. (Mirrors
   the existing as-of decoupling at `:132`.)
2. **`table_role` controlled vocabulary.** New `TABLE_ROLES = {"dimension", "event_fact",
   "snapshot_fact", "bridge", "reference", "unknown"}`. Normalize case; a value outside the set is dropped
   (not written as advisory evidence) and logged — never invented.
3. **`primary_entity` gated through the governed registry.** Validate against `known_entities()`
   (`taxonomy/dimensions.py`), clear-on-miss — exactly the established pattern
   (`recognition.py:246`, `contract.py:312`). The LLM cannot introduce a new operational entity.
4. **`event_or_snapshot` normalization on the synthesis path.** Apply the same `{"event","snapshot",None}`
   normalization the summary path already does (`make_summary_accept`, `:169-171`) — today the synthesis
   path passes it through unchecked.
5. **Per-field disposition surfaced.** Each dropped/abstained field increments a counter and logs a reason
   (as `dropped_bad_as_of` already does), so a reviewer sees *why* a field is absent. The persisted
   dispositions stay: grain/availability as PROPOSED governed facts, the three advisory fields as PROPOSED
   field evidence — all human-gated, none auto-verified.

Optional hardening (note, not required): tighten the canonical `_SCHEMAS` so `table_role` is a 6-value
enum (Phase-1's projection already handles enums/nullable-enums), constraining the model at generation.
`primary_entity` stays a free string validated post-hoc (a 38-value enum is verbose; the registry is the
source of truth).

## Slice 3 — Feature-generation consumption

Enrich the **generation** menu only. **Invariant: the deterministic validator gauntlet is not touched** —
it already reads operational fields (`_column_meta` additivity/unit/currency `:150`, `_table_has_as_of`
`:157`) and must remain the sole safety authority. Richer prompts must never weaken governance.

1. **Widen `_candidate_columns`** (`feature_assist.py:96`) to also select `data_type` (operational_type),
   `declared_type`, `semantic_terms`, `entity`, `additivity`, `unit`, `currency`, `is_grain`, `is_as_of`,
   `grain_fact_event_id`, `availability_fact_event_id`. Add a per-table read of `definition` (table term),
   `table_role`, `primary_entity`, `event_or_snapshot`.
2. **Stop `_menu` discarding fields** (`:117`). Emit per column: `object_ref/table/column/concept/domain`,
   `definition` (bounded), `semantic_terms` (bounded), `operational_type`, `declared_type_hint`, `entity`,
   `additivity`, `unit`, `currency`, `is_grain`, `is_as_of` — each governed field tagged with its tier:
   - `is_grain`/`is_as_of`: `"verified"` when the flag is true **and** the `*_fact_event_id` is non-null
     (CONFIRMED-fact projection); `"file_declared"` when the flag is true with a null event id; else
     `"none"`.
   - `additivity`/`unit`/`currency`/`entity`: `"operational"` when present on the node — the deterministic
     validator already treats these as authoritative regardless of whether they were file-declared or
     governed-confirmed; else `"none"`. (Surfacing the finer file-declared-vs-confirmed provenance is a
     plan-time option, not required for the generator.)
   - `declared_type_hint`: always `"hint"` (never operational).
3. **Add a per-table context block** sent once per table (not repeated per column): `table_definition`,
   `table_role`/`primary_entity`/`event_or_snapshot` each tagged `"advisory"` (RECOMMENDATION-ceilinged —
   they can never be `"verified"`), plus the confirmed `grain_columns` and `availability_column` read from
   the projected flags.
4. **Relevance selection** (bound the 126-column prompt). Deterministically select the columns worth
   sending in full: always include confirmed grain columns, the as-of column, and columns whose `entity`
   matches the objective's entity; then columns whose `concept`/`domain` match the objective's roles;
   cap at a configured N; **`log()` the count summarized-but-dropped** (no silent truncation). The rest are
   represented by a compact per-table summary (counts by domain/term-type). The existing `roles`/`entity`
   parameters already scope candidates; this refines it.

The `FeatureIdea.verification` stamp (`:133`) is the existing proposed-vs-verified precedent to extend.

## Cross-cutting invariants (must hold)

- **Governance is not weakened.** Advisory fields stay RECOMMENDATION-ceilinged and never satisfy a safety
  check; the deterministic validator is unchanged; slice 3 enriches generation only.
- **Binding-only matching.** Metadata attaches by validated `source+schema+table+column`, never by LLM or
  value-shape inference.
- **operational_type ≠ declared_type.** Kept as two fields end-to-end; `declared_type` is an LLM hint,
  `operational_type` stays `unknown` until externally confirmed.
- **Metadata-only egress.** Sanitized values only; the Phase-1 bounds (per-value ≤200, definition ≤600)
  and the egress allowlist continue to gate every outbound field.
- **Flag-off byte-for-byte.** With Pass B/Pass C off, behavior is unchanged (the view is built regardless
  but only Pass B/feature-assist read the new fields).

## Testing strategy

- **Slice 1:** view assembly keeps operational/declared separate; `table_definition` attaches from the
  `is_table` record; mixed-case FQN binds correctly; Pass B item carries the new keys and passes egress.
- **Slice 2:** a bad grain column drops only grain (role/entity survive); an off-vocab `table_role` is
  dropped; a non-registry `primary_entity` is cleared; `event_or_snapshot` normalized; valid fields still
  propose facts/evidence; nothing auto-verifies.
- **Slice 3:** `_menu` now carries the new fields with correct tier tags; the validator gauntlet result is
  unchanged for a fixed idea set (proving generation-only); relevance selection includes grain/as-of/entity
  columns and logs the dropped count; a large table doesn't blow the prompt bound.
- **Integration:** extend the Phase-1 synthetic FTR acceptance test — the same 126-col fixture flows a
  view into Pass B, projects to the graph, and produces an enriched feature menu whose operational fields
  are correctly tiered; a churn-style objective yields grounded, non-rejected feature ideas.

## Global constraints

- Three independently-implementable slices; slice 1 is the spine slices 2 and 3 build on. Each becomes its
  own implementation plan; recommended order 1 → 2 → 3.
- No governance regression; the deterministic feature validator is untouched.
- All subagent work on Opus 4.8 (Fable credits exhausted).
- Reuse the existing `known_entities()` registry and the RECOMMENDATION-ceiling policy — do not invent a
  parallel vocabulary or a new status column for advisory fields.

## Out of scope / deferred

- Rewriting `build_graph`/search to route through `ColumnMetadataView` (they already receive the fields).
- A full per-field authority object on the view (coarse per-tier tagging suffices for the consumers here).
- Any change to how governed facts are confirmed/projected (Phase-1 + prior governance surfaces own that).
- External data-access verification (uniqueness, population, freshness) — remains the downstream execution
  platform's job; Pass B proposes the contract, humans confirm, data verifies later.
