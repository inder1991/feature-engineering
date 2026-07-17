# FTR Glossary Adapter — Delivery A1 (adapter core) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Scope split (decided with the user):** Delivery A is split. **A1 (this plan)** = the adapter core that makes the FTR file ingest correctly, enriched, searchable, and sample-safe. **A2 (separate spec, to be brainstormed)** = governed provenance/storage: a *real* restricted raw-upload store (classification / KMS-at-rest / retention / erasure), the complete distinct-count model, durable accepted-row + table-record `source_row` provenance, and LLM-call→ingestion-run attribution. A1 must not pretend A2's infrastructure exists.

**Goal:** The exact FTR compliance-glossary export ingests fully — 126 column terms + 1 table term, enriched (when the provider/flags are on), searchable — with **no sample value ever persisted or egressed**, and with all near-FTR / malformed cases quarantined honestly. No re-keying of existing operational graph state.

**Architecture:** `read_ftr_glossary(text, source)` runs at parse time and returns a typed `PreparedFtrUpload`. It recognizes the exact FTR header multiset, maps headers with whitelisted transforms, and — critically — **parses each definition's sample profile into safe derived facets (`logical_representation`, `semantic_type`), then strips + PII-redacts every uploader free-text field, discarding the raw sample values entirely**. So everything downstream (`content_hash`, `validate_rows`, enrichment, evidence, `build_graph`, `search_doc`) only ever sees sanitized, hash-stable data, and there is no raw sample value anywhere to leak — hence no restricted store is needed in A1. The typed envelope is threaded explicitly through `_read_rows` → route → `ingest_upload`. Schema is preserved additively (`graph_node.schema_name`); the operational key stays `public.table.column`.

**Tech Stack:** Python 3 (FastAPI, psycopg3, PostgreSQL 15+), pytest + pytest-postgresql, React + TypeScript + Vitest.

## Global Constraints

