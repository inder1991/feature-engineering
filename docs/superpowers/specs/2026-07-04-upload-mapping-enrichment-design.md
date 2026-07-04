# Upload → Mapping → Enrichment — design

Date: 2026-07-04. Status: proposed. Depends on the
[upload-catalog pivot](2026-07-04-upload-catalog-pivot-design.md).

> **Amended by the [architecture-review resolutions](2026-07-04-upload-catalog-review-resolutions.md)** —
> composite grain/join contract, controlled concept vocabulary, defined ranking + provenance/trace,
> diff-append + incremental enrichment, enrichment-as-events, and the **redacted-sample** rule (the LLM
> sees schema + column *statistics*, never raw cell values — no PII into the immutable log).

## Purpose & scope

The **front door** of the upload-driven catalog. It turns arbitrary uploaded schema files (Excel/CSV,
varying headers, one or more per source) into validated **canonical rows** plus advisory enrichment,
then commits them as events that feed the facts / drift / graph / search projections.

In scope: reading, header→field mapping (incl. **save & remember** per source), value rules, validation,
the large-change brake, LLM enrichment, and — at the design level — how ingested content **links** across
sources (graph edges) and is **retrieved** at feature-engineering time. Out of scope (separate docs): the
detailed search-ranking algorithm and the serve/query implementation. Assumes the pivot: no live DB, no
ownership, no approval, governance retired.

## Principles (decided in design dialogue)

1. **Read deterministically; understand once.** Turning a file into a *grid of cells* needs no
   understanding and is always deterministic. Deciding *which grid-column means what* is a one-time,
   saved judgment — configuration, not per-upload parsing.
2. **LLM for judgment, never for data.** The LLM may propose mappings and enrichment; it never reads
   or produces the bulk data values (deterministic parsers do). A hallucinated schema value would
   silently corrupt the source of truth.
3. **Confidence-gated, human by exception.** Confident + validated mappings auto-apply with no human;
   only ambiguous *load-bearing* fields surface for a one-time human confirm at source onboarding.
4. **Load-bearing vs advisory.** Facts (grain/join/as-of/sensitivity) drive *feature correctness* —
   high stakes, checked. Enrichment (domain/concept/definition) drives *search quality* — low stakes,
   auto-applied.
5. **Mapping is stable per source.** A source's mapping is decided once and reused on every re-upload,
   so drift diffs are honest — a real rename shows as a change, not as the platform reinterpreting the
   file differently the second time.
6. **Fail closed & atomic.** A bad, ambiguous, or oversized-change upload is rejected or held; the
   prior catalog is never partially overwritten.

## The canonical rows contract (fixed internal shape)

One row per column. This is the ONLY thing downstream depends on; the uploaded file's layout does not
have to match it.

| field | required | category | meaning |
|-------|----------|----------|---------|
| `source` | yes | key | one catalog per source (resolved by the ladder above; not the filename) |
| `table` | yes | schema | table/view name |
| `column` | yes | schema | column name |
| `type` | yes | schema | data type (normalized) |
| `is_grain` | no | **load-bearing** | member of the table's grain key (marked columns = the grain *set*) |
| `join_id` | no | **load-bearing** | groups the columns of one join; same `join_id` on both tables = a composite join (per S2) |
| `joins_to` | no | **load-bearing** | `table.column` this column pairs with, within its `join_id` |
| `cardinality` | no | **load-bearing** | `N:1` / `1:1` / `1:N` for the join — governs safe aggregation |
| `as_of_column` | no | **load-bearing** | marks the point-in-time / as-of column |
| `valid_from` / `valid_to` | no | **load-bearing** | SCD-2 effective-dating columns (silent-wrongness if absent on a versioned table) |
| `unit` / `currency` | no | **load-bearing** | scale/unit of a numeric column (dollars vs **cents**, currency) — wrong value = silently wrong feature |
| `sensitivity` | no | **load-bearing** | pii / restricted / … |
| `definition` | no | advisory | business description (drives search) |
| `domain` / `subdomain` | no | advisory | business grouping |
| `concept` | no | advisory | glossary concept tag (controlled vocabulary) |

