# Phase-2 Slice 1 — ColumnMetadataView + Schema-Safe Binding + Field-Aware Egress — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Assemble one authority-aware `ColumnMetadataView` at ingest time — operational/declared types kept separate, parser facets reconciled, the table definition attached — and feed it to Pass B through a structured (non-string-delimited) roster and a field-aware sanitized egress projection, with a versioning seam so the changed Pass B request ships as v2.

**Architecture:** The view is an in-memory ingest-time assembly built from `CanonicalRow` + `GlossaryRecord` + the Pass A enrichment maps, attached only via validated bindings (`may_attach` + the table-schema-disagreement skip). It subsumes Phase-1's ad-hoc `records` map. `graph_node` remains a lossy projection (unchanged here). A field-aware egress projection sanitizes every outbound free-text field and bounds structural fields, emitting a `{path, sanitizer_version, state, removed_count}` audit record.

**Tech Stack:** Python 3.11, psycopg3, PostgreSQL, `jsonschema`. Interpreter `.venv/bin/python`.

## Global Constraints

- Branch off the Phase-1 branch tip (`phase1-llm-enrichment-hardening`, or its merged main once merged). Confirm the base with the user before starting.
- All subagent work on **Opus 4.8**.
- `operational_type` (`CanonicalRow.type`, stays `unknown`) and `declared_type` (`GlossaryRecord.declared_type`, a hint) are **two fields end-to-end** — never conflated into one `type` key.
- Binding is **single-schema-per-source** (FTR fence). The view keys by schema-preserving `logical_ref`; `(table, column)` normalization is valid only under that fence. State this scope; do not add multi-schema keying.
- **Metadata-only egress.** Every outbound free-text field passes the field-aware sanitizer (`sanitize_definition`); structural fields (column/table names, types, refs, enums) are allowlisted + bounded, **never** sample-stripped. Per-value bounds: default 200, `business_definition`/`table_definition` ≤ `MAX_DEFINITION_LEN` (600).
- Reuse existing machinery — do not reinvent: `may_attach` (`overlay/object_identity.py:124`), `classify_upload` (`overlay/upload/upload_identity.py:114`), the table-schema-disagreement skip (`ingest.py` `_ingest_glossary_evidence`, the `column_schemas` / `declared != {schema}` check), `reconcile_profile` + `ParsedProfile` (`sample_parser.py`), `sanitize_definition`/`redact_text` (`sanitize.py`), `bounded_definition`/`MAX_DEFINITION_LEN`/`content_hash` (`enrich.py`/`enrich_llm.py`), the egress guards (`enrich_llm.py` `_ITEM_META_ALLOWED`/`_COLUMN_PROFILE_KEYS`/`_item_egress_ok`/`_max_len_for`).
- **`flag-off byte-for-byte`:** with Pass B off, behavior is unchanged. The version seam defaults to `1`.
- **Verify line numbers before editing** — they shift across tasks; anchor on symbol names, not the line numbers quoted here.
- Run `.venv/bin/python -m pytest <targets> -q` after each task; `.venv/bin/ruff check <files>`.

---

### Task 1: Versioning seam for enrichment calls

**Files:**
- Modify: `src/featuregen/overlay/upload/enrich_llm.py` (`audited_structured_call`, `audited_batch_call`)
- Modify: `src/featuregen/overlay/upload/enrich_batch.py` (`run_batched` — pass versions through)
- Test: `tests/featuregen/overlay/upload/test_enrich_versioning.py`

**Interfaces:**
- Produces: `audited_structured_call(..., prompt_version: int = 1, schema_version: int = 1)` and `audited_batch_call(..., prompt_version: int = 1, schema_version: int = 1)`; `run_batched(..., prompt_version: int = 1, schema_version: int = 1)`. Every hardcoded `1` in `reg.schema_for(schema_id, 1)`, `LLMRequest(prompt_version=1, output_schema_version=1)`, `reg.validate(schema_id, 1, …)` becomes the parameter. Default `1` → byte-for-byte.

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/overlay/upload/test_enrich_versioning.py
"""The version seam: a caller can pin a schema/prompt version other than 1; default stays 1."""
from featuregen.overlay.upload import enrich_llm


