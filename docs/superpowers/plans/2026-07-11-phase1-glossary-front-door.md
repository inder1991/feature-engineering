# Phase 1 — FTR Glossary Front Door Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.
> **Build on a fresh branch off `main` `1c2f6b0`** (Phase 0 is merged/pushed). Verify the next free migration slot before implementing (Phase 0 used up to `0982`).

**Goal:** Turn a business-glossary CSV (the FTR `schema.table.column` / definition / domain / BIAN-FIBO shape) into a searchable, semantically-classified catalog whose fields carry **evidence + authority** — proving the Phase-0 kernel end-to-end on a real file, with **no join or grain promotion** (those are Phase 2/3).

**Architecture:** A new glossary reader + a deterministic sample-value parser + the merged Pass A batching engine become **evidence producers** that write per-field `field_evidence`; a field-policy registry + the Phase-0 `resolve_field_authority` resolve each field into a **display** value (projected to `graph_node`) and a **load-bearing** value (only when authority suffices, else unresolved); a blocker-based readiness diagnostic reports what's ready. Everything reuses the Phase-0 kernel — no parallel machinery.

**Tech Stack:** Python 3.12, psycopg raw SQL, pytest (live-PG `db`/`conn` fixture), `uv`. Frontend untouched (a readiness/provenance surface is a follow-on).

**Spec:** `docs/superpowers/specs/2026-07-11-evidence-authority-ingestion.md` (v4). Phase-1 = §14 "FTR semantic front door" + the **must-prove list**; §5.1 (FieldEvidenceRecord), §6 (resolver), §8 (concept advisory/operational), §9 (readiness), §5.1/§6.3 (staleness). §0 reuse map governs.

## The must-prove list (this plan's acceptance criteria — from spec §14)
1. FTR row → a **stable logical object**. 2. **Rich context** (BIAN/FIBO/definition) reaches the LLM. 3. Pass A writes **item-level field_evidence**. 4. Display projection **shows proposals**. 5. Operational projection **stays unresolved** where authority is insufficient. 6. **No safety/structural field becomes load-bearing from LLM evidence alone.** 7. Re-upload **stales changed proposals**. 8. Human-confirmed values **survive but revalidate**.

