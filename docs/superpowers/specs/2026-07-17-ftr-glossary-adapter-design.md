# FTR Glossary Adapter (Delivery A) — Design Spec

**Status:** design; Delivery A split into A1 (adapter core) + A2 (provenance/storage) after plan review.
**Scope:** A registered, code-reviewed adapter for the exact FTR glossary export format, split into two sub-deliveries:
- **A1 (adapter core)** — parse-time sanitize (raw sample values discarded), exact fingerprint + closed `term_type` vocab, `term_type`→Pass C, table term via source evidence, additive `schema_name` + cross-schema fence, search indexing, honest run label. This is the plan `docs/superpowers/plans/2026-07-17-ftr-glossary-adapter-plan.md`.
- **A2 (provenance/governed storage, separate spec — to be brainstormed)** — a *real* restricted raw-upload store (classification / KMS-at-rest / retention / erasure), the complete distinct-count model, durable accepted-row + table-record `source_row` provenance, LLM-call→ingestion-run attribution + enrichment modes in the effective-config snapshot. A1 discards raw sample values, so it needs none of this to be sample-safe; A2 adds governed retention-as-audit if the bank requires it.

Delivery B (general mapping engine) and Delivery C (graph re-identity) remain out of scope, referenced only where A must not foreclose them.
**Date:** 2026-07-17 (rev 3)

---

## 1. Problem

The real FTR glossary export (`FTR_Column_Mapping_final.csv`, 127 data rows) is **rejected in full** today: all 127 rows quarantine with "missing required field(s): table, column". Root cause, verified end-to-end against the code:

- Glossary detection recognizes only three literal header names (`business_term`, `bian_path`, `fibo_path`); the FTR file uses `term_name`, `bian_level_1..4`, `fibo_level_1`, so it misroutes to the **technical** reader, which needs `table`/`column`/`type` and finds none (`source_profile.py:92`, `uploads.py:76`).
- Even forced onto the glossary reader, the FQN column is literally named `schema.table.column`, which is not an FQN alias (`glossary_reader.py:44`) → no physical identity → still all-quarantined.
- `GlossaryRecord` models ~4 of the 17 columns; `term_type`, taxonomy levels, related processes/terms, `source_row`, and `synonyms_aliases` have no destination.
- The failure returns before enrichment, so nothing is classified.

The file's **data** is valid: 126 well-formed 3-part column FQNs, one legitimate 2-part table term, clean UTF-8, no duplicate FQNs. The defect is entirely in our adapter.

The permanent program is three independent deliveries: **A — ship FTR correctly** (this spec); **B — generalize mapping** (registered-adapter → confirmed-template → analyze/confirm → reusable template); **C — graph re-identity** (schema-preserving operational key, dual-key migration, `public` removal last). This spec delivers A.

## 2. Governing principle

> Any column names can be supported, but an unknown identity mapping cannot be safely auto-applied on first sight.

For Delivery A there is exactly one *known* format, so its mapping is **code-reviewed authority** — a registered adapter, not inference. No value-shape or LLM identity inference appears anywhere in A. Representation mapping (which input header supplies a canonical field) stays strictly separate from identity binding (which governed object the canonical identity denotes); the existing EXACT/ALIASED/AMBIGUOUS/UNRESOLVED binding rules run unchanged *after* mapping.

## 3. Architecture

### 3.1 A registered FTR adapter, selected explicitly (not header-guessed)

