# Phase-1 LLM-Enrichment Hardening — Design

**Date:** 2026-07-18
**Status:** Approved (user-directed "one pass"; batch-ceiling section added per architect review 2026-07-18)
**Predecessor:** FTR glossary adapter A1 (merged origin/main `9bb3e4b`) + all-flags-on release blockers (merged origin/main `9852b1c`) + maxItems schema fix (`325fd84`, this branch)

## Problem

With `FEATUREGEN_LLM_PROVIDER=anthropic` enabled in the Kind deploy, LLM enrichment does not
work end-to-end. The enrichment output schemas are strict JSON Schema (built for local
`jsonschema` validation), but Anthropic's structured-output API accepts only a **subset** of JSON
Schema. The wire request is a hand-built `output_config={"format":{"type":"json_schema","schema":
<raw canonical schema>}}` (`llm_claude.py:117-127`) — the canonical schema is sent verbatim, so the
provider rejects it:

- `maxItems`/`minItems` on arrays → `BadRequestError 400` (fixed in `325fd84`).
- **19 `maxLength`** constraints remain (`enrich_llm.py` `_SCHEMAS`) → next 400.
- **5 nullable-enums** (`{"type":["string","null"],"enum":[...,null]}`, Pass B) → `400: "Enum value
  '<x>' does not match declared type '['string','null']'"`.

Because CI drives a scripted `FakeLLM` that never validates the schema against the real API, none of
this is caught before deploy. Separately, running the real bank file through the enabled pipeline
surfaced quality/correctness defects the synthetic fixtures never exposed (parser misclassification,
Pass B starved of glossary context, abstention counted as failure, an untruthful result, an
undefended source-selection footgun, blind 200-char definition truncation), and the batch-size
ceilings that gate throughput have **no accuracy evidence** behind them.

## Architecture — the linchpin: provider-schema projection

Keep **one canonical strict schema per task** as the single source of truth for local validation and
persistence. Derive a **provider-projected schema** only at the wire boundary:

```
canonical _SCHEMAS[task]  ──(register)──►  DocumentSchemaRegistry   ──► reg.validate(response)   [enforces maxLength/enum locally]
        │
        └──(ClaudeLLM.call, wire only)──►  project_for_anthropic()  ──► output_config.format.schema [provider-compatible]
```

- **Wire projection** (`project_for_anthropic`) strips provider-unsupported keywords
  (`maxLength`, `maxItems`, `minItems`, `minimum`, `maximum`, `exclusiveMinimum`,
  `exclusiveMaximum`, `multipleOf`) and normalizes each nullable-enum
  `{"type":["T","null"],"enum":[...,null]}` into the provider-accepted union form
  `{"anyOf":[{"type":"T","enum":[<non-null members>]},{"type":"null"}]}`. Pure, deterministic,
  SDK-independent, fully unit-testable.
- **Response validation stays canonical.** The shared driver already validates every response with
  `reg.validate(schema_id, 1, output)` against the **registered canonical schema**
  (`llm.py:216-236` + `enrich_llm.py:420,603`). So the constraints we strip from the wire (length
  caps, enum membership) remain enforced on the model's *output* — a too-long string or off-enum
  value still triggers `SchemaValidationError` → bounded repair → fail-closed. No new response-
  validation code is needed; we add a test proving it.
- **Fail at registration.** `register_enrichment_schemas` projects each schema and asserts the
  projection is provider-clean (`provider_incompatibilities(projected) == []`), raising if not — so
  an incompatible schema can never reach a live deploy silently. A static CI test runs the same
  assertion across **every** `_SCHEMAS` entry.
- **Safe 400 recording.** The `APIStatusError` branch in `ClaudeLLM.call` records the provider HTTP
  status + the rejected-schema keyword (parsed from the error text, no request/response content, no
  PII) as a diagnostic before failing closed.

We deliberately **do not** use the SDK's `anthropic.transform_schema` helper: it is not applied on
the `output_config` path we use, it demotes unsupported keywords into `description` text rather than
removing them, and it does **not** normalize nullable-enums — so it neither covers our case nor gives
us the clean, testable, version-independent transform the projection provides.

## The eight work items