def test_audited_batch_call_defaults_to_v1(monkeypatch, db):
    seen = {}
    real = enrich_llm.drive_structured_call

    def spy(client, req, validate):
        seen["prompt_version"] = req.prompt_version
        seen["schema_version"] = req.output_schema_version
        return real(client, req, validate)
    monkeypatch.setattr(enrich_llm, "drive_structured_call", spy)
    # a FakeLLM call through audited_batch_call with the existing concept schema; assert v1 by default
    # (construct the minimal batch call the concept path uses; see enrich_batch for the FakeLLM setup)
    ...
    assert seen["prompt_version"] == 1 and seen["schema_version"] == 1


def test_audited_batch_call_honors_explicit_version(monkeypatch, db):
    # register a v2 of a test schema, call with schema_version=2, assert reg.schema_for/validate use 2
    ...
```
(The implementer completes the FakeLLM wiring from `tests/featuregen/overlay/upload/test_enrich_batch.py` patterns; the two assertions above are the contract.)

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_enrich_versioning.py -q` → FAIL (no version param).

- [ ] **Step 3: Thread version params**

In `enrich_llm.py`, add `prompt_version: int = 1, schema_version: int = 1` to both `audited_structured_call` and `audited_batch_call`. Replace the four hardcoded `1`s in each: `reg.schema_for(schema_id, schema_version)`, `LLMRequest(..., prompt_version=prompt_version, ..., output_schema_version=schema_version, ...)`, `reg.validate(schema_id, schema_version, ...)`, and the self-register fallback re-fetch `reg.schema_for(schema_id, schema_version)`. In `enrich_batch.py` `run_batched`, add the same params and pass them into `audited_batch_call`.

- [ ] **Step 4: Run to verify pass + no regression**

Run: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_enrich_versioning.py tests/featuregen/overlay/upload/test_enrich_batch.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/enrich_llm.py src/featuregen/overlay/upload/enrich_batch.py tests/featuregen/overlay/upload/test_enrich_versioning.py
git commit -m "feat(enrich): version seam — explicit prompt/schema version params (default 1, byte-for-byte)"
```

---

### Task 2: Field-aware egress-sanitizer foundation

**Files:**
- Create: `src/featuregen/overlay/upload/feature_egress.py`
- Test: `tests/featuregen/overlay/upload/test_feature_egress.py`

**Interfaces:**
- Produces:
  - `FREE_TEXT = "free_text"`, `STRUCTURAL = "structural"`
  - `@dataclass(frozen=True) EgressAudit: path: str; sanitizer_version: str; state: str; removed_count: int`
  - `sanitize_free_text(path: str, value: str | None, *, limit: int) -> tuple[str, EgressAudit]` — uses `sanitize_definition`; on `state == "suspected_unhandled"` returns `("", audit)` (fail-closed).
  - `bound_structural(value: str | None, *, limit: int = 200) -> str` — allowlist-safe bound, **no** sample-strip.
  - `project_record(record: dict, field_kinds: dict[str, str], *, limits: dict[str, int] | None = None) -> tuple[dict, list[EgressAudit]]` — applies the right sanitizer per key; unknown keys are dropped (allowlist).

- [ ] **Step 1: Write the failing tests**

```python
# tests/featuregen/overlay/upload/test_feature_egress.py
from featuregen.overlay.upload.feature_egress import (
    FREE_TEXT, STRUCTURAL, sanitize_free_text, bound_structural, project_record)


def test_free_text_strips_sample_clause_and_audits():
    raw = "The customer's settlement account. Representative values such as ACC-11, ACC-22."
    clean, audit = sanitize_free_text("col.definition", raw, limit=600)
    assert "ACC-11" not in clean and "ACC-22" not in clean
    assert audit.state in ("stripped", "suspected_unhandled")
    assert audit.removed_count >= 1 and audit.sanitizer_version