- **No identity inference.** Code-reviewed representation mapping only. (Spec §2.)
- **Sanitize at parse time, discard raw sample values.** `content_hash` (`enrich.py:59`) includes `definition`; enrichment runs (`ingest.py:1050`) before `build_graph` (`ingest.py:1084`) and `ValidationResult` is frozen (`canonical.py:82`) — so sanitizing late crashes and orphans concepts. The adapter emits sanitized rows/records; **raw sample values are discarded, not stored** (their retention-as-audit is A2). (Findings #1, #4.)
- **Preserve parser evidence.** `_write_glossary_parser_evidence` (`ingest.py:613`) derives `logical_representation`/`semantic_type` by re-parsing `rec.definition`. Since A1 strips the sample clause first, the adapter parses the profile BEFORE stripping and carries the two SAFE facets on the record; the evidence writer consumes those instead of re-parsing. (Finding #4.)
- **Cross-upload schema fence holds the WHOLE upload before any side effect.** `build_graph` does `DELETE ... WHERE catalog_source` then rebuilds (`graph.py:163`), so row-level quarantine would delete the prior node. A pre-flight check (before `UploadCatalog`, `ingest.py:881`) that finds an incoming `(table,column)` already present under a *different* `schema_name` for the SAME `catalog_source` returns `held`/`rejected` for the whole upload. (Findings #2 framing, #3.)
- **`term_type` is a versioned CLOSED vocabulary.** Unknown values quarantine the row (raw value retained in the quarantine reason), so a typo (`Mesure`) is caught, not silently treated as non-measure. Seed the vocabulary from the real file's distinct values (enumerated read-only at implementation) + the known FTR set. (Finding #7; reverses rev-2's open-vocab call, per spec §5.)
- **Sanitize ALL uploader free-text before persistence** — `term_name`, `synonyms`, `domain`, `bian_path`, `fibo_path`, `process_path`, `related_terms` get PII-redaction; `definition` gets full sample-strip + redaction. Nothing uploader-authored reaches source evidence / `search_doc` raw. Fail-closed. (Finding #10.)
- **Operational identity is lowercased** (`_norm`, `canonical.py:205`) — all ref assertions use lowercase (`public.comp_fin_tran.cust_name`) and are scoped by `catalog_source` (PK is `(catalog_source, object_ref)`). (Findings #9, #11.)
- **`source` is `Form(...)`** (`uploads.py:92`) — tests post `data={"source": ...}` via `_helpers.upload_csv`, never `params=`. `search()` requires keyword `now` (`search.py:133`). The `ingestion_run` PK is `id`, not `ingestion_run_id`. (Findings #11.)
- **`IngestResult.asserted` counts grain/as-of FACTS** (`ingest.py:126`); FTR declares none → `asserted == 0`. (Findings #4, #8.)
- **Enrichment is not on by default:** batch mode defaults to `single` (`enrich_config.py`), Pass C is off, and no provider means `client is None`. A1 acceptance tests prove the WIRING with a `FakeLLM` and explicit flags; production enrichment requires `OVERLAY_PASS_C=1` + a provider + batch mode. (Finding #9; full config-snapshot + LLM-run linkage is A2.)
- **Migrations:** new files only, auto-discovered, `NNNN_snake.sql`. **Recompute the next free number immediately before implementation** (a concurrent 3B.4 branch also allocates; max here is `0998`). (Finding #13.)
- **Transforms whitelist:** `copy`, `split_fqn`, `join_path`, `split_list([";","|"])`, `normalize_enum`. No LLM-generated code/SQL/regex.
- **Never read `~/Downloads` or commit the real CSV.** Fixtures inline, sanitized, exact FTR header names, sample values scrubbed, **CSV cells with commas quoted**.

---

## Deferred to A2 (NOT in this plan — a separate spec)

Restricted raw-upload store (classification/KMS/retention/erasure); durable accepted-row + table-record `source_row` provenance (`ingestion_run_row`); the complete distinct-count model (`input_row_count` / `column_record_count` / `table_record_count` / `accepted_count` / `asserted_fact_count`); LLM-call→ingestion-run attribution + enrichment modes/budgets in the effective-config snapshot; Pass B wide-table two-phase chunking; **run-correlation completeness — `source_fingerprint` (gn-v1, `ingestion_run.py:84`) EXCLUDES `schema_name`, so a pure schema-population/change is invisible to pre/post fingerprints; A2 bumps to `gn-v2` including schema identity (Finding #13, documented gap, not fixed in A1).** A1 must not build half-versions of these. Where A1 needs an honest count today, it uses only what already exists and does not invent a false one.

---

## Round-4 review resolutions (authoritative — supersede the task text where they conflict)

1. **`data_type` honesty (Finding #5).** There is no type evidence/resolver; `r.type` goes straight to `graph_node.data_type` and is read operationally (Pass C `is_id_like`, drift fingerprint). A "PROPOSED" label is fiction. **Decision:** the FTR adapter emits **`UNKNOWN_TYPE` operationally** (respecting `FTR_GLOSSARY_PROFILE`, which attests no structural fields — a business glossary is not the physical-type authority) and retains the FTR-declared type as a **non-operational additive `graph_node.declared_type` column** (added in the Task-5 migration). This diverges deliberately from the generic glossary reader (which uses declared type operationally — a pre-existing inconsistency A1 does not inherit). *Reversible: if you'd rather trust FTR's declared type operationally, flip to "attested" and drop `declared_type` — flag on review.*
2. **Sample marker precision (Finding #11).** The `suspected_unhandled` detector must key on clause-INTRODUCER phrases that precede a list — `\be\.g\.`, `\bsuch as\b`, `\bfor example\b`, `\bexamples? include\b`, `\brepresentative values?\b`, `\bsample (?:values?|profile)\b` — NOT bare words. "sample population size" and "representative office" must stay `state=none`. Task 2's corpus MUST include those as negative controls, plus punctuation/case variants, multiple clauses, quoted text, time/code/decimal samples, and sentence-boundary cases.
3. **`data_type` is validated + bounded (Finding #11 tail).** Even as declared metadata + classifier input, `data_type` bypasses the free-text control. Validate it against a bounded SQL-type token (length ≤ 64, `^[a-z0-9 _()]+$` after lowercasing) in the adapter; reject/`UNKNOWN_TYPE` otherwise.
4. **Cross-upload fence = `held`, not `rejected`, with honest counts (Finding #4).** On a schema conflict: create real `RowError`s for the conflicting rows, `persist_quarantine`, `record_stage(..., "brake"/"deferred", ...)` and `record_skipped_downstream`, and return `IngestResult("held", <file-level reason>, 0, 0, <real quarantine count>)` — never `len(rows)` as a phantom count. **Legacy-NULL policy:** an existing `graph_node` with `schema_name IS NULL` matching an incoming `(table,column)` is *unverifiable* — **hold** unless an explicit backfill/attestation permits adoption (do not let a new schema silently claim a public-flattened identity).
5. **Dedicated schema helper (Finding #2/#3).** Do NOT use `_schema_preserving_ref_map` (it maps public-ref→schema-preserving-ref). Add `schema_by_ref(glossary) -> dict[str,str]` returning graph refs → real schema: `{_column_ref(t,c): SCHEMA, _table_ref(t): SCHEMA}`. `build_graph` takes **both** column and table maps and writes `schema_name` in the table AND column INSERTs (Finding #3 — `schema_name` is a graph column, NOT projectable by `resolve_and_project`). Validate the table record's schema agrees with its columns before attaching table evidence (Finding #8).
6. **Envelope provenance via the PARSE stage, not a second ingest attempt (Finding #6).** `ingest_upload` (`ingest.py:805`) takes no prepared metadata; do NOT append a second `glossary_evidence` write from the route. Record `sanitized_count` + `sanitizer_version` + `redaction_version` in the route's existing PARSE stage `detail`. Task 10 asserts these fields.
7. **Term_type vocabulary is EXPLICIT and versioned (Finding #7).** Do not "seed from the file" (that contradicts never-read-Downloads). The closed set `TERM_TYPE_VOCAB_V1` lives in the code + spec + tests: `{"measure","dimension","code_value","reference_data","business_term"}` (normalized: lowercase, spaces→`_`). Unknown → quarantine; the raw value in the reason is length-bounded (≤ 32 chars) and PII-redacted before persistence. The fixture uses only in-vocab values (`Reference Data`, not `Reference`).
8. **Table taxonomy reaches search (Finding #8).** Task 8 also populates the TABLE node's `semantic_terms` (business term + BIAN + FIBO from the table sidecar) and rebuilds the table's `search_doc`.
9. **Inline quarantine repair is disabled for FTR rows (Finding #9).** `_row_from_raw`/`resolve_quarantine_row` cannot reconstruct a `GlossaryRecord` (schema/term_type/taxonomy/facets/evidence). The adapter stamps `raw["_adapter"]="ftr"`; `resolve_quarantine_row` refuses inline resolution for those rows and returns "re-upload the corrected FTR file." Adapter-aware repair is future work.
10. **Near-FTR = REJECTED with a specific diagnostic, not quarantined (Finding #10).** Deterministic rule: if the FTR-distinctive header `schema.table.column` (normalized) is present but the header multiset is not the exact FTR set, `_read_rows` raises `HTTPException(400, <missing/extra/duplicate headers>)`. Otherwise fall through. (No "≥12/17".) The goal wording is "rejected with a fingerprint diagnostic."
11. **Acceptance fixture is a real 126-column + 1-table body (Finding #12).** Synthesize it programmatically in the test (a helper emitting 126 column rows, ≥1 Measure, ≥1 recognized sample clause) — NOT by reading Downloads. Assert: concept stage `expected==126 and resolved==126`; ≥2 audited batch calls occurred (proves chunking past the 40-item batch limit); ≥1 expected LLM call exists (no vacuous pass); sample tokens absent from `graph_node`, `field_evidence`, `semantic_terms`, `search_doc::text`, `quarantine_row.raw`, AND `llm_call.redacted_input`; both table and column `schema_name` populated.

---

## Field Disposition Matrix (A1)

| FTR field | Canonical/sidecar | A1 durable home |
|---|---|---|
| `schema.table.column` | `(schema,table,column)` | operational id `public.t.c` (lowercased) + `graph_node.schema_name` (additive) |
| `term_name` | `GlossaryRecord.term_name` (redacted) | source evidence `business_term` (ATTESTED) + `semantic_terms` (search) |
| `description_business_definition` | `CanonicalRow.definition` (sample-stripped + redacted) | `graph_node.definition` + source evidence `definition` (ATTESTED) |
| sample profile (parsed, pre-strip) | `GlossaryRecord.logical_representation`, `.semantic_type` (SAFE facets) | parser evidence (SUPPORTED). **Raw values discarded.** |
| `data_domain` | `GlossaryRecord.domain` (redacted) | source evidence `domain` (PROPOSED) + `graph_node.domain` |
| `term_type` | `GlossaryRecord.term_type` (closed-vocab, normalized) | Pass C `ColMeta.term_type`; unknown → quarantine (raw value in reason) |
| `synonyms_aliases` | `GlossaryRecord.synonyms` (redacted) | `semantic_terms` (search) |
| `bian_level_1..4` | `GlossaryRecord.bian_path` (joined, redacted) | source evidence `bian_path` (ATTESTED) + `semantic_terms` |
| `fibo_level_1` | `GlossaryRecord.fibo_path` (redacted) | source evidence `fibo_path` (ATTESTED) + `semantic_terms` |
| `related_business_process_l1..3` | `GlossaryRecord.process_path` (joined, redacted) | `semantic_terms` (search) |
| `related_terms` | `GlossaryRecord.related_terms` (redacted) | `semantic_terms` (search) |
| `source_row` | `CanonicalRow.source_row` | quarantine `raw` (repair). **Accepted-row provenance is A2.** |
| `data_type` | `CanonicalRow.type` (lowercased) | `graph_node.data_type` — **DECLARED (PROPOSED strength), not attested** (`FTR_GLOSSARY_PROFILE` attests no structural fields, `source_profile.py:61`); a structural source reconciles on drift. (Finding #7b, documented.) |

---

## File Structure

**New:** `src/featuregen/overlay/upload/ftr_adapter.py`; `sanitize.py`; migration `NNNN_graph_node_schema_name.sql` (adds `schema_name`, `semantic_terms`); tests `test_ftr_adapter.py`, `test_sanitize.py`, `test_ftr_ingest_e2e.py`, `tests/featuregen/api/test_ftr_upload.py`; extend `frontend/src/screens/RunDetailPanel.test.tsx`.

**Modified:** `glossary_reader.py` (extend `GlossaryRecord`; `join_path`/`split_list`); `uploads.py` (`_read_rows` returns the envelope; dispatch); `ingest.py` (pre-flight fence; `term_type`→ColMeta; table-term evidence; parser-evidence from carried facets; `schema_name`/`semantic_terms` population); `graph.py` (`schema_name`/`semantic_terms` columns + `_SEARCH_DOC`/`_search_doc_params`/5 render sites); `canonical.py` (`CanonicalRow.source_row`); `review_queue.py` (carry `source_row`); `RunDetailPanel.tsx`.

---

## Task 1: Extend `GlossaryRecord` + shared transforms

**Files:** Modify `glossary_reader.py`; Test `test_glossary_reader.py`.

**Produces:** `GlossaryRecord` gains (all defaulted): `source_row: str=""`, `term_type: str=""`, `process_path: str=""`, `related_terms: tuple[str,...]=()`, `schema: str=""`, `physical_fqn: str=""`, `logical_representation: str=""`, `semantic_type: str=""`. `join_path`/`split_list` helpers; `_split_synonyms`→`split_list`.

- [ ] Step 1 — failing test: assert `join_path(["Party","","Customer"])=="Party / Customer"`, `split_list("A; B | C")==("A","B","C")`, and all eight new fields default empty.
- [ ] Step 2 — run, expect FAIL: `uv run pytest tests/featuregen/overlay/upload/test_glossary_reader.py -q -k "join_path or split_list or new_fields"`
- [ ] Step 3 — implement the fields + the two helpers (see rev-2 code; `join_path` drops blank parts, `split_list` splits on `[;|]`).
- [ ] Step 4 — run, expect PASS (existing glossary tests green): `uv run pytest tests/featuregen/overlay/upload/test_glossary_reader.py -q`
- [ ] Step 5 — commit: `feat(glossary): extend GlossaryRecord (+safe parser facets) + join_path/split_list`

---

## Task 2: Fail-closed free-text sanitizer (definition + all uploader text; preserve safe facets)

**Files:** Create `sanitize.py`, `test_sanitize.py`.

**Consumes:** `parse_sample_profile` (returns `ParsedProfile` with `.logical_representation`, `.semantic_type` — SAFE facets — plus the raw clause), `strip_sample_values` (returns text unchanged when NO clause matches — can't distinguish clean from unrecognized), `redact_free_text` (`.text is None` ⟹ fail closed). **Produces:**
```python
@dataclass(frozen=True)
class DefinitionSanitize:
    clean: str; state: str  # "none"|"stripped"|"suspected_unhandled"
    logical_representation: str; semantic_type: str   # SAFE derived facets (never raw values)
    removed: int; sanitizer_version: str; redaction_version: str | None
def sanitize_definition(text: str) -> DefinitionSanitize
def redact_text(text: str) -> tuple[str, str | None]   # PII-redact a non-definition free-text field; ("", ver) if fail-closed
```
A sample MARKER regex (`e.g.`, `such as`, `for example`, `examples include`, `representative`, `sample`) drives `state`: marker present AND `strip_sample_values` changed nothing ⟹ `suspected_unhandled` ⟹ `clean=""`. `logical_representation`/`semantic_type` come from `parse_sample_profile` BEFORE stripping (so they survive). The raw clause is never returned.

- [ ] Step 1 — failing tests + labeled corpus: recognized clause → `state=="stripped"`, facets populated, no raw value in `clean`; no clause → `state=="none"`; `examples include Acme and Beta` → `state=="suspected_unhandled"`, `clean==""`; PII-only → redactor blanks.
- [ ] Step 2 — run, expect FAIL: `uv run pytest tests/featuregen/overlay/upload/test_sanitize.py -q`
- [ ] Step 3 — implement per interface (`SANITIZER_VERSION` constant; marker scan; parse-then-strip-then-redact; `redact_text` for non-definition fields).
- [ ] Step 4 — run, expect PASS.
- [ ] Step 5 — commit: `feat(sanitize): fail-closed free-text sanitizer preserving safe parser facets`

---

## Task 3: FTR adapter — `read_ftr_glossary` + typed envelope + dispatch

**Files:** Create `ftr_adapter.py`; Modify `uploads.py` (`_read_rows` signature + dispatch); `canonical.py` (`CanonicalRow.source_row: str=""`); `review_queue.py` (carry `source_row`); Test `test_ftr_adapter.py`.

**Produces:**
```python
@dataclass(frozen=True)
class PreparedFtrUpload:
    rows: list[CanonicalRow]        # sanitized definitions; source_row stamped
    records: list[GlossaryRecord]   # sanitized free-text; schema/physical_fqn/safe-facets set
    quarantined: list[RowError]     # bad/duplicate FQN, bad/duplicate source_row, unknown term_type, multi-schema
    sanitized_count: int; sanitizer_version: str; redaction_version: str | None
def is_ftr_glossary(headers: list[str]) -> bool                       # exact multiset, rejects dup headers
def ftr_fingerprint_error(headers: list[str]) -> str | None          # near-FTR diagnostic (missing/extra/dup header)
def read_ftr_glossary(text: str, *, source: str) -> PreparedFtrUpload
def to_glossary_upload(p: PreparedFtrUpload) -> GlossaryUpload
```
**`_read_rows` new signature** (Finding #2 — thread the envelope explicitly):
```python
def _read_rows(filename, data, source) -> tuple[list[CanonicalRow], SourceCapabilityProfile | None,
                                                GlossaryUpload | None, PreparedFtrUpload | None]
```
non-FTR paths return a 4th element `None`. The route unpacks the 4th and (in A1) uses `prepared.sanitized_count`/versions for the glossary stage `detail`.

**Rules:** fingerprint via `Counter(_norm(h) for h in headers)` == exact FTR multiset with no count>1 (Findings #8, #12); if NOT exact but ≥ (say) 12/17 FTR headers present, `ftr_fingerprint_error` returns a specific message and `_read_rows` raises `HTTPException(400, ...)` instead of falling through to the technical reader (Finding #12). Duplicate FQN on the **normalized** `(schema,table,column)` tuple → quarantine both (Findings #7-dedup, #12). `source_row` non-empty integer + unique **as parsed int** → else quarantine (Finding #12). `term_type` matched against the versioned closed vocab (case-normalized); unknown → quarantine with the raw value in the reason (Finding #7). Every definition via `sanitize_definition`; every other free-text field via `redact_text`. Reuse `read_glossary`'s within-upload multi-schema fold-collision quarantine. Unresolved FQN → identity-less `CanonicalRow` (validate quarantines) + no record.

**Corrected fixture** (2 columns + 1 table ⇒ `rows==2, records==3`; `related_terms` is the column between l1 and l2; **definition commas quoted**):
```python
_HDR = ("source_row,schema.table.column,term_name,description_business_definition,data_domain,"
        "term_type,related_business_process_l1,related_terms,related_business_process_l2,"
        "related_business_process_l3,synonyms_aliases,bian_level_1,bian_level_2,bian_level_3,"
        "bian_level_4,fibo_level_1,data_type\n")
_FTR_CSV = _HDR + (
    '18,DPL_EIB_COMPLIANCE.COMP_FIN_TRAN.CUST_NAME,Customer Name,'
    '"Registered legal name of the counterparty.",Party,Dimension,Onboarding,KYC Alias;Screening Alias,'
    'KYC,Screening,Client Name|Account Holder,Party,Customer,Identification,Legal,'
    'fibo-be-le-lp:LegalPerson,VARCHAR\n'
    '19,DPL_EIB_COMPLIANCE.COMP_FIN_TRAN.TXN_AMT,Transaction Amount,'
    '"The monetary amount of the transaction.",Payments,Measure,Settlement,Amount Alias,Clearing,,Amt,'
    'Payment,Transaction,Amount,,fibo-fbc:MonetaryAmount,DECIMAL\n'
    '20,DPL_EIB_COMPLIANCE.COMP_FIN_TRAN,Financial Transaction Repository,'
    '"Daily compliance transaction repository.",Compliance,Reference,,,,,,Reference,Table,,,,\n')
```

- [ ] Step 1 — failing tests (corrected contracts): fingerprint exact + rejects a duplicated header; `len(rows)==2 and len(records)==3`; mapping (`schema=="DPL_EIB_COMPLIANCE"`, `term_type=="dimension"`, `bian_path=="Party / Customer / Identification / Legal"`, `process_path=="Onboarding / KYC / Screening"`, `synonyms==("Client Name","Account Holder")`, `related_terms==("KYC Alias","Screening Alias")`, `source_row=="18"`); duplicate normalized FQN → both quarantined; unknown `term_type` "Mesure" → quarantined with "Mesure" in reason; non-integer/duplicate `source_row` → quarantined; a properly-quoted `"...examples include Acme and Beta."` definition → row survives with `definition==""`; a near-FTR header set (one header renamed) → `ftr_fingerprint_error` non-None.
- [ ] Step 2 — run, expect FAIL: `uv run pytest tests/featuregen/overlay/upload/test_ftr_adapter.py -q`
- [ ] Step 3 — implement `ftr_adapter.py` (mirror `read_glossary` Pass-1/Pass-2 at `glossary_reader.py:158-208`; add fingerprint/diagnostic, dup-FQN, source_row, term_type vocab, sanitize); add `CanonicalRow.source_row`; carry it through `persist_quarantine`'s `asdict`.
- [ ] Step 4 — dispatch: `_read_rows` new signature; in `uploads.py:80` before `is_glossary_csv`, if `is_ftr_glossary(headers)` use the adapter; elif `ftr_fingerprint_error(headers)` raise 400; else existing paths. Route unpacks the 4th element.
- [ ] Step 5 — run, expect PASS: `uv run pytest tests/featuregen/overlay/upload/test_ftr_adapter.py tests/featuregen/api/test_uploads.py -q`
- [ ] Step 6 — commit: `feat(ftr): read_ftr_glossary — exact fingerprint, closed term_type vocab, parse-time sanitize, typed envelope`

---

## Task 4: Thread `term_type` into Pass C

**Files:** Modify `ingest.py:355`; Test `test_identifiers.py`, `test_passc_ingest.py`.

- [ ] Step 1 — pinning test: `is_join_key_eligible(ColMeta(..., term_type="measure"))` is False; `"dimension"` is True (see rev-2 `_col` helper).
- [ ] Step 2 — run: `uv run pytest tests/featuregen/overlay/upload/passc/test_identifiers.py -q -k measure`
- [ ] Step 3 — implement: `ingest.py:355` `term_type="",` → `term_type=rec.term_type if rec else "",`.
- [ ] Step 4 — ingest-level test with `monkeypatch.setenv("OVERLAY_PASS_C","1")` (Finding #11 — flag must be on), a glossary with a `measure` id-like column, asserting NO strong candidate for it and that a candidate pair otherwise exists: `uv run pytest tests/featuregen/overlay/upload/passc/ -q`
- [ ] Step 5 — commit: `fix(passc): thread glossary term_type so Measures are excluded from join keys`

---

## Task 5: Cross-upload schema fence (whole-upload hold) + `schema_name`/`semantic_terms` migration

**Files:** Migration `NNNN_graph_node_schema_name.sql`; Modify `ingest.py` (pre-flight fence before `UploadCatalog`), `graph.py` (persist `schema_name`); Test `test_ftr_ingest_e2e.py`.

**Produces:** `graph_node.schema_name text NULL`, `graph_node.semantic_terms text NULL`. A pre-flight fence in `ingest_upload` (before `UploadCatalog`, `ingest.py:881`): build the incoming `(table,column)→schema` map from `glossary.records`; `SELECT object_ref, schema_name FROM graph_node WHERE catalog_source=%s AND schema_name IS NOT NULL`; if any incoming row's lowercased `(table,column)` matches an existing node whose `schema_name` differs → **return `IngestResult("rejected", reason, 0, 0, len(rows))` for the whole upload**, before any side effect (Finding #3).

- [ ] Step 1 — migration (recompute NNNN): `ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS schema_name text NULL;` + `semantic_terms text NULL;`
- [ ] Step 2 — failing tests (lowercase refs, `catalog_source`-scoped): (a) after ingest, `schema_name=='DPL_EIB_COMPLIANCE'` on `public.comp_fin_tran.cust_name`; (b) a second upload to the SAME source declaring `OTHER_SCHEMA.COMP_FIN_TRAN.CUST_NAME` returns `status=='rejected'` AND the original node's `schema_name` is unchanged.
- [ ] Step 3 — run, expect FAIL.
- [ ] Step 4 — implement: add `schema_name` to the column-node INSERTs (`graph.py:182-191`, `242-250`) via a `schemas: dict[str,str] | None = None` param on `build_graph` (default None → technical uploads NULL), built by the caller from `_schema_preserving_ref_map(glossary)`; add the pre-flight fence in `ingest_upload`.
- [ ] Step 5 — run, expect PASS.
- [ ] Step 6 — commit: `feat(graph): additive schema_name + whole-upload cross-schema fence (fail-closed)`

---

## Task 6: Table-level term via source evidence + binding

**Files:** Modify `ingest.py` (`_ingest_glossary_evidence` ~700-770; the `is_table` skips at 333/489/706); Test `test_ftr_ingest_e2e.py`.

**Design (Finding #5):** the table term has no `CanonicalRow` and no `classify_upload` binding, so it needs a dedicated path: for each `is_table` record, write SOURCE evidence at the schema-preserving TABLE `logical_ref` (`business_term`/`definition`/`domain`/`bian_path`/`fibo_path`, via the same `_write_producer_field` used for columns), include the table ref in the `resolve_and_project` set (`ingest.py:770`), and set the table node's `schema_name`. The table graph node already exists (written by `build_graph`); do NOT direct-UPDATE it — let `resolve_and_project` materialize the resolved definition/domain from the evidence, exactly like columns.

- [ ] Step 1 — failing test (lowercase, scoped): after ingest, `public.comp_fin_tran` (kind=table) has resolved `definition` containing "compliance transaction repository" and `domain=='compliance'` and `schema_name=='DPL_EIB_COMPLIANCE'`.
- [ ] Step 2 — run, expect FAIL.
- [ ] Step 3 — implement the dedicated `is_table` evidence path + include table refs in `resolve_and_project`.
- [ ] Step 4 — run, expect PASS.
- [ ] Step 5 — commit: `feat(glossary): table terms via source evidence + resolve_and_project (no direct UPDATE)`

---

## Task 7: Preserve parser evidence from carried facets

**Files:** Modify `ingest.py` (`_write_glossary_parser_evidence` ~613 + its caller); Test `test_ftr_ingest_e2e.py`.

**Design (Finding #4):** `_write_glossary_parser_evidence` currently re-parses `rec.definition` — which A1 has stripped. Refactor it to accept the SAFE facets carried on the record (`rec.logical_representation`, `rec.semantic_type`) and write them at `parser:SUPPORTED` (unchanged strength), with the same present/absent reconciliation. No re-parse of the sanitized definition.

- [ ] Step 1 — failing test: ingest a glossary whose definition carried a (now-stripped) sample profile; assert `field_evidence` for that column has a `logical_representation`/`semantic_type` row at `parser`/`supported` strength (query `field_evidence` scoped by `logical_ref`).
- [ ] Step 2 — run, expect FAIL (facets gone because definition was stripped).
- [ ] Step 3 — implement: change `_write_glossary_parser_evidence` signature to take `logical_representation`/`semantic_type` from the record; update the caller.
- [ ] Step 4 — run, expect PASS.
- [ ] Step 5 — commit: `fix(glossary): parser evidence from carried safe facets (survives sample-stripping)`

---

## Task 8: Index glossary semantics in search

**Files:** Modify `graph.py` (`_SEARCH_DOC` + `_search_doc_params` + all 5 render sites + `rebuild_search_doc`), `ingest.py` (populate `semantic_terms`); Test `test_ftr_ingest_e2e.py`.

**Produces:** a 6th weight-`C` `semantic_terms` slot in `_SEARCH_DOC` fed with `term_name + synonyms + bian_path + fibo_path + process_path + related_terms` (Finding #12 — process_path included), populated per column during `_ingest_glossary_evidence` then `rebuild_search_doc`. The text is already redacted (Task 3), so no re-redaction needed.

- [ ] Step 1 — failing test (lowercase, scoped, **`now=NOW`** — Finding #11): `search(db, "Account Holder", now=NOW, roles=["catalog_viewer"])` hits `public.comp_fin_tran.cust_name`.
- [ ] Step 2 — run, expect FAIL.
- [ ] Step 3 — implement: extend `_SEARCH_DOC` (graph.py:93-99), `_search_doc_params` (signature+return), and ALL 5 render sites (131, 174, 190-191, 239, 250); populate `semantic_terms` + `rebuild_search_doc`.
- [ ] Step 4 — run, expect PASS. Also `uv run pytest tests/featuregen/overlay/upload -q -k "search or graph"`.
- [ ] Step 5 — commit: `feat(search): index term/synonyms/BIAN/FIBO/process via semantic_terms (all 5 render sites)`

---

## Task 9: Honest run label in `RunDetailPanel` (#15, A1-minimal)

**Files:** Modify `RunDetailPanel.tsx`; Test `RunDetailPanel.test.tsx`.

**Design (Finding #8, A1 scope):** the full distinct-count model is A2. A1 only removes the FALSE "asserted" label — `row_count` is a parsed-row count, not an asserted-fact count. Show `row_count` as "rows" and `quarantined_count` as "quarantined"; do NOT compute a fake asserted figure.

- [ ] Step 1 — failing test: a `rejected` run with `row_count:126, quarantined_count:126` renders `126 rows · 126 quarantined` and NOT the substring `asserted`.
- [ ] Step 2 — run, expect FAIL: `cd frontend && npx vitest run src/screens/RunDetailPanel.test.tsx`
- [ ] Step 3 — implement: Rows `<dd>` → `{run.row_count ?? '—'} rows · {run.quarantined_count ?? '—'} quarantined`.
- [ ] Step 4 — run, expect PASS.
- [ ] Step 5 — commit: `fix(frontend): drop the false "asserted" run label (#15); full count model deferred to A2`

---

## Task 10: Full API→PG→search acceptance test

**Files:** `tests/featuregen/api/test_ftr_upload.py`.

**Design (Finding #11 — all prior test bugs fixed):** use the `make_client` fixture with a scripted `FakeLLM`; post via `_helpers.upload_csv` (form data); set `OVERLAY_PASS_C=1` + `OVERLAY_ENRICH_CONCEPT_MODE=batch` via `monkeypatch`; define `_FTR_CSV_FULL` explicitly (a realistic multi-row body, all commas quoted, ≥1 Measure, ≥1 sample-bearing definition); assert `status=="ingested"`, `asserted==0`, `quarantined==0`, the expected column count (scoped by `catalog_source`), the table node resolved, **no sample token in any `definition` or in `search_doc`** (`to_tsquery`), and **the immutable `llm_call` audit payload carries no sample value** (query the audit records for the enrichment run bucket and assert the scrubbed token is absent).

- [ ] Step 1 — write the acceptance test with the fixtures/flags above.
- [ ] Step 2 — run (FAIL until Tasks 1–8 land, then PASS): `uv run pytest tests/featuregen/api/test_ftr_upload.py -q`
- [ ] Step 3 — full affected suites: `uv run pytest tests/featuregen/overlay/upload tests/featuregen/api -q`
- [ ] Step 4 — commit: `test(ftr): full API→PG→search acceptance (asserted=0, sample-safe, flags-on)`

---

## Self-Review

- **A1 closes:** #2 typed envelope through `_read_rows`/route; #3 whole-upload pre-flight fence; #4 parse-time sanitize + parser-evidence from carried safe facets (Tasks 2/3/7); #5 table term via dedicated evidence path (Task 6) — its accepted-row *provenance* is A2; #7 closed `term_type` vocab + documented `data_type` authority; #10 sanitize ALL uploader free-text; #11 every test bug (now=, run key, quoted commas, Pass C flag, make_client/`_FTR_CSV_FULL`, audit check); #12 exact-multiset fingerprint + near-FTR diagnostic + normalized dup-FQN + int source_row; #8 honest label (no fake asserted). #13 migration recompute (constraints).
- **Deferred to A2 (explicitly):** #1 restricted store (A1 discards raw, needs none); #6 `ingestion_run_row` schema; #8 full count model; #9 LLM-run linkage + config snapshot; Pass B chunking.
- **Type flow:** `GlossaryRecord` facets (T1) → sanitizer (T2) → adapter (T3) → Pass C (T4), parser evidence (T7), search (T8); `PreparedFtrUpload` (T3) threaded via `_read_rows`; `graph_node.schema_name/semantic_terms` (T5) → T5/T6/T8; `CanonicalRow.source_row` (T3) defaulted → readers unaffected.
- **Ordering:** T1→T2→T3; T5 migration before T8; T6/T7 after T3 (need carried facets/records); T9 independent; T10 last.
