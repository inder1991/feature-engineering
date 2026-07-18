# Slice 3A-iii — Menu Enrichment + Nested Field-Aware Egress + Deterministic Relevance Implementation Plan
> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (- [ ]) syntax.

**Goal:** Widen the feature menu into a governed, authority-wrapped, per-table-context payload that reaches the LLM only after a nested field-aware egress adapter has sample-stripped every definition and deterministic relevance has bounded it to one byte budget — with flag-off byte-identical to pre-Slice-3.

**Architecture:** `_candidate_columns` selects the full feature-correctness column set (plus the table node's definition/primary_entity via a single scoped join); a flag-gated enriched menu wraps each governed field in `OperationalColumnFacts{value,authority}` via 3A-i's `read_column_facts`, and a per-table context block is derived from those same authorized rows (never a second query). A new `sanitize_feature_context` adapter runs INSIDE `audited_structured_call` before `build_llm_inputs`/`assert_llm_safe`/dispatch — routing definition-kind fields through `sanitize_definition`, allowlist-bounding structural fields, and failing closed on any unclassified key or blanked definition. Deterministic relevance (governed `ConfirmedScope` → direct-assist entity+objective → lexical fallback) picks a mandatory set plus top-scored optionals under one hard byte budget, raising `CONTEXT_TOO_LARGE` rather than chunking.

## Global Constraints
- **Branch base:** the **3A-ii branch tip** (3A-i → 3A-ii → **3A-iii** → 3A-iv). Do NOT branch off `main`; 3A-i's `column_authority.py`/`read_column_facts`/`OperationalColumnFacts` and the extended `FeatureIdea` MUST already be present on the base.
- **Implementers on FABLE; reviews on OPUS.** Set the model explicitly per subagent.
- **Shared-interface names are VERBATIM** (from `slice3-shared-interfaces.md`) — do not redefine or drift:
  - `OperationalColumnFacts{value: str|None, authority: "governed"|"hint", provenance: str|None}` and `read_column_facts(conn, logical_ref: str, field_name: str) -> OperationalColumnFacts` — module `featuregen.overlay.upload.column_authority` (3A-i). Recognized `field_name` domain: `additivity`, `logical_representation`, `is_grain`, `is_as_of`, `unit`, `currency`, `entity`, `declared_type`.
  - `ConfirmedScope` — `featuregen.overlay.upload.taxonomy.applicability` (`primary: str|None`, `secondary: tuple[str,...]`, `unscoped: bool`, `target_entity: str|None`, `modelling_contexts: tuple[str,...]`).
  - `sanitize_definition(text) -> DefinitionSanitize{clean, state, removed, sanitizer_version, redaction_version, reason, redacted_spans}` — `featuregen.overlay.upload.sanitize` (Slice-1 primitive; REUSE, do not reimplement).
  - The nested field-aware egress adapter is invoked INSIDE `audited_structured_call` (`enrich_llm.py`) BEFORE `build_llm_inputs`/`assert_llm_safe`/dispatch; audit → `llm_call.input_redaction` (`redacted_spans` + `sample_strip`), reusing the Slice-1 spans pattern.
  - Relevance: deterministic objective (NO `recognize()` call); mandatory set = confirmed grain cols + as-of col + entity-matching cols; ONE hard byte budget; overflow → `CONTEXT_TOO_LARGE`, do NOT chunk; log dropped count.
  - Flag: env `FEATUREGEN_FEATURE_CONTEXT` (default off) gates ALL enrichment (menu widening, context, relevance, versioned shape). Flag-OFF payload/snapshot BYTE-IDENTICAL to pre-Slice-3.
- **Run pytest DIRECTLY** with the repo interpreter, never piped: `.venv/bin/python -m pytest <path> -x -q`. **Never** `| tail`.
- **ruff line-length 100.** No placeholders / no `...` anywhere — every test carries concrete assertions; every implementation block is complete code.
- Verify line numbers by SYMBOL before editing (they shift). Anchor tests on the real dataclass/function signatures confirmed in the files below.

**Confirmed anchors (base = 3A-ii tip; symbols, not fixed lines):**
- `feature_assist.py`: `_candidate_columns(conn, catalog_source, roles, entity=None) -> list[dict]`; `_menu(cols) -> list[dict]`; `class RejectCode`; `_generate(...)` builds `menu = _menu(cols)` and `inputs = {"columns": menu, "avoid": avoid}`; `_fix_pass(..., menu, ...)`; `refine_idea(...)` and `feature_recipe(...)` each call `_menu(cols)`.
- `enrich_llm.py`: `audited_structured_call(...)` calls `_redact_free_text_meta(dict(catalog_metadata))` → `safe_metadata`; on `None` audits + returns; then `build_llm_inputs(redaction, catalog_metadata=safe_metadata, ...)`. `sanitize_definition` already imported. `_audit_egress_block(conn, *, task, actor, reason)` records `EGRESS_BLOCKED`.
- `graph_node` columns exist: `data_type, declared_type, semantic_terms, entity, additivity, unit, currency, is_grain, is_as_of, grain_fact_event_id, availability_fact_event_id` (columns); `definition, primary_entity` (table nodes, `kind='table'`). `build_graph(db, source, rows)` creates one `kind='table'` node per table (`table_name` set, `column_name` NULL) and flattens object_refs to `public.<table>.<column>`.
- `record_llm_call` persists `redacted_input = Jsonb(dict(request.inputs))` (the sanitized inputs) and `input_redaction` — both queryable from the request `db` conn in tests (no DSN → durable write lands on the request conn).
- Test fixture: `db` (in `tests/featuregen/conftest.py`); `FakeLLM(script={task: FakeResponse(output=...)})`, `FakeResponse`, `LLMResult(output, self_reported_scores, call_ref, status, cost_metadata)`.

---

## Task 1 — Widen `_candidate_columns` (feature-correctness fields + table-node context, single scoped query)

**Files:**
- Modify: `src/featuregen/overlay/upload/feature_assist.py` (`_candidate_columns`)
- Test: `tests/featuregen/overlay/upload/test_feature_menu_enrichment.py` (new)

**Interfaces:**
- Produces: each candidate dict gains keys `data_type, declared_type, semantic_terms, entity, additivity, unit, currency, is_grain, is_as_of, grain_fact_event_id, availability_fact_event_id, table_definition, table_primary_entity` (in addition to the existing `catalog_source, object_ref, table, column, concept, domain, definition`). Read-scope filter and `entity`/`catalog_source` narrowing UNCHANGED. `_menu(cols)` STILL returns only the 5 thin keys (flag-off byte-identity).
- Consumes: nothing new.

**Steps:**
- [ ] Write the failing test:
```python
# tests/featuregen/overlay/upload/test_feature_menu_enrichment.py
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.feature_assist import _candidate_columns, _menu
from featuregen.overlay.upload.graph import build_graph


def _bank_graph(db):
    rows = [
        CanonicalRow("bank", "transactions", "amount", "numeric", definition="txn amount",
                     additivity="additive", unit="dollars", currency="USD", entity="Account"),
        CanonicalRow("bank", "transactions", "txn_date", "timestamp", as_of=True),
        CanonicalRow("bank", "accounts", "account_id", "integer", is_grain=True, entity="Account"),
    ]
    build_graph(db, "bank", rows)
    db.execute("UPDATE graph_node SET declared_type='numeric', semantic_terms='payment amount' "
               "WHERE object_ref='public.transactions.amount'")
    db.execute("UPDATE graph_node SET grain_fact_event_id='fe_grain1' "
               "WHERE object_ref='public.accounts.account_id'")
    db.execute("UPDATE graph_node SET availability_fact_event_id='fe_avail1' "
               "WHERE object_ref='public.transactions.txn_date'")
    db.execute("UPDATE graph_node SET definition='Accounts master', primary_entity='Account' "
               "WHERE kind='table' AND table_name='accounts'")


def test_candidate_columns_carries_feature_correctness_and_table_fields(db):
    _bank_graph(db)
    cols = _candidate_columns(db, "bank", roles=())
    by_ref = {c["object_ref"]: c for c in cols}
    amount = by_ref["public.transactions.amount"]
    assert amount["declared_type"] == "numeric"
    assert amount["semantic_terms"] == "payment amount"
    assert amount["additivity"] == "additive"
    assert amount["unit"] == "dollars"
    assert amount["currency"] == "USD"
    assert amount["entity"] == "Account"
    assert amount["is_grain"] is False
    acct = by_ref["public.accounts.account_id"]
    assert acct["is_grain"] is True
    assert acct["grain_fact_event_id"] == "fe_grain1"
    assert acct["table_definition"] == "Accounts master"
    assert acct["table_primary_entity"] == "Account"
    txn_date = by_ref["public.transactions.txn_date"]
    assert txn_date["is_as_of"] is True
    assert txn_date["availability_fact_event_id"] == "fe_avail1"


def test_thin_menu_unchanged_after_widening(db):
    _bank_graph(db)
    cols = _candidate_columns(db, "bank", roles=())
    menu = _menu(cols)
    # The thin menu still projects EXACTLY the five structural keys — flag-off byte-identity.
    assert all(set(m.keys()) == {"object_ref", "table", "column", "concept", "domain"}
               for m in menu)
    amount = next(m for m in menu if m["object_ref"] == "public.transactions.amount")
    assert amount == {"object_ref": "public.transactions.amount", "table": "transactions",
                      "column": "amount", "concept": None, "domain": None}
```
- [ ] Run it (expect FAIL — `KeyError`/missing keys): `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_feature_menu_enrichment.py -x -q`
- [ ] Implement — replace the body of `_candidate_columns` (keep the signature, the read-scope filter, and the `entity`/`catalog_source` branches):
```python
def _candidate_columns(conn, catalog_source: str | None, roles: Iterable[str],
                       entity: str | None = None) -> list[dict]:
    # Read-scope: never feed a sensitivity-tagged column the caller can't see to the LLM (M6).
    # The LEFT JOIN reads the column's OWN table node (kind='table') for the table-level definition
    # and primary_entity — one scoped query, NOT a second unscoped fetch (spec §5). One table node
    # per (catalog, table), so the join never fans a column into duplicate rows.
    sql = ("SELECT c.catalog_source, c.object_ref, c.table_name, c.column_name, c.concept, "
           "c.domain, c.definition, c.data_type, c.declared_type, c.semantic_terms, c.entity, "
           "c.additivity, c.unit, c.currency, c.is_grain, c.is_as_of, c.grain_fact_event_id, "
           "c.availability_fact_event_id, t.definition, t.primary_entity "
           "FROM graph_node c "
           "LEFT JOIN graph_node t ON t.catalog_source = c.catalog_source AND t.kind = 'table' "
           "AND t.table_name = c.table_name "
           "WHERE c.kind = 'column' "
           "AND (c.sensitivity IS NULL OR c.sensitivity = ANY(%s))")
    params: list = [allowed_sensitivities(roles)]
    if entity:
        # Cross-domain gather: candidates from EVERY catalog that contains this entity, not one source.
        sql += (" AND c.catalog_source IN "
                "(SELECT DISTINCT catalog_source FROM graph_node WHERE entity = %s)")
        params.append(entity)
    elif catalog_source:
        sql += " AND c.catalog_source = %s"
        params.append(catalog_source)
    rows = conn.execute(sql, params).fetchall()
    return [{"catalog_source": r[0], "object_ref": r[1], "table": r[2], "column": r[3],
             "concept": r[4], "domain": r[5], "definition": r[6], "data_type": r[7],
             "declared_type": r[8], "semantic_terms": r[9], "entity": r[10], "additivity": r[11],
             "unit": r[12], "currency": r[13], "is_grain": r[14], "is_as_of": r[15],
             "grain_fact_event_id": r[16], "availability_fact_event_id": r[17],
             "table_definition": r[18], "table_primary_entity": r[19]} for r in rows]
```
- [ ] Run it (expect PASS): `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_feature_menu_enrichment.py -x -q`
- [ ] Commit: `feat(feature-assist): widen _candidate_columns with feature-correctness + table-node context (single scoped query)`

---

## Task 2 — Enriched menu wrapping governed fields in `OperationalColumnFacts` + the feature-context flag

**Files:**
- Modify: `src/featuregen/overlay/upload/feature_assist.py` (imports; `_feature_context_enabled`; `_enriched_column`; `_enriched_menu`)
- Test: `tests/featuregen/overlay/upload/test_feature_menu_enrichment.py`

**Interfaces:**
- Consumes: `read_column_facts(conn, logical_ref, field_name) -> OperationalColumnFacts` (3A-i). Fact `field_name` mapping (menu key → contract field_name): `data_type`→`logical_representation` (value = operational `graph_node.data_type`), `declared_type`→`declared_type`, `entity`→`entity`, `additivity`→`additivity`, `unit`→`unit`, `currency`→`currency`, `is_grain`→`is_grain`, `is_as_of`→`is_as_of`.
- Produces: `_feature_context_enabled() -> bool`; `_enriched_column(conn, c) -> dict`; `_enriched_menu(conn, cols) -> list[dict]`. Each enriched column: structural identity keys bare; `definition`/`semantic_terms` as free-text strings (sanitized at egress in Task 4); each fact key as `{"value", "authority"}` (NEVER a bare display value).

**Steps:**
- [ ] Write the failing test (append):
```python
def test_enriched_menu_wraps_governed_fields_and_flag_gates(db, monkeypatch):
    import featuregen.overlay.upload.feature_assist as fa
    _bank_graph(db)
    monkeypatch.delenv("FEATUREGEN_FEATURE_CONTEXT", raising=False)
    assert fa._feature_context_enabled() is False
    monkeypatch.setenv("FEATUREGEN_FEATURE_CONTEXT", "1")
    assert fa._feature_context_enabled() is True

    cols = fa._candidate_columns(db, "bank", roles=())
    menu = fa._enriched_menu(db, cols)
    amount = next(m for m in menu if m["object_ref"] == "public.transactions.amount")
    # Structural identity stays bare; definition/semantic_terms are free-text strings.
    assert amount["table"] == "transactions"
    assert amount["definition"] == "txn amount"
    assert amount["semantic_terms"] == "payment amount"
    # Every fact field is a {value, authority} wrapper, never a bare value.
    for field in ("data_type", "declared_type", "entity", "additivity", "unit", "currency",
                  "is_grain", "is_as_of"):
        assert set(amount[field].keys()) == {"value", "authority"}, field
        assert amount[field]["authority"] in ("governed", "hint"), field
    # Hint fields carry the flat value verbatim.
    assert amount["declared_type"] == {"value": "numeric", "authority": "hint"}
    assert amount["unit"] == {"value": "dollars", "authority": "hint"}
    assert amount["currency"] == {"value": "USD", "authority": "hint"}
```
- [ ] Run it (expect FAIL — `AttributeError: _feature_context_enabled`): `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_feature_menu_enrichment.py::test_enriched_menu_wraps_governed_fields_and_flag_gates -x -q`
- [ ] Implement — add `import json`, `import os` to the existing `import logging`/`import re` block; add the `read_column_facts` import next to the other overlay imports:
```python
from featuregen.overlay.upload.column_authority import read_column_facts
```
Then add (after `_menu`, keeping `_menu` unchanged as the thin projection):
```python
FEATURE_CONTEXT_FLAG = "FEATUREGEN_FEATURE_CONTEXT"


def _feature_context_enabled() -> bool:
    """The single env gate for the whole Slice-3 enrichment (menu widening, per-table context,
    relevance, versioned shape). Default OFF ⟹ the thin pre-Slice-3 menu, byte-for-byte."""
    return os.environ.get(FEATURE_CONTEXT_FLAG, "").strip().lower() in ("1", "true", "yes", "on")


# Menu fact key -> read_column_facts field_name. `data_type` reads the OPERATIONAL structural type
# under the contract's `logical_representation` authority field (value = graph_node.data_type).
_MENU_FACT_FIELDS = {
    "data_type": "logical_representation",
    "declared_type": "declared_type",
    "entity": "entity",
    "additivity": "additivity",
    "unit": "unit",
    "currency": "currency",
    "is_grain": "is_grain",
    "is_as_of": "is_as_of",
}
_MENU_IDENTITY_FIELDS = ("object_ref", "table", "column", "concept", "domain")
_MENU_DEFINITION_FIELDS = ("definition", "semantic_terms")


def _enriched_column(conn, c: dict) -> dict:
    """One flag-ON menu column: structural identity bare, definition-kind free text kept (sanitized
    at egress in enrich_llm), and each governed/hint fact wrapped as OperationalColumnFacts
    {value, authority} via read_column_facts (never a bare display value; spec §5)."""
    out: dict = {}
    for k in _MENU_IDENTITY_FIELDS:
        v = c.get(k)
        if v is not None:
            out[k] = v
    for k in _MENU_DEFINITION_FIELDS:
        v = c.get(k)
        if v:
            out[k] = v
    for menu_key, field_name in _MENU_FACT_FIELDS.items():
        facts = read_column_facts(conn, c["object_ref"], field_name)
        out[menu_key] = {"value": facts.value, "authority": facts.authority}
    return out


def _enriched_menu(conn, cols: list[dict]) -> list[dict]:
    return [_enriched_column(conn, c) for c in cols]
```
- [ ] Run it (expect PASS): `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_feature_menu_enrichment.py -x -q`
- [ ] Commit: `feat(feature-assist): enriched menu wraps governed fields in OperationalColumnFacts behind FEATUREGEN_FEATURE_CONTEXT`

---

## Task 3 — Per-table context block (from the authorized candidate rows only)

**Files:**
- Modify: `src/featuregen/overlay/upload/feature_assist.py` (`_table_context`)
- Test: `tests/featuregen/overlay/upload/test_feature_menu_enrichment.py`

**Interfaces:**
- Produces: `_table_context(cols: list[dict]) -> list[dict]`. One block per `(catalog, table)` present in the candidate rows: `{"table", ["table_definition"], ["grain_columns"], ["as_of_column"], ["primary_entity"]}`. Confirmed grain columns require `is_grain AND grain_fact_event_id` non-null; the as-of column requires `is_as_of AND availability_fact_event_id` non-null (governed-VERIFIED, not merely file-declared). A table with every column read-scope-excluded has no candidate rows here and therefore no block.
- Consumes: the widened candidate dicts from Task 1 only (NEVER a second query).

**Steps:**
- [ ] Write the failing test (append):
```python
def test_table_context_from_authorized_rows_requires_fact_event_id(db):
    import featuregen.overlay.upload.feature_assist as fa
    rows = [
        CanonicalRow("bank", "accounts", "account_id", "integer", is_grain=True),
        CanonicalRow("bank", "accounts", "region", "text", is_grain=True),  # is_grain but no fact id
        CanonicalRow("bank", "transactions", "txn_date", "timestamp", as_of=True),
    ]
    build_graph(db, "bank", rows)
    db.execute("UPDATE graph_node SET grain_fact_event_id='fe_grain1' "
               "WHERE object_ref='public.accounts.account_id'")
    db.execute("UPDATE graph_node SET availability_fact_event_id='fe_avail1' "
               "WHERE object_ref='public.transactions.txn_date'")
    db.execute("UPDATE graph_node SET definition='Accounts master', primary_entity='Account' "
               "WHERE kind='table' AND table_name='accounts'")

    cols = fa._candidate_columns(db, "bank", roles=())
    ctx = {b["table"]: b for b in fa._table_context(cols)}
    assert ctx["accounts"]["table_definition"] == "Accounts master"
    assert ctx["accounts"]["primary_entity"] == "Account"
    # Only the fact-event-linked grain column is confirmed; the file-declared one is excluded.
    assert ctx["accounts"]["grain_columns"] == ["account_id"]
    assert "as_of_column" not in ctx["accounts"]
    assert ctx["transactions"]["as_of_column"] == "txn_date"
    assert "grain_columns" not in ctx["transactions"]


def test_table_context_skips_read_scope_excluded_table(db):
    import featuregen.overlay.upload.feature_assist as fa
    rows = [
        CanonicalRow("bank", "accounts", "balance", "numeric", definition="ledger balance"),
        CanonicalRow("bank", "secrets", "ssn", "text", sensitivity="pii", definition="cust SSN"),
    ]
    build_graph(db, "bank", rows)
    cols = fa._candidate_columns(db, "bank", roles=())  # no pii role
    tables = {b["table"] for b in fa._table_context(cols)}
    assert "accounts" in tables
    assert "secrets" not in tables  # every column excluded -> no block
```
- [ ] Run it (expect FAIL — `AttributeError: _table_context`): `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_feature_menu_enrichment.py -x -q -k table_context`
- [ ] Implement — add after `_enriched_menu`:
```python
def _table_context(cols: list[dict]) -> list[dict]:
    """One context block per TABLE, assembled ONLY from the already-authorized candidate rows
    (spec §5): a table whose columns were all read-scope-excluded has no rows here and gets no
    block. Confirmed grain columns require a non-null grain_fact_event_id and the as-of column a
    non-null availability_fact_event_id (governed-VERIFIED, not merely file-declared);
    primary_entity is ADVISORY."""
    by_table: dict[tuple[str, str], list[dict]] = {}
    for c in cols:
        by_table.setdefault((c["catalog_source"], c["table"]), []).append(c)
    blocks: list[dict] = []
    for (_catalog, table), members in sorted(by_table.items()):
        block: dict = {"table": table}
        tdef = next((m["table_definition"] for m in members if m.get("table_definition")), None)
        if tdef:
            block["table_definition"] = tdef
        grain_cols = sorted(m["column"] for m in members
                            if m["is_grain"] and m["grain_fact_event_id"])
        if grain_cols:
            block["grain_columns"] = grain_cols
        as_of = next((m["column"] for m in sorted(members, key=lambda x: x["column"])
                      if m["is_as_of"] and m["availability_fact_event_id"]), None)
        if as_of:
            block["as_of_column"] = as_of
        pentity = next((m["table_primary_entity"] for m in members
                        if m.get("table_primary_entity")), None)
        if pentity:
            block["primary_entity"] = pentity
        blocks.append(block)
    return blocks
```
- [ ] Run it (expect PASS): `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_feature_menu_enrichment.py -x -q`
- [ ] Commit: `feat(feature-assist): per-table context block from authorized candidate rows (governed grain/as-of)`

---

## Task 4 — Nested field-aware egress adapter, invoked inside `audited_structured_call`

**Files:**
- Modify: `src/featuregen/overlay/upload/enrich_llm.py` (classification constants; `sanitize_feature_context`; wire into `audited_structured_call`)
- Test: `tests/featuregen/overlay/upload/test_feature_context_egress.py` (new)

**Interfaces:**
- Produces: `sanitize_feature_context(metadata: dict) -> tuple[dict | None, list[dict], list[dict], str | None]` — `(safe_metadata|None, pii_spans, sample_audits, sanitizer_version)`, mirroring `_redact_free_text_meta`'s return shape. Traverses `columns[*]` (dict elements only) + `table_context[*]`. Returns the SAME `metadata` object untouched when no feature menu is present or when only structural passthrough occurred (byte-identity). `None` ⟹ fail closed.
- Consumes: `sanitize_definition` (already imported). Wired into `audited_structured_call` AFTER `_redact_free_text_meta` and BEFORE `build_llm_inputs`/`assert_llm_safe`/dispatch; spans → `input_redaction["redacted_spans"]`, sample audits → `input_redaction["sample_strip"]`; a `None` result audits `EGRESS_BLOCKED` and returns `None` (no dispatch).

**Steps:**
- [ ] Write the failing test:
```python
# tests/featuregen/overlay/upload/test_feature_context_egress.py
from featuregen.overlay.upload.enrich_llm import sanitize_feature_context

_SAMPLE = ("Posting amount is the monetary value of the ledger entry, with representative values "
           "such as 3708484836801; 3708446902413; 3708454004701, which supports interpretation.")


def test_non_feature_payload_untouched():
    meta = {"table": "t", "columns": ["a:int", "b:int"]}  # enrichment roster (list of strings)
    safe, spans, audits, ver = sanitize_feature_context(meta)
    assert safe is meta          # same object -> byte-identical
    assert (spans, audits, ver) == ([], [], None)


def test_thin_menu_with_null_identity_untouched():
    meta = {"columns": [{"object_ref": "public.t.c", "table": "t", "column": "c",
                         "concept": None, "domain": None}], "avoid": []}
    safe, spans, audits, ver = sanitize_feature_context(meta)
    assert safe is meta          # no definition-kind field -> untouched (flag-off byte-identity)
    assert (spans, audits, ver) == ([], [], None)


def test_definition_sample_clause_stripped_and_audited():
    meta = {"columns": [{"object_ref": "public.t.amount", "table": "t", "column": "amount",
                         "definition": _SAMPLE,
                         "additivity": {"value": "additive", "authority": "governed"}}]}
    safe, spans, audits, ver = sanitize_feature_context(meta)
    assert safe is not None
    clean = safe["columns"][0]["definition"]
    assert "3708484836801" not in clean
    assert "representative values" not in clean
    assert ver  # a sanitizer/redaction version was stamped
    assert any(a["path"] == "columns[0].definition" and a["removed_count"] >= 1 for a in audits)
    # The {value, authority} fact wrapper passes through structurally unchanged.
    assert safe["columns"][0]["additivity"] == {"value": "additive", "authority": "governed"}


def test_unclassified_key_fails_closed():
    meta = {"columns": [{"object_ref": "public.t.c", "evil": "surprise"}]}
    safe, _spans, _audits, _ver = sanitize_feature_context(meta)
    assert safe is None          # any unclassified key blocks the payload


def test_blanked_definition_fails_closed():
    # A data marker the stripper cannot consume -> sanitize_definition blanks -> block dispatch.
    meta = {"columns": [{"object_ref": "public.t.c", "definition": "sample values: 4111 2222"}]}
    safe, _spans, audits, _ver = sanitize_feature_context(meta)
    assert safe is None
    assert any(a["state"] == "suspected_unhandled" for a in audits)


def test_bad_fact_wrapper_and_table_context():
    ok = {"columns": [{"object_ref": "x", "unit": {"value": "dollars", "authority": "hint"}}],
          "table_context": [{"table": "t", "grain_columns": ["id"], "table_definition": _SAMPLE}]}
    safe, _spans, audits, _ver = sanitize_feature_context(ok)
    assert safe is not None
    assert "3708484836801" not in safe["table_context"][0]["table_definition"]
    assert any(a["path"] == "table_context[0].table_definition" for a in audits)
    bad = {"columns": [{"object_ref": "x", "additivity": {"value": "additive"}}]}  # missing authority
    assert sanitize_feature_context(bad)[0] is None
```
- [ ] Run it (expect FAIL — `ImportError: sanitize_feature_context`): `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_feature_context_egress.py -x -q`
- [ ] Implement — add classification constants near the other egress allowlists in `enrich_llm.py`:
```python
# The feature MENU's nested field-aware egress classification (spec §5, [F4]). Distinct top-level
# keys (`columns` of DICTS, `table_context`) from the enrichment payload, so this adapter is inert
# on every enrichment/contract call (a `columns` list of STRINGS is left untouched).
_FEATURE_COLUMN_DEFINITION_KEYS = frozenset({"definition", "semantic_terms"})
_FEATURE_COLUMN_IDENTITY_KEYS = frozenset({"object_ref", "table", "column", "concept", "domain"})
_FEATURE_COLUMN_FACT_KEYS = frozenset({
    "data_type", "declared_type", "entity", "additivity", "unit", "currency",
    "is_grain", "is_as_of"})
_FEATURE_FACT_SUBKEYS = frozenset({"value", "authority"})
_TABLE_CONTEXT_DEFINITION_KEYS = frozenset({"table_definition"})
_TABLE_CONTEXT_IDENTITY_KEYS = frozenset({"table", "as_of_column", "primary_entity"})
_TABLE_CONTEXT_LIST_KEYS = frozenset({"grain_columns"})
_FEATURE_STRUCTURAL_MAX_LEN = 200


def _fact_wrapper_ok(v: object) -> bool:
    """A governed/hint fact wrapper: exactly {value, authority}; value a bounded str or None;
    authority in {governed, hint}. Enum-ish tokens, never sample-bearing prose."""
    if not isinstance(v, dict) or any(k not in _FEATURE_FACT_SUBKEYS for k in v):
        return False
    val = v.get("value")
    if val is not None and not (isinstance(val, str) and len(val) <= _FEATURE_STRUCTURAL_MAX_LEN):
        return False
    return v.get("authority") in ("governed", "hint")


def sanitize_feature_context(metadata: dict) -> tuple[dict | None, list[dict], list[dict], str | None]:
    """Nested field-aware egress adapter for the feature menu ([F4], spec §5). Traverses
    ``columns[*]`` (DICT elements only — a list-of-strings `columns` from enrichment is left
    untouched) and any ``table_context[*]`` block. Definition-kind fields (`definition`,
    `semantic_terms`, `table_definition`) route through `sanitize_definition` (sample-clause strip +
    fail-closed data-marker scan + PII redaction); structural fields (identity strings, {value,
    authority} fact wrappers, grain-column ref lists) are exact-key allowlisted + length-bounded,
    never sample-stripped. FAIL CLOSED: any unclassified key anywhere, or a definition the sanitizer
    BLANKS, returns (None, ...) — the caller blocks dispatch + audits EGRESS_BLOCKED. Returns the
    SAME ``metadata`` object (untouched) when no feature menu is present or only structural
    passthrough occurred, so non-feature and flag-off payloads stay byte-identical."""
    columns = metadata.get("columns")
    table_context = metadata.get("table_context")
    has_cols = isinstance(columns, list) and any(isinstance(c, dict) for c in columns)
    has_ctx = isinstance(table_context, list) and any(isinstance(b, dict) for b in table_context)
    if not has_cols and not has_ctx:
        return metadata, [], [], None

    pii_spans: list[dict] = []
    sample_audits: list[dict] = []
    version: str | None = None

    def _defn(text: object, path: str) -> str | None:  # None ⟹ fail closed
        nonlocal version
        if not isinstance(text, str):
            return None
        d = sanitize_definition(text)
        version = version or d.redaction_version or d.sanitizer_version
        sample_audits.append({"path": path, "sanitizer_version": d.sanitizer_version,
                              "state": d.state, "removed_count": d.removed})
        if d.reason:
            return None
        pii_spans.extend({"key": path, **dict(s)} for s in d.redacted_spans)
        return d.clean

    def _structural_ok(v: object) -> bool:  # identity strings may be None (concept/domain nullable)
        return v is None or (isinstance(v, str) and len(v) <= _FEATURE_STRUCTURAL_MAX_LEN)

    new_columns = columns
    if has_cols:
        rebuilt: list = []
        for idx, col in enumerate(columns):
            if not isinstance(col, dict):
                rebuilt.append(col)              # enrichment roster token — untouched
                continue
            out: dict = {}
            for k, v in col.items():
                path = f"columns[{idx}].{k}"
                if k in _FEATURE_COLUMN_DEFINITION_KEYS:
                    clean = _defn(v, path)
                    if clean is None:
                        return None, pii_spans, sample_audits, version
                    out[k] = clean
                elif k in _FEATURE_COLUMN_IDENTITY_KEYS:
                    if not _structural_ok(v):
                        return None, pii_spans, sample_audits, version
                    out[k] = v
                elif k in _FEATURE_COLUMN_FACT_KEYS:
                    if not _fact_wrapper_ok(v):
                        return None, pii_spans, sample_audits, version
                    out[k] = v
                else:
                    return None, pii_spans, sample_audits, version   # unclassified — fail closed
            rebuilt.append(out)
        new_columns = rebuilt

    new_ctx = table_context
    if has_ctx:
        rebuilt_ctx: list = []
        for idx, block in enumerate(table_context):
            if not isinstance(block, dict):
                return None, pii_spans, sample_audits, version
            out = {}
            for k, v in block.items():
                path = f"table_context[{idx}].{k}"
                if k in _TABLE_CONTEXT_DEFINITION_KEYS:
                    clean = _defn(v, path)
                    if clean is None:
                        return None, pii_spans, sample_audits, version
                    out[k] = clean
                elif k in _TABLE_CONTEXT_IDENTITY_KEYS:
                    if not _structural_ok(v):
                        return None, pii_spans, sample_audits, version
                    out[k] = v
                elif k in _TABLE_CONTEXT_LIST_KEYS:
                    if not (isinstance(v, list) and all(
                            isinstance(x, str) and len(x) <= _FEATURE_STRUCTURAL_MAX_LEN
                            for x in v)):
                        return None, pii_spans, sample_audits, version
                    out[k] = v
                else:
                    return None, pii_spans, sample_audits, version
            rebuilt_ctx.append(out)
        new_ctx = rebuilt_ctx

    if version is None:
        return metadata, [], [], None            # structural passthrough only — untouched
    safe = dict(metadata)
    safe["columns"] = new_columns
    if has_ctx:
        safe["table_context"] = new_ctx
    return safe, pii_spans, sample_audits, version
```
Then wire it into `audited_structured_call` — locate the block right after `_redact_free_text_meta` returns (the `if safe_metadata is None:` guard) and BEFORE `redaction_version = free_text_version or _REDACTION_VERSION`, insert:
```python
    ctx_meta, ctx_spans, ctx_sample_audits, ctx_version = sanitize_feature_context(safe_metadata)
    if ctx_meta is None:
        logger.warning("feature-context egress adapter blocked %s (schema %s); no dispatch",
                       task, schema_id)
        _audit_egress_block(conn, task=task, actor=actor,
                            reason="feature-context egress adapter failed closed")
        return None                       # hard fail closed — no dispatch, no cache
    safe_metadata = ctx_meta
    spans = spans + ctx_spans
    sample_audits = sample_audits + ctx_sample_audits
    free_text_version = free_text_version or ctx_version
```
(`build_llm_inputs(..., catalog_metadata=safe_metadata, raw_input_classification="contains_pii" if spans else "clean")` and the `input_redaction={"redacted_spans": spans, "sample_strip": sample_audits}` record then carry the merged results unchanged.)
- [ ] Add a wiring test (append to `test_feature_context_egress.py`) proving the seam blocks dispatch on an unclassified key and never records an `llm_call`:
```python
def test_audited_structured_call_blocks_unclassified_menu_key(db):
    from featuregen.intake.llm import FakeLLM, FakeResponse
    from featuregen.overlay.upload.enrich_llm import audited_structured_call
    client = FakeLLM(script={"overlay.feature.recommend": FakeResponse(output={"features": []})})
    before = db.execute("SELECT count(*) FROM llm_call").fetchone()[0]
    out = audited_structured_call(
        db, client, task="overlay.feature.recommend", prompt_id="feature_recommend_v1",
        schema_id="feature_ideas",
        catalog_metadata={"columns": [{"object_ref": "public.t.c", "evil": "surprise"}]},
        instruction="predict churn")
    assert out is None                              # blocked, no dispatch
    after = db.execute("SELECT count(*) FROM llm_call").fetchone()[0]
    assert after == before                          # no llm_call recorded
```
- [ ] Run it (expect PASS): `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_feature_context_egress.py -x -q`
- [ ] Regression-guard the pre-existing enrichment egress path is untouched: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_enrich_llm.py -x -q`
- [ ] Commit: `feat(enrich-llm): nested field-aware egress adapter for the feature menu inside audited_structured_call`

---

## Task 5 — Deterministic relevance selection (pure function + byte budget + `CONTEXT_TOO_LARGE`)

**Files:**
- Modify: `src/featuregen/overlay/upload/feature_assist.py` (`RejectCode.CONTEXT_TOO_LARGE`; `ContextTooLarge`; `FEATURE_CONTEXT_BYTE_BUDGET`; `_tokenize`; `_objective_tokens`; `_objective_entity`; `_column_tokens`; `_is_mandatory`; `_assembled_bytes`; `select_relevant_context`)
- Test: `tests/featuregen/overlay/upload/test_feature_relevance.py` (new)

**Interfaces:**
- Consumes: `ConfirmedScope` (governed route), `_enriched_column`/`_table_context` (byte measurement of the assembled batch), `read_column_facts` (transitively via `_enriched_column`).
- Produces: `select_relevant_context(conn, cols, *, objective, entity, scope, byte_budget=None) -> tuple[list[dict], list[dict], int]` = `(selected_enriched_columns, table_context, dropped_count)`. Mandatory (confirmed grain / as-of / entity-match) ALWAYS included; optionals added by descending shared-token score, stable `(-score, object_ref asc)`, until ONE hard byte budget on the assembled batch. Raises `ContextTooLarge` (surfaced as `RejectCode.CONTEXT_TOO_LARGE`) when the mandatory set alone exceeds the budget — do NOT chunk. Logs the dropped count.

**Steps:**
- [ ] Write the failing test:
```python
# tests/featuregen/overlay/upload/test_feature_relevance.py
import pytest

import featuregen.overlay.upload.feature_assist as fa
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.taxonomy.applicability import ConfirmedScope


def test_tokenize_and_objective_source_priority():
    assert fa._tokenize("Churn, 30-day!") == {"churn", "30", "day"}
    scope = ConfirmedScope(primary="retail_churn", secondary=("deposit_growth",),
                           target_entity="Account", modelling_contexts=("ifrs9",))
    # Governed route derives tokens from the scope, NOT the (unrelated) objective string.
    gov = fa._objective_tokens("weather forecast", None, scope)
    assert {"retail", "churn", "deposit", "growth", "account", "ifrs9"} <= gov
    assert "weather" not in gov
    # Direct-assist route: objective free text + explicit entity.
    assert fa._objective_tokens("predict churn", "Account", None) == {"predict", "churn", "account"}
    # Lexical fallback: objective only.
    assert fa._objective_tokens("predict churn", None, None) == {"predict", "churn"}
    # unscoped governed scope falls through to assist/lexical.
    uns = ConfirmedScope(primary=None, unscoped=True)
    assert fa._objective_tokens("predict churn", None, uns) == {"predict", "churn"}


def _seed(db):
    rows = [
        CanonicalRow("bank", "accounts", "account_id", "integer", is_grain=True),
        CanonicalRow("bank", "accounts", "churn_flag", "boolean", definition="churn label"),
        CanonicalRow("bank", "accounts", "region", "text", definition="branch region"),
    ]
    build_graph(db, "bank", rows)
    db.execute("UPDATE graph_node SET grain_fact_event_id='fe_grain1' "
               "WHERE object_ref='public.accounts.account_id'")


def test_mandatory_grain_always_selected_and_scoring_order(db):
    _seed(db)
    cols = fa._candidate_columns(db, "bank", roles=())
    selected, _ctx, dropped = fa.select_relevant_context(
        db, cols, objective="predict churn", entity=None, scope=None)
    refs = [c["object_ref"] for c in selected]
    assert dropped == 0
    assert "public.accounts.account_id" in refs                 # grain is mandatory
    # churn_flag shares the token 'churn' with the objective -> ranked above region.
    assert refs.index("public.accounts.churn_flag") < refs.index("public.accounts.region")


def test_byte_budget_drops_lowest_scored_and_counts(db, monkeypatch):
    _seed(db)
    cols = fa._candidate_columns(db, "bank", roles=())
    # Budget large enough for mandatory + the single highest-scored optional, not the rest.
    mand = fa.select_relevant_context(db, [c for c in cols
                                           if c["object_ref"] == "public.accounts.account_id"],
                                      objective="predict churn", entity=None, scope=None)[0]
    one_more = fa.select_relevant_context(
        db, [c for c in cols if c["object_ref"] in
             ("public.accounts.account_id", "public.accounts.churn_flag")],
        objective="predict churn", entity=None, scope=None)[0]
    budget = fa._assembled_bytes(one_more, [])
    monkeypatch.setattr(fa, "FEATURE_CONTEXT_BYTE_BUDGET", budget)
    selected, _ctx, dropped = fa.select_relevant_context(
        db, cols, objective="predict churn", entity=None, scope=None)
    refs = [c["object_ref"] for c in selected]
    assert "public.accounts.account_id" in refs and "public.accounts.churn_flag" in refs
    assert "public.accounts.region" not in refs
    assert dropped == 1
    assert len(mand) == 1  # sanity: the mandatory-only assembly had exactly the grain column


def test_overflow_raises_context_too_large_not_chunk(db, monkeypatch):
    _seed(db)
    cols = fa._candidate_columns(db, "bank", roles=())
    monkeypatch.setattr(fa, "FEATURE_CONTEXT_BYTE_BUDGET", 5)  # smaller than mandatory alone
    with pytest.raises(fa.ContextTooLarge):
        fa.select_relevant_context(db, cols, objective="predict churn", entity=None, scope=None)
    assert fa.RejectCode.CONTEXT_TOO_LARGE == "CONTEXT_TOO_LARGE"
```
- [ ] Run it (expect FAIL — `AttributeError: _tokenize`/`select_relevant_context`): `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_feature_relevance.py -x -q`
- [ ] Implement — add `CONTEXT_TOO_LARGE = "CONTEXT_TOO_LARGE"` to `class RejectCode`; then add (after `_table_context`):
```python
# One hard byte budget on the assembled feature-context batch (spec §6). Referenced at call time so
# tests can monkeypatch it; select_relevant_context reads this module global when byte_budget is None.
FEATURE_CONTEXT_BYTE_BUDGET = 60_000

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class ContextTooLarge(Exception):
    """The mandatory feature-context set alone exceeds the single-call byte budget — surfaced as
    RejectCode.CONTEXT_TOO_LARGE. We do NOT chunk: one audited_structured_call is one audited
    llm_call, so chunking would need N calls + cross-chunk dedup and defeat the single fail-open
    audit; relevance ordering already floats the highest-relevance items into the one bounded call
    ([F13])."""


def _tokenize(text: str | None) -> set[str]:
    return set(_TOKEN_RE.findall((text or "").lower()))


def _objective_tokens(objective: str | None, entity: str | None, scope) -> set[str]:
    """The objective token set, by source priority (spec §6): the GOVERNED confirmed scope (leaf ids
    + target_entity + modelling_contexts) when present and not unscoped; else the DIRECT-ASSIST
    objective free-text + explicit entity; else the LEXICAL objective alone. NO LLM call."""
    if scope is not None and not scope.unscoped:
        toks: set[str] = set()
        for uid in ([scope.primary] if scope.primary else []) + list(scope.secondary):
            toks |= _tokenize(uid)
        toks |= _tokenize(scope.target_entity)
        for mc in scope.modelling_contexts:
            toks |= _tokenize(mc)
        return toks
    return _tokenize(objective) | _tokenize(entity)


def _objective_entity(entity: str | None, scope) -> str | None:
    """The entity used for the mandatory entity-match: the confirmed target_entity (governed) else
    the explicit assist entity."""
    if scope is not None and not scope.unscoped and scope.target_entity:
        return scope.target_entity
    return entity


def _column_tokens(col: dict) -> set[str]:
    toks: set[str] = set()
    for k in ("object_ref", "table", "column", "concept", "domain", "semantic_terms", "entity"):
        v = col.get(k)
        if isinstance(v, str):
            toks |= _tokenize(v)
    return toks


def _is_mandatory(col: dict, objective_entity: str | None) -> bool:
    """Always-included: a confirmed grain column, the confirmed as-of column, or a column whose
    entity matches the objective entity (spec §6)."""
    if col["is_grain"] and col["grain_fact_event_id"]:
        return True
    if col["is_as_of"] and col["availability_fact_event_id"]:
        return True
    ent = col.get("entity")
    return (objective_entity is not None and isinstance(ent, str)
            and ent.lower() == objective_entity.lower())


def _assembled_bytes(columns: list[dict], table_context: list[dict]) -> int:
    return len(json.dumps({"columns": columns, "table_context": table_context},
                          sort_keys=True, default=str).encode("utf-8"))


def select_relevant_context(conn, cols: list[dict], *, objective: str | None,
                            entity: str | None, scope=None,
                            byte_budget: int | None = None) -> tuple[list[dict], list[dict], int]:
    """Deterministic relevance selection ([F13], spec §6). Returns
    (selected_enriched_columns, table_context, dropped_count). Mandatory columns (confirmed grain,
    as-of, entity-match) are ALWAYS included; the rest are added by descending shared-token score,
    stable (-score, object_ref asc), until the ONE hard byte budget on the assembled batch is
    reached. Raises ContextTooLarge when the mandatory set alone exceeds the budget (do NOT chunk).
    Logs the dropped count."""
    if byte_budget is None:
        byte_budget = FEATURE_CONTEXT_BYTE_BUDGET
    obj_tokens = _objective_tokens(objective, entity, scope)
    obj_entity = _objective_entity(entity, scope)
    enriched_by_ref = {(c["catalog_source"], c["object_ref"]): _enriched_column(conn, c)
                       for c in cols}

    def _enriched(rows: list[dict]) -> list[dict]:
        return [enriched_by_ref[(c["catalog_source"], c["object_ref"])] for c in rows]

    mandatory = [c for c in cols if _is_mandatory(c, obj_entity)]
    optional = [c for c in cols if not _is_mandatory(c, obj_entity)]
    scored = sorted(optional,
                    key=lambda c: (-len(_column_tokens(c) & obj_tokens), c["object_ref"]))

    selected = list(mandatory)
    if _assembled_bytes(_enriched(selected), _table_context(selected)) > byte_budget:
        raise ContextTooLarge(
            f"mandatory feature context ({len(mandatory)} columns) exceeds byte budget "
            f"{byte_budget}; not chunking")
    dropped = 0
    for i, c in enumerate(scored):
        trial = selected + [c]
        if _assembled_bytes(_enriched(trial), _table_context(trial)) > byte_budget:
            dropped = len(scored) - i
            break
        selected = trial
    if dropped:
        logger.info("feature-context relevance dropped %d of %d optional columns (byte budget %d)",
                    dropped, len(optional), byte_budget)
    return _enriched(selected), _table_context(selected), dropped
```
- [ ] Run it (expect PASS): `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_feature_relevance.py -x -q`
- [ ] Commit: `feat(feature-assist): deterministic relevance selection with one byte budget and CONTEXT_TOO_LARGE`

---

## Task 6 — Wire the enriched menu + context + relevance into the generation paths (flag-gated)

**Files:**
- Modify: `src/featuregen/overlay/upload/feature_assist.py` (`_build_menu`; `_generate` + `scope` threading; `_fix_pass` table_context; `refine_idea`; `feature_recipe`)
- Test: `tests/featuregen/overlay/upload/test_feature_relevance.py`

**Interfaces:**
- Produces: `_build_menu(conn, cols, *, objective=None, entity=None, scope=None) -> tuple[list[dict], list[dict]]` = `(menu, table_context)`. Flag-OFF ⟹ `(_menu(cols), [])` — the thin menu, no context (BYTE-IDENTICAL). Flag-ON ⟹ `select_relevant_context(...)` (may raise `ContextTooLarge`). `_generate`/`recommend_features`/`recommend_features_report` gain `scope: ConfirmedScope | None = None`. `_generate` catches `ContextTooLarge` and returns `([], [{"name":"", "reason":..., "code": RejectCode.CONTEXT_TOO_LARGE}])`.
- Consumes: `_build_menu`, `select_relevant_context`, `ContextTooLarge`.

**Steps:**
- [ ] Write the failing test (append to `test_feature_relevance.py`):
```python
def _capture_client(captured):
    from featuregen.intake.llm import LLMResult

    class _CaptureLLM:
        def call(self, request):
            captured.append(dict(request.inputs.get("catalog_metadata", {})))
            return LLMResult(output={"features": []}, self_reported_scores={}, call_ref="",
                             status="ok")

    return _CaptureLLM()


def test_flag_off_menu_byte_identical(db, monkeypatch):
    _seed(db)
    monkeypatch.delenv("FEATUREGEN_FEATURE_CONTEXT", raising=False)
    captured: list = []
    fa.recommend_features(db, "predict churn", _capture_client(captured), catalog_source="bank",
                          budget=1, critic=False)
    meta = captured[0]
    assert "table_context" not in meta                 # no context block flag-off
    assert all(set(m.keys()) == {"object_ref", "table", "column", "concept", "domain"}
               for m in meta["columns"])               # thin projection only


def test_flag_on_menu_enriched_with_context_and_relevance(db, monkeypatch):
    _seed(db)
    monkeypatch.setenv("FEATUREGEN_FEATURE_CONTEXT", "1")
    captured: list = []
    fa.recommend_features(db, "predict churn", _capture_client(captured), catalog_source="bank",
                          budget=1, critic=False)
    meta = captured[0]
    assert "table_context" in meta
    amount = next(m for m in meta["columns"] if m["object_ref"] == "public.accounts.churn_flag")
    assert amount["additivity"]["authority"] in ("governed", "hint")  # wrapped fact


def test_flag_on_overflow_surfaces_context_too_large(db, monkeypatch):
    _seed(db)
    monkeypatch.setenv("FEATUREGEN_FEATURE_CONTEXT", "1")
    monkeypatch.setattr(fa, "FEATURE_CONTEXT_BYTE_BUDGET", 5)
    report = fa.recommend_features_report(db, "predict churn", _capture_client([]),
                                          catalog_source="bank", budget=1, critic=False)
    assert report.ideas == []
    assert any(r["code"] == fa.RejectCode.CONTEXT_TOO_LARGE for r in report.rejections)
```
- [ ] Run it (expect FAIL — flag-on has no `table_context`; overflow raises instead of surfacing): `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_feature_relevance.py -x -q -k "flag_off or flag_on"`
- [ ] Implement — add `_build_menu` (after `select_relevant_context`):
```python
def _build_menu(conn, cols: list[dict], *, objective: str | None = None,
                entity: str | None = None, scope=None) -> tuple[list[dict], list[dict]]:
    """The menu + per-table context for one generation call. Flag-OFF ⟹ the thin pre-Slice-3 menu
    and NO context (byte-identical). Flag-ON ⟹ the enriched, relevance-selected menu + context
    (may raise ContextTooLarge)."""
    if not _feature_context_enabled():
        return _menu(cols), []
    columns, table_context, _dropped = select_relevant_context(
        conn, cols, objective=objective, entity=entity, scope=scope)
    return columns, table_context
```
Import `ConfirmedScope` for the type hints/threading:
```python
from featuregen.overlay.upload.taxonomy.applicability import ConfirmedScope
```
In `_generate`: add `scope: ConfirmedScope | None = None` to the signature; replace `menu = _menu(cols)` with a guarded build and thread the context:
```python
    try:
        menu, table_context = _build_menu(
            conn, cols, objective=objective, entity=entity, scope=scope)
    except ContextTooLarge as exc:
        logger.warning("feature context too large for %r: %s", objective, exc)
        return [], [{"name": "", "reason": str(exc), "code": RejectCode.CONTEXT_TOO_LARGE}]
```
In the Phase-1 generation loop, build `inputs` so flag-off stays byte-identical (only add `table_context` when non-empty):
```python
        inputs: dict = {"columns": menu, "avoid": avoid}
        if table_context:
            inputs["table_context"] = table_context
        if feedback:
            inputs["feedback"] = feedback
```
Pass `table_context` into the `_fix_pass` call:
```python
                accepted = _fix_pass(conn, client, objective, accepted, issues, menu, known, src_of,
                                     registered, target_ref, now, fresh_within, feedback,
                                     table_context=table_context, actor=actor)
```
Add `table_context: list[dict] | None = None` (keyword-only via `*`) to `_fix_pass` and include it (non-empty only):
```python
    inputs: dict = {"columns": menu, "fix": fix_hints}
    if table_context:
        inputs["table_context"] = table_context
    if feedback:
        inputs["feedback"] = feedback
```
Thread `scope` through `recommend_features` and `recommend_features_report` (add `scope: ConfirmedScope | None = None` and pass `scope=scope` into their `_generate(...)` calls). In `refine_idea`, replace `inputs: dict = {"columns": _menu(cols), "fix": fix}` with a guarded build that surfaces overflow as a rejection:
```python
    try:
        menu, table_context = _build_menu(conn, cols, objective=objective, entity=entity)
    except ContextTooLarge as exc:
        return None, {"name": str(idea.get("name", "")), "reason": str(exc),
                      "code": RejectCode.CONTEXT_TOO_LARGE}
    inputs: dict = {"columns": menu, "fix": fix}
    if table_context:
        inputs["table_context"] = table_context
```
In `feature_recipe`, replace `{"columns": _menu(cols)}` with a guarded build (an overflow returns an empty Recipe honestly):
```python
    try:
        menu, table_context = _build_menu(conn, cols, objective=nl_query)
    except ContextTooLarge as exc:
        logger.warning("feature-recipe context too large for %r: %s", nl_query, exc)
        return Recipe(intent=nl_query, grain_table=None, derives_from=[], aggregation=None,
                      as_of_column=None)
    recipe_inputs: dict = {"columns": menu}
    if table_context:
        recipe_inputs["table_context"] = table_context
    out = _call_raw(conn, client, "overlay.feature.recipe", "feature_recipe_v1", "feature_recipe",
                    nl_query, recipe_inputs, actor=actor)
```
- [ ] Run it (expect PASS): `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_feature_relevance.py -x -q`
- [ ] Run the existing feature-assist suites to confirm flag-off is untouched: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_feature_assist.py tests/featuregen/overlay/upload/test_feature_assist_hitl.py tests/featuregen/api/test_feature_assist.py -x -q`
- [ ] Commit: `feat(feature-assist): flag-gated enriched menu + context + relevance wiring; surface CONTEXT_TOO_LARGE`

---

## Task 7 — CRITICAL sample-safety end-to-end (planted token absent from request AND `llm_call.redacted_input`)

**Files:**
- Test: `tests/featuregen/overlay/upload/test_feature_context_egress.py`

**Interfaces:**
- Consumes: the full flag-ON path (`recommend_features` → `_build_menu` → `_enriched_menu` → `audited_structured_call` → `sanitize_feature_context` → `record_llm_call`). No production code changes — this is the acceptance gate for the whole slice.

**Steps:**
- [ ] Write the failing test (append to `test_feature_context_egress.py`):
```python
import json

import featuregen.overlay.upload.feature_assist as fa
from featuregen.intake.llm import LLMResult
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.graph import build_graph

_PLANTED = "3708484836801"
_DEF = ("Posting amount is the monetary value of the ledger entry, with representative values "
        "such as 3708484836801; 3708446902413; 3708454004701, which supports interpretation.")


def test_planted_sample_token_never_egresses_and_is_audited(db, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_FEATURE_CONTEXT", "1")
    build_graph(db, "bank", [
        CanonicalRow("bank", "transactions", "amount", "numeric", definition=_DEF)])

    captured: list = []

    class _CaptureLLM:
        def call(self, request):
            captured.append(json.loads(json.dumps(dict(request.inputs), default=str)))
            return LLMResult(output={"features": []}, self_reported_scores={}, call_ref="",
                             status="ok")

    fa.recommend_features(db, "predict spend", _CaptureLLM(), catalog_source="bank",
                          budget=1, critic=False)

    # 1. Absent from the actual provider request.
    assert captured, "the model was never called"
    req_meta = captured[0]["catalog_metadata"]
    col = next(c for c in req_meta["columns"] if c["object_ref"] == "public.transactions.amount")
    assert _PLANTED not in col["definition"]
    assert _PLANTED not in json.dumps(captured[0])

    # 2. Absent from the persisted llm_call.redacted_input.
    row = db.execute("SELECT redacted_input, input_redaction FROM llm_call "
                     "WHERE task = 'overlay.feature.recommend' "
                     "ORDER BY created_at DESC LIMIT 1").fetchone()
    redacted_input, input_redaction = row[0], row[1]
    assert _PLANTED not in json.dumps(redacted_input)

    # 3. A sample_strip audit was persisted for that definition path.
    sample_strip = input_redaction.get("sample_strip", [])
    hit = next((a for a in sample_strip if a["path"] == "columns[0].definition"), None)
    assert hit is not None and hit["removed_count"] >= 1
    assert hit["state"] == "stripped"
```
- [ ] Run it (expect PASS if Tasks 1-6 are correct; a FAIL here is a real sample-safety defect — debug the adapter/wiring, never the test): `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_feature_context_egress.py::test_planted_sample_token_never_egresses_and_is_audited -x -q`
- [ ] Full-slice regression: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_feature_menu_enrichment.py tests/featuregen/overlay/upload/test_feature_context_egress.py tests/featuregen/overlay/upload/test_feature_relevance.py tests/featuregen/overlay/upload/test_enrich_llm.py -q`
- [ ] Ruff: `.venv/bin/ruff check src/featuregen/overlay/upload/feature_assist.py src/featuregen/overlay/upload/enrich_llm.py tests/featuregen/overlay/upload/test_feature_menu_enrichment.py tests/featuregen/overlay/upload/test_feature_context_egress.py tests/featuregen/overlay/upload/test_feature_relevance.py`
- [ ] Commit: `test(feature-context): CRITICAL sample-safety e2e — planted token absent from request + llm_call, audited`

---

## Self-Review

**Spec coverage (§5, §6):**
- §5 menu widening — Task 1 selects `data_type, declared_type, semantic_terms, entity, additivity, unit, currency, is_grain, is_as_of` + `grain_fact_event_id`/`availability_fact_event_id`; `_menu` no longer discards (Task 2 `_enriched_menu` emits every field, each governed/hint fact wrapped as `OperationalColumnFacts{value,authority}` via `read_column_facts`).
- §5 per-table context — Task 3 builds one block per table from the ALREADY-AUTHORIZED candidate rows only (single scoped query in Task 1's LEFT JOIN; NO second unscoped query); confirmed grain/as-of require a non-null `*_fact_event_id`; a fully read-scope-excluded table yields no rows → no block; `primary_entity` advisory.
- §5 nested field-aware egress adapter — Task 4 `sanitize_feature_context` invoked INSIDE `audited_structured_call` before `build_llm_inputs`/`assert_llm_safe`/dispatch; definition-kind → `sanitize_definition` (Slice-1 reuse) with spans → `input_redaction["redacted_spans"]` and `{path,sanitizer_version,state,removed_count}` → `input_redaction["sample_strip"]`; structural → exact-key allowlist + length bound; fail-closed shape gate (unclassified key blocks; blanked definition → no dispatch + `EGRESS_BLOCKED`).
- §5 CRITICAL sample-safety — Task 7 proves a planted token in `columns[0].definition` is absent from the provider request AND from `llm_call.redacted_input`, with a `sample_strip` audit persisted.
- §6 deterministic relevance — Task 5: objective from `ConfirmedScope` (governed) / `entity`+`objective` (assist) / lexical fallback (lowercase-tokenize, shared-token score, stable `(-score, object_ref asc)`); mandatory set (grain/as-of/entity-match); ONE hard byte budget; overflow → `CONTEXT_TOO_LARGE`, NOT chunk; dropped count logged. Task 6 wires it flag-gated and surfaces `CONTEXT_TOO_LARGE` as a rejection.
- Flag-off byte-identity — Tasks 1/2/6: `_menu` stays thin; `_build_menu` returns `(_menu(cols), [])` flag-off; `table_context` added only when non-empty; the egress adapter returns the SAME object on structural-only/non-feature payloads (Task 4 tests). `assert_llm_safe`/enrichment path unchanged (Task 4 enrich_llm regression run).

**Placeholder scan:** No `...` in any test or implementation block; every test carries concrete assertions; every code block is complete and paste-ready. Commands are direct `.venv/bin/python -m pytest ... -x -q` (never `| tail`).

**Type consistency vs the shared contract:**
- `OperationalColumnFacts{value,authority,provenance}` + `read_column_facts(conn, logical_ref, field_name)` consumed verbatim from `column_authority` (3A-i); the menu emits only `{value, authority}` (provenance is an internal id, not egressed) per spec §5.
- `ConfirmedScope` fields (`primary/secondary/unscoped/target_entity/modelling_contexts`) consumed verbatim from `taxonomy.applicability`.
- `sanitize_definition`/`DefinitionSanitize` reused verbatim (`clean/state/removed/sanitizer_version/redaction_version/reason/redacted_spans`).
- `FEATUREGEN_FEATURE_CONTEXT` flag (default off) and `CONTEXT_TOO_LARGE` code introduced exactly as named. `FeatureIdea`/`Requirement`/`validation_status` are NOT redefined here (owned by 3A-i/3A-ii); this plan does not touch the validator or the contract-carry.

**Concerns where the spec/contract is ambiguous for this area (surfaced, not silently resolved):**
1. **`read_column_facts` signature omits `catalog_source`.** It takes only `logical_ref` (object_ref). In the cross-catalog `entity` gather, one `object_ref` can exist in two catalogs; `read_column_facts` cannot disambiguate which node's authority to read. `_enriched_column` passes `c["object_ref"]`, and relevance keys enrichment by `(catalog_source, object_ref)`, but the authority read itself is catalog-blind — a latent cross-catalog ambiguity owned by 3A-i's signature.
2. **`data_type` → `logical_representation` field-name seam.** The spec §5 names the menu key `data_type`, but the shared contract's `read_column_facts` field_name domain uses `logical_representation` for the structural type (value = operational `graph_node.data_type`). I mapped menu-key→field-name explicitly (Task 2); if 3A-i instead recognizes `"data_type"` as a field_name, the mapping constant is the single change point.
3. **Governed-route wiring point for the feature suggester.** §6 places the governed `ConfirmedScope` at `contract.py:312-342` *before* `build_considered_set`, but that path builds the TEMPLATE considered-set — `recommend_features` (the feature suggester this plan enriches) is invoked from the assist routes with `entity`+`objective`, not a `ConfirmedScope`. I implemented the capability (a `scope` kwarg threaded through `_generate`/`recommend_features[_report]`) and default to the assist/lexical route; the actual caller that passes a `ConfirmedScope` into `recommend_features` is unspecified and left to the route owner (touches assist-route request shapes owned by 3A-iv).
4. **`table_definition` sourcing vs "never a second unscoped query."** I read it via a LEFT JOIN to the column's own table node in the SAME scoped candidate query (Task 1) — one query, read-scoped on the column. If the reviewer reads "never a second query" as forbidding even the joined table-node read, drop `t.definition`/`t.primary_entity` from the SELECT and omit `table_definition`/`primary_entity` from the context block (the egress adapter's `table_definition` branch stays as defensive dead code).
5. **"Summarize the rest" on optional overflow.** The spec says both "summarize the rest" and "log the dropped/summarized count." I implemented the log + a returned `dropped_count` (kept off the wire to avoid perturbing the byte budget); no summary block is added to the payload. If a payload-visible summary is required, add a single bounded `{"omitted_columns": N}` scalar and reserve headroom in the budget.