def test_free_text_fail_closed_on_unhandled_marker():
    # a bare data-marker with no canonical clause → suspected_unhandled → cleared
    clean, audit = sanitize_free_text("col.definition", "sample values: OPN; CLS; PND", limit=600)
    assert clean == "" and audit.state == "suspected_unhandled"


def test_structural_is_not_sample_stripped():
    # a column name containing 'values such as'-like tokens must survive (structural, not free text)
    assert bound_structural("values_such_as_flag") == "values_such_as_flag"
    assert len(bound_structural("x" * 500, limit=200)) == 200


def test_project_record_applies_per_field_kind_and_drops_unknown():
    rec = {"column": "amount", "definition": "A fee. Values such as 1, 2.", "secret": "x"}
    kinds = {"column": STRUCTURAL, "definition": FREE_TEXT}
    clean, audits = project_record(rec, kinds, limits={"definition": 600})
    assert "secret" not in clean                 # unknown key dropped (allowlist)
    assert clean["column"] == "amount"
    assert "1, 2" not in clean["definition"]
    assert any(a.path == "definition" for a in audits)
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_feature_egress.py -q` → FAIL (module missing).

- [ ] **Step 3: Implement the module**

```python
# src/featuregen/overlay/upload/feature_egress.py
"""Field-aware egress sanitization for LLM payloads. Free-text fields (definitions, prose) are
sample-clause-stripped + PII-redacted via `sanitize_definition`; STRUCTURAL fields (names, refs,
types, enums, ids) are allowlisted + bounded but NEVER sample-stripped (stripping would corrupt a
column name). Every free-text field yields an audit record {path, sanitizer_version, state,
removed_count}. Fail-closed: an unhandled data-marker clears the value."""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.overlay.upload.sanitize import sanitize_definition

FREE_TEXT = "free_text"
STRUCTURAL = "structural"
_DEFAULT_LIMIT = 200


@dataclass(frozen=True, slots=True)
class EgressAudit:
    path: str
    sanitizer_version: str
    state: str            # "none" | "stripped" | "suspected_unhandled"
    removed_count: int


def sanitize_free_text(path: str, value: str | None, *, limit: int) -> tuple[str, EgressAudit]:
    r = sanitize_definition(value)
    clean = "" if r.state == "suspected_unhandled" else r.clean[:limit]
    return clean, EgressAudit(path, r.sanitizer_version, r.state, r.removed)


def bound_structural(value: str | None, *, limit: int = _DEFAULT_LIMIT) -> str:
    return (value or "")[:limit]


def project_record(record: dict, field_kinds: dict[str, str], *,
                   limits: dict[str, int] | None = None) -> tuple[dict, list[EgressAudit]]:
    limits = limits or {}
    out: dict = {}
    audits: list[EgressAudit] = []
    for key, kind in field_kinds.items():
        if key not in record or record[key] is None:
            continue
        lim = limits.get(key, _DEFAULT_LIMIT)
        if kind == FREE_TEXT:
            clean, audit = sanitize_free_text(key, str(record[key]), limit=lim)
            audits.append(audit)
            if clean:
                out[key] = clean
        else:
            out[key] = bound_structural(str(record[key]), limit=lim)
    return out, audits
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_feature_egress.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/feature_egress.py tests/featuregen/overlay/upload/test_feature_egress.py
git commit -m "feat(egress): field-aware sanitizer foundation (free-text strip + audit, structural bound)"
```

---

### Task 3: ColumnMetadataView + TableMetadataView + attachable builder

**Files:**
- Create: `src/featuregen/overlay/upload/column_view.py`
- Test: `tests/featuregen/overlay/upload/test_column_view.py`

**Interfaces:**
- Consumes: `CanonicalRow`, `GlossaryRecord`, `GlossaryUpload`, `reconcile_profile`/`ParsedProfile`, `may_attach`/`classify_upload`, the table-schema-disagreement rule, `normalize_ref`/`parse_ref`, `content_hash`, `sanitize_free_text` (Task 2).
- Produces:
  - `@dataclass(frozen=True) ColumnMetadataView` with fields: `source, schema, table, column, logical_ref, operational_type, declared_type, term_name, business_definition, domain, term_type, process_path, synonyms: tuple[str,...], bian_path, fibo_path, semantic_type: str|None, logical_representation: str|None, concept: str|None, drafted_definition: str|None, classified_domain: str|None`.
  - `@dataclass(frozen=True) TableMetadataView: source, schema, table, logical_ref, table_definition: str|None, term_name: str|None, columns: tuple[ColumnMetadataView,...]`.
  - `build_table_views(rows: list[CanonicalRow], *, glossary: GlossaryUpload | None, concepts: dict[str,str], definitions: dict[str,str], domains: dict[str,str]) -> dict[str, TableMetadataView]` (keyed by table name).

- [ ] **Step 1: Write the failing tests**

```python
# tests/featuregen/overlay/upload/test_column_view.py
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.glossary_reader import GlossaryRecord, GlossaryUpload
from featuregen.overlay.upload.column_view import build_table_views
from featuregen.overlay.upload.enrich import content_hash