## Global Constraints
- **Reuse the Phase-0 kernel** (`overlay/field_authority.py` resolver + predicate + FieldPolicy + ConflictStrategy; `object_identity.py`; `safety_floor.py`; `field_decision.py`; `evidence.py` producer/strength/lifecycle enums). Do NOT reimplement resolution, authority, or the enums. No parallel event log.
- **Advisory + fail-soft.** A producer failure degrades the catalog, never aborts the upload's facts (the deterministic backbone — validate/brake/facts/drift/quarantine/`build_graph` — is unchanged).
- **No LLM-only load-bearing safety/structural fields** (must-prove #6): the field-policy registry gates concept-operational, sensitivity, logical_type, grain, join per §16. Sensitivity uses `safety_floor.SENSITIVITY_ORDER` as its `severity_order` (Phase-0 carry-forward #1).
- **No joins, no grain promotion** in Phase 1. `joins_to`/grain stay untouched (the governed-join seam is default-OFF; grain is Phase 2).
- **Additive & default-safe.** Glossary detection is additive in `_read_rows` — the existing CSV/Excel path is unchanged for non-glossary files. Migrations additive/forward-only.
- **Strength propagation** (spec §3.2): a derived value's strength ≤ the min of its inputs' strengths (taxonomy-derived from an `llm/proposed` concept is `taxonomy/proposed`, not `taxonomy/attested`).
- **Conventions:** raw SQL via `conn.execute`; `from __future__ import annotations`; frozen slotted dataclasses; `now` seam; ruff + mypy clean; TDD; commit per task; `uv run pytest <path> -q`.

## Scope note (Phase 1 is large — 9 tasks)
This spans the whole vertical the spec's must-prove requires. Tasks 1–3 are the producer/store foundation; 4–6 are evidence producers; 7 is resolution+projection (the payoff); 8–9 are readiness + re-upload. Execute in that order — 7 depends on 1–6. Consider a review checkpoint after Task 7 (the vertical is provable there; 8–9 harden it).

---

## File Structure
- Create `overlay/field_evidence.py` + migration `0983_field_evidence.sql` — the §5.1 per-field evidence store (Phase 0 only added the axis to `overlay_evidence`; this is the missing per-field proposal record). Foundational.
- Create `overlay/upload/upload_identity.py` — upload-context object identity (the uploaded rows ARE the catalog; EXACT unless duplicated → AMBIGUOUS). Reuses Phase-0 `classify_identity`.
- Create `overlay/upload/glossary_reader.py` — detect + map the glossary CSV shape; emit thin `CanonicalRow`s + a raw glossary sidecar; SOURCE-declared field_evidence.
- Create `overlay/upload/sample_parser.py` — deterministic sample-profile/value extraction → logical/semantic type; PARSER field_evidence.
- Modify `overlay/upload/enrich.py` — Pass A also writes concept field_evidence (LLM, item-level).
- Create `overlay/upload/taxonomy_evidence.py` — derive additivity/pit_role/sensitivity/leakage from a concept, strength-propagated; TAXONOMY field_evidence.
- Create `overlay/upload/field_policies.py` — the `FieldPolicy` registry per field (sensitivity wired to `SENSITIVITY_ORDER`).
- Create `overlay/upload/field_resolution.py` — resolve each (logical_ref, field) → display+load-bearing, emit `field_decision_event`, project display into `graph_node`.
- Create `overlay/upload/readiness.py` — blocker-based `FeatureReadiness` at catalog/table scope.
- Modify `overlay/upload/ingest.py` + `api/routes/uploads.py` — wire the glossary path (detection, producers, resolution) into the upload flow, advisory/fail-soft.

Locked interfaces (cross-task):
```python
# field_evidence.py  (§5.1)
@dataclass(frozen=True, slots=True)
class FieldEvidence:
    evidence_id: str; logical_ref: str; field_name: str
    proposed_value: object; proposed_value_hash: str
    producer: EvidenceProducer; strength: AssertionStrength; lifecycle: EvidenceLifecycle
    producer_ref: str; producer_item_ref: str | None; producer_configuration_hash: str | None
    evidence_spans: tuple[str, ...]; confidence_band: str | None
    source_snapshot_id: str; input_hash: str; created_at: object
def record_field_evidence(conn, *, logical_ref, field_name, proposed_value, producer, strength,
    producer_ref, source_snapshot_id, input_hash, producer_item_ref=None,
    producer_configuration_hash=None, evidence_spans=(), confidence_band=None, lifecycle=ACTIVE) -> str
def read_active_field_evidence(conn, logical_ref, field_name) -> list[FieldEvidence]   # lifecycle==ACTIVE only
def stale_field_evidence(conn, *, logical_ref, field_name, keep_input_hash) -> int      # STALE all but keep_input_hash
def to_view(ev: FieldEvidence) -> FieldEvidenceView                                     # for the resolver
# upload_identity.py
def upload_bindings(rows: list[CanonicalRow]) -> dict[str, ObjectBinding]   # keyed by logical_ref string
def logical_ref_str(source, schema, table, column) -> str
# glossary_reader.py
def is_glossary_csv(headers: list[str]) -> bool
def read_glossary(text: str, *, source: str) -> GlossaryUpload   # .rows (CanonicalRow) + .records (raw sidecar)
# sample_parser.py
@dataclass(frozen=True) class ParsedProfile: logical_type: str|None; semantic_type: str|None; sample_values: tuple[str,...]; diagnostic: str|None
def parse_sample_profile(description: str) -> ParsedProfile
# taxonomy_evidence.py
def derive_concept_evidence(concept: str, concept_strength: AssertionStrength) -> list[tuple[str, object, AssertionStrength]]  # (field_name, value, strength)
# field_policies.py
def policy_for(field_name: str) -> FieldPolicy | None
# field_resolution.py
def resolve_and_project(conn, *, source, logical_refs, now=None) -> None
# readiness.py
def compute_readiness(conn, *, source, scope: ReadinessScopeType, subset=None) -> FeatureReadiness
```

---

## Task 1: `field_evidence` store (§5.1 per-field proposal record)
**Files:** Create `overlay/field_evidence.py`, migration `0983_field_evidence.sql`; Test `tests/featuregen/overlay/test_field_evidence.py`.
**Why:** Every Phase-1 producer writes here; the resolver reads here. Phase 0 only added producer/strength to the metrics-oriented `overlay_evidence` (keyed by fact_key) — this is the per-(logical_ref, field) proposal store.

- [ ] **Step 1: Failing test** — write evidence for `(logical_ref, "concept")` from `llm/proposed`; `read_active_field_evidence` returns it as a `FieldEvidence`; `to_view` yields a `FieldEvidenceView(producer, strength, value, evidence_id)`; a second write with a different `input_hash` then `stale_field_evidence(keep_input_hash=<new>)` marks the old one STALE so `read_active_field_evidence` returns only the fresh one.
- [ ] **Step 2: Red.**
- [ ] **Step 3a: Migration** `0983_field_evidence.sql` — table with the §5.1 columns (see locked interface); `producer/strength/lifecycle text NOT NULL`; `proposed_value jsonb`, `evidence_spans jsonb NOT NULL DEFAULT '[]'`; index `(logical_ref, field_name, lifecycle)`. Verify slot free.
- [ ] **Step 3b: Implement** the record/read/stale/to_view helpers (mirror `overlay/field_decision.py` style: `mint_id("fev")`, `Jsonb`, `dict_row`, `now` seam). `read_active_field_evidence` filters `lifecycle='active'`. `stale_field_evidence` `UPDATE ... SET lifecycle='stale' WHERE logical_ref=%s AND field_name=%s AND lifecycle='active' AND input_hash <> %s`. `to_view` imports `FieldEvidenceView` from `overlay.field_authority` and maps producer/strength (as the enums) + value + evidence_id.
- [ ] **Step 4/5:** Green; commit `feat(overlay): field_evidence per-field proposal store (spec §5.1)`.

---

## Task 2: Upload-context object identity
**Files:** Create `overlay/upload/upload_identity.py`; Test `tests/featuregen/overlay/upload/test_upload_identity.py`.
**Why:** There is no live catalog adapter in the upload flow (Phase-0 finding), so `object_identity.resolve_object_identity` returns UNRESOLVED for uploaded columns. But the uploaded rows ARE the catalog — a column present exactly once is EXACT; a column appearing in conflicting rows is AMBIGUOUS. Evidence must not attach to AMBIGUOUS (spec §2).

- [ ] **Step 1: Failing test** — `upload_bindings` over rows with a unique `accounts.balance` → that logical_ref is `EXACT`/`may_attach`; two rows for the same `(source,table,column)` with different `type`/`definition` → `AMBIGUOUS`/not-attachable; `logical_ref_str` is stable and round-trippable.
- [ ] **Step 3: Implement** — `logical_ref_str(source, schema, table, column)` = a stable dotted/normalized string (schema defaults `"public"`). `upload_bindings(rows)`: group by `logical_ref_str`; for each, candidate count = distinct `_material(row)` (reuse the notion from `canonical._material` or a local material tuple of type+facts); 1 → EXACT, >1 → AMBIGUOUS; build `ObjectBinding` via Phase-0 `classify_identity` (reuse it — pass the candidate list). Import `classify_identity`/`ObjectBinding`/`may_attach` from `overlay.object_identity`.
- [ ] **Step 4/5:** Green; commit `feat(upload): upload-context object identity (uploaded rows are the catalog)`.

---

## Task 3: Glossary reader
**Files:** Create `overlay/upload/glossary_reader.py`; Modify `api/routes/uploads.py` (`_read_rows` detection); Test `tests/featuregen/overlay/upload/test_glossary_reader.py`.

- [ ] **Step 1: Failing test** (use a small inline FTR-shaped CSV fixture — do NOT read `~/Downloads`; embed representative rows): `is_glossary_csv(headers)` true for glossary headers (`schema.table.column`/`term_name`/`bian_level_1`), false for the canonical headers; `read_glossary(text, source=...)` on a 3-part row → a `CanonicalRow(table="COMP_...", column="CUST_NAME", type="", definition=<business definition>)`; the 2-part table row (`schema.table`) is NOT emitted as a column (captured as a table record); the `.records` sidecar carries `data_domain`, `bian_path`, `fibo_path`, `term_type`, `synonyms` for provenance.
- [ ] **Step 3a:** `is_glossary_csv(headers)` — normalized-header check for the glossary signature. `read_glossary` — parse rows; split `schema.table.column` (3-part → table=middle, column=last; 2-part → table record, skip as column); `CanonicalRow` with `definition` from `description_business_definition`, other declared fields blank (type unknown → filled by the parser/LLM later); collect a `GlossaryRecord` per row with the BIAN/FIBO/domain/synonym sidecar.
- [ ] **Step 3b:** In `uploads.py _read_rows`, for a `.csv`, peek headers; if `is_glossary_csv` → route to a glossary ingest path (returns the `GlossaryUpload`); else the existing `read_csv_rows`. Keep the change ADDITIVE — non-glossary CSVs are byte-for-byte unchanged. (The glossary path's downstream wiring is Task 9/ingest; here just detect + parse.)
- [ ] **Step 4/5:** Green; commit `feat(upload): glossary CSV reader (FQN split, definition, BIAN/FIBO sidecar)`.

**Note:** the glossary reader emits SOURCE evidence in Task 9's wiring (definition@source:attested; data_domain@source:proposed — declared-but-unverified per the source capability profile). Keep `read_glossary` pure here; evidence-writing is wired in Task 9.

---

## Task 4: Deterministic sample-value parser
**Files:** Create `overlay/upload/sample_parser.py`; Test `tests/featuregen/overlay/upload/test_sample_parser.py`.

- [ ] **Step 1: Failing test** — `parse_sample_profile("... sample profile is NUMERIC ... representative values such as 3708484836801; 3708446902413 ...")` → `logical_type` in {numeric_string|decimal|integer}, `semantic_type` `identifier` (fixed-length all-digit), `sample_values` extracted; `NUMERIC_SPECIAL` with `15:07:08` → time; `ALPHA_SPECIAL` with names → text; no profile phrase → `diagnostic` set, `logical_type=None` (never a silent wrong type).
- [ ] **Step 3:** Regex-extract the "sample profile is X" token + the "representative values such as ..." list; map NUMERIC→(numeric_string if all fixed-length digits else decimal), ALPHA→text, time-like→time; classify `semantic_type=identifier` when values are uniform-length digit strings. Emit a `diagnostic` when nothing parseable (Task 9 turns a diagnostic into a review note, never a wrong type). This produces PARSER evidence in Task 9 (logical_type@parser:supported).
- [ ] **Step 4/5:** Green; commit `feat(upload): deterministic sample-profile/value parser`.

---

## Task 5: Pass A concept evidence (extend the merged batching engine)
**Files:** Modify `overlay/upload/enrich.py`; Test `tests/featuregen/overlay/upload/test_pass_a_evidence.py`.
**VERIFY FIRST:** read the merged `enrich_concepts`/`run_batched`/`audited_batch_call` (they exist on `main`) to find the item-level hook — `run_batched` returns `{ref: value}` where `ref = content_hash(row)`; the per-item outcome (with the batch item ref) is in `BatchCallResult.outcomes`. You need, per resolved column: the concept value + the batch item ref (for `producer_item_ref`) + the vocabulary/prompt fingerprint (for `producer_configuration_hash`).

- [ ] **Step 1: Failing test** — with a scripted `FakeLLM` in batch mode, `enrich_concepts` (or a new `enrich_concepts_with_evidence`) writes a `field_evidence` row per classified column: `field_name="concept"`, `producer=llm`, `strength=proposed`, `producer_item_ref`=the batch ref, `producer_configuration_hash`=the vocab fingerprint, `proposed_value`=the concept; and returns the same `{content_hash: concept}` dict as today (build_graph unaffected).
- [ ] **Step 3:** Add evidence-writing to the concept batch path (guard: only when a `logical_ref` is attachable — pass the Task-2 `upload_bindings` in). Reuse `_vocab_fingerprint()` (already in enrich.py) for `producer_configuration_hash`. Keep single-mode + the return shape unchanged. Do NOT write evidence for UNCLASSIFIED-invalid (respect C3). Advisory/fail-soft: an evidence-write failure logs, never aborts enrichment.
- [ ] **Step 4/5:** Green (+ existing enrich tests stay green); commit `feat(enrich): Pass A writes item-level concept field_evidence (llm/proposed)`.

---

## Task 6: Taxonomy derivation (strength-propagated)
**Files:** Create `overlay/upload/taxonomy_evidence.py`; Test `tests/featuregen/overlay/upload/test_taxonomy_evidence.py`.
**VERIFY:** the concept registry (`overlay/upload/concepts.py`) `Concept` carries `additivity`, `pit_role`, `sensitivity`, `leakage_anchor`; `concept(name)` returns it.

- [ ] **Step 1: Failing test** — `derive_concept_evidence("monetary_stock", AssertionStrength.PROPOSED)` yields `(additivity, "semi_additive", PROPOSED)`, `(pit_role, ...)`, `(sensitivity, <floor>, PROPOSED)`, `(leakage_anchor, False, PROPOSED)` — every derived strength equals the input `PROPOSED` (NOT attested/confirmed), proving §3.2 propagation. `derive_concept_evidence("monetary_stock", AssertionStrength.CONFIRMED)` yields those fields at `CONFIRMED`.
- [ ] **Step 3:** Look up `concept(name)`; for each behavioral field, emit `(field_name, value, min(GOVERNED_baseline, input_strength))` — since the derivation RULE is governed but the strength is capped by the input concept's strength, the emitted strength = the input's strength (never higher). Sensitivity is emitted as a **floor** (Task 9 feeds it through `safety_floor.apply_sensitivity_floor`, not as an operational override). Unknown/UNCLASSIFIED concept → no derived evidence.
- [ ] **Step 4/5:** Green; commit `feat(upload): concept-registry taxonomy derivation with strength propagation (spec §3.2)`.

---

## Task 7: Field-policy registry + resolve & project (the payoff)
**Files:** Create `overlay/upload/field_policies.py`, `overlay/upload/field_resolution.py`; Modify `overlay/bootstrap.py` (register policies); Test `tests/featuregen/overlay/upload/test_field_resolution.py`.
**This is the load-bearing task** — it proves must-prove #4/#5/#6.

- [ ] **Step 1a: Field policies** — `policy_for(field_name)` returns a `FieldPolicy` (Phase-0 contract) per field:
  - `concept` (advisory): influence RECOMMENDATION; display on `llm/proposed`; operational rule requires source-attested/human-confirmed (advisory concept is NOT load-bearing on LLM alone — §8).
  - `definition`, `domain`, `feature_role`: RECOMMENDATION; LLM-proposed allowed.
  - `logical_type`: OPERATIONAL; operational rule `AnyOf(parser/supported, source/attested)`.
  - `sensitivity`: OPERATIONAL, `conflict_strategy=MOST_RESTRICTIVE`, `severity_order=safety_floor.SENSITIVITY_ORDER` (**carry-forward #1 wired here**); operational rule source/taxonomy/human (never llm-alone); the sensitivity floor from taxonomy feeds `apply_sensitivity_floor`.
  - `additivity`/`temporal_role`: OPERATIONAL; operational rule requires the concept CONFIRMED (taxonomy-derived-from-a-proposed-concept is `taxonomy/proposed` and thus does NOT meet the bar — §3.2 in action).
- [ ] **Step 1b: Failing test** for `resolve_and_project`: given seeded `field_evidence` for a column (`concept@llm:proposed`, `definition@source:attested`, `logical_type@parser:supported`, `sensitivity@taxonomy:proposed`): after `resolve_and_project`, `graph_node` shows the **display** concept/definition; the **load-bearing** `additivity` is unresolved (concept only proposed → §3.2 blocks); `sensitivity` operational is the floor (most-restrictive), never lowered by an llm proposal; a `field_decision_event` is written per field with both effective values.
- [ ] **Step 3:** `resolve_and_project(conn, source, logical_refs, now)`: for each (logical_ref, field with a policy): read active field_evidence → `to_view` list → `resolve_field_authority(views, policy, active_disqualifiers)`; emit a `field_decision_event` (Phase-0 `record_field_decision`) with the display+load-bearing hashes; UPDATE the corresponding `graph_node` column with the **display** value (concept/domain/definition/sensitivity), leaving load-bearing-unresolved fields NULL/unstamped. Sensitivity specifically runs through `safety_floor.apply_sensitivity_floor(floor=<taxonomy floor>, proposals=<source/human>, ...)`. Register `policy_for` in `bootstrap.register_overlay`.
- [ ] **Step 4/5:** Green; commit `feat(upload): field-policy registry + resolve-and-project (display vs load-bearing) (spec §4/§6/§8)`.

---

## Task 8: Scoped readiness diagnostics (Phase-0-deferred contract 8)
**Files:** Create `overlay/upload/readiness.py`; optional read endpoint in `api/routes/`; Test `tests/featuregen/overlay/upload/test_readiness.py`.

- [ ] **Step 1: Failing test** — after resolution, `compute_readiness(conn, source, scope=CATALOG)` returns `operational_status="blocked"` with `blocking_requirements` naming the unresolved structural/safety fields (e.g. grain missing, additivity unresolved), `review_requirements` for proposed-but-unconfirmed, `advisory_gaps` for low-confidence domain; `summary_scores` are display-only. A TABLE-scoped call subsets to one table.
- [ ] **Step 3:** Build `ReadinessRequirement`/`FeatureReadiness` (Phase-0 spec §9 contracts — define them here). Derive requirements from field_decision_events + the field policies: a field whose policy is OPERATIONAL and whose load-bearing value is unresolved → a blocking requirement (blocking=structural/safety, review=advisory). Gate is blocker-based; percentages are derived, never the gate. Scope subsets by table.
- [ ] **Step 4/5:** Green; commit `feat(upload): blocker-based scoped feature-readiness (spec §9)`.

---

## Task 9: Re-upload staleness + confirmation survival + full wiring
**Files:** Modify `overlay/upload/ingest.py` (glossary path wiring), `overlay/upload/glossary_reader.py`/`sample_parser.py`/`taxonomy_evidence.py` (evidence-writing hooks); Test `tests/featuregen/overlay/upload/test_glossary_reupload.py`.

- [ ] **Step 1: Failing tests** (must-prove #7/#8): (a) upload a glossary → re-upload with a CHANGED definition for one column → the old `definition@source` evidence for that column is STALE and the new one ACTIVE; unchanged columns' evidence is reused (same input_hash). (b) a human-confirmed sensitivity survives a re-upload of the same column, but if the column's type/definition MATERIALLY changed, it is flagged pending-revalidation (the `CONFIRMATION_PENDING_REVALIDATION` disqualifier fires) rather than silently carried.
- [ ] **Step 3a: Wire the producers** into the glossary ingest path (in `ingest.py`, guarded to the glossary upload): compute `upload_bindings`; for each attachable column, write SOURCE evidence (definition/domain from the reader), PARSER evidence (from `parse_sample_profile` over the description), run Pass A (concept evidence), then taxonomy-derived evidence; each `record_field_evidence` carries `source_snapshot_id` (the upload id) + `input_hash` (hash of the row's material). Then call `resolve_and_project` + `compute_readiness`. All advisory/fail-soft.
- [ ] **Step 3b: Staleness** — on re-upload, per (logical_ref, field), `stale_field_evidence(keep_input_hash=<current>)` so a changed input supersedes the prior proposal (lifecycle STALE); reused-identical inputs stay ACTIVE (skip re-writing). Human confirmations (a confirmed `field_decision_event` / a `HUMAN` evidence row) survive; if the column's material changed, mark the field pending-revalidation for the resolver's disqualifier.
- [ ] **Step 4:** Green + the full upload suite stays green (`uv run pytest tests/featuregen/overlay/ -q`).
- [ ] **Step 5: Commit** `feat(upload): glossary ingest wiring — producers, staleness, confirmation revalidation (spec §6.3)`.

---

## Self-Review
**Must-prove coverage:** #1 → Tasks 2+3 (upload identity + FQN split); #2 → Task 5 (BIAN/FIBO/definition in the Pass A input — VERIFY the enrich prompt carries the sidecar; if not, extend it in Task 5); #3 → Task 5; #4/#5/#6 → Task 7 (display vs load-bearing; §3.2 blocks additivity-from-proposed-concept; sensitivity never lowered by LLM); #7 → Task 9a; #8 → Task 9b.
**Reuse guardrails:** the resolver/predicate/policy/enums/safety-floor/decision-log are all Phase-0 — no reimplementation. `field_evidence` (Task 1) is the missing §5.1 store, distinct from `overlay_evidence` (metrics) and `field_decision_event` (decisions). No joins/grain promotion.
**Carry-forwards resolved:** sensitivity source-of-truth (SENSITIVITY_ORDER wired in Task 7's sensitivity policy). The upload-context adapter carry-forward is satisfied for identity by Task 2 (governed-join `propose_fact` dispatch remains Phase-3, still adapter-gated).
**Verify-before-build flags:** free migration slot on `main` (0983+); the merged `enrich_concepts`/`run_batched` item-level hook (Task 5); whether the Pass A prompt already carries rich glossary context or needs extending (Task 5, must-prove #2); `canonical._material` reuse for the upload-identity candidate signature (Task 2); the `graph_node` columns available to project into (Task 7 — concept/domain/definition/sensitivity exist; a display-vs-load-bearing distinction on the node may need a small stamp column, decide in Task 7).
**Type consistency:** `FieldEvidence`/`to_view`→`FieldEvidenceView` feed the Phase-0 resolver unchanged; producer/strength are the Phase-0 enums; `logical_ref` is the stable string from Task 2 used consistently by Tasks 1/5/6/7/9.

## Execution Handoff
Two options: **(1) Subagent-Driven (recommended)** — fresh subagent per task + two-stage review; **(2) Inline**. Given the size, a review checkpoint after **Task 7** (the vertical is provable there) is advised. Tasks 5, 7, and 9 carry the most integration judgment; read the merged Pass A + the Phase-0 kernel modules before them.