### The projection layer (linchpin, above)
Plus: declare `anthropic` **version-bounded** in `pyproject.toml` (`[project.optional-dependencies].llm`,
floor pinned to the proven-good deploy version) and point the Kind Dockerfile at that extra, instead
of an unbounded `pip install "anthropic>=0.40"`.

### MF-1 — Parser-evidence reconciliation
`parse_sample_profile` classifies a definition's sample clause from `(values, token)` alone
(`sample_parser.py:155-186`) — it never sees the declared SQL type or the column name. So an epoch/
timestamp integer or a decimal sampled without a visible point is written as
`semantic_type=identifier` / `logical_representation=numeric_string` **at `parser:supported`** (the
operational tier — `field_policies.py:85-96`). Fix: before writing parser evidence, reconcile the
sample shape against `rec.declared_type` and the column name; on contradiction, **withhold** the
field (do not assert) and record a diagnostic. `_write_glossary_parser_evidence` widens to receive
`declared_type` + column (both already in scope at the call site, `ingest.py:708-744`).

### MF-2 — Pass B receives the complete FTR sidecar
`assemble_table_items`/`_descriptor` (`table_synth.py:25-63`) send each column as
`{column, type=unknown, business_definition?}` — and `business_definition` is sourced only from the
blank-column draft dict, so exactly the glossary columns that *have* a curated meaning arrive with
none. Fix: thread the `GlossaryRecord` sidecar (already in scope at `ingest.py:1424`) into
`assemble_table_items`; build each descriptor with `type=declared_type`, the sanitized glossary
definition, business term, term type, domain, process path, and the parser classification. Extend the
egress allowlist (`_COLUMN_PROFILE_KEYS`) + `_column_profile_ok` to admit the new keys within the
length bound.

### MF-3 — Abstention is a valid outcome, not a failure
`make_ref_accept` rejects any synthesis with no grain and no availability as `empty_synthesis` →
`None`, discarding even a valid `table_role`/`primary_entity` it returned; the table is then absent
from `syntheses` → counted unresolved → stage `failed`/`partial` (`table_synth.py:100-111`,
`ingest.py:147-166`). Fix: a parseable synthesis with no grain/as-of is a valid **abstention** —
retain and write its advisory role/entity fields, propose zero grain/availability facts, mark the
table resolved-but-abstained, and report `succeeded` with an explicit abstention count (not
`failed`).

### MF-4 — Bound LLM execution time
The source advisory lock is held across enrichment; a hung provider call would hold it indefinitely
and could fail the whole catalog ingest. `messages.create` passes no `timeout`; retries are already
bounded (2, no backoff) but there is no wall-clock ceiling. Fix: add `ClaudeConfig.timeout`
(env `FEATUREGEN_LLM_TIMEOUT`, default 60s) passed to `messages.create(timeout=…)`; add a
stage-level deadline (injectable monotonic clock) that stops issuing new calls past the budget and
marks the stage `partial`/`timed_out` — while catalog ingestion still commits (enrichment failure is
already isolated from fact assertion).

### MF-5 — Truthful upload result
`IngestResult` reports `asserted`/`changed_objects`/`quarantined` only, conflating "127 nodes stored"
with "126 columns" and asserting nothing about edges, join candidates, or Pass B. Fix: add additive
count fields (`objects_stored` = tables+columns, `tables`, `columns`, `containment_edges`,
`facts_asserted`, `join_candidates`, `passb_proposed`, `passb_abstained`), compute them on the success
path, and surface them in the upload-result callout. Fields are additive with `=0` defaults so the
five existing positional constructor sites and the FastAPI serialization stay intact.

