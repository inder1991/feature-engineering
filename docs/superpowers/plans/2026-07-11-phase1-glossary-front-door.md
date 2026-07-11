# Phase 1 — FTR Glossary Front Door Implementation Plan (v2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.
> **Build on a fresh branch off `main` `1c2f6b0`** (Phase 0 merged/pushed). Verify the next free migration slot before implementing (Phase 0 used up to `0982`).

**Goal:** Turn a business-glossary CSV (FTR `schema.table.column` / definition / domain / BIAN-FIBO) into a searchable, evidence+authority-carrying catalog — proving the Phase-0 kernel end-to-end, **no join or grain promotion**.

**v2 changes (folding in the plan review's 10 must-fixes + should-fixes):** glossary rows use `type="unknown"` through a semantic-compatible validation path (never silent-quarantine); **schema is preserved** via a normalized object-ref that both evidence and `graph_node` share; **producer-scoped staleness** (a source re-upload never stales human/taxonomy evidence); a concrete **`field_revalidation`** store for pending-revalidation disqualifiers; **display projection is provably not operational authority** (decision-id link + feature code reads decisions, not flat columns); **rich LLM context is a required test**; a **`SourceCapabilityProfile` for FTR**; **identity-ambiguity vs metadata-conflict** disentangled; **sensitivity floor vs classification-status** separated; `record_field_decision` verified as a hard prerequisite; **canonical JSON hashing**, **field-specific input hashes**, a **failure-class table**, and readiness reframed as **diagnostic** scope.

**Tech Stack:** Python 3.12, psycopg raw SQL, pytest (live-PG `db`/`conn`), `uv`.

**Spec:** `docs/superpowers/specs/2026-07-11-evidence-authority-ingestion.md` (v4) — §14 must-prove; §5.1, §3.3 (source capability), §6, §7 (floor), §8, §9. §0 reuse map governs.

## Must-prove list (acceptance criteria, spec §14)
1. FTR row → stable logical object. 2. Rich context (BIAN/FIBO/definition) reaches the LLM. 3. Pass A writes item-level evidence. 4. Display projection shows proposals. 5. Operational projection stays unresolved where authority insufficient. 6. No safety/structural field load-bearing from LLM alone. 7. Re-upload stales changed proposals. 8. Human-confirmed values survive but revalidate.

## Global Constraints
- **Reuse the Phase-0 kernel** (`field_authority.py` resolver/predicate/FieldPolicy/ConflictStrategy; `object_identity.py` incl. `classify_identity`; `safety_floor.py`; `field_decision.py` `record_field_decision`; `evidence.py` enums; `conflict_review.py`). No parallel machinery.
- **`type="unknown"` for glossary rows** — never `type=""` (that quarantines). A glossary upload runs a semantic-compatible validation that accepts `type="unknown"`; quarantine is reserved for real problems (invalid FQN, missing table/column identity, source mismatch). Missing physical type is a **readiness blocker, not a quarantine**.
- **Schema preserved end-to-end.** One normalized object ref (`schema.table.column`, schema default `public`) is the single identity used by `field_evidence.logical_ref`, `field_decision_event.logical_ref`, and the `graph_node` join key. Never silently drop schema.
- **Display ≠ authority.** A value projected into `graph_node` is display-only; feature/operational code MUST read `field_decision_event` (load-bearing value) or a specialized fact — never a flat `graph_node` column for operational semantics. Every projected field carries a `*_decision_id` link.
- **Producer-scoped staleness.** A source re-upload stales only the SOURCE producer's evidence for a changed field; human/taxonomy/LLM evidence is not staled by a source change. Human-confirmed values survive and are marked **pending-revalidation** (a disqualifier), never staled.
- **Strength propagation** (§3.2): a derived value's strength ≤ min of its inputs' strengths.
- **Safety floor vs classification.** A proposed taxonomy sensitivity RESTRICTS (raises the floor) but does NOT certify classification — track `effective_restriction` (the floor) separately from `classification_status` (proposed/confirmed).
- **Advisory + fail-soft, by failure class** (do NOT swallow everything):

  | Failure | Behavior |
  |---|---|
  | Invalid glossary shape (headers) | reject upload with a diagnostic |
  | Row FQN invalid / missing identity | quarantine that row |
  | Duplicate rows, conflicting metadata (same FQN) | attach evidence + open a conflict-review item (NOT quarantine) |
  | Sample parser: no profile | diagnostic, continue (no type evidence) |
  | LLM (Pass A) fails | continue without LLM evidence |
  | Evidence write fails | continue, emit an ingestion warning |
  | Resolver/projection fails | continue with the raw graph, emit a degraded-status diagnostic |
- **Conventions:** raw SQL; `from __future__ import annotations`; frozen slotted dataclasses; `now` seam; canonical JSON hashing (`json.dumps(v, sort_keys=True, separators=(",",":"))` → sha256) everywhere a value is hashed; ruff + mypy clean; TDD; commit per task; migrations additive/forward-only, applied before deploy.

## Scope note (10 tasks; review checkpoint after Task 8)
1–3 = store + identity + source trust; 4–5 = glossary reader + parser; 6–7 = LLM + taxonomy producers; 8 = policies + resolve/project (the payoff, provable here); 9–10 = readiness + re-upload.

---

## File Structure
- Create `overlay/field_evidence.py` + mig `0983_field_evidence.sql` — §5.1 per-field store (canonical-hashed value, producer-scoped staleness, field-specific input_hash).
- Create `overlay/upload/object_ref.py` — the single normalized object ref (`normalize_ref`, `logical_ref_str`) preserving schema; shared by evidence + graph.
- Create `overlay/upload/upload_identity.py` — identity (EXACT/AMBIGUOUS) vs **metadata-conflict** classification over an upload.
- Create `overlay/upload/source_profile.py` — `SourceCapabilityProfile` + `FTR_GLOSSARY_PROFILE`.
- Create `overlay/upload/glossary_reader.py` — detect + parse (schema-preserving, `type="unknown"`, BIAN/FIBO sidecar).
- Create `overlay/upload/glossary_validate.py` — semantic-compatible validation (accepts `unknown`; quarantines only real problems).
- Create `overlay/upload/sample_parser.py` — `logical_representation`/`semantic_type`/`computational_type` + no-profile diagnostic.
- Modify `overlay/upload/enrich.py` — Pass A writes concept evidence + a required prompt-context test.
- Create `overlay/upload/taxonomy_evidence.py` — strength-propagated derivation; sensitivity as a floor.
- Create `overlay/upload/field_policies.py` — FieldPolicy registry (logical_type nuance; sensitivity floor+classification; sensitivity `severity_order=SENSITIVITY_ORDER`).
- Create `overlay/upload/field_resolution.py` — resolve → decision events → display projection with `*_decision_id` links.
- Create `overlay/upload/field_revalidation.py` + mig — pending-revalidation store feeding the resolver's disqualifiers.
- Create `overlay/upload/readiness.py` — diagnostic, blocker-based, catalog/table scope.
- Modify `overlay/upload/ingest.py`, `api/routes/uploads.py` — wire the glossary path, fail-soft by class.

Locked interfaces:
```python
# object_ref.py
def normalize_ref(source, schema, table, column) -> str           # "source|schema|table|column", schema default 'public'
# field_evidence.py (§5.1)
def canonical_hash(value: object) -> str                          # sha256(json.dumps(sort_keys,separators))
def field_input_hash(*, logical_ref, field_name, material: object) -> str   # per-FIELD input, not whole row
def record_field_evidence(conn, *, logical_ref, field_name, proposed_value, producer, strength,
    producer_ref, source_snapshot_id, input_hash, producer_item_ref=None, producer_configuration_hash=None,
    evidence_spans=(), confidence_band=None, lifecycle=ACTIVE) -> str
def read_active_field_evidence(conn, logical_ref, field_name) -> list[FieldEvidence]
def stale_source_evidence(conn, *, logical_ref, field_name, producer: EvidenceProducer, keep_input_hash) -> int
def to_view(ev) -> FieldEvidenceView
# upload_identity.py
def classify_upload(rows) -> tuple[dict[str, ObjectBinding], list[MetadataConflict]]   # bindings + conflicts (NOT ambiguity)
# source_profile.py
@dataclass class SourceCapabilityProfile: source_type; attested_fields; proposed_fields; structural_fields
def strength_for(profile, field_name) -> AssertionStrength        # attested|proposed per (source,field)
# sample_parser.py
@dataclass class ParsedProfile: logical_representation; semantic_type; computational_type; sample_values; diagnostic
# field_revalidation.py
def flag_pending_revalidation(conn, *, logical_ref, field_name, reason, source_snapshot_id) -> str
def active_disqualifiers_for(conn, logical_ref, field_name) -> frozenset[Disqualifier]
# field_resolution.py
def resolve_and_project(conn, *, source, logical_refs, now=None) -> None
```

---

## Task 1: `field_evidence` store (§5.1) — canonical hash, producer-scoped staleness, field input hash
**Files:** Create `overlay/field_evidence.py`, `overlay/upload/object_ref.py`, mig `0983_field_evidence.sql`; Test `tests/featuregen/overlay/test_field_evidence.py`.

- [ ] **Step 1: Failing tests** —
  - `canonical_hash({"b":2,"a":1}) == canonical_hash({"a":1,"b":2})` (order-independent — for stable staleness/decisions).
  - write `concept@llm:proposed` for a `logical_ref`; `read_active_field_evidence` returns it; `to_view` → `FieldEvidenceView`.
  - **producer-scoped staleness:** write `definition@source` (input_hash h1) + `definition@human:confirmed` (h_human); `stale_source_evidence(producer=SOURCE, keep_input_hash=h2)` marks the OLD source row STALE but leaves the human row ACTIVE.
  - `field_input_hash` differs per field for the same row (definition-input ≠ concept-input).
- [ ] **Step 3a: Migration** `0983` — `field_evidence` (evidence_id PK, logical_ref, field_name, proposed_value jsonb, proposed_value_hash, producer, strength, lifecycle, producer_ref, producer_item_ref, producer_configuration_hash, evidence_spans jsonb, confidence_band, source_snapshot_id, input_hash, created_at) + index `(logical_ref, field_name, lifecycle)`. Verify slot.
- [ ] **Step 3b: Implement** — `object_ref.normalize_ref` (schema-preserving); `canonical_hash`; `field_input_hash` (hash of the FIELD's input material, e.g. the definition text for the definition field — NOT the whole row); `record_field_evidence` (mint `fev_`, `canonical_hash(proposed_value)` for the hash); `read_active_field_evidence` (lifecycle='active'); `stale_source_evidence` (`UPDATE ... SET lifecycle='stale' WHERE logical_ref=%s AND field_name=%s AND producer=%s AND lifecycle='active' AND input_hash<>%s` — **producer-scoped**); `to_view`. `source_snapshot_id` is the ingestion-run id (see Task 10); reuse across snapshots keys on `input_hash` (unchanged input → not re-written/staled).
- [ ] **Step 4/5:** Green; commit `feat(overlay): field_evidence store — canonical hash, producer-scoped staleness (spec §5.1)`.

---

## Task 2: Upload identity — EXACT/AMBIGUOUS vs metadata-conflict
**Files:** Create `overlay/upload/upload_identity.py`; Test `tests/featuregen/overlay/upload/test_upload_identity.py`.
**Fix (review #12):** two rows with the same FQN but different `definition` are a **metadata conflict** (attach evidence + open a conflict), NOT identity ambiguity. Identity `AMBIGUOUS` is reserved for a ref that can't be pinned to one object (unparseable/duplicated FQN structure). Never block evidence attach on a definition disagreement.

- [ ] **Step 1: Failing test** — unique `accounts.balance` → `EXACT`/attachable; two rows same FQN different definition → still ONE `EXACT` binding **plus** a `MetadataConflict(logical_ref, field="definition")` returned (not `AMBIGUOUS`, still attachable); an identical-duplicate row → deduped, no conflict; a genuinely unparseable/duplicated identity → `AMBIGUOUS`/not-attachable.
- [ ] **Step 3:** `classify_upload(rows)` → `(bindings, conflicts)`: group by `normalize_ref`; a group is `EXACT` if it resolves to one object identity (the normal case, even with differing metadata); emit a `MetadataConflict` per field where the group's rows disagree on a load-bearing/attested value (definition/type); `AMBIGUOUS` only for a ref that fails to pin (reuse Phase-0 `classify_identity` for the pin decision). Conflicts are handed to Task 10 to open `conflict_review` items.
- [ ] **Step 4/5:** Green; commit `feat(upload): upload identity — metadata-conflict vs ambiguity`.

---

## Task 3: Source capability profile (per-(source,field) trust)
**Files:** Create `overlay/upload/source_profile.py`; Test `tests/featuregen/overlay/upload/test_source_profile.py`.
**Fix (review #15):** don't hardcode source trust ad hoc.

- [ ] **Step 1: Failing test** — `FTR_GLOSSARY_PROFILE`: `strength_for(profile,"definition")==ATTESTED`, `strength_for(profile,"domain")==PROPOSED`, `strength_for(profile,"sensitivity")==PROPOSED`; an unknown field defaults to PROPOSED.
- [ ] **Step 3:** `SourceCapabilityProfile(source_type, attested_fields, proposed_fields, structural_fields)`; `FTR_GLOSSARY_PROFILE = SourceCapabilityProfile("ftr_glossary", attested_fields={"definition","business_term","bian_path","fibo_path"}, proposed_fields={"domain","sample_profile","sensitivity"}, structural_fields=frozenset())`; `strength_for` returns ATTESTED for attested_fields else PROPOSED. Evidence-writing (Tasks 6/10) uses this — never a literal.
- [ ] **Step 4/5:** Green; commit `feat(upload): source capability profile for FTR glossary (spec §3.3)`.

---

## Task 4: Glossary reader + semantic-compatible validation (`type="unknown"`, schema preserved)
**Files:** Create `overlay/upload/glossary_reader.py`, `overlay/upload/glossary_validate.py`; Modify `api/routes/uploads.py`; Test `tests/featuregen/overlay/upload/test_glossary_reader.py`.
**Fixes (review #1, #2, #3):** rows emit `type="unknown"` (not `""`), schema preserved, validation accepts unknown.

- [ ] **Step 1: Failing tests** (inline FTR-shaped fixture; do NOT read `~/Downloads`) —
  - `is_glossary_csv(headers)` true for the glossary signature, false for canonical headers.
  - `read_glossary(text, source)` on a 3-part `DPL_EIB_COMPLIANCE.COMP_...REPOS_DLY.CUST_NAME` → a `CanonicalRow` with **`type="unknown"`**, definition from `description_business_definition`, and the **schema preserved** (the row's `normalize_ref` includes `DPL_EIB_COMPLIANCE`); the 2-part table row → a table record, not a column.
  - `validate_glossary_rows(rows, source)`: a well-formed glossary row with `type="unknown"` **passes** (goes to `good`), NOT quarantined; a row with an invalid/missing FQN or a source mismatch **is** quarantined.
- [ ] **Step 3a:** `read_glossary` — split FQN preserving schema (3-part → schema/table/column; 2-part → table record); `CanonicalRow(type="unknown", definition=...)`; a `GlossaryRecord` sidecar per row with term_name/definition/domain/synonyms/bian_path/fibo_path (keyed by `normalize_ref`).
- [ ] **Step 3b:** `validate_glossary_rows` — a glossary-aware validation: require source/table/column present + a resolvable FQN; **accept `type="unknown"`**; quarantine only invalid-FQN / missing-identity / source-mismatch (per the failure-class table). Missing physical type is NOT a quarantine (it's a Task-9 readiness blocker).
- [ ] **Step 3c:** `uploads.py _read_rows` — peek headers; glossary-shaped `.csv` → the glossary path (returns rows + records + validation); else the existing `read_csv_rows` (byte-for-byte unchanged).
- [ ] **Step 4/5:** Green; commit `feat(upload): glossary reader + semantic-compatible validation (type=unknown, schema preserved)`.

---

## Task 5: Deterministic sample-value parser (representation/semantic/computational split)
**Files:** Create `overlay/upload/sample_parser.py`; Test `tests/featuregen/overlay/upload/test_sample_parser.py`.
**Fix (review #9):** parser-supported type must NOT certify numeric computation for identifier-like values.

- [ ] **Step 1: Failing tests** — `parse_sample_profile("... sample profile is NUMERIC ... representative values such as 3708484836801; 3708446902413 ...")` → `logical_representation="numeric_string"`, `semantic_type="identifier"`, **`computational_type=None`** (fixed-length all-digit ID → NOT a decimal measure), sample_values extracted; a genuine amount ("values such as 1250.00; 9.99") → `computational_type="decimal"`, `semantic_type="amount"`; `NUMERIC_SPECIAL` time → `logical_representation="time"`; no profile phrase → `diagnostic` set, all types None.
- [ ] **Step 3:** regex-extract the profile token + representative values; classify: uniform-length all-digit → identifier (representation `numeric_string`, `computational_type=None`, and a downstream signal `allowed_numeric_aggregation="none"`); decimals with points → `computational_type="decimal"`, `semantic_type="amount"`; time-like → time; else text. A `diagnostic` when nothing parseable (never a silent/wrong type). This feeds PARSER evidence per field: `logical_representation@parser:supported`, `semantic_type@parser:supported`.
- [ ] **Step 4/5:** Green; commit `feat(upload): sample parser — representation/semantic/computational split`.

---

## Task 6: Pass A concept evidence + required rich-context test
**Files:** Modify `overlay/upload/enrich.py`; Test `tests/featuregen/overlay/upload/test_pass_a_evidence.py`.
**Fixes (review #10):** rich glossary context reaching the LLM is a REQUIRED test, not a note.
**VERIFY FIRST:** read the merged `enrich_concepts`/`run_batched`/`audited_batch_call` for the item-level ref + vocab fingerprint.

- [ ] **Step 1: Failing tests** —
  - **rich context (required):** a capturing `FakeLLM` records the request payload; assert the concept-enrichment input for a glossary column carries the **business definition, term name, synonyms/aliases, data_domain, BIAN path, FIBO path** (not just table/column/type). If the current prompt lacks these, EXTEND the enrichment input to include the glossary sidecar for glossary uploads.
  - **item-level evidence:** in batch mode, enrichment writes a `field_evidence` row per classified column: `field="concept"`, `producer=llm`, `strength=proposed`, `producer_item_ref`=batch ref, `producer_configuration_hash`=vocab fingerprint, value=concept; return shape unchanged; no evidence for invalid/UNCLASSIFIED (C3).
- [ ] **Step 3:** thread the glossary sidecar into the concept-enrichment `catalog_metadata` for glossary uploads (guarded so non-glossary uploads are unchanged); write concept `field_evidence` keyed by `normalize_ref` (only for attachable columns). Reuse `_vocab_fingerprint()`. Fail-soft (evidence-write failure logs, never aborts).
- [ ] **Step 4/5:** Green (+ existing enrich tests green); commit `feat(enrich): Pass A rich-context input + item-level concept evidence`.

---

## Task 7: Taxonomy derivation — strength-propagated, sensitivity as a floor
**Files:** Create `overlay/upload/taxonomy_evidence.py`; Test `tests/featuregen/overlay/upload/test_taxonomy_evidence.py`.
**Fixes (review #8 partial, strength propagation):** sensitivity is a floor with a separate classification status.

- [ ] **Step 1: Failing test** — `derive_concept_evidence("monetary_stock", PROPOSED)` → additivity/pit_role at strength `PROPOSED` (== input, never higher); sensitivity emitted as a `("sensitivity_floor", <value>, PROPOSED)` (a FLOOR signal, distinct from an operational classification); `derive_concept_evidence(..., CONFIRMED)` → those at CONFIRMED. UNCLASSIFIED → no evidence.
- [ ] **Step 3:** look up `concept(name)`; emit `(field_name, value, input_strength)` per behavioral field. Sensitivity is emitted under `sensitivity_floor` (fed to `apply_sensitivity_floor` at resolution, NOT as an operational sensitivity classification). Additivity/temporal at the input strength (so a proposed concept yields proposed derivations → Task 8's policy blocks them from gating).
- [ ] **Step 4/5:** Green; commit `feat(upload): taxonomy derivation (strength-propagated, sensitivity floor)`.

---

## Task 8: Field policies + resolve/project (the payoff)
**Files:** Create `overlay/upload/field_policies.py`, `overlay/upload/field_resolution.py`; Modify `overlay/bootstrap.py`; Test `tests/featuregen/overlay/upload/test_field_resolution.py`.
**HARD PREREQUISITE (review #14):** confirm `overlay/field_decision.py record_field_decision` exists and accepts display+load-bearing hashes (it does — Phase 0 Task 8; verify the exact signature before wiring).
**Fixes (review #5, #7, #8, #9):** logical_type nuance; sensitivity floor vs classification; display ≠ authority.

- [ ] **Step 1a: Field policies** — `policy_for(field)`:
  - `concept` (advisory, RECOMMENDATION): display on `llm/proposed`; operational requires source/human (LLM-alone NOT load-bearing — §8).
  - `definition`, `domain`, `feature_role`: RECOMMENDATION; LLM/source proposed allowed.
  - `logical_representation`/`semantic_type`: OPERATIONAL-limited — `parser/supported` can gate `semantic_type` + the no-aggregation signal, but a `computational_type=decimal` requires a stronger/non-identifier parser signal or source attestation (a proposed identifier NEVER certifies numeric computation).
  - `sensitivity`: `conflict_strategy=MOST_RESTRICTIVE`, `severity_order=safety_floor.SENSITIVITY_ORDER`; the taxonomy `sensitivity_floor` sets `effective_restriction` via `apply_sensitivity_floor`, but `classification_status` stays `proposed` until source/human confirms (a proposed floor RESTRICTS but does not CERTIFY).
  - `additivity`/`temporal_role`: OPERATIONAL; require the concept CONFIRMED (taxonomy-from-a-proposed-concept is `taxonomy/proposed` → does not gate).
- [ ] **Step 1b: Failing test** — seed field_evidence for a column; after `resolve_and_project`: `graph_node` shows the display concept/definition **with a `concept_decision_id`**; `additivity` load-bearing is unresolved (proposed concept → §3.2); `sensitivity` `effective_restriction` = the floor (never lowered by an LLM proposal) while `classification_status="proposed"`; a `field_decision_event` per field carries both effective values; **a test asserts the flat display value exists AND the load-bearing value is unresolved AND a helper `is_feature_eligible(field)` returns False** (proving display ≠ authority).
- [ ] **Step 3:** `resolve_and_project`: per (logical_ref, field-with-policy): read active field_evidence → `to_view` → `resolve_field_authority(views, policy, active_disqualifiers_for(...))`; emit `record_field_decision` (display+load-bearing hashes); UPDATE the `graph_node` display column AND a companion `*_decision_id` link column (migration adds the link columns for concept/domain/definition/sensitivity/logical_type). Sensitivity runs through `apply_sensitivity_floor`. Add/confirm a boundary helper so operational/feature code reads the decision (never the flat column) — document + test. Register `policy_for` in `bootstrap.register_overlay`.
- [ ] **Step 4/5:** Green; commit `feat(upload): field policies + resolve-and-project (display vs authority, decision-linked) (spec §4/§6/§8)`.

---

## Task 9: Blocker-based scoped readiness (diagnostic)
**Files:** Create `overlay/upload/readiness.py`; Test `tests/featuregen/overlay/upload/test_readiness.py`.
**Fix (review #13):** framed as diagnostic; distinguish Phase-1-not-promoted from failed ingestion.

- [ ] **Step 1: Failing test** — after resolution, `compute_readiness(source, scope=CATALOG)` → `operational_status` with `blocking_requirements` labelled by CAUSE: `not_promoted_in_phase1` (grain/join — expected, not a failure) vs `unresolved_authority` (additivity awaiting concept confirmation) vs `ingestion_error`; `review_requirements` for proposed-unconfirmed; `advisory_gaps`; `summary_scores` display-only. TABLE scope subsets to one table.
- [ ] **Step 3:** define `ReadinessScopeType`/`ReadinessRequirement`/`FeatureReadiness` (spec §9); derive from field_decision_events + policies; a blocking requirement carries a `cause` so the report never conflates "Phase 1 doesn't promote grain" with "ingestion failed". Blocker-based gate; percentages derived. Catalog/table = **diagnostic** (recipe/run-scoped gating is Phase 2+).
- [ ] **Step 4/5:** Green; commit `feat(upload): diagnostic scoped readiness with cause labels (spec §9)`.

---

## Task 10: Glossary ingest wiring — staleness, revalidation, conflicts, failure classes
**Files:** Modify `overlay/upload/ingest.py`; Create `overlay/upload/field_revalidation.py` + mig; Test `tests/featuregen/overlay/upload/test_glossary_reupload.py`.
**Fixes (review #4, #5, #6, #16):** ingestion-run snapshot id; producer-scoped staleness; concrete revalidation store; conflicts; failure classes.

- [ ] **Step 1: Failing tests** (must-prove #7/#8) —
  - re-upload with a CHANGED definition → the old `definition@source` evidence is STALE, the new ACTIVE (via `stale_source_evidence(producer=SOURCE, keep_input_hash=<new field_input_hash>)`); an UNCHANGED column's evidence is reused (same `input_hash`, not re-written) even though `source_snapshot_id` changed.
  - a human-confirmed `sensitivity` **survives** a re-upload; if the column's material changed, `flag_pending_revalidation` writes a row and `active_disqualifiers_for` returns `{CONFIRMATION_PENDING_REVALIDATION}` so Task 8's resolver blocks the load-bearing value until re-confirmed — the human evidence is NOT staled.
  - a same-FQN conflicting-definition upload opens a `conflict_review` item (Task 2's `MetadataConflict` → `open_or_reopen_conflict`).
- [ ] **Step 3a: Revalidation store** — mig `field_revalidation` (logical_ref, field_name, reason, source_snapshot_id, status, created_at); `flag_pending_revalidation`; `active_disqualifiers_for(conn, logical_ref, field_name)` returns the disqualifier set the resolver consumes (Task 8 calls this).
- [ ] **Step 3b: Wire the glossary ingest path** in `ingest.py` (glossary uploads only, guarded): mint an `ingestion_run_id` (the `source_snapshot_id`); compute `classify_upload` (bindings + conflicts → open conflict items); for each attachable column write SOURCE evidence (strength via `strength_for(FTR_GLOSSARY_PROFILE, field)`), PARSER evidence (from `parse_sample_profile`), Pass A concept evidence, taxonomy-derived evidence — each with a **field-specific `input_hash`**; producer-scoped stale prior source evidence; human-confirmed material changes → `flag_pending_revalidation`; then `resolve_and_project` + `compute_readiness`. Each stage fail-soft **by the failure-class table** (emit warnings, don't swallow silently).
- [ ] **Step 4:** Green + full upload suite green (`uv run pytest tests/featuregen/overlay/ -q`).
- [ ] **Step 5: Commit** `feat(upload): glossary ingest wiring — snapshot id, producer-scoped staleness, revalidation, conflicts (spec §6.3)`.

---

## Self-Review
**Must-prove:** #1 → Tasks 1(object_ref)+2+4 (schema-preserving identity); #2 → Task 6 (required rich-context test); #3 → Task 6; #4/#5/#6 → Task 8 (display+decision-id, §3.2 blocks additivity, sensitivity floor-not-lowered, LLM-alone not load-bearing); #7 → Task 10 (producer-scoped staleness); #8 → Task 10 (survive + `field_revalidation` disqualifier).
**Review must-fixes:** #1 type=unknown (Task 4); #2 schema (Task 1 object_ref + Task 8 link cols); #3 producer-scoped staleness (Task 1/10); #4 revalidation store (Task 10); #5 display≠authority (Task 8 decision-id + boundary); #6 rich-context test (Task 6); #7 source profile (Task 3); #8 identity-vs-conflict (Task 2); #9 floor-vs-classification (Tasks 7/8); #10 record_field_decision prereq (Task 8). Should-fixes: canonical hash + field input hash (Task 1); no-profile diagnostic (Task 5); readiness cause labels (Task 9); failure classes (Global + Task 10); snapshot id (Task 10).
**Reuse:** resolver/policy/enums/safety-floor/decision-log/conflict-review all Phase-0. `field_evidence` (proposals) ≠ `overlay_evidence` (metrics) ≠ `field_decision_event` (decisions). No joins/grain promotion.
**Verify-before-build:** free migration slots on `main` (0983+); `record_field_decision` signature (Task 8 hard prereq); the merged `enrich_concepts` item-level hook + whether its prompt carries context (Task 6); whether `graph_node` needs new `*_decision_id`/`classification_status` columns (Task 8 migration); `canonical._material` reuse (Task 2).
**Type consistency:** `normalize_ref` string is the single `logical_ref` across evidence/decisions/graph; `to_view`→`FieldEvidenceView` feeds the Phase-0 resolver unchanged; producer/strength are Phase-0 enums; `active_disqualifiers_for` (Task 10) feeds `resolve_field_authority` (Task 8).

## Execution Handoff
**(1) Subagent-Driven (recommended)** — fresh subagent per task + two-stage review; review checkpoint after **Task 8** (vertical provable). **(2) Inline.** Tasks 6, 8, 10 carry the most integration judgment; read the merged Pass A + Phase-0 kernel before them.