def _row(table, col, typ="unknown", defn=""):
    return CanonicalRow(source="s", table=table, column=col, type=typ, definition=defn)


def _rec(ref, **kw):
    base = dict(logical_ref=ref, term_name="T", definition="A settled amount.", declared_type="double",
                term_type="measure", domain="Payments", schema="banking")
    base.update(kw)
    return GlossaryRecord(**base)


def test_view_keeps_operational_and_declared_separate():
    rows = [_row("txn", "fee")]
    g = GlossaryUpload(rows=rows, records=[_rec("s::banking.txn.fee")])
    views = build_table_views(rows, glossary=g, concepts={}, definitions={}, domains={})
    col = views["txn"].columns[0]
    assert col.operational_type == "unknown"     # CanonicalRow.type, unchanged
    assert col.declared_type == "double"          # a hint, separate


def test_table_definition_attached_from_is_table_record():
    rows = [_row("txn", "fee")]
    g = GlossaryUpload(rows=rows, records=[
        _rec("s::banking.txn.fee"),
        _rec("s::banking.txn", is_table=True, definition="Customer transaction events.")])
    views = build_table_views(rows, glossary=g, concepts={}, definitions={}, domains={})
    assert views["txn"].table_definition == "Customer transaction events."


def test_reconciled_facet_is_withheld_for_contradiction():
    # a timestamp declared type with an identifier parser facet must be reconciled AWAY in the view
    rows = [_row("txn", "event_ts")]
    g = GlossaryUpload(rows=rows, records=[
        _rec("s::banking.txn.event_ts", declared_type="timestamp",
             semantic_type="identifier", logical_representation="numeric_string")])
    views = build_table_views(rows, glossary=g, concepts={}, definitions={}, domains={})
    col = views["txn"].columns[0]
    assert col.semantic_type is None and col.logical_representation is None


def test_table_term_schema_mismatch_does_not_attach_definition():
    # columns say schema 'banking'; the table term says 'risk' -> columns win, table def withheld
    rows = [_row("txn", "fee")]
    g = GlossaryUpload(rows=rows, records=[
        _rec("s::banking.txn.fee", schema="banking"),
        _rec("s::risk.txn", is_table=True, schema="risk", definition="wrong schema")])
    views = build_table_views(rows, glossary=g, concepts={}, definitions={}, domains={})
    assert views["txn"].table_definition is None


