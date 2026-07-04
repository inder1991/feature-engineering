# Upload → Mapping → Enrichment — design

Date: 2026-07-04. Status: proposed. Depends on the
[upload-catalog pivot](2026-07-04-upload-catalog-pivot-design.md).

## Purpose & scope

The **front door** of the upload-driven catalog. It turns arbitrary uploaded schema files (Excel/CSV,
varying headers, one or more per source) into validated **canonical rows** plus advisory enrichment,
then commits them as events that feed the facts / drift / graph / search projections.

In scope: reading, header→field mapping, value rules, validation, the large-change brake, and LLM
enrichment. Out of scope (separate docs): the downstream projections, search ranking, and the serve
path. Assumes the pivot: no live DB, no ownership, no approval, governance retired.

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
  2. **LLM proposal for the remainder** — given the *unmatched headers + a few sample cell values*
     (never the full data), propose `field`, `confidence`, `reason`.
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

## Per-source mapping & drift stability

The saved mapping per source: `{ source, header→field, value_rules, established_at, established_by }`,
reused on every re-upload. Two distinct change types must not be confused:
- **Header change** (the FILE's columns change — a header added/removed/renamed) → a **mapping event**:
  re-run Step B for the changed headers only, update the saved mapping (human glance if load-bearing).
- **Data change** (the schema the file describes changes — a table/column dropped/renamed) → a **drift
  event**: handled by the drift diff in F.
Keeping the mapping stable is what lets the drift diff attribute a change to the *schema*, not to the
platform re-reading the file.

## LLM usage boundaries (safety)

- **Used:** Step B mapping proposal (headers + samples only), Step E enrichment + fact suggestions.
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
