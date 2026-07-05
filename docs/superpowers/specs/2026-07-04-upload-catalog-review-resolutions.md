# Upload-Catalog Spec — architecture review resolutions

Date: 2026-07-04. Dispositions for the head-of-architecture critique of the
[pivot](2026-07-04-upload-catalog-pivot-design.md) +
[upload/mapping/enrichment](2026-07-04-upload-mapping-enrichment-design.md) specs.
Each item is **RESOLVE** (decision folded into the design), **BUILD** (a rule to apply during
implementation), or **V2** (explicitly deferred with reason).

## Must-resolve-before-build (structural)

### S1 — Features have no entry point → **RESOLVE: descope v1 to catalog/column search**
v1 is **schema + facts catalog search**, not feature search. The graph's nodes in v1 are
tables · columns · (declared) joins · concepts · domains — **not features**. Drift-impact in v1 is
"which *facts/columns* are affected," not "which features break." Feature nodes, `derives-from` edges,
and feature-lineage are **phase-2**, gated on a *feature source* (the intake layer wired, or a features
upload). The diagram and pivot doc are corrected to stop promising feature search on day one.

### S2 — Fact model can't express composite/multi joins → **RESOLVE: extend the contract**
- **Grain:** the grain of a table is the *set* of columns marked `is_grain=Y` (composite handled; a
  single Y = simple grain). v1 treats the marked set as *the* grain; primary-vs-candidate distinction is V2.
- **Joins:** replace the single `joins_to` with a **join declaration**: a `join_id` + `joins_to`
  (`table.column`) + optional `cardinality`. Columns sharing a `join_id` across the two tables form a
  **composite** join (paired by target); a column may appear in several `join_id`s (multi-target). A
  single-column join is just one `join_id` with one pair — the common case stays trivial.
- Ingest validates: every `join_id`'s targets resolve, pair counts match on both sides, and (BUILD) the
  paired columns are **type-compatible** (the referent type check, applied at ingest).

### S3 — Read-side access control was deleted with governance → **RESOLVE: keep a small read-authz**
Deleting *fact-approval* governance ≠ deleting *read* authz. The catalog is a searchable map of where
PII/restricted data lives, so v1 keeps a **lightweight read-scope model**: metadata visibility gated by
role × domain × sensitivity (at minimum, `sensitivity`-tagged objects require a role to view/search).
This is distinct from — and much smaller than — the retired propose/confirm machinery. The pivot's
"retire governance" list is corrected: **read-scope authz stays.**

### S4 — Enrichment must be an event, or replay re-runs the LLM → **RESOLVE**
LLM enrichment output (domain/subdomain/concept/definition, suggested-then-confirmed facts) is persisted
as **`ENRICHMENT_APPLIED` events**. The graph/search projection folds those events and **never invokes
the LLM on replay** — so rebuild-from-events is deterministic and free. The LLM runs only at ingest, on
the delta (see H2).

## Correctness & data-model (RESOLVE / BUILD)

- **T1 Type normalization (BUILD, vocabulary RESOLVED).** Canonical type set: `integer, bigint,
  decimal(p,s), float, text, boolean, date, timestamp, timestamptz, json, binary, other`. A
  normalization map folds tool spellings (`NUMBER(18,2)`→`decimal(18,2)`, `VARCHAR2`→`text`, `int4`→
  `integer`). Drift compares **normalized** types, so a re-export from a different tool doesn't false-fire.
- **T2 `source` identity stability (BUILD).** `source` is set explicitly (a `source` column, else chosen
  at upload) and **decoupled from the filename**. On upload, if the chosen source doesn't match any known
  source but its objects overlap an existing source > X%, warn "did you mean to update `<existing>`?" —
  guards accidental identity change.
- **T3 One-source-per-file for v1 (RESOLVE), multi-file-per-source V2.** A single upload carries whole
  sources (each `source` value is complete in that file). Splitting one source across files is V2. Same
  table in two sources = two distinct source-qualified objects by design; cross-source "same table"
  reconciliation is V2.
- **T4 Duplicate/empty rows (BUILD).** Two rows for the same `table.column`: identical → dedup; conflicting
  (e.g. two types) → validation error. Empty/header-only file → clean rejection.
- **T5 Graph metadata model + contract additions (RESOLVE).** Concrete per-node/per-edge fields defined
  (see *Graph metadata model* in the mapping spec): load-bearing → typed columns, advisory → JSONB, every
  item provenance-stamped. The contract gains **`unit`/`currency`** and SCD **`valid_from`/`valid_to`** as
  *load-bearing* — their absence causes *silently wrong features* (wrong scale / no effective-dating),
  which is worse than a missing definition. **Statistical** metadata (cardinality/null-rate/distribution)
  stays **sparse** (no-DB tax; declared-only, phase-2 profiling if a sample appears); **enums** are V2.
- **T6 Aggregation-semantics + entity metadata (RESOLVE).** Contract + graph gain **`additivity`**
  (additive/semi/non — never SUM a balance over time) and **`time_grain`** as *load-bearing*, an **Entity**
  node (`Customer`/`Account`, resolving ids across sources), and **proactive PII flagging** in enrichment.
  Declared → trusted; LLM-inferred load-bearing ones follow suggest→confirm.
- **P1 Phase-2 LLM feature-assist (V2, on record).** Gated on the feature layer (S1): multi-hop
  join-path suggestion, target-leakage warnings, feature recommendation, NL→recipe. Named so v1 metadata
  is built to feed them; all are human-acted suggestions, never auto-wired into load-bearing facts.