def test_definition_carries_sanitized_and_bounded():
    rows = [_row("txn", "fee")]
    g = GlossaryUpload(rows=rows, records=[
        _rec("s::banking.txn.fee", definition="A fee. Values such as 1.23, 4.56.")])
    views = build_table_views(rows, glossary=g, concepts={}, definitions={}, domains={})
    assert "1.23" not in (views["txn"].columns[0].business_definition or "")
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_column_view.py -q` → FAIL.

- [ ] **Step 3: Implement `column_view.py`**

Implement the two dataclasses and `build_table_views`. Assembly rules (reuse existing machinery — do not reinvent):
1. Index `glossary.records` by normalized `(table, column)` for columns and by normalized `table` for `is_table=True` terms (via `parse_ref` — a 4-tuple `(source, schema, table, column)`; skip `ValueError`).
2. Build `column_schemas: dict[table, set[str]]` from the column records' `schema` (mirror `ingest.py` `_ingest_glossary_evidence`). For a table term, attach its definition **only if** `column_schemas.get(table) in (None, {term.schema})` — the table-schema-disagreement skip. Otherwise `table_definition=None` and log.
3. For each `CanonicalRow`, look up its record; set `operational_type = row.type`, `declared_type = rec.declared_type` (separate).
4. **Reconcile facets:** build `ParsedProfile(logical_representation=rec.logical_representation or None, semantic_type=rec.semantic_type or None, computational_type=None, sample_values=(), diagnostic=None)` then `reconcile_profile(..., declared_type=rec.declared_type, column=column)`; take the reconciled `.logical_representation`/`.semantic_type` (None when withheld).
5. `business_definition`: from `rec.definition` (else the Pass A draft `definitions.get(content_hash(row))`), passed through `sanitize_free_text(..., limit=600)`; drop if the sanitizer clears it.
6. `concept = concepts.get(content_hash(row))`; `drafted_definition = definitions.get(content_hash(row))`; `classified_domain = domains.get(row.table)`.
7. Apply `may_attach`: compute the upload bindings via `classify_upload(rows)` and skip a column whose binding is missing / not attachable (do not silently attach an AMBIGUOUS/UNRESOLVED sidecar). Key each view by `normalize_ref(source, rec.schema or None, table, column)` (schema-preserving).

Show the full implementation in the commit; the tests above pin the observable contract.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_column_view.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/column_view.py tests/featuregen/overlay/upload/test_column_view.py
git commit -m "feat(view): ColumnMetadataView/TableMetadataView + attachable, schema-reconciled, facet-reconciled builder"
```

---

### Task 4: Structured dual-type roster + Pass B descriptor from the view (schema/prompt v2)

**Files:**
- Modify: `src/featuregen/overlay/upload/table_synth.py` (`_descriptor`, `assemble_table_items`, `_synthesize_wide_tables` roster, phase-2 item, instructions)
- Modify: `src/featuregen/overlay/upload/enrich_llm.py` (`_SCHEMAS` add `*_v2` with `operational_type`/`declared_type` + `table_definition`; `_COLUMN_PROFILE_KEYS`, `_ITEM_META_ALLOWED`, `_column_profile_ok` for a structured roster entry)
- Test: `tests/featuregen/overlay/upload/test_passb_roster.py`

**Interfaces:**
- Consumes: `TableMetadataView`/`ColumnMetadataView` (Task 3), the version seam (Task 1), `sanitize_free_text` (Task 2).
- Produces: `assemble_table_items(views: dict[str, TableMetadataView]) -> list[BatchItem]`; each column profile carries `{column, operational_type, declared_type, concept?, business_definition?, term_type?, domain?, process_path?, semantic_type?}`; the item metadata carries a sanitized `table_definition`; the wide roster is a list of `{column, operational_type, declared_type}` objects (NOT a `name:type` string). Pass B ships schema/prompt **v2** via the version seam.

Key rules:
- **Do not use a `column:operational/declared` string** — column names may contain `:`/`/`. The wide roster entry is a structured object. Add a bounded structured-roster shape to the egress allowlist (mirror how `column_roster`/`chunk_summaries` were added: an allowlisted key + a per-entry validator + a `_MAX_*` bound).
- **Thread `table_definition` through the wide phase-2 item** (`_synthesize_wide_tables` phase-2 build currently carries only `{table, chunk_summaries, column_roster}` — add `table_definition`). Adding it to the initial item is not enough.
- Bump the two synth schemas to v2 (`overlay_table_synth_batch`/`overlay_table_synth` — and the summary schema if its item shape changes) and pass `schema_version=2`/`prompt_version=2` from `synthesize_tables`/`_run_synthesis`/`_synthesize_wide_tables` via `run_batched`. Update `_INSTRUCTION`/`_SUMMARY_INSTRUCTION`/`_SYNTH_WIDE_INSTRUCTION` to describe the operational-vs-declared distinction (declared type is a hint).

- [ ] **Step 1: Write the failing tests**

