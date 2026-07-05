# Upload-Catalog Implementation — Deep-Dive Review Findings

Date: 2026-07-05. Four adversarial reviewers (opus) over the built `overlay/upload/` package +
feature layer + migrations 0945–0958 + the `catalog_changes.py` edit, each grounded in source and the
relevant plan. Every finding below was **re-verified against the code** by the lead before inclusion.

## Meta-insight (why green tests missed these)
The full suite (1216) is green, yet the review found 1 blocker + ~9 majors. The tests only cover
**happy paths**: single-source files, a first-upload-then-column-drop sequence, and `FakeLLM`. They do
**not** cover: a re-upload that *changes a fact's value*, a *real* LLM provider, a *multi-source* file,
a *reverse* join hop, or *provider failures*. Every serious finding lives in exactly those gaps. The
implementation also shipped **well beyond the plans** (enrichment, graph, review-queue, 9 extra
`CanonicalRow` fields all wired into `ingest_upload` — none in the slice plan's Task 5), so much of it
was never plan-reviewed until now.

## BLOCKER — silent data corruption in the core serve path

**B1. A re-upload never updates a fact's VALUE; the stale value is served.** `ingest._assert_fact`
skips whenever the fact's stream exists (`if load_fact(fk): return False`), and `fact_key` is
table-keyed and value-independent. So a re-upload that *changes* a value — grain `[id]` → composite
`[id, cust_id]`, or the `as_of` flag moves to a different still-present column — appends nothing, and
drift only stales on drop/type-change/rename. `resolve_fact` keeps serving the OLD value → a pipeline
dedups/joins on `id` as unique when the true grain is composite → **silent wrong data.** Also: the
graph is rebuilt wholesale each upload, so `graph_node.is_grain` reflects upload-2 while the served
*fact* reflects upload-1 — two sources of truth that disagree. *Fix:* key the skip on
`proposal_fingerprint(value)`; on a value change, re-assert (or STALE to fail closed).

## MAJOR

**M1. A staled upload fact can never recover.** Upload staling uses `open_reverify=False` (no task) and
`_assert_fact` won't re-assert an existing stream, so once a column is dropped→STALE, re-adding it in a
later upload leaves the fact STALE/`value=None` **forever** even after the user fixes the file.
*Fix:* the re-assert skip must not apply to non-VERIFIED streams; re-confirm on re-declare.

**M2. Enrichment is `FakeLLM`-only; a real provider fails closed and poisons the cache.** `enrich._call`
calls `client.call()` directly, never attaching `output_schema` — `ClaudeLLM` fails closed without it
(`llm_claude.py:111-112`, verified), returning `output={}`. It also passes bare input keys instead of
the reserved `INPUT_KEY_INTENT`/`INPUT_KEY_CATALOG` the adapter reads, and the output-schema ids are
registered nowhere. So with a real provider every enrichment → empty → concept `unclassified`,
definition/domain `""`. *Latent BLOCKER the day `FEATUREGEN_LLM_PROVIDER=anthropic` is set.*

**M3. Enrichment cache-poisons on any provider failure.** `_call` ignores `LLMResult.status`; a
transient 503/429 or refusal → `output={}` → `""` → written to the cache permanently (cache-first =
never retried). One hiccup poisons a column's concept/definition/domain forever. *Fix:* only cache a
`PROVIDER_OK`, non-empty, validated result.

**M4. Enrichment bypasses the egress guard — uploader free-text can leak PII to the LLM unscanned.**
Going around `call_llm` skips `assert_llm_safe`. The concept/definition inputs include the uploader's
free-text `definition`; a definition containing an SSN is sent to the provider verbatim, with no PII
scan, no `llm_call` audit record, and enrichment spend **off** the run's cost-breaker budget. *Fix:*
route enrichment content through the redactor / an egress check before dispatch.

**M5. Multi-source upload crashes the whole ingest (duplicate-key).** `validate_rows` dedups on
`(source, table, column)`, but `build_graph` writes `graph_node` PK `(catalog_source, object_ref)`
using the `catalog_source` **argument** and ignoring per-row `source`, with **no `ON CONFLICT`**. The
readers explicitly support a `source` column (tested), so a single file with two `source` values →
both rows "good" → `UniqueViolation` → entire ingest rolls back (confirmed against real Postgres).
*Fix:* enforce `r.source == catalog_source` (reject/quarantine otherwise), or fully source-qualify the
node identity.

**M6. `feature_assist` bypasses read-scope authz → the PII leak read-scope exists to prevent.**
`_candidate_columns` `SELECT … FROM graph_node WHERE kind='column'` has **no sensitivity filter and no
role param**; `recommend_features`/`feature_recipe` feed those names/definitions/concepts to the LLM
and return them. A `sensitivity='pii'` column that `search()` correctly hides is fully exposed here.
*Fix:* centralize the `sensitivity IS NULL OR = ANY(allowed)` filter; require roles on every
`graph_node` column read.

**M7. Join-path reverse hop reports the wrong cardinality.** `find_join_path` copies each edge's stored
`cardinality` in both directions. A stored `N:1` edge traversed backwards is really `1:N`, but the
`JoinStep` still says `N:1` — a feature-builder would think a hop fans in safely when it fans out →
double-counting. `test_feature_assist.py:48` asserts the buggy value, locking it in. *Fix:* invert
cardinality + swap refs for reverse edges at adjacency-build time.

**M8. `availability_time.basis` is hard-coded to `"posted_at"`** for every ingested as-of column,
regardless of the real column semantics — an ingestion-timestamp as-of is served with a false basis,
VERIFIED. *Fix:* carry basis in `CanonicalRow` (or default explicitly and mark unknown).

**M9. `draft_definitions`/`classify_domains` do not validate LLM output** (unlike concept). A garbage
value, a list stringified to `"['Deposits','Payments']"`, or a 5,000-char paragraph is stored and shown
/ folded into `search_doc`. *Fix:* controlled domain vocabulary + non-empty/length bounds.

## MINOR (robustness / edge / UX — grouped)
- **is_first_upload soft-gate never surfaced** — computed in the brake, dropped by ingest (no
  `IngestResult` field); a first bulk upload ingests with no reviewer signal.
- **Quarantine not refreshed on held/rejected** — contradicts the "re-evaluated every upload" claim in
  the code/migration comments (prior quarantine lingers after a held re-upload). Doc-vs-behavior.
- **Multiple `as_of` columns silently collapse to the first**; **dedup drops a differing `is_grain`/
  `as_of` flag** (dedup compares only `type`). Both need a cardinality/semantic check in `validate_rows`.
- **Concurrency-truncated drift scan returns "ingested"** with the watermark unwritten → reads fail
  closed for the whole source, yet status lies. Also `run_projection` default batch=500 could lag an
  upload >500 events.
- **Malformed / non-`public` `joins_to`** (typo'd FK, wrong dot-count) silently becomes an unresolvable
  edge indistinguishable from a real pending cross-source join.
- **Unknown sensitivity value** (`confidential`, or `public`/`none` meaning not-sensitive) → node
  permanently invisible to everyone (fail-closed, safe, but silent).
- **`build_graph` rebuild has no per-source advisory lock** — concurrent same-source ingests can
  duplicate-key/deadlock. (Cross-source orphaning confirmed NOT an issue.)
- **CSV reader breaks on a UTF-8 BOM** (`﻿` not stripped by `_norm`) — Excel-exported CSVs drop
  their first column silently. **`field_map` last-alias-wins** silently drops a duplicately-aliased
  column. **Excel blank-row detection** (`all None`) inconsistent with header detection (`any strip`).
- **`content_hash` delimiter collision** (unescaped `|`); **cross-source definition cache sharing**
  (hash omits `source` → one source's drafted definition shown for another's same-named column);
  **`_call` not guarded against a non-dict `.output`** (feature_assist guards, enrich doesn't).

## Confirmed NON-issues (suspects checked, clean)
`_SEARCH_DOC` param alignment (5 %s, 9/9 + 20/20 params — AST-verified); column-list vs VALUES counts;
brake math (snapshot holds both tables+columns; overlap rule subsumes removal>40%); `open_reverify`
default preserves all governance callers; `UploadCatalog` satisfies the `CatalogAdapter` protocol;
drop→stale dependency wiring; `_table_of` on a normal `joins_to` (3-part ref → correct table); dynamic
SQL in the caches (fixed `_CACHES` dict, no injection); declared-definition-never-overwritten;
zero-LLM-on-reingest (cache-first); the empty-`allowed` authz path (PII hidden with no role); Excel
header-generator resume (no first-data-row consumption); CSV short/long rows via `restval`/`restkey`.

## Priority / disposition
- **Fix before ANY real use (silent-data + crash + PII):** B1, M1, M5, M6.
- **Fix before wiring a real LLM provider:** M2, M3, M4, M9 (the enrichment path is not production-ready).
- **Fix before feature-builder consumers rely on paths:** M7, M8.
- **Harden opportunistically:** the minors (several are latent — no production callers yet).
