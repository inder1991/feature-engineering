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
| `source` | yes | key | one catalog per source (filename stem, a `source` column, or chosen at upload) |
| `table` | yes | schema | table/view name |
| `column` | yes | schema | column name |
| `type` | yes | schema | data type (normalized) |
| `is_grain` | no | **load-bearing** | part of the table's grain key |
| `joins_to` | no | **load-bearing** | `table.column` this column joins to |
| `as_of_column` | no | **load-bearing** | marks the point-in-time / as-of column |
| `sensitivity` | no | **load-bearing** | pii / restricted / … |
| `definition` | no | advisory | business description (drives search) |
| `domain` / `subdomain` | no | advisory | business grouping |
| `concept` | no | advisory | glossary concept tag |

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
  resolves to a table present in this upload; no blank table/column. Report the failing rows; a hard
  failure rejects the whole file (all-or-nothing).
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
  against). Must be **stable across re-uploads**. A random/uninformative filename is bad *only* for this,
  so `source` is set explicitly (a `source` column, else the filename stem, overridable at upload).
- **Meaning** (domain / concept / definition) — *inferred from the file's CONTENT* by Step E, never from
  the name. A file of `txn_id, amount, merchant, posted_at` classifies to *Payments / Card Transactions*
  regardless of what it's called. A random filename costs nothing on understanding.

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

## LLM usage boundaries (safety)

- **Used:** Step B mapping proposal (headers + redacted column stats only — never raw values), Step E
  enrichment + fact suggestions.
- **Never used:** reading files, applying mappings, producing any table/column/type value, or gating a
  load-bearing fact without a human confirm.
- Every LLM output is either **validated by deterministic code** or **confirmed by a human** before it
  affects the catalog. All LLM calls are audited in the event backbone (prompt, model, output).

## Fail-closed behaviors (summary)

| condition | outcome |
|-----------|---------|
| unreadable / unknown format | reject; prior catalog intact |
| contract validation failure | reject with the failing rows |
| ambiguous load-bearing mapping | hold for a one-time human confirm (onboarding) |
| oversized removal (> brake threshold) | hold for explicit "confirm large change" |
| enrichment failure | non-fatal; schema + facts still ingest; re-runnable |
| any partial state | never — commit is all-or-nothing |

## Open decisions

- **`source` determination** — a `source` column, the filename stem, or chosen at upload. Recommend:
  a `source` column when present, else the filename stem, overridable at upload.
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