### MF-6 — Protect the dedicated-source limitation
`source` is a free-text form field, unvalidated against the target's kind (`uploads.py:112-128`); an
FTR upload can be pointed at an existing schema-less technical source, where it half-lands behind the
column-level cross-schema fence with an opaque message. Fix: for an FTR/glossary upload, detect that
the target source already exists as a schema-less technical source and return a `held` result with an
actionable message ("this FTR upload requires a new or existing FTR-only source; it cannot enrich a
schema-less technical source"); a new or already-FTR source proceeds.

### MF-7 — Definition truncation preserves meaning
Every cleaned definition is cut to the first 200 chars (`enrich.py:212-219`, `table_synth.py:33-36`),
because the egress guard caps every value at 200. Sanitized business definitions are exactly the
payload we *want* the model to see, and all real ones exceed 200. Fix: raise the bound for the
sanitized-definition field specifically to a larger-but-bounded cap (`_MAX_DEFINITION_LEN`, 600),
make the egress guard key-aware (definition ≤600, everything else ≤200), and truncate on a
word/sentence boundary rather than mid-token.

### MF-8 — Evidence-based batch ceilings *(architect review, 2026-07-18)*
`_DEFAULT_MAX_ITEMS = {concept:40, definition:12, domain:20, table_synth:8}` (`enrich_config.py:17`)
are throughput limits with **no accuracy evidence**. The only quality gate
(`tests/eval/test_enrich_batch_quality.py`) drives a scripted `FakeLLM` that returns the expected
concept for every column — it validates the harness, not Anthropic; it measures no cross-column
contamination, compares no batch sizes, has no definition/domain/Pass B gold set, and its gold set
(37 cols) is smaller than the 40-col ceiling. Fix, two parts:
- **8a (ship now):** lower defaults to `{concept:20, definition:8, domain:8, table_synth:4}` —
  conservative isolation boundaries until measured. Ceilings stay ceilings; the token budget remains
  the second boundary; env overrides unchanged.
- **8b (build the evidence):** a key-gated real-provider batch-size sweep harness — concept
  {5,10,20,40}, definition {4,8,12}, domain {4,8,20}, Pass B {1,2,4,8} — measuring accuracy,
  abstention, missing/duplicate refs, cross-item contamination, latency, and cost against single-item
  baselines, so a future promotion to a larger ceiling is earned. Not a CI gate; run manually with a
  key against a throwaway DB.

## Testing strategy

- **Static compatibility test** over every `_SCHEMAS` entry: `provider_incompatibilities(
  project_for_anthropic(schema)) == []`, and projection preserves structure (properties/required/enum
  members).
- **Wire-shape test**: capture the exact `output_config` `ClaudeLLM` sends (mock client) and assert no
  `maxLength`/nullable-enum survives.
- **Local-enforcement test**: a response violating a *stripped* constraint (too-long string, off-enum
  value) is still rejected by `reg.validate` (canonical), proving the projection didn't weaken
  enforcement.
- **Safe-400 test**: a mocked `APIStatusError(400)` with a schema-keyword body → diagnostic records
  status + keyword, no content.
- **Live canary** (skipped without `ANTHROPIC_API_KEY`): concept batch + domain batch + Pass B summary
  + Pass B synthesis against the real API — assert no 400 and schema conformance.
- **PG-backed acceptance test** on a committed **synthetic** FTR fixture that mirrors the real file's
  structure (17-col FTR headers, 126 col-terms + 1 table-term, canonical sample clauses, varied
  declared types incl. timestamp/double/varchar, a regulatory term type): 127 records accepted; 1
  table + 126 columns; 0 quarantine on a fresh source; declared types preserved; no contradictory
  parser evidence; sanitized defs contain no sample values; Pass A receives sanitized defs + declared
  types; Pass B receives the complete FTR metadata; abstention doesn't fail the stage; re-upload is
  deterministic; run/stage/object counts agree. Hermetic (FakeLLM) — the **real file** stays a manual
  read-only final proof the user runs, per the standing security rule.

## Global constraints

- Base the Phase-1 branch off `325fd84` (= origin/main `9852b1c` + the maxItems fix, folded in).
- **All subagent dispatches on Opus 4.8** (Fable credits exhausted).
- `anthropic` declared version-bounded in `pyproject.toml`; never imported at module scope.
- Canonical strict schema for local validation/persistence; projected schema on the wire only;
  response validation stays against the canonical schema.
- Metadata-only egress; sanitize before transmission; per-value egress ≤200, sanitized
  business_definition ≤600.
- Batch ceilings = `{concept:20, definition:8, domain:8, table_synth:4}` (ceilings; token budget
  second).
- The real `FTR_Column_Mapping*.csv` is **read-only, never committed/copied**; CI acceptance uses a
  synthetic fixture. The exposed Anthropic API key **must be rotated** by the user; it lives only in
  the Kind Secret `featuregen-llm`, never in git.