```python
# tests/featuregen/overlay/upload/test_passb_roster.py
from featuregen.overlay.upload.column_view import build_table_views
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.glossary_reader import GlossaryRecord, GlossaryUpload
from featuregen.overlay.upload.table_synth import assemble_table_items
from featuregen.overlay.upload.enrich_llm import _item_egress_ok


def _views():
    rows = [CanonicalRow(source="s", table="txn", column="fee:amt", type="unknown", definition="")]
    g = GlossaryUpload(rows=rows, records=[GlossaryRecord(
        logical_ref="s::banking.txn.fee:amt", term_name="Fee", definition="A fee.",
        declared_type="double", term_type="measure", domain="Payments", schema="banking")])
    return build_table_views(rows, glossary=g, concepts={}, definitions={}, domains={})


def test_descriptor_has_separate_types_not_conflated():
    items = assemble_table_items(_views())
    prof = items[0].metadata["column_profiles"][0]
    assert prof["operational_type"] == "unknown" and prof["declared_type"] == "double"
    assert "type" not in prof                       # the conflated key is gone
    assert _item_egress_ok(items[0].metadata) is True


def test_table_definition_in_item_metadata_when_present():
    rows = [CanonicalRow(source="s", table="txn", column="fee", type="unknown", definition="")]
    g = GlossaryUpload(rows=rows, records=[
        GlossaryRecord(logical_ref="s::banking.txn.fee", term_name="Fee", definition="A fee.",
                       declared_type="double", schema="banking"),
        GlossaryRecord(logical_ref="s::banking.txn", term_name="Txn", definition="Events.",
                       is_table=True, schema="banking")])
    views = build_table_views(rows, glossary=g, concepts={}, definitions={}, domains={})
    item = assemble_table_items(views)[0]
    assert item.metadata["table_definition"] == "Events."


def test_wide_roster_entry_is_structured_not_delimited():
    # a column name containing ':' must round-trip intact in the roster
    from featuregen.overlay.upload.table_synth import _roster_entry   # helper introduced in this task
    entry = _roster_entry(_views()["txn"].columns[0])
    assert entry["column"] == "fee:amt"
    assert entry["operational_type"] == "unknown" and entry["declared_type"] == "double"
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_passb_roster.py -q` → FAIL.

- [ ] **Step 3: Implement the roster + descriptor + schema v2**

Rewrite `_descriptor` to take a `ColumnMetadataView` and emit the separate-type profile (sanitize `business_definition` via `sanitize_free_text`/already-sanitized-in-view). Change `assemble_table_items` to take `dict[str, TableMetadataView]` and set `metadata={"table", "column_profiles", "table_definition"?}`. Add `_roster_entry(view) -> {column, operational_type, declared_type}` and use a list of these in `_synthesize_wide_tables`; add `table_definition` to the phase-2 item. Add the `overlay_table_synth*` v2 schemas + register them; extend `_COLUMN_PROFILE_KEYS` (add `operational_type`,`declared_type`; the roster entry gets its own allowlisted key + `_roster_entry_ok` validator + `_MAX_ROSTER`), `_ITEM_META_ALLOWED` (add `table_definition`, the structured roster key), and `_max_len_for` (`table_definition` → 600). Pass `prompt_version=2, schema_version=2` from the synth drivers.

- [ ] **Step 4: Run to verify pass + Pass B regression**