`unit`/`currency` and `valid_from`/`valid_to` are **load-bearing** deliberately: unlike a missing `definition`
(which only weakens search), a wrong unit or an unmodelled SCD produces a *silently wrong feature* — worse
than a missing one.

## The pipeline (A–F)

```
 file ─A─▶ grid ─B─▶ mapping ─C─▶ canonical rows ─D─▶ validated ─E─▶ enriched ─F─▶ events
        (read,    (headers→fields,  (apply saved    (contract +    (LLM: domain/    (atomic;
      determin.)   decided ONCE      mapping,        large-change   concept/def +    facts+catalog
                   per source)       determin.)      brake)         fact suggests)   +graph+search)
```

### A. Read → grid (deterministic, no understanding, no LLM)
Detect format (extension + content sniff). Excel → `openpyxl` cells; CSV → reader rows. Skip
title/blank rows. Output: header row + raw data rows. Unknown/corrupt format → clean rejection, prior
catalog untouched.

### B. Map headers → canonical fields (decided ONCE per source)
- **Known source** (mapping already saved): load and reuse — skip to C. No LLM, no human.
- **New source** (or the file's header set changed):
  1. **Deterministic aliasing** — match headers to fields via a variant dictionary
     (case/space/underscore-insensitive: `table` ← {`table_name`, `TABLE_NAME`, `physical_table`}, …).
  2. **LLM proposal for the remainder** — given the *unmatched headers + column statistics/patterns*
     (redacted — **never raw cell values**, so no PII enters the immutable log), propose `field`,
     `confidence`, `reason`.
  3. **Value rules** where a field is encoded — e.g. `PII Flag` Y/N → `sensitivity = pii`.
  4. **Confidence gate:**
     - **schema fields** (table/column/type) high-confidence + validation-clean → **auto**.
     - **load-bearing fact fields** (is_grain/joins_to/as_of) low-confidence or ambiguous → surface
       *only those* for a one-time human confirm.
  5. **Save the mapping** (`header→field` + value rules) keyed to the source. Reused forever.

### C. Apply mapping → canonical rows (deterministic)
Using the frozen mapping, turn every grid row into a canonical row. Same file + same mapping = same
rows, every time (this is what keeps drift honest).

### D. Validate + large-change brake (fail closed)
- **Contract validation:** required fields present per row; type recognized/normalized; `joins_to`
  resolves to a table present in this upload; no blank table/column. **Per-row** problems quarantine the
  row (see *Graceful degradation*); only a **structural** failure (a required field unmappable for the
  whole file) rejects the upload.
- **Large-change brake:** compare this upload's object set to the source's last snapshot; if it would
  remove more than a threshold (default **30%**) of objects, **HOLD** and require an explicit
  "confirm large change" — so a truncated / wrong-source file can't silently stale the catalog.

### E. Enrich (LLM — advisory auto, fact suggestions human-confirm)
On the validated canonical rows:
- **Advisory (auto-apply, reviewable):** classify `domain`/`subdomain`, tag `concept`, and **draft
  missing `definition`s** from name + type + table context. Wrong = worse search, not wrong facts.
- **Fact suggestions (human-confirm, load-bearing):** where a fact column is BLANK, the LLM may
  *suggest* candidates ("likely grain: `account_id`"; "`acct_id` likely joins `accounts.account_id`")
  with confidence — surfaced for a quick OK, **never auto-applied**.
Enrichment failures are non-fatal: schema + facts still ingest; enrichment is re-runnable.

### F. Commit (atomic → events)
Append the fact + catalog-snapshot events (**auto-active, no approval**) in one transaction; the
projections (facts / drift / graph / search) rebuild from there, and the drift diff runs against the
source's prior snapshot.

## Load-bearing vs advisory — the split that governs LLM autonomy

| field group | LLM role | human | a wrong value causes |
|-------------|----------|-------|----------------------|
| schema (table/column/type) | map headers | none (validation backstops) | rejected by validation, not silent |
| facts (grain/join/as-of/sensitivity) | map + *suggest* | one-time confirm if ambiguous | **wrong features / data leakage** |
| enrichment (domain/concept/definition) | classify + draft, auto | none (correct later) | worse search only |

Test for "can the LLM decide with no human?": *if it is confidently wrong, does code catch it, or does
it silently corrupt something?* Code-catchable or low-stakes → no human. Silent + high-stakes → one
confirm at onboarding.

## Mapping — save & remember (the load-bearing memory)

This is the piece everything downstream rides on: get the "remember" wrong and re-uploads either need
re-mapping every time or corrupt drift.

### Two identities — never conflate them
- **`source`** — a *bookkeeping* label used only for drift (which prior snapshot the next upload diffs
  against). Must be **stable across re-uploads**. It is the *drift baseline key*, so getting it right
  matters; see the resolution ladder below.
- **Meaning** (domain / concept / definition) — *inferred from the file's CONTENT* by Step E, never from
  the name. A file of `txn_id, amount, merchant, posted_at` classifies to *Payments / Card Transactions*
  regardless of what it's called. A random filename costs nothing on understanding.

### Resolving `source` — the DBA almost never types it (onboard-once, auto-thereafter)
Determined by a ladder, cheapest-first — the filename is **not** used (random/inconsistent names are the
whole problem):
1. **`source` column in the file → zero-touch.** If the export carries a `system`/`source` column, read
   it. No prompt, even on a first upload. (Encouraged in the template.)
2. **Content auto-match on re-upload → automatic, confirm by exception.** Before asking anything, compare
   the file's *table/column set* to every existing source's snapshot. Strong, unambiguous overlap →
   **recognized as that source automatically** (covers the whole monthly-re-upload case — nothing to
   select). This is **confidence-gated because it is load-bearing**: a wrong match diffs against the wrong
   baseline (false drift / missed drift), and table names *can* collide (`deposits` vs `deposits_reporting`
   both have `accounts`). So: high overlap → auto; medium / two plausible sources → "looks like `deposits`
   (85%) — confirm?"; low / none → treat as new.
3. **New source (no match) → propose a name, don't ask for one.** Derive a candidate from the `source`
   column if present, else the LLM inferring it from the content ("these look like a Deposits system →
   `deposits`"); the user **confirms or renames in one click**, never a blank field.

Manual selection is only the fallback when auto-match is genuinely ambiguous — not the default. Same
"silent + load-bearing → gate it" rule as grain/join mapping: auto when confident, confirm when not,
never blind.

### The mapping record (per source)
```
source            : "deposits"
format            : "csv"
header_signature  : ["Physical Table","Attribute Name","SQL Type","PII Flag","Comment"]   # exact expected headers
field_map         : { "Physical Table"→table, "Attribute Name"→column, "SQL Type"→type,
                      "PII Flag"→sensitivity, "Comment"→definition }
value_rules       : { sensitivity: { "Y"→pii, "N"→null } }
established_by     : human | llm      established_at : 2026-07-04      confidence : …
```
`field_map` (header→field) + `header_signature` (the fingerprint of the header set this mapping was built
for) do the work.

### Stored as EVENTS, not a config row
`MAPPING_ESTABLISHED` / `MAPPING_UPDATED` events in the same append-only log; the "current mapping per
source" is a projection (fold). Buys **audit** (who/when/why a source is mapped this way), **history**
(the mapping changed when the export tool changed), and **consistency** with the backbone. The durable
data is still the committed canonical rows (fact/catalog events) — the mapping only interprets the *next*
upload, so old files are never re-mapped.

### Reuse on re-upload — the header-signature match
1. Identify the `source` → load its current mapping (projection). None → **new source** → full Step B
   (aliasing + LLM, human only on ambiguous load-bearing fields) → `MAPPING_ESTABLISHED`.
2. Mapping exists → compare the file's headers to the saved `header_signature`:
   - **match → reuse the mapping directly** — no LLM, no human, deterministic (the normal re-upload).
   - **mismatch → the file's columns changed** → re-run Step B for the *changed headers only* →
     `MAPPING_UPDATED` (human glance if load-bearing).
3. Apply → canonical rows → D onward.

### The crux: mapping-change vs drift-change (the `header_signature` disambiguates)
- **Headers change** (file format changed, e.g. tool now writes `COL` not `Attribute Name`) → a **mapping
  event** — remap; do NOT fire drift.
- **Data changes** (headers same, but `posted_at` renamed to `event_ts` in the schema) → a **drift event**
  — mapping untouched, the drift diff stales the affected facts.
- Confusing these is catastrophic: a harmless tool-format change would look like the whole schema drifted,
  or a real rename would be silently absorbed by re-guessing the mapping. Stable + fingerprinted mapping
  prevents both.

*deposits, three months:* M1 new source → establish + save. M2 same headers, `posted_at→event_ts` in the
data → headers match → reuse automatically → drift stales the fact. M3 export tool changed the headers →
mismatch → remap (`MAPPING_UPDATED`) → then ingest + drift.

## Graph metadata model — what nodes/edges carry, and how features use it

### Mechanics (how metadata is attached)
Nodes and edges are **rows** in projection tables (`graph_node`, `graph_edge`), folded from the event log:
- **Load-bearing** metadata (type, grain, join, as-of, valid-from/to, unit/currency, sensitivity) ←
  `FACT_ASSERTED` → **typed columns** (the things you filter/join/aggregate on).
- **Advisory** metadata (definition, concept, domain, any stats) ← `ENRICHMENT_APPLIED` → a **JSONB
  payload** (extensible without a migration per tag type).
- **Every item carries its own provenance** (`origin`, `confidence`, `source_upload_id`,
  `mapping_version` or `llm_call_id`). The *same* field can arrive from different origins and you must
  know which to trust: `sensitivity=pii` *declared* by the DBA vs *guessed* by the LLM.

### Per-node fields
- **Column** — identity (source·table·column); `type` (normalized); facts (`is_grain`, `is_as_of`,
  `valid_from/to`, `unit/currency`, `sensitivity`); enrichment (`concept`, `domain`, `definition`);
  `state` (active/stale + source watermark); `stats` (sparse — only if declared); provenance per item.
- **Table** — grain **set**, the `as_of` column, SCD `valid_from/to` columns, domain/subdomain,
  definition, join-degree, column count, drift status.
- **Concept** — controlled-vocabulary name + definition + members. **Domain** — name, subdomain
  hierarchy, member tables.

### Per-edge fields
- **Join** (the load-bearing one) — `join_id`; the **paired** `from→to` columns (composite); `cardinality`
  (N:1 / 1:1 / 1:N); `origin` + `confidence`; `type_compat`.
- **Contains** (table→column) — ordinal. **Concept** (column→concept) / **Domain** (table→domain) —
  provenance + confidence.

### How feature-building leverages it (each field earns its place)

| the builder's question | the metadata that answers it |
|---|---|
| where do I filter to avoid **leaking the future**? | the table's **`as_of`** (and SCD `valid_from/to`) |
| how exactly do I **join** these tables? | the **join edge's paired columns** (composite) |
| is the join safe to aggregate, or will it **double-count**? | the edge's **`cardinality`** (N:1 = safe fan-in) |
| what key do I **aggregate to**? | the tables' **grain sets** |
| what operations are **valid**, and in what **unit**? | the **normalized `type`** + **`unit`/`currency`** |
| am I **allowed** to use this? | the **`sensitivity`** tag → mask / exclude / flag |
| how much do I **trust** this before building on it? | **provenance + drift** (declared vs llm_suggested; active vs stale) |

So "avg customer balance over 90 days" becomes an automatic, correct recipe: as-of → point-in-time
filter; grain → aggregate key; join + N:1 → safe fan-in; type+unit → valid avg in the right scale;
sensitivity → drop PII; provenance → "one join in this path is only LLM-suggested, verify."

### Is it enough? (honest gaps)
Enough for **correctness** (join right, aggregate right, no leakage, compliance-aware). Thin on:
- **Statistical metadata** (cardinality, null-rate, distribution, min/max) — the **no-DB tax**; only
  present if the upload declares it. Phase-2 may profile if a data sample is ever available.
- **Enums / valid value sets** (for one-hot features) — V2.
`unit`/`currency` and SCD `valid_from/to` are pulled *into* the contract (above) precisely because their
absence causes *silent wrongness*, not mere sparsity.

## Linking — how columns connect across sources

An uploaded file becomes *connected*, not a lonely blob, through graph **edges** at four strengths:

1. **Declared joins (strongest).** `joins_to = accounts.account_id` becomes an explicit edge — and it may
   cross sources (a transactions file → the `accounts` table from a *different* upload). Every object is
   **source-qualified**, so cross-file joins are unambiguous. The more `joins_to` your files carry, the
   richer and more trustworthy the graph.
2. **Pending joins.** If the target isn't loaded yet (`accounts` not uploaded), the edge is recorded
   **pending** and **resolves automatically** when that source arrives — so upload order doesn't matter.
3. **Suggested joins (LLM/heuristic).** Undeclared links are *proposed* from name + type + concept
   similarity (`transactions.acct_id` ↔ `accounts.account_id`) → **surfaced for a one-time human confirm,
   never auto-applied** (load-bearing).
4. **Concept links.** Columns sharing a `concept` tag connect through a concept node (`transactions.amount`
   and `accounts.balance` both → *monetary amount*), powering estate-wide queries ("every monetary field
   at the customer grain").
5. **Domain grouping.** The file's tables join their `domain` node as siblings of related tables.

**No-DB limitation (be explicit):** without the data we cannot verify a join by value overlap. So
cross-source links come from **declared `joins_to`** (reliable) and **metadata similarity** (a hint, not
proof) — which is exactly why *suggested* joins are advisory and human-confirmed. Declared joins are
always the strongest glue.

## Retrieval — how it's found at feature-engineering time

Ingested content becomes **graph nodes** (name, type, definition, domain/subdomain, concept tags) and
flows into the **search index** (full-text over names + definitions + tags). So an analyst never needs the
filename:

- Search "customer transaction amount" → `transactions.amount` surfaces because full-text matched its
  name/definition, its `concept = monetary amount` matched "amount", its `domain = Payments` matched
  "transaction" — **ranked by graph context** (connected/joinable/grained/already-used beats an orphan).
- The result carries context, not just a hit: *"amount (transactions) · Payments · monetary · joins
  accounts on acct_id · accounts is customer-grained."* So the analyst gets **the join key and the grain**
  needed to build the feature correctly, point-in-time — without archaeology.

Retrieval quality is therefore a product of: good **definitions** (drafted at ingest), **concept/domain**
tags, and the **link edges** above — the graph is what makes a random-named transaction dump a findable,
placed, wired-in part of the estate.

## Search ranking function (detail)

**Searchable document** per node — a **weighted `tsvector`**: `A`=name, `B`=definition, `C`=concept,
`D`=domain+table (so a name/definition hit outweighs a domain hit). Plus a **`pg_trgm`** index on name
for fuzzy/partial matches. Both Postgres-native; no external search engine.

**Auth pre-filter (a hard filter, NOT a weight).** Nodes the user's read-scope can't see (e.g. `pii`
without the role) are **dropped before scoring** — security must never be a rankable weight.

**Four terms, each normalized to [0,1]:**
- `text` = `ts_rank_cd(weighted_tsvector, query)` + a boost for exact/prefix **name** match.
- `sem` = 1 if the node's `concept`/`domain` matches the **query's classified** concept/domain (partial
  for related concepts), else 0. Query classification: synonym-dictionary lookup first, an optional cheap
  LLM classify for hard queries (cached). This is the "meaningful" lift keyword alone misses — without
  embeddings.
- `graph` = a **precomputed** node-usefulness score — normalized blend of join-degree, `is_grain`,
  has-`as_of` (+ phase-2 feature-usage). Precomputed in the projection → **no query-time graph walk**.
- `fresh` = 1 if the source is fresh; linear decay past the drift SLA; **excluded** if stale.

**Score** = `w_text·text + w_sem·sem + w_graph·graph + w_fresh·fresh`. Default weights **.50 / .25 / .15 /
.10**, tunable per deployment (R4/V2: usage feedback tunes them). **Tiebreakers:** `declared` >
`llm_suggested` provenance, then shorter/exact name.

**Homonyms / generic names** (`id`, `amount`, `date`): text alone is ambiguous → `sem` + `graph` break
ties, queries can be **domain-scoped** ("amount in Payments"), and results **group by domain/table** so
the user disambiguates.

**Explainability** — because the score is a sum of named terms, each result shows its contribution:
*"matched 'balance' in definition · concept=monetary amount · grain key · fresh."* This is the
explainability edge over embedding cosine scores.

**Performance** — a GIN index on the `tsvector` + the precomputed `graph_signal` column + a freshness
join = a **single indexed query**, bounded, no per-query traversal.

## Trace queries (detail)

Every belief the platform holds traces back to its origin via **bounded lookups/joins over the event log
+ provenance stamps (L1)** — no special infra. Each record carries the join keys:
- `FACT_ASSERTED`/`FACT_RETRACTED`: fact id, value, `origin`, `confidence`, `source_upload_id`,
  `mapping_version`, `catalog_change_ref` (for staling), actor, ts.
- `upload`: `upload_id`, source, file, format, uploaded_by, ts, mapping_version, row_count, brake_status.
- `mapping` (versioned): `MAPPING_ESTABLISHED`/`UPDATED` events.
- `llm_call`: id, purpose (mapping/enrichment/plausibility), model, prompt, **redacted** input, output,
  confidence, ts.
- `ENRICHMENT_APPLIED`: node, field, value, `llm_call_id`, confidence.

The queries:
1. **Fact provenance** — *"why does the platform believe accounts' grain is `(branch,account)`?"* → fact →
   asserting event → upload (file / uploaded_by / time) + `mapping_version` + the source row(s) + origin.
2. **Join/edge provenance** — edge → `origin` (declared / suggested / confirmed) + confidence + upload/rows
   (+ confirmer/time if human-confirmed).
3. **Enrichment provenance** — *"why is `ssn_hash` PII?"* → tag/definition → `ENRICHMENT_APPLIED` →
   `llm_call` (prompt / model / redacted input / output / confidence), or the mapping value-rule if declared.
4. **Drift causation** — *"why was this fact staled?"* → stale fact → `STALED`/`RETRACTED` event →
   `catalog_change_ref` → the drift scan → the re-upload + the exact object delta.
5. **Upload impact (reverse)** — *"what did this upload change?"* → `upload_id` → every event stamped with
   it (facts asserted/retracted, objects +/−, staled, enrichment applied).
6. **Column history** — fold the events for an object over time → its type/facts/tags timeline across uploads.
7. **Feature-freshness lineage (phase-2)** — feature → derives-from columns → sources → watermarks →
   fresh/stale. Deferred (needs features, S1).

Audit value: for **any** belief `X`, there is a bounded query back to the upload / row / mapping-version /
LLM-call that produced it — the bank-grade "show me why."

## LLM usage boundaries (safety)

- **Used:** Step B mapping proposal (headers + redacted column stats only — never raw values), Step E
  enrichment + fact suggestions.
- **Never used:** reading files, applying mappings, producing any table/column/type value, or gating a
  load-bearing fact without a human confirm.
- Every LLM output is either **validated by deterministic code** or **confirmed by a human** before it
  affects the catalog. All LLM calls are audited in the event backbone (prompt, model, output).

## Graceful degradation — the deterministic layer is robust, not brittle

Reading is deterministic and needs no understanding; *understanding* is a separate, flexible ladder
(aliasing → LLM → human → preserve). So the pipeline never "fails to understand" — when it can't place
something, it **preserves and flags** rather than dropping, guessing, or halting. **Partial
understanding is fine: ingest what's understood, preserve what isn't, flag it, never silently lose or
invent anything.**

- **Unknown *column*** (a header no alias knows and the LLM can't confidently place, e.g. `RGLTRY_CLSS`)
  → kept as an **`unclassified` attribute** on those columns + queued for mapping. The rest of the file
  ingests; nothing about the column is discarded or fabricated.
- **Structural junk rows** (blank / title / subtotal / merged-cell rows that don't fit the grid) →
  skipped deterministically by the reader.
- **Unrecognized *value* in a mapped field** (e.g. `type = "blobby"`) → that **row is quarantined**
  (kept as `unclassified` + review queue), *not* dropped and *not* rejecting the file — the other
  99% of good rows still ingest.

### "All-or-nothing" ≠ "one bad row rejects everything"
All-or-nothing protects against a **half-applied catalog**, not against imperfect rows. Precisely: the
**commit is atomic** (the good rows + the quarantine list land together, or nothing does), but per-row
problems **quarantine**, they do not reject the file. Only a **structural** failure rejects the whole
upload — an unreadable file, or a *required* field (`table`/`column`/`type`) that cannot be mapped
**at all**.

## Fail-closed behaviors (summary)

| condition | outcome |
|-----------|---------|
| unreadable / unknown format | reject; prior catalog intact |
| **structural** failure (required field unmappable for the whole file) | reject with the reason |
| unknown column (unmappable) | **preserve as `unclassified`** + review queue; rest ingests |
| unrecognized value in a mapped field (per row) | **quarantine that row** (`unclassified` + review); rest ingests |
| ambiguous load-bearing mapping | hold for a one-time human confirm (onboarding) |
| oversized removal / low overlap (> brake) | hold for explicit "confirm large change" |
| enrichment failure | non-fatal; schema + facts still ingest; re-runnable |
| partial *catalog* state | never — the commit (good rows + quarantine) is atomic |

## Open decisions

- **`source` determination** — RESOLVED: the ladder in "Resolving `source`" above (`source` column →
  content auto-match, confidence-gated → propose-a-name for a new source). Filename is not used.
- **One file → many sources?** Recommend: allow a `source` column so one file can carry several.
- **Brake threshold** (default 30% object removal) — policy, tunable per deployment.
- **First-onboarding human confirm** — recommend: required *only* when a load-bearing field is
  low-confidence; a fully-confident first upload can auto-onboard.

## Build sequence (ties to the vertical slice)

1. **Read** (CSV + Excel) → grid.
2. **Deterministic aliasing + apply** → canonical rows (no LLM yet).
3. **Validate + brake.**
4. **Commit → facts + drift** — the slice's core (proves the spine end-to-end).
5. **LLM mapping proposal** (Step B) — confidence gate + save-per-source.
6. **LLM enrichment** (Step E) — advisory + fact suggestions.

The vertical slice is **1–4 with a hand-written mapping**; the LLM (5–6) layers on *after* the
deterministic spine is proven, so an LLM bug can never be confused with a spine bug.