Add a registered adapter that owns the exact FTR representation mapping. It plugs into the two seams the Explore confirmed already exist — the single reader dispatch in `_read_rows` (`uploads.py:68-86`) and the explicit `profile=` parameter on `ingest_upload` (`ingest.py:805`) — so it adds a reader, not a new write path (consistent with the connectors' "readers into the unchanged spine" contract).

Selection for A: the FTR header fingerprint (the exact 17-header set, BOM/case/space/underscore-normalized) selects the FTR adapter. This is a lookup key, not the brittle 3-literal signature — it demotes header-signature detection to an adapter lookup (the same demotion B generalizes). The generic glossary path and technical path remain untouched for other files.

**FTR representation mapping (code-reviewed):**

| Source header(s) | Canonical target | Transform |
|---|---|---|
| `schema.table.column` | schema-preserving FQN → (schema, table, column) | `split_fqn` (3-part → column term, 2-part → table term) |
| `term_name` | business term | copy |
| `description_business_definition` | definition | copy (then sanitize — §3.5) |
| `data_domain` | domain | copy |
| `term_type` | term type | `normalize_enum` (lowercased) |
| `synonyms_aliases` | synonyms | `split_list` on confirmed delimiters `[";", "|"]` |
| `bian_level_1..4` | BIAN path | `join_path` (ordered, non-empty levels joined) |
| `fibo_level_1` | FIBO path | copy |
| `related_business_process_l1..3` | process path | `join_path` (ordered) |
| `related_terms` | related terms | `split_list` on `[";", "|"]` |
| `source_row` | source-row provenance | copy (integer locator) |
| `data_type` | declared physical type | copy (absent → `UNKNOWN_TYPE`) |

`source_row` is a **provenance locator (18..144)**, explicitly NOT the catalog `source`.

### 3.2 Extend `GlossaryRecord` additively (keep `CanonicalRow`)

No `CanonicalRow` replacement. `CanonicalRow` stays the shared ingest contract; the FTR adapter emits `CanonicalRow`s for the validate → graph spine exactly as the glossary reader does today, and carries the richer semantics in the sidecar. Extend `GlossaryRecord` with: `source_row`, `term_type`, `bian_path` (from levels), `fibo_path`, `process_path`, `related_terms`, and fix `synonyms` (the `synonyms_aliases` header must map). Preserve the physical **schema** and full physical **FQN** on the sidecar.

### 3.3 Schema preservation without re-keying (A-safe)

- The sidecar `logical_ref` already schema-preserves via `normalize_ref(source, schema, table, column)`.
- Add the real schema as a single **additive** `graph_node.schema_name` column (populated, non-key). The physical FQN is NOT a separate column — it is deterministically reconstructed as `schema_name + "." + table_name + "." + column_name` (Finding #14; the earlier "additive physical_fqn column" promise is dropped).
- The **operational key stays `public.table.column`** for A. The binding between the schema-preserving physical ref and the legacy graph ref is the existing `ObjectBinding` layer; A adds no re-key.
- **Fence (hardening — carried from review finding 4):** until Delivery C, two rows sharing `table.column` under *different* schemas — within OR across uploads — are **fail-closed (quarantined), never silently merged** to the shared `public` key. The FTR file is single-schema (`DPL_EIB_COMPLIANCE`), so this never fires for it; the fence exists so A cannot corrupt a later multi-schema upload while the operational key is still `public`.

### 3.4 `term_type` → Pass C; ingest the table-level term

- Thread `term_type` from the sidecar into the `ColMeta` handed to Pass C so `is_join_key_eligible` can exclude `term_type == "measure"` (`passc/identifiers.py:76`) — today ingestion hardcodes `term_type=""` (`ingest.py:355`), so the file's Measure rows would wrongly be join-key candidates. **`term_type` is a versioned CLOSED vocabulary** `TERM_TYPE_VOCAB_V1 = {"measure","dimension","code_value","reference_data","business_term"}` (normalized: lowercase, spaces→`_`); an unknown value **quarantines the row** (so a typo like `Mesure` is caught, not silently treated as non-measure), with the raw value length-bounded (≤32) and PII-redacted in the quarantine reason. In Pass C: `measure` → excluded; all other in-vocab values → eligible by the existing heuristics.
- Ingest the **table-level** glossary record: its definition, domain, and taxonomy must reach the table node (today `is_table` records are skipped at `ingest.py:333/489/709`).

### 3.5 Sample-value handling — bank-grade, fail-closed (hardening — review finding 1)

The verbal "samples are fine to store" directive is **retracted** by the user; A restores the standing bank-grade policy. The existing LLM-egress redactor (`_redact_free_text_meta`, `enrich_llm.py:79`) already fails closed (`_one(...) -> str | None  # None ⟹ fail closed`) — but it guards only the **LLM payload**. Finding #10 established that `graph_node.definition` and `search_doc` persist the **raw** definition today. So A must add a **strip-before-persistence** control, which is net-new:

- **Sanitize at parse time, before hashing/enrichment/evidence/graph.** `content_hash` includes `definition` and enrichment consumes rows *before* `build_graph`, so sanitizing late both crashes (frozen `ValidationResult`) and orphans the LLM concepts. The FTR adapter emits already-sanitized `CanonicalRow`s + `GlossaryRecord`s; the raw text and the parsed sample profile never enter the ingest spine.
- Deterministically strip representative-value clauses, then apply the free-text redactor, producing an explicit **state** — `none` / `stripped` / `suspected_unhandled`. A sample MARKER present but not excised ⟹ `suspected_unhandled` ⟹ the field is **blanked** (the row still ingests; identity is intact), never persisted raw. Record the sanitizer and redaction **versions**, not just a count.
- **A1 discards raw sample values entirely** — they are parsed into safe derived facets (`logical_representation`, `semantic_type`, used for parser evidence) and then dropped; nothing raw is persisted, so A1 is sample-safe with **no store required**. (There is no existing governed/KMS blob store to reuse — `intake/blobs.py`/`write_blob` do not exist; migration `0511`'s `blob` table is a bare write-once JSONB table with no classification/KMS/retention/erasure.)
- **A2** owns retention-as-audit: a *real* restricted raw-upload store (classification / KMS-at-rest / retention class / erasure lifecycle), written durably and referenced by the ingestion run — built only if the bank requires the raw audit-of-record.
- Never index or externally transmit raw representative values.

Because this is a compliance control on A's critical path, its recall must be measurable **in A**: a small **labeled sample-clause corpus** (derived from the file's ~85 sample-bearing definitions, sanitized) ships with A as a test asset — the control cannot be validated with B's corpus, which arrives later.

### 3.6 Enrichment at scale (hardening — review finding 2)

"Enrich everything" = every eligible mapped row is processed; it does **not** mean raw samples leave the boundary. Key facts, confirmed against the code:

- **Concept enrichment (Pass A) already handles 126 columns** — it chunks *across* columns (batch mode via `chunk_items`, or one call per column in single mode). So once parsing works, all 126 columns enter bounded concept enrichment with no change. This is the acceptance-relevant path.
- **The 200-char-per-field limit is egress-only** — `graph_node.definition` has no length limit and persists the full *sanitized* definition. Resolution of the long-definition tension: persist the full sanitized definition to graph/search; send the sanitized definition (truncated to ≤200 for egress) to the concept classifier, which only needs enough text to pick a concept. This is already the code's behavior — no Global-Constraint change.
- **Pass B table synthesis (grain/availability) is DEFERRED out of Delivery A.** It is default-off (`OVERLAY_TABLE_SYNTH`) and the FTR file declares no grain, so it never runs for this file. Its 64-column cap (`_MAX_COLUMN_PROFILES`) is a real limit, but the naive "synthesize each chunk then merge verdicts" is **semantically invalid** (Pass B emits ONE grain/availability verdict per table; independent chunks can nominate incompatible grains; `table#chunk` refs break `make_ref_accept`). The correct fix is a two-phase chunk-summary → single-synthesis design, built as its own task when a wide table that actually declares grain arrives — not in A.
- Partial enrichment is recorded explicitly: **ingestion success must not imply enrichment completeness.**

### 3.7 Search + quarantine repair

- Index business term, synonyms, BIAN/FIBO paths, and related terms into `search_doc` (today it indexes only column/definition/table/concept/domain — `graph.py:90-98`). All render sites of the single `_SEARCH_DOC` expression update together (the #20 invariant).
- Quarantine references the **`source_row` provenance locator and original mapped context** so a reviewer can map a failed row back to spreadsheet line 18..144 (today it stores the flat `CanonicalRow`, showing blank identity — review finding #3). Required anyway by the acceptance criterion "source rows 18..144 retained".

### 3.8 Honest run counts (review finding #15)

`RunDetailPanel.tsx` labels `row_count` as "asserted", so a fully-rejected upload reads "127 asserted · 127 quarantined" when `asserted` is really 0. Relabel the file-row count honestly (e.g. "127 rows · 0 asserted · 127 quarantined", or drive the asserted number from the real asserted count rather than `row_count`). Two-line frontend fix, independent of the adapter, folded into A so the reviewer sees truthful numbers for exactly the kind of rejected upload this file produced.

## 4. Data model note (from review finding 6)

Delivery A's adapter is a **static registered mapping**, so it needs neither `MappingAnalysisCandidate` (inference event) nor `ConfirmedMappingTemplate` — those are Delivery B. When B lands, the confirmed-template artifact must use a distinct verb (`registered_by` / `approved_by`), NOT `confirmed_by`, to avoid collision with the evidence `CONFIRMED` strength (`evidence.py:41`). Confirming a *mapping* governs the transformation only; the existing `SourceCapabilityProfile` + `AssertionStrength` (PROPOSED/SUPPORTED/ATTESTED/CONFIRMED) still decide each value's strength after transformation.

## 5. Testing

- **Sanitized fixture with the EXACT FTR headers** (all 17, real names, sample *values* scrubbed) — replacing the invented-header fixture that let this ship broken (finding #16). Never read `~/Downloads`; the fixture is authored inline/sanitized.
- **Golden happy path:** 126 column terms + 1 table term, **zero quarantine**, `source_row` 18..144 retained, table-level definition/domain persisted, `term_type` reaches Pass C, all 126 columns enter bounded (chunked) enrichment.
- **Negative/malformed fixtures (hardening — review finding 5):** bad FQN arity, empty FQN component, unknown `term_type`, duplicate FQN, multi-schema `table.column` collision — asserting each **quarantines correctly** so the adapter is not a yes-machine.
- **Sample-clause recall corpus (§3.5):** labeled definitions asserting representative values are absent from `graph_node.definition`, `search_doc`, and the LLM audit payload.
- **Full API → PostgreSQL → search integration test** over the sanitized exact-header fixture.

## 6. Acceptance criteria

1. 126 valid column rows and one table record ingest.
2. Zero quarantine for the supplied valid file; correct quarantine for the malformed fixtures.
3. **A1:** `source_row` visible for **quarantined** rows only (in the quarantine record). **A2:** durable `source_row`→object retention for all 127 accepted records (accepted-row provenance is an A2 concern; A1 does not claim it).
4. Table-level definition and domain persisted to the table node.
5. `term_type` reaches Pass C; the six Measure rows are excluded from join-key candidacy.
6. All 126 columns enter bounded, chunked concept enrichment; partial enrichment recorded, not silently dropped.
7. Sample values absent from graph, search, and LLM audit payloads; persistence stripper fails closed; sanitization recorded as provenance.
8. Physical schema retained (sidecar + additive graph columns) without re-keying existing operational graph state; multi-schema `table.column` fenced fail-closed until C.
9. Full API→PG→search integration test green on the sanitized exact-header fixture.
10. `RunDetailPanel` shows honest counts for a rejected upload (asserted 0, not `row_count`), covered by a frontend test.

## 7. Out of scope (carried to B / C)

- **B:** registered-adapter/template lookup chain, mapping analysis, human analyze/confirm workflow, reusable deterministic templates. **Note (review finding 3):** template auto-apply on a header-fingerprint match must include a cheap post-apply drift guard (does `split_fqn` still resolve? is `term_type` still in the known set?) that bounces to preview on mismatch — determinism ≠ never re-examine — plus a template lifecycle (owner / supersede / revoke; `mapping_version` alone is insufficient).
- **C:** schema-preserving operational key, identity-binding table, dual-read/dual-write, VERIFIED approved-join preservation via aliases/migration records, equivalence gates, `public` removal last. No governance event or VERIFIED join rewritten or orphaned.

## 8. Open decision for the user

Everything above is resolved except one product call worth confirming: **should A also surface the sanitization to the reviewer** (e.g., a "definitions sanitized: N clauses removed" line on the ingestion run / review queue), or keep it as silent provenance only? My recommendation is to surface a count — it makes the compliance control visible and auditable at review time — but it is a small scope addition, so I'm flagging rather than assuming.