- **Q1 Quarantine handling + review queue (increment after the slice).** Backend detects + counts
  quarantine in the slice; the increment persists detail (raw row + reason + resolution, redacted, into
  L2 trace), surfaces a per-source **review queue** (discovery via upload result / queue / notification to
  a configured reviewer), and offers fix paths: inline-edit→revalidate, accept-suggestion, rule fix for
  systematic values, dismiss — plus re-upload as the canonical fallback. UI mocked (`quarantine-review`).

## Drift & brake (RESOLVE)

- **B1 Brake is absolute + relative + overlap, not a flat %.** Hold when *removed* > `max(30%, 5 objects)`,
  **or** the new upload's overlap with the prior snapshot < 60% (**wrong-source / replace detection** — the
  case a removal-% misses), **or** an implausible *add* burst. Thresholds are per-deployment policy.
- **B2 First upload has no baseline → soft-gate.** No brake possible, so a first upload is flagged
  ("new source, N objects — review") and, in a bank context, may require the S3 role to establish a source.

## Retrieval quality (RESOLVE / V2)

- **R1 Ranking function is defined (RESOLVE).** `score = w_text·fulltext + w_sem·(concept+domain match)
  + w_graph·graph_signal + w_fresh·freshness`, where `graph_signal` rewards join-degree, is-grain,
  and (phase-2) feature-usage. Default weights specified at build; **per-domain scoping** and
  homonym handling: a generic name (`id`, `amount`) is disambiguated by domain/table context and the
  query may be scoped to a domain.
- **R2 Controlled concept ontology (RESOLVE).** The LLM classifies into a **fixed seed vocabulary** of
  banking concepts (monetary amount, account identifier, customer identifier, as-of date, PII, …), NOT
  free text — otherwise the same idea fragments into un-linked concept nodes. Unknown → `unclassified`
  + a review queue that grows the vocabulary deliberately.
- **R3 Definition quality gate (BUILD).** LLM-drafted definitions carry `origin=llm, confidence`; low
  confidence is flagged for review; a definition never silently *replaces* a human-authored one.
- **R4 Relevance feedback (V2).** Click/selection signals to tune ranking — deferred.

## Tracing & lineage (RESOLVE)

- **L1 Provenance stamps on every fact and edge.** Each fact/edge carries `origin ∈
  {declared, llm_suggested, human_confirmed}`, `confidence`, `source_upload_id`, `mapping_version`; each
  enrichment annotation carries its `llm_call_id`. This makes the trust of a served fact/edge inspectable.
- **L2 Defined trace queries.** First-class: *fact → upload/file/mapping-version/row*; *edge → origin*;
  *tag/definition → LLM call (prompt, model, confidence)*; *staled fact → the re-upload + object change
  that caused it*. Feature-freshness lineage (feature → columns → sources → watermarks) is **phase-2**
  (needs features, S1).

## Time & space complexity (RESOLVE)

- **H1 Diff-append, not whole-file re-assertion.** A re-upload computes the delta vs the current folded
  state and appends only **changed** facts (`FACT_ASSERTED` / `FACT_RETRACTED`) — the log grows with
  *change*, not with *upload count*. Bounds log size and replay time.
- **H2 Incremental enrichment.** Enrich only new/changed columns, keyed by a content hash of
  (name + type + table context); unchanged columns keep their prior `ENRICHMENT_APPLIED`. LLM cost scales
  with the *delta*, not the full catalog, per upload.
- **H3 Bounded, streaming ingest.** Very large schema files parse/validate in a streamed pass; the
  all-or-nothing commit is staged, not "hold the whole file in RAM." Search stays on Postgres FTS
  (scales to millions of rows); graph traversals for ranking are bounded to 1–2 hops.

## Event model & wording (RESOLVE)

- **E1 One clean event.** Use **`FACT_ASSERTED`** (+ `FACT_RETRACTED`), not `OVERLAY_FACT_PROPOSED`+
  `CONFIRMED` back-to-back — faking a proposal/confirmation for auto-active facts muddies the audit. The
  pivot doc is corrected.
- **E2 Reconcile "no approval."** Wording unified to: **"no per-fact approval; a one-time onboarding
  confirm on ambiguous load-bearing mappings/joins only, plus S3 read-scope authz."**

## Edge cases (BUILD rules)

- **Concurrent uploads of a source** → serialize with a per-source advisory lock (mirrors the drift lock).
- **Case sensitivity** → per-source case-folding policy (default fold; a case-sensitive source opts out),
  so `ACCOUNTS`/`accounts` don't silently merge where identifiers are case-sensitive.
- **Format switch (CSV↔Excel) same source** → mapping is keyed by `source`, not format; the
  `header_signature` carries the reuse decision; a format change alone doesn't force a remap if headers match.
- **PII in LLM samples (SECURITY).** Mapping/enrichment gets **schema + column statistics/patterns, never
  raw cell values**; if a sample is ever needed it is redacted and not persisted. No raw PII into the
  immutable log (restores the erasability the retired blob path had).
- **Rollback** of a through-the-brake bad upload → append compensating `FACT_RETRACTED` / snapshot revert;
  no destructive edit (event-sourced).
- **Notification with no owners** → a **review queue** (drift impact, low-confidence enrichment, brake
  holds, unclassified concepts) replaces owner routing; it's the human touchpoint the pivot left implicit.

## Net effect on the build

The vertical slice is unchanged in shape but scoped honestly: **catalog/column** search (not feature),
**FACT_ASSERTED** diff-append events, **composite-capable** join/grain contract, **read-scope** authz,
**enrichment-as-events** + incremental, a **real ranking function** over a **controlled concept
vocabulary**, **provenance stamps + trace queries**, and the **redacted-sample** rule. Feature nodes,
feature lineage, relevance feedback, and multi-file-per-source are the named V2.
