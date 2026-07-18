# Phase-2 Slice 1 — ColumnMetadataView + Schema-Safe Binding + Field-Aware Egress — Implementation Plan (rev. 2)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development, task-by-task. Steps use checkbox (`- [ ]`) syntax.
> **rev. 2** incorporates a 17-finding review; Slice-1 items `[F1]`–`[F8]`,`[F16]`,`[F17]` addressed inline.

**Goal:** Assemble one authority-aware `ColumnMetadataView` at ingest time (operational/declared types separate, parser facets reconciled, table definition attached), attach the glossary sidecar **only** through the ingest's existing validated bindings, feed Pass B a structured dual-type roster, and route every free-text egress field through a **field-aware** sanitizer whose audit reaches `llm_call.input_redaction`.

**Architecture:** The view is an in-memory assembly built from `CanonicalRow` (always included) + the *optional* `GlossaryRecord` sidecar (attached only when `may_attach` holds on the ingest's raw-row binding). It subsumes Phase-1's ad-hoc `records` map. `graph_node` stays a lossy projection (unchanged). Free-text egress is sanitized at the **existing** redaction boundary (`_redact_free_text_meta`), made field-aware and audit-producing.

**Tech Stack:** Python 3.11, psycopg3, PostgreSQL, `jsonschema`. Interpreter `.venv/bin/python`.

## Global Constraints

- Branch off the Phase-1 branch tip (`phase1-llm-enrichment-hardening`). Confirm the base with the user.
- All subagent work on **Opus 4.8**.
- `operational_type` (`CanonicalRow.type`, stays `unknown`) and `declared_type` (`GlossaryRecord.declared_type`, a hint) are **two fields end-to-end** — never one `type` key.
- **Binding:** single-schema-per-source (FTR fence). Derive `source/schema/table/column` from `parse_ref(rec.logical_ref)` (a 4-tuple; `rec.schema` is only a consistency check — the generic reader does not populate it) `[F5]`. The outer builder result is an **FTR-only convenience index keyed by table name**; each `ColumnMetadataView` carries its own schema-preserving `logical_ref` `[F6]`.
- **Attachment `[F2]`:** the CanonicalRow column is **always** in Pass B. `may_attach` gates only whether the glossary **sidecar metadata** attaches. Use the **existing** `bindings` map ingest computes from the RAW rows (`ingest.py:1361`, `classify_upload(identified)`) — never recompute `classify_upload(vr.good)` (vr.good is deduped and would yield a weaker binding).
- **Fail-soft `[F4]`:** `concepts`/`definitions`/`domains` may be `None` (each Pass A stage fails independently) → normalize to `{}`.
- **Domain precedence `[F8]`:** the Pass B profile `domain` = curated glossary `domain` first, Pass-A `classified_domain` as fallback.
- **Egress `[F1]`,`[F7]`:** every free-text field is sanitized at `_redact_free_text_meta` by **field kind** — `definition` keys via `sanitize_definition` (sample-clause strip + PII), `prose` keys via `redact_free_text` (PII only); an unknown kind is a hard error (fail-closed), values must be scalars. Structural fields (names/types/refs) are allowlisted + bounded, never stripped. The audit `{path, sanitizer_version, state, removed_count}` (+ the existing PII spans) reaches `llm_call.input_redaction`.
- **Verify line numbers before editing** — anchor on symbol names.
- **Test commands:** run pytest directly (never `pytest … | tail` — the pipe reports `tail`'s exit code and hides failures) `[F16]`. For a tail summary use `set -o pipefail; pytest … | tail`.

---

### Task 1: Versioning seam for enrichment calls

**Files:** Modify `enrich_llm.py` (`audited_structured_call`, `audited_batch_call`), `enrich_batch.py` (`run_batched`). Test `tests/featuregen/overlay/upload/test_enrich_versioning.py`.

**Interfaces:** `audited_structured_call`/`audited_batch_call`/`run_batched` gain `prompt_version: int = 1, schema_version: int = 1`; every hardcoded `1` in `reg.schema_for(schema_id, 1)`, `LLMRequest(prompt_version=1, output_schema_version=1)`, `reg.validate(schema_id, 1, …)` uses the param. Default `1` → byte-for-byte.

- [ ] **Step 1: Write the failing test (concrete, no placeholders)**

```python
# tests/featuregen/overlay/upload/test_enrich_versioning.py
"""The version seam: audited_batch_call pins the request's prompt/schema version; default is 1."""
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload import enrich_llm
from featuregen.overlay.upload.enrich_batch import BatchItem


def _capture_versions(monkeypatch):
    seen = {}
    real = enrich_llm.drive_structured_call

    def spy(client, req, validate):
        seen["prompt_version"] = req.prompt_version
        seen["schema_version"] = req.output_schema_version
        return real(client, req, validate)
    monkeypatch.setattr(enrich_llm, "drive_structured_call", spy)
    return seen


def test_audited_batch_call_defaults_to_v1(db, monkeypatch):
    seen = _capture_versions(monkeypatch)
    client = FakeLLM(script={"overlay.enrich.concept": FakeResponse(output={"results": []})})
    enrich_llm.audited_batch_call(
        db, client, task="overlay.enrich.concept", prompt_id="overlay_concept_v1",
        schema_id="overlay_concept_batch", shared_metadata={}, items=[], out_key="concept",
        instruction="x", accept=lambda raw, ref: (raw, "valid"), ref_aware=True)
    assert seen["prompt_version"] == 1 and seen["schema_version"] == 1


def test_audited_batch_call_honors_explicit_version(db, monkeypatch):
    seen = _capture_versions(monkeypatch)
    client = FakeLLM(script={"overlay.enrich.concept": FakeResponse(output={"results": []})})
    enrich_llm.audited_batch_call(
        db, client, task="overlay.enrich.concept", prompt_id="overlay_concept_v1",
        schema_id="overlay_concept_batch", shared_metadata={}, items=[], out_key="concept",
        instruction="x", accept=lambda raw, ref: (raw, "valid"), ref_aware=True,
        prompt_version=3, schema_version=2)
    assert seen["prompt_version"] == 3 and seen["schema_version"] == 2
```
(If an empty `items` list short-circuits before `drive_structured_call`, pass one trivial `BatchItem(ref="t", metadata={"table": "t", "column_profiles": []})` and script a matching `results` entry.)

- [ ] **Step 2: Run — expect FAIL** `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_enrich_versioning.py -q`
- [ ] **Step 3: Thread the params** through both call builders + `run_batched` (replace the four hardcoded `1`s in each with `schema_version`/`prompt_version`; the self-register re-fetch too).
- [ ] **Step 4: Run — expect PASS** `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_enrich_versioning.py tests/featuregen/overlay/upload/test_enrich_batch.py -q`
- [ ] **Step 5: Commit** `feat(enrich): version seam — explicit prompt/schema version params (default 1)`

---

### Task 2: Field-aware egress at the redaction boundary + audit into `llm_call` `[F1]`,`[F7]`

**Files:** Modify `enrich_llm.py` (`_FREE_TEXT_META_KEYS` split; `_redact_free_text_meta` field-aware + sample-strip audit; `_ITEM_META_ALLOWED` add `table_definition`; the two `input_redaction=` sites carry the sample-strip audit). Test `tests/featuregen/overlay/upload/test_egress_field_aware.py`, extend `tests/featuregen/overlay/upload/test_enrich_llm.py`.

**Interfaces:**
- `_DEFINITION_META_KEYS = frozenset({"business_definition", "table_definition"})`, `_PROSE_META_KEYS = frozenset({"term_name", "synonyms", "data_domain", "bian_path", "fibo_path"})`. `_FREE_TEXT_META_KEYS = _DEFINITION_META_KEYS | _PROSE_META_KEYS`.
- `_redact_free_text_meta(metadata) -> tuple[dict | None, list[dict], list[dict], str | None]` — now returns `(redacted, pii_spans, sample_audits, version)` where `sample_audits` is a list of `{path, sanitizer_version, state, removed_count}`. Definition keys (top-level + `column_profiles[].business_definition` + `table_definition`) go through `sanitize_definition` (fail-closed on `state=="suspected_unhandled"` → returns `None`); prose keys through `redact_free_text`. An unrecognized free-text key kind raises `ValueError` (fail-closed) `[F7]`.
- `input_redaction={"redacted_spans": pii_spans, "sample_strip": sample_audits}` at both call sites; `_record_llm_call_durable` persists it unchanged.

- [ ] **Step 1: Write the failing tests (assert BOTH the request AND the persisted audit)**

```python
# tests/featuregen/overlay/upload/test_egress_field_aware.py
from featuregen.overlay.upload.enrich_llm import _redact_free_text_meta, _FREE_TEXT_META_KEYS


def test_table_definition_is_a_covered_definition_key():
    assert "table_definition" in _FREE_TEXT_META_KEYS


def test_definition_key_sample_clause_stripped_with_audit():
    meta = {"table": "txn",
            "table_definition": "Txn events. Representative values such as A1, B2.",
            "column_profiles": [{"column": "amt",
                                 "business_definition": "A fee. Values such as 1.23, 4.56."}]}
    out, pii_spans, sample_audits, version = _redact_free_text_meta(meta)
    assert out is not None
    assert "A1" not in out["table_definition"] and "1.23" not in out["column_profiles"][0][
        "business_definition"]
    paths = {a["path"] for a in sample_audits}
    assert "table_definition" in paths and "column_profiles.business_definition" in paths
    assert all({"path", "sanitizer_version", "state", "removed_count"} <= a.keys()
               for a in sample_audits)


def test_prose_key_is_pii_redacted_not_sample_stripped():
    # a term name that merely contains 'values such as'-shaped words must survive (prose, not stripped)
    meta = {"table": "t", "term_name": "Values Such As Flag"}
    out, _pii, sample_audits, _v = _redact_free_text_meta(meta)
    assert out["term_name"] == "Values Such As Flag"
    assert all(a["path"] != "term_name" for a in sample_audits)   # prose keys emit no sample audit


def test_unhandled_marker_fails_closed():
    meta = {"table": "t", "table_definition": "sample values: OPN; CLS; PND"}
    out, _pii, _sa, _v = _redact_free_text_meta(meta)
    assert out is None            # suspected_unhandled -> the caller must not egress the item
```

```python
# add to tests/featuregen/overlay/upload/test_enrich_llm.py — the audit must reach llm_call
def test_llm_call_records_sample_strip_audit(db):
    """A Pass B batch with a sample-bearing definition egresses clean AND persists the sample-strip
    audit in llm_call.input_redaction."""
    from featuregen.intake.llm import FakeLLM, FakeResponse
    from featuregen.overlay.upload import enrich_llm
    from featuregen.overlay.upload.enrich_batch import BatchItem
    item = BatchItem(ref="txn", metadata={"table": "txn", "table_definition":
        "Txn events. Values such as SECRET1.", "column_profiles": []})
    client = FakeLLM(script={"overlay.table_synth":
        FakeResponse(output={"results": [{"ref": "txn", "synthesis": {"grain_columns": []}}]})})
    enrich_llm.audited_batch_call(db, client, task="overlay.table_synth",
        prompt_id="overlay_table_synth_v1", schema_id="overlay_table_synth_batch",
        shared_metadata={}, items=[item], out_key="synthesis",
        instruction="x", accept=lambda raw, ref: (raw, "valid"), ref_aware=True)
    row = db.execute("SELECT redacted_input, input_redaction FROM llm_call "
                     "ORDER BY created_at DESC LIMIT 1").fetchone()
    blob = str(row["redacted_input"] if isinstance(row, dict) else row[0])
    assert "SECRET1" not in blob
    ir = row["input_redaction"] if isinstance(row, dict) else row[1]
    assert any(a["path"] == "table_definition" for a in ir["sample_strip"])
```

- [ ] **Step 2: Run — expect FAIL** `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_egress_field_aware.py -q`
- [ ] **Step 3: Implement** — split the key sets; rewrite `_redact_free_text_meta` to a field-aware `_one(text, key, kind)` that dispatches `definition`→`sanitize_definition` (append a `{path, sanitizer_version, state, removed_count}` audit; return `None` on `suspected_unhandled`) vs `prose`→`redact_free_text` (append PII spans); recurse `column_profiles[].business_definition` as `definition`; cover top-level `table_definition`; raise `ValueError` for a key whose kind is unknown. Add `table_definition` to `_ITEM_META_ALLOWED`. Update both `audited_*` call sites to unpack the 4-tuple and set `input_redaction={"redacted_spans": pii_spans, "sample_strip": sample_audits}`.
- [ ] **Step 4: Run — expect PASS** `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_egress_field_aware.py tests/featuregen/overlay/upload/test_enrich_llm.py -q`
- [ ] **Step 5: Commit** `feat(egress): field-aware redaction (definition sample-strip vs prose PII) + sample-strip audit into llm_call.input_redaction`

---

### Task 3: ColumnMetadataView + attachable builder (uses the ingest binding map)

**Files:** Create `column_view.py`. Test `tests/featuregen/overlay/upload/test_column_view.py`.

**Interfaces:**
- `@dataclass(frozen=True) ColumnMetadataView`: `source, schema, table, column, logical_ref, operational_type, declared_type, term_name, business_definition, domain, term_type, process_path, synonyms: tuple[str,...], bian_path, fibo_path, semantic_type: str|None, logical_representation: str|None, concept: str|None, drafted_definition: str|None, classified_domain: str|None, sidecar_attached: bool`.
- `@dataclass(frozen=True) TableMetadataView: source, schema, table, logical_ref, table_definition: str|None, term_name: str|None, columns: tuple[ColumnMetadataView,...]`.
- `build_table_views(rows, *, glossary: GlossaryUpload | None, bindings: dict[str, ObjectBinding] | None, concepts, definitions, domains) -> dict[str, TableMetadataView]` (FTR convenience index keyed by table name). `concepts`/`definitions`/`domains` normalized to `{}`.

- [ ] **Step 1: Write the failing tests (concrete)**

```python
# tests/featuregen/overlay/upload/test_column_view.py
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.glossary_reader import GlossaryRecord, GlossaryUpload
from featuregen.overlay.upload.column_view import build_table_views


def _row(t, c, typ="unknown", defn=""):
    return CanonicalRow(source="s", table=t, column=c, type=typ, definition=defn)


def _rec(ref, **kw):
    base = dict(logical_ref=ref, term_name="T", definition="A settled amount.",
                declared_type="double", term_type="measure", domain="Payments")
    base.update(kw)
    return GlossaryRecord(**base)


def test_types_separate_and_domain_precedence():
    rows = [_row("txn", "fee")]
    g = GlossaryUpload(rows=rows, records=[_rec("s::banking.txn.fee", domain="Payments")])
    v = build_table_views(rows, glossary=g, bindings=None,
                          concepts={}, definitions={}, domains={"txn": "GeneratedDomain"})
    col = v["txn"].columns[0]
    assert col.operational_type == "unknown" and col.declared_type == "double"
    assert col.domain == "Payments"                      # curated glossary domain wins


def test_domain_falls_back_to_pass_a_when_no_glossary():
    rows = [_row("txn", "fee")]
    v = build_table_views(rows, glossary=None, bindings=None,
                          concepts={}, definitions={}, domains={"txn": "GeneratedDomain"})
    assert v["txn"].columns[0].domain == "GeneratedDomain"   # Pass-A fallback


def test_technical_upload_fallback_blank_sidecar():
    rows = [_row("txn", "id", typ="unknown")]
    v = build_table_views(rows, glossary=None, bindings=None,
                          concepts={}, definitions={}, domains={})
    col = v["txn"].columns[0]
    assert col.declared_type == "" and col.term_name == "" and col.sidecar_attached is False
    assert col.operational_type == "unknown"


def test_column_kept_but_sidecar_omitted_when_not_attachable():
    from featuregen.overlay.object_identity import ObjectBinding, ObjectIdentityStatus
    rows = [_row("txn", "fee")]
    g = GlossaryUpload(rows=rows, records=[_rec("s::banking.txn.fee")])
    # an AMBIGUOUS binding -> may_attach False -> the COLUMN stays, the sidecar is dropped
    key = "s::public.txn.fee"
    bindings = {key: ObjectBinding(logical_ref=key, status=ObjectIdentityStatus.AMBIGUOUS,
                                   candidates=())}
    v = build_table_views(rows, glossary=g, bindings=bindings,
                          concepts={}, definitions={}, domains={})
    cols = v["txn"].columns
    assert len(cols) == 1                                 # column NOT dropped
    assert cols[0].sidecar_attached is False and cols[0].declared_type == ""


def test_reconciled_facet_withheld():
    rows = [_row("txn", "event_ts")]
    g = GlossaryUpload(rows=rows, records=[_rec("s::banking.txn.event_ts",
        declared_type="timestamp", semantic_type="identifier",
        logical_representation="numeric_string")])
    v = build_table_views(rows, glossary=g, bindings=None, concepts={}, definitions={}, domains={})
    col = v["txn"].columns[0]
    assert col.semantic_type is None and col.logical_representation is None


def test_table_term_schema_mismatch_withholds_definition():
    rows = [_row("txn", "fee")]
    g = GlossaryUpload(rows=rows, records=[
        _rec("s::banking.txn.fee"),
        _rec("s::risk.txn", is_table=True, definition="wrong schema")])
    v = build_table_views(rows, glossary=g, bindings=None, concepts={}, definitions={}, domains={})
    assert v["txn"].table_definition is None
```

- [ ] **Step 2: Run — expect FAIL** `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_column_view.py -q`
- [ ] **Step 3: Implement** the assembly. Rules: derive `(source, schema, table, column)` from `parse_ref(rec.logical_ref)` `[F5]`; the column's binding key is `normalize_ref(row.source, None, table, column)` (public-scoped, the key `bindings` is keyed under); attach the sidecar only when `bindings is None or may_attach(bindings.get(key))` — else keep the CanonicalRow column with blank sidecar + `sidecar_attached=False` `[F2]`. Build `column_schemas` from attached column records; a table term attaches its `table_definition` only when `column_schemas.get(table) in (None, {term_schema})` `[F7-slice1-orig]`. Reconcile facets via `ParsedProfile(...)`→`reconcile_profile(declared_type=…, column=…)`. `business_definition` from `rec.definition` else the draft, passed through the field-aware sanitizer helper (or left raw — the egress boundary in Task 2 is now authoritative; still bound to 600). `domain = rec.domain or domains.get(table)` `[F8]`. `concept/drafted_definition = maps.get(content_hash(row))`; `classified_domain = domains.get(table)`.
- [ ] **Step 4: Run — expect PASS** `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_column_view.py -q`
- [ ] **Step 5: Commit** `feat(view): ColumnMetadataView + attachable builder (uses ingest bindings; sidecar-optional, column-always)`

---

### Task 4: Structured dual-type roster + Pass B input from the view (schema/prompt v2)

**Files:** Modify `table_synth.py` (`_descriptor`, `assemble_table_items(views)`, `_roster_entry`, wide roster + phase-2 item `table_definition`, instructions), `enrich_llm.py` (`_SCHEMAS` `overlay_table_synth*` **v2** with `operational_type`/`declared_type`/structured roster/`table_definition`; `_COLUMN_PROFILE_KEYS` add `operational_type`,`declared_type`; roster-entry allowlist + validator; `_ITEM_META_ALLOWED` already has `table_definition` from Task 2). Test `tests/featuregen/overlay/upload/test_passb_roster.py`.

**Interfaces:** `assemble_table_items(views: dict[str, TableMetadataView]) -> list[BatchItem]`; column profile `{column, operational_type, declared_type, concept?, business_definition?, term_type?, domain?, process_path?, semantic_type?}`; item metadata carries `table_definition`; wide roster entry `{column, operational_type, declared_type}` (structured — a column name may contain `:`/`/`). Ship schema/prompt **v2** via the seam.

- [ ] **Step 1: Write the failing tests** (assert both separate types, `"type" not in prof`, `_item_egress_ok` True, `table_definition` in metadata, and a `:`-containing column round-trips in `_roster_entry`; construct views via `build_table_views`). Full assertions as in rev.1 Task 4 but with no `...`.
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** the dual-type descriptor from a `ColumnMetadataView`, `_roster_entry(view)`, the v2 schemas + registration, allowlist additions, and pass `prompt_version=2, schema_version=2` from the synth drivers. Thread `table_definition` into the wide phase-2 item.
- [ ] **Step 4: Run — expect PASS** `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_passb_roster.py tests/featuregen/overlay/upload/test_table_synth*.py -q`
- [ ] **Step 5: Commit** `feat(passb): structured dual-type roster + table_definition + schema/prompt v2`

---

### Task 5: Wire the view into ingest's Pass B path (pass the existing bindings)

**Files:** Modify `ingest.py` (Pass B block). Test `tests/featuregen/overlay/upload/test_passb_view_wiring.py`.

- [ ] **Step 1: Write the failing test** — a request-capturing FakeLLM (extend the Phase-1 capturing fixture with `captured_for(task)`); assert the captured Pass B `column_profiles[0]` has `operational_type` and `declared_type` and the metadata has `table_definition` when a table term exists. No `...`.
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** — replace the `records`-map build with `views = build_table_views(vr.good, glossary=glossary, bindings=bindings, concepts=concepts, definitions=definitions, domains=domains)` then `assemble_table_items(views)`. `bindings` is already in scope (`ingest.py:1361`); if it can be `None` when `glossary is None`, `build_table_views` tolerates that. `domains` in scope from `build_graph` wiring; else `{}`.
- [ ] **Step 4: Run — expect PASS** `.venv/bin/python -m pytest tests/featuregen/overlay/upload/ -q`
- [ ] **Step 5: Commit** `feat(passb): assemble Pass B from ColumnMetadataView using the ingest raw-row bindings`

---

### Task 6: Integration + persisted-audit assertion on the synthetic FTR fixture `[F16]`

**Files:** Create `tests/featuregen/overlay/upload/test_slice1_acceptance.py`.

- [ ] **Step 1: Write assertions** (all concrete): on the Phase-1 synthetic fixture — every Pass B profile has `operational_type=="unknown"` and non-empty `declared_type`; the table item has `table_definition`; the reconciled-away timestamp/double column has no `semantic_type`; a `:`-containing column round-trips; a planted sample token is absent from the captured Pass B request; and **query `llm_call`** to assert `SECRET`-token absent from `redacted_input` and a `sample_strip` audit entry with a non-`none` state exists in `input_redaction`.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_slice1_acceptance.py -q`; then full suite `.venv/bin/python -m pytest -q` (run directly — no `| tail`).
- [ ] **Step 3: Commit** `test(view): slice-1 integration — view→Pass B, separate types, reconciled facets, persisted egress audit`

---

### Task 7: Real-provider canary for the v2 Pass B contract `[F17]`

**Files:** Modify `tests/eval/test_anthropic_live_canary.py` (extend, key-gated).

- [ ] **Step 1: Add** the `overlay_table_synth_batch` **v2** schema (structured roster + `operational_type`/`declared_type` + `table_definition`) to the canary's parametrized schema list, registering v2 and validating a real response against the canonical v2 schema. Import `anthropic` only inside the test body; `skipif(not ANTHROPIC_API_KEY)`.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest -m eval tests/eval/test_anthropic_live_canary.py -q` → SKIPPED without a key (proves it collects + gates). Note in the report that a keyed run is the real gate before declaring the slice complete.
- [ ] **Step 3: Commit** `test(eval): live canary covers the Pass B v2 structured-roster schema`

---

## Self-Review

**Spec coverage:** version seam → T1; field-aware egress + audit persistence `[F1,F7]` → T2; view + attachable-via-existing-bindings + technical/generic/fail-soft/domain-precedence `[F2,F3,F4,F5,F6,F8]` → T3; structured roster + v2 → T4; wiring with existing bindings → T5; integration + persisted-audit query `[F16]` → T6; provider canary `[F17]` → T7.
**Placeholder scan:** no `...` in any test step; the only non-inlined bodies are Task 4/5 test-construction that reuse Task 3's `build_table_views` (assertions are explicit). Test commands run pytest directly `[F16]`.
**Type consistency:** `build_table_views(..., bindings=…)` (T3) → ingest wiring (T5); `assemble_table_items(views)` (T4) consumes T3's output; the 4-tuple `_redact_free_text_meta` return (T2) is unpacked at both call sites.

---

## rev. 3 — BINDING corrections (verified against code; override the tasks above)

Each was adversarially confirmed against the current source. Apply verbatim.

- **[F4] Task 3 binding — never crash, use the record source.** The existing governed path is `ingest.py:928-929`: `binding = bindings.get(normalize_ref(rec_source, None, table, column))` then `if binding is None or not may_attach(binding): continue`. Mirror it: build the lookup key from the **record** source (`rec_source, _schema, table, column = parse_ref(rec.logical_ref)`), resolve `binding = None if bindings is None else bindings.get(key)`, and attach the sidecar only when `bindings is None or (binding is not None and may_attach(binding))` — **never** call `may_attach(None)` (crashes; reachable because `ingest.py:1369` sets `bindings={}` on classify failure). Keying by `row.source` admits a cross-source sidecar; keying by `rec_source` restores the implicit cross-source guard (bindings is row-source-keyed). Add a test: a non-None dict with an **absent** key → sidecar withheld, column kept.
- **[F8] Task 3 fence — build `column_schemas` from ALL parsed non-table records**, via `parse_ref(rec.logical_ref)`, independent of `may_attach`/attachment (mirror `ingest.py:988-997`). Attached-only lets a mismatched table term through when every column sidecar is withheld.
- **[F3] Task 2 — preserve PII spans for definition fields.** `sanitize_definition` returns only a `removed` count, not spans (`sanitize.py:133`), so routing definition keys through it drops the `{type,start,end}` PII spans persisted today. Add a `redacted_spans: tuple` field to `DefinitionSanitize`, populate it from `result.redacted_spans` in `sanitize_definition` (empty on fail-closed paths); in the field-aware `_redact_free_text_meta`, the **definition** branch must extend `pii_spans` with those spans (annotated `{key/path, type, start, end}`) **in addition to** the sample audit — so definition-field PII spans keep reaching `input_redaction["redacted_spans"]`.
- **[F6] Global constraint — free-text may be a LIST.** `synonyms` is prose emitted as `list[str]` (`enrich.py:245`). Do **not** require scalars globally. Support three kinds: `definition` (scalar → `sanitize_definition`), `prose` (scalar → `redact_free_text`), `list_of_prose` (`synonyms` → per-item `redact_free_text`, audit indexed paths `synonyms[0]`). The existing `_redact_free_text_meta` already iterates list values — keep that.
- **[F7] Task 2 — split shape gate from length gate; give `table_definition` the 600 bound.** `_item_egress_ok` (the allowlist+length gate) runs BEFORE `_redact_free_text_meta` (`enrich_llm.py:583` before `:596`), so a long un-stripped definition is excluded before sanitization can shorten it. Split into (1) a SHAPE/allowlist gate kept pre-redaction and (2) a per-value LENGTH gate applied AFTER sanitization on the sanitized items. And set `_MAX_LEN_BY_KEY = {"business_definition": MAX_DEFINITION_LEN, "table_definition": MAX_DEFINITION_LEN}` (both 600; today `table_definition` inherits 200).
- **[F10] Task 1 — thread versions through single fallback too.** `run_batched → _fallback → _single_fallback` (`enrich_batch.py:117`) hardcodes `_v1` (`:129`) and calls `audited_enrich_call`/`audited_structured_call`, which pin `prompt_version=1`/`output_schema_version=1`/`schema_for(schema_id,1)` (`enrich_llm.py:399,419-420`). Thread `prompt_version`/`schema_version` (and the correct scalar `prompt_id`/`schema_id`) through `_fallback`, `_single_fallback`, `audited_enrich_call`, and `audited_structured_call` — else a versioned scalar batch retries under the v1 contract.
- **[F11] Task 1 test — pass a valid item + register a test-only v2 schema.** `audited_batch_call` returns early on empty items (`enrich_llm.py:607`), so the spy never fires. Pass one valid item `BatchItem(ref="t", metadata={"table":"t"})` and a matching scripted `results` entry so `drive_structured_call` runs. For the explicit-version test (`schema_version=2`), `DocumentSchemaRegistry(db).register_schema("overlay_concept_batch", 2, schema, owner)` first (only v1 exists), else it resolves via the repair-exhausted `STATUS_FAILED` path, not a clean validated call.
- **[F14] Task 6 acceptance — fix impossible assertions.** (a) FTR defs are sanitized at read (`ftr_adapter.py:273`) → at egress they re-sanitize to `state=="none"`; assert `state=="none"` on the fixture and rely on Task 2's raw-item unit test for the non-`none` sample-strip coverage. (b) The fixture has no `:`-containing column → add one to `ftr_sample_synthetic.csv` (e.g. FQN `…COMP_FIN_TRAN.SOME:COL`) or move the `:`-roundtrip assertion to Task 4's constructed-view unit.
- **[F15] Task 7 canary — parameterize by (schema_id, version, input).** `_build_request` hardcodes version 1 at all three slots (`test_anthropic_live_canary.py:93,100-102,132`) and the parametrize is version-blind. Give `_build_request` a `version` param, replace the three `1`s, iterate `(schema_id, version)` tuples, and supply per-version inputs carrying the **actual** v2 metadata (structured roster objects with separate operational/declared type + `table_definition`) — else the new wire schema never reaches the live API.