Run: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_passb_roster.py tests/featuregen/overlay/upload/test_table_synth*.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/table_synth.py src/featuregen/overlay/upload/enrich_llm.py tests/featuregen/overlay/upload/test_passb_roster.py
git commit -m "feat(passb): structured dual-type roster + table_definition + schema/prompt v2 (operational vs declared)"
```

---

### Task 5: Wire the view into ingest's Pass B path

**Files:**
- Modify: `src/featuregen/overlay/upload/ingest.py` (Pass B block — build views via `build_table_views`, pass to `assemble_table_items`; retire the ad-hoc `records` map)
- Test: `tests/featuregen/overlay/upload/test_passb_view_wiring.py`

**Interfaces:**
- Consumes: `build_table_views` (Task 3), `assemble_table_items(views)` (Task 4).
- Produces: the live Pass B call assembles items from `TableMetadataView`s built from `vr.good` + `glossary` + `concepts`/`definitions`/`domains`.

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/overlay/upload/test_passb_view_wiring.py
# Ingest the synthetic FTR fixture (Phase-1 conftest `synthetic_ftr_upload`); a request-capturing
# FakeLLM records the Pass B request inputs. Assert the captured column_profiles carry BOTH
# operational_type and declared_type, and that the item metadata carries table_definition.
def test_passb_receives_view_shaped_input(db, synthetic_ftr_upload_capturing):
    cap = synthetic_ftr_upload_capturing(db, source="ftr_view")
    passb_inputs = cap.captured_for("table_synth")
    prof = passb_inputs[0]["column_profiles"][0]
    assert "operational_type" in prof and "declared_type" in prof
```
(Extend the Phase-1 capturing FakeLLM fixture to expose `captured_for(task)`.)

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_passb_view_wiring.py -q` → FAIL.

- [ ] **Step 3: Replace the records-map build with view assembly**

In `ingest.py` Pass B block (currently builds `records: dict[(t,c), GlossaryRecord]` then `assemble_table_items(vr.good, concepts=…, definitions=…, records=records)`), replace with:
```python
                views = build_table_views(vr.good, glossary=glossary, concepts=concepts,
                                          definitions=definitions, domains=domains)
                items = assemble_table_items(views)
```
Add the import. Keep the surrounding savepoint/`columns_by_table`/`synthesize_tables` wiring unchanged. Confirm `domains` is in scope at this point (it is used by `build_graph`); if not, pass `{}`.

- [ ] **Step 4: Run to verify pass + full upload regression**

Run: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/ -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/ingest.py tests/featuregen/overlay/upload/test_passb_view_wiring.py
git commit -m "feat(passb): assemble Pass B input from ColumnMetadataView (retire the ad-hoc records map)"
```

---

### Task 6: Integration + egress fail-closed on the synthetic FTR fixture

**Files:**
- Modify: `tests/featuregen/overlay/upload/test_ftr_phase1_acceptance.py` (extend) or a new `test_slice1_acceptance.py`

**Interfaces:** consumes the Phase-1 synthetic FTR fixture.

- [ ] **Step 1: Write the acceptance assertions**

Assert on the synthetic fixture: every Pass B column profile has `operational_type == "unknown"` and a non-empty `declared_type`; the table item carries `table_definition`; the timestamp/double column whose parser facet was reconciled away carries no `semantic_type` in the view; a planted sample token in a definition is absent from the captured Pass B request input AND an `EgressAudit` was produced with `state`/`removed_count`; a column name containing `:` round-trips intact through the roster.

- [ ] **Step 2: Run**

Run: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_slice1_acceptance.py -q` → PASS.

- [ ] **Step 3: Full suite + commit**

Run: `.venv/bin/python -m pytest -q 2>&1 | tail -8` (green). Commit.

```bash
git add tests/featuregen/overlay/upload/test_slice1_acceptance.py
git commit -m "test(view): slice-1 integration — view→Pass B, separate types, reconciled facets, egress fail-closed"
```

---

## Self-Review

**Spec coverage:** version seam → Task 1; field-aware egress (§C) → Task 2 + used in 3/4/6; ColumnMetadataView + attachable + schema-reconcile + reconciled facets → Task 3; structured dual-type roster + table_definition + schema/prompt v2 → Task 4; live wiring → Task 5; integration + egress fail-closed → Task 6.
**Placeholder scan:** Task 1 Step 1 and Task 3 Step 3 describe FakeLLM/assembly the implementer completes against existing patterns (the observable contract is pinned by explicit assertions) — flagged, not silent. All other code steps carry code.
**Type consistency:** `build_table_views` (Task 3) → `assemble_table_items(views)` (Task 4) → ingest wiring (Task 5). `sanitize_free_text`/`EgressAudit` (Task 2) reused in 3/4/6. `_roster_entry` introduced in Task 4 and tested there.
