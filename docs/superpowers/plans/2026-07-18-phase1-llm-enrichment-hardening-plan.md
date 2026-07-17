# Phase-1 LLM-Enrichment Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make LLM enrichment work correctly and safely against Anthropic's structured-output API, and make the FTR pipeline's enrichment truthful, bounded, and evidence-based.

**Architecture:** A provider-schema **projection** layer keeps one canonical strict schema per task (local validation/persistence) and derives an Anthropic-compatible schema for the wire only; response validation stays canonical. Around it, seven correctness/quality fixes (parser reconciliation, complete Pass B context, valid abstention, bounded execution, truthful result, source guard, meaning-preserving truncation) plus evidence-based batch ceilings and a real-provider sweep.

**Tech Stack:** Python 3.11, FastAPI, psycopg3, PostgreSQL, `jsonschema>=4.21`, `anthropic` SDK (lazy prod import), React/TypeScript/vitest frontend.

## Global Constraints

- Branch `phase1-llm-enrichment-hardening` **off `325fd84`** (= origin/main `9852b1c` + maxItems fix). Do **not** commit to the shared root checkout.
- **All subagent dispatches on Opus 4.8** (Fable credits exhausted).
- Canonical strict schema for local validation/persistence; projected schema on the wire **only**; response validation stays against the canonical schema (`reg.validate` in the driver — unchanged).
- Provider-unsupported keywords to strip on the wire: `maxLength`, `maxItems`, `minItems`, `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum`, `multipleOf`.
- Nullable-enum wire form: `{"anyOf":[{"type":"T","enum":[<non-null members>]},{"type":"null"}]}`.
- `anthropic` declared version-bounded in `pyproject.toml` (`[project.optional-dependencies].llm`, floor `>=0.117,<1.0` — the proven-good deploy version); never imported at module scope.
- Metadata-only egress; sanitize before transmission; per-value egress ≤200 chars, sanitized `business_definition` ≤600 (`_MAX_DEFINITION_LEN`).
- Batch ceilings = `{concept:20, definition:8, domain:8, table_synth:4}` (ceilings; token budget second).
- The real `FTR_Column_Mapping*.csv` is **read-only, never committed/copied**; CI acceptance uses a synthetic fixture. Remind the user to rotate the exposed Anthropic key.
- Run the backend suite (`pytest -q`) after each task; the overlay/upload + intake suites are the fast inner loop.

---

### Task 1: Provider-schema projection module

**Files:**
- Create: `src/featuregen/intake/schema_projection.py`
- Test: `tests/featuregen/intake/test_schema_projection.py`

**Interfaces:**
- Produces:
  - `PROVIDER_UNSUPPORTED_KEYWORDS: frozenset[str]`
  - `project_for_anthropic(schema: dict) -> dict` — deep-copied, provider-compatible schema.
  - `provider_incompatibilities(schema: dict) -> list[str]` — human-readable `"<keyword> at <path>"` list; `[]` means compatible.
  - `assert_schemas_provider_compatible(schemas: Iterable[tuple[str, dict]]) -> None` — raises `ValueError` naming the first schema whose **projection** is still incompatible.

- [ ] **Step 1: Write the failing tests**

```python
# tests/featuregen/intake/test_schema_projection.py
from featuregen.intake.schema_projection import (
    project_for_anthropic, provider_incompatibilities, assert_schemas_provider_compatible,
)


def test_strips_unsupported_keywords_everywhere():
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "maxLength": 40, "minLength": 1},
            "items": {"type": "array", "items": {"type": "string", "maxLength": 8},
                      "maxItems": 10, "minItems": 1},
            "n": {"type": "integer", "minimum": 0, "maximum": 5, "multipleOf": 1},
        },
        "required": ["name"],
    }
    out = project_for_anthropic(schema)
    assert provider_incompatibilities(out) == []
    # structure preserved
    assert out["required"] == ["name"]
    assert out["properties"]["name"]["type"] == "string"
    assert out["properties"]["items"]["items"]["type"] == "string"
    # minLength is allowed by the API and is kept
    assert out["properties"]["name"].get("minLength") == 1


def test_normalizes_nullable_enum_to_anyof():
    schema = {"type": "object", "properties": {
        "basis": {"type": ["string", "null"], "enum": ["event", "snapshot", None]}}}
    out = project_for_anthropic(schema)
    basis = out["properties"]["basis"]
    assert "enum" not in basis and basis["type"] != ["string", "null"]
    variants = basis["anyOf"]
    string_variant = next(v for v in variants if v.get("type") == "string")
    assert string_variant["enum"] == ["event", "snapshot"]
    assert any(v.get("type") == "null" for v in variants)
    assert provider_incompatibilities(out) == []


def test_plain_enum_and_plain_nullable_are_untouched():
    schema = {"type": "object", "properties": {
        "role": {"type": "string", "enum": ["a", "b"]},
        "note": {"type": ["string", "null"]},
    }}
    out = project_for_anthropic(schema)
    assert out["properties"]["role"]["enum"] == ["a", "b"]
    assert out["properties"]["note"]["type"] == ["string", "null"]


def test_does_not_mutate_input_and_is_idempotent():
    schema = {"type": "object", "properties": {"x": {"type": "string", "maxLength": 3}}}
    once = project_for_anthropic(schema)
    twice = project_for_anthropic(once)
    assert "maxLength" in schema["properties"]["x"]      # input untouched
    assert once == twice                                 # idempotent


def test_incompatibilities_reports_paths():
    schema = {"type": "object", "properties": {"x": {"type": "string", "maxLength": 3}}}
    probs = provider_incompatibilities(schema)
    assert any("maxLength" in p for p in probs)


def test_assert_raises_when_projection_cannot_clean():
    # a schema node with no type/anyOf is unprojectable-clean → guard raises
    bad = {"type": "object", "properties": {"x": {"maxLength": 3}}}  # x has no 'type'
    try:
        assert_schemas_provider_compatible([("bad", project_for_anthropic(bad))])
    except ValueError as e:
        assert "bad" in str(e)
    else:
        raise AssertionError("expected ValueError")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/featuregen/intake/test_schema_projection.py -q`
Expected: FAIL (module does not exist).

- [ ] **Step 3: Implement the module**

```python
# src/featuregen/intake/schema_projection.py
"""Provider-schema projection for Anthropic structured outputs.

The canonical enrichment schemas are strict JSON Schema, built for local `jsonschema` validation and
persistence. Anthropic's structured-output API accepts only a SUBSET of JSON Schema, so we project a
provider-compatible schema for the WIRE ONLY (this module) while the canonical schema remains the
source of truth for validating the model's RESPONSE (the driver's `reg.validate`, unchanged).

Two transforms: (1) strip provider-unsupported constraint keywords; (2) normalize a nullable-enum
`{"type":["T","null"],"enum":[...,null]}` into the accepted union `{"anyOf":[{"type":"T",
"enum":[...]},{"type":"null"}]}`. Pure + deterministic + SDK-independent so a static test can prove
every outbound schema is clean before any deploy."""
from __future__ import annotations

import copy
from collections.abc import Iterable

# Constraint keywords Anthropic's json_schema output format rejects. Length/array-size/numeric bounds.
PROVIDER_UNSUPPORTED_KEYWORDS = frozenset({
    "maxLength", "maxItems", "minItems", "minimum", "maximum",
    "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
})

_NESTED_SCHEMA_KEYS = ("properties", "$defs", "definitions")
_COMBINATOR_KEYS = ("anyOf", "oneOf", "allOf")


def project_for_anthropic(schema: dict) -> dict:
    """Return a deep-copied, Anthropic-compatible projection of `schema`."""
    return _project(copy.deepcopy(schema))


def _project(node: object) -> object:
    if not isinstance(node, dict):
        if isinstance(node, list):
            return [_project(x) for x in node]
        return node
    # 1) nullable-enum → anyOf union (before stripping, so we don't touch enum on plain strings)
    node = _normalize_nullable_enum(node)
    # 2) drop unsupported constraint keywords at this level
    for kw in list(node):
        if kw in PROVIDER_UNSUPPORTED_KEYWORDS:
            del node[kw]
    # 3) recurse into nested schema containers
    for key in _NESTED_SCHEMA_KEYS:
        if isinstance(node.get(key), dict):
            node[key] = {k: _project(v) for k, v in node[key].items()}
    if isinstance(node.get("items"), (dict, list)):
        node["items"] = _project(node["items"])
    for key in _COMBINATOR_KEYS:
        if isinstance(node.get(key), list):
            node[key] = [_project(v) for v in node[key]]
    return node


def _normalize_nullable_enum(node: dict) -> dict:
    t, enum = node.get("type"), node.get("enum")
    if not (isinstance(t, list) and "null" in t and isinstance(enum, list)):
        return node
    non_null_types = [x for x in t if x != "null"]
    members = [m for m in enum if m is not None]
    variants: list[dict] = []
    for st in non_null_types:
        variants.append({"type": st, "enum": members})
    variants.append({"type": "null"})
    rebuilt = {k: v for k, v in node.items() if k not in ("type", "enum")}
    rebuilt["anyOf"] = variants
    return rebuilt


def provider_incompatibilities(schema: object, _path: str = "$") -> list[str]:
    """List `"<keyword> at <path>"` for every provider-incompatibility in `schema` ([] = clean)."""
    problems: list[str] = []
    if isinstance(schema, list):
        for i, x in enumerate(schema):
            problems += provider_incompatibilities(x, f"{_path}[{i}]")
        return problems
    if not isinstance(schema, dict):
        return problems
    for kw in schema:
        if kw in PROVIDER_UNSUPPORTED_KEYWORDS:
            problems.append(f"{kw} at {_path}")
    t, enum = schema.get("type"), schema.get("enum")
    if isinstance(t, list) and "null" in t and isinstance(enum, list):
        problems.append(f"nullable-enum at {_path}")
    for key in _NESTED_SCHEMA_KEYS:
        if isinstance(schema.get(key), dict):
            for k, v in schema[key].items():
                problems += provider_incompatibilities(v, f"{_path}.{key}.{k}")
    if "items" in schema:
        problems += provider_incompatibilities(schema["items"], f"{_path}.items")
    for key in _COMBINATOR_KEYS:
        if isinstance(schema.get(key), list):
            for i, v in enumerate(schema[key]):
                problems += provider_incompatibilities(v, f"{_path}.{key}[{i}]")
    return problems


def assert_schemas_provider_compatible(schemas: Iterable[tuple[str, dict]]) -> None:
    """Raise ValueError if any already-projected schema is still provider-incompatible."""
    for name, schema in schemas:
        problems = provider_incompatibilities(schema)
        if problems:
            raise ValueError(f"schema {name!r} is not Anthropic-compatible after projection: "
                             f"{', '.join(problems)}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/featuregen/intake/test_schema_projection.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/intake/schema_projection.py tests/featuregen/intake/test_schema_projection.py
git commit -m "feat(intake): provider-schema projection for Anthropic structured outputs"
```

---

### Task 2: Wire projection into ClaudeLLM + safe 400 recording + registration guard + SDK dep + static/enforcement tests

**Files:**
- Modify: `src/featuregen/intake/llm_claude.py` (`ClaudeLLM.call` — project before `output_config`; record 400 keyword)
- Modify: `src/featuregen/overlay/upload/enrich_llm.py` (`register_enrichment_schemas` — projection guard)
- Modify: `pyproject.toml` (`[project.optional-dependencies].llm = ["anthropic>=0.117,<1.0"]`)
- Modify: `deploy/kind/Dockerfile.backend` (`pip install -e ".[llm]"` instead of the separate unbounded install)
- Test: `tests/featuregen/intake/test_llm_claude_projection.py`
- Test: `tests/featuregen/overlay/upload/test_enrich_schema_compat.py`

**Interfaces:**
- Consumes: Task 1 `project_for_anthropic`, `provider_incompatibilities`, `assert_schemas_provider_compatible`.
- Produces: `ClaudeLLM.call` sends a projected schema; `register_enrichment_schemas` raises on an incompatible schema; `_SCHEMAS` reachable for the static test.

- [ ] **Step 1: Write the failing tests**

```python
# tests/featuregen/intake/test_llm_claude_projection.py
"""ClaudeLLM must send Anthropic a PROJECTED schema, and record a schema-rejection 400 safely."""
from featuregen.intake.llm import LLMRequest
from featuregen.intake.llm_claude import ClaudeConfig, ClaudeLLM
from featuregen.intake.schema_projection import provider_incompatibilities

CANONICAL = {"type": "object", "properties": {
    "results": {"type": "array", "items": {"type": "object", "properties": {
        "ref": {"type": "string", "maxLength": 128},
        "basis": {"type": ["string", "null"], "enum": ["event", "snapshot", None]},
    }}, "maxItems": 40}}}


class _CaptureClient:
    def __init__(self): self.sent_schema = None
    def messages(self): ...  # placeholder; real seam is `create` below


def test_call_projects_schema_before_send(monkeypatch):
    captured = {}

    class FakeMessages:
        def create(self, **kwargs):
            captured["schema"] = kwargs["output_config"]["format"]["schema"]
            captured["timeout"] = kwargs.get("timeout")
            raise RuntimeError("stop-after-capture")  # we only need the outbound schema

    class FakeClient:
        messages = FakeMessages()

    llm = ClaudeLLM(ClaudeConfig(enabled=True))
    monkeypatch.setattr(llm, "_ensure_client", lambda: FakeClient())
    req = LLMRequest(task="t", prompt_id="p", prompt_version=1, inputs={"x": 1},
                     output_schema_id="s", output_schema_version=1,
                     generation_settings={}, output_schema=CANONICAL)
    llm.call(req)  # RuntimeError is swallowed into a fail outcome by call()'s guards
    assert provider_incompatibilities(captured["schema"]) == []


def test_schema_rejection_400_records_keyword(monkeypatch, caplog):
    import anthropic

    class FakeMessages:
        def create(self, **kwargs):
            raise anthropic.APIStatusError(
                message="output_config.format.schema: 'maxLength' is not supported",
                response=_Resp(400), body=None)

    class FakeClient:
        messages = FakeMessages()

    llm = ClaudeLLM(ClaudeConfig(enabled=True))
    monkeypatch.setattr(llm, "_ensure_client", lambda: FakeClient())
    req = LLMRequest(task="t", prompt_id="p", prompt_version=1, inputs={"x": 1},
                     output_schema_id="s", output_schema_version=1,
                     generation_settings={}, output_schema=CANONICAL)
    out = llm.call(req)
    assert out.status  # a fail status, not a raise
    assert any("maxLength" in r.message and "400" in r.message for r in caplog.records)
```

```python
# a tiny status-carrying stub for APIStatusError construction
class _Resp:
    def __init__(self, status_code): self.status_code = status_code
    request = None
```

```python
# tests/featuregen/overlay/upload/test_enrich_schema_compat.py
"""Every enrichment schema must project to an Anthropic-compatible schema, and registration must
refuse an incompatible one."""
import pytest

from featuregen.intake.schema_projection import project_for_anthropic, provider_incompatibilities
from featuregen.overlay.upload.enrich_llm import _SCHEMAS


def test_every_enrichment_schema_projects_clean():
    for (name, ver), schema in _SCHEMAS.items():
        projected = project_for_anthropic(schema)
        assert provider_incompatibilities(projected) == [], f"{name} v{ver} still incompatible"
        # structure preserved: top-level required/properties survive
        if "properties" in schema:
            assert set(projected["properties"]) == set(schema["properties"])


def test_registration_rejects_incompatible_schema(db, monkeypatch):
    from featuregen.overlay.upload import enrich_llm
    poisoned = dict(_SCHEMAS)
    poisoned[("overlay_bad", 1)] = {"type": "object", "properties": {"x": {"maxLength": 3}}}
    monkeypatch.setattr(enrich_llm, "_SCHEMAS", poisoned)
    with pytest.raises(ValueError):
        enrich_llm.register_enrichment_schemas(db)
```

```python
# tests/featuregen/overlay/upload/test_enrich_response_enforcement.py
"""A response violating a STRIPPED constraint is still rejected by canonical validation."""
from featuregen.overlay.upload.enrich_llm import _SCHEMAS
from featuregen.documents.registry import DocumentSchemaRegistry


def test_canonical_validation_still_enforces_maxlength(db):
    reg = DocumentSchemaRegistry(db)
    schema = _SCHEMAS[("overlay_concept_batch", 1)]
    reg.register_schema("overlay_concept_batch", 1, schema, "test")
    too_long = {"results": [{"ref": "x" * 500, "concept": "amount"}]}
    import pytest
    from featuregen.documents.registry import SchemaValidationError
    with pytest.raises(SchemaValidationError):
        reg.validate("overlay_concept_batch", 1, too_long)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/featuregen/intake/test_llm_claude_projection.py tests/featuregen/overlay/upload/test_enrich_schema_compat.py -q`
Expected: FAIL (projection not wired; guard absent).

- [ ] **Step 3: Wire the projection + 400 recording in `ClaudeLLM.call`**

In `src/featuregen/intake/llm_claude.py`, import at top of file (module scope is fine — pure, no SDK):
```python
from featuregen.intake.schema_projection import project_for_anthropic
```
Change the `output_config` build (currently lines 117-120) to project the schema:
```python
            wire_schema = project_for_anthropic(request.output_schema)
            output_config = {
                "effort": request.generation_settings.get("effort", self._config.effort),
                "format": {"type": "json_schema", "schema": wire_schema},
            }
```
In the `except anthropic.APIStatusError as exc:` branch (line 128), before the existing status
mapping, add a safe diagnostic for a schema-rejection 400 (no request/response content):
```python
        except anthropic.APIStatusError as exc:
            status = getattr(exc, "status_code", 0)
            if status == 400:
                keyword = _rejected_schema_keyword(str(getattr(exc, "message", exc)))
                logger.warning("anthropic rejected structured-output schema (HTTP 400, keyword=%s)",
                               keyword or "unknown")
            if status in (401, 403):
                return _fail(PROVIDER_AUTH_ERROR)
            ...  # unchanged 429/5xx and catch-all below
```
Add the helper (module scope):
```python
_SCHEMA_KEYWORDS = ("maxLength", "maxItems", "minItems", "minimum", "maximum",
                    "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "enum", "type")


def _rejected_schema_keyword(message: str) -> str | None:
    """Best-effort extraction of the rejected JSON-Schema keyword from a provider 400 message.
    Returns only a keyword token — never the message body — so nothing content-bearing is logged."""
    for kw in _SCHEMA_KEYWORDS:
        if kw in message:
            return kw
    return None
```
Ensure `logger` exists in the module (add `import logging` + `logger = logging.getLogger(__name__)` if absent).

- [ ] **Step 4: Add the registration guard in `register_enrichment_schemas`**

In `src/featuregen/overlay/upload/enrich_llm.py`, at the top of `register_enrichment_schemas` (before the register loop), assert every schema projects clean:
```python
def register_enrichment_schemas(conn) -> None:
    from featuregen.intake.schema_projection import (
        assert_schemas_provider_compatible, project_for_anthropic)
    assert_schemas_provider_compatible(
        [(name, project_for_anthropic(schema)) for (name, _v), schema in _SCHEMAS.items()])
    ...  # existing register loop unchanged
```

- [ ] **Step 5: Declare the SDK dependency + point the Dockerfile at it**

In `pyproject.toml` add under `[project.optional-dependencies]`:
```toml
llm = [
    # Anthropic structured-output client (production-only, lazy-imported; CI uses FakeLLM). Floor
    # pinned to the deploy-proven version that supports output_config + json_schema outputs.
    "anthropic>=0.117,<1.0",
]
```
In `deploy/kind/Dockerfile.backend` replace lines 10-13:
```dockerfile
RUN pip install -e ".[llm]"
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/featuregen/intake/test_llm_claude_projection.py tests/featuregen/overlay/upload/test_enrich_schema_compat.py tests/featuregen/overlay/upload/test_enrich_response_enforcement.py -q`
Expected: PASS. Then `pytest tests/featuregen/intake -q` (no regressions).

- [ ] **Step 7: Commit**

```bash
git add src/featuregen/intake/llm_claude.py src/featuregen/overlay/upload/enrich_llm.py pyproject.toml deploy/kind/Dockerfile.backend tests/featuregen/intake/test_llm_claude_projection.py tests/featuregen/overlay/upload/test_enrich_schema_compat.py tests/featuregen/overlay/upload/test_enrich_response_enforcement.py
git commit -m "feat(intake): project schemas for the wire, fail-closed registration guard, safe 400 diagnostic, version-bounded anthropic dep"
```

---

### Task 3: MF-1 — Parser-evidence reconciliation

**Files:**
- Modify: `src/featuregen/overlay/upload/sample_parser.py` (add `reconcile_profile`)
- Modify: `src/featuregen/overlay/upload/ingest.py` (`_write_glossary_parser_evidence` signature + call site)
- Test: `tests/featuregen/overlay/upload/test_parser_reconciliation.py`

**Interfaces:**
- Consumes: `ParsedProfile` (`sample_parser.py:49-68`), `parse_sample_profile`.
- Produces: `reconcile_profile(parsed: ParsedProfile, *, declared_type: str, column: str) -> ParsedProfile` — a possibly-narrowed profile (contradicted fields set to `None`, diagnostic appended). `_write_glossary_parser_evidence(conn, *, logical_ref, description, declared_type, column, snapshot_id)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/featuregen/overlay/upload/test_parser_reconciliation.py
from featuregen.overlay.upload.sample_parser import ParsedProfile, reconcile_profile


def _p(logical, semantic):
    return ParsedProfile(logical_representation=logical, semantic_type=semantic,
                         computational_type=None, sample_values=(), diagnostic=None)


def test_temporal_declared_type_withholds_identifier():
    # epoch-like integers sampled → parser said numeric_string/identifier; declared TIMESTAMP contradicts
    out = reconcile_profile(_p("numeric_string", "identifier"),
                            declared_type="timestamp", column="event_ts")
    assert out.semantic_type is None and out.logical_representation is None
    assert out.diagnostic and "timestamp" in out.diagnostic.lower()


def test_decimal_declared_type_withholds_identifier():
    out = reconcile_profile(_p("numeric_string", "identifier"),
                            declared_type="double", column="fee_amount")
    assert out.semantic_type is None and out.logical_representation is None
    assert out.diagnostic


def test_identifier_name_withholds_amount_measure():
    out = reconcile_profile(_p("decimal", "amount"),
                            declared_type="varchar", column="account_id")
    assert out.semantic_type is None
    assert out.diagnostic


def test_consistent_profile_is_unchanged():
    out = reconcile_profile(_p("decimal", "amount"),
                            declared_type="decimal", column="fee_amount")
    assert out.logical_representation == "decimal" and out.semantic_type == "amount"
    assert out.diagnostic is None


def test_unknown_declared_type_is_permissive():
    out = reconcile_profile(_p("numeric_string", "identifier"),
                            declared_type="unknown", column="cust_ref")
    assert out.semantic_type == "identifier"  # no declared signal to contradict
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/featuregen/overlay/upload/test_parser_reconciliation.py -q`
Expected: FAIL (`reconcile_profile` missing).

- [ ] **Step 3: Implement `reconcile_profile`**

Add to `src/featuregen/overlay/upload/sample_parser.py`:
```python
import dataclasses

_TEMPORAL_DECLARED = ("timestamp", "datetime", "date", "time")
_NUMERIC_MEASURE_DECLARED = ("double", "float", "real", "decimal", "numeric", "number", "money")
_IDENTIFIER_NAME_SUFFIXES = ("_id", "_key", "_code", "_no", "_num", "_ref")
_IDENTIFIER_SEMANTICS = ("identifier",)
_MEASURE_SEMANTICS = ("amount",)


def reconcile_profile(parsed: ParsedProfile, *, declared_type: str, column: str) -> ParsedProfile:
    """Reconcile the deterministic sample-shape classification against the declared SQL type and the
    column name. On a contradiction, WITHHOLD the parser fields (set them to None) and record a
    diagnostic rather than asserting a possibly-wrong operational value at parser:supported."""
    dt = (declared_type or "").strip().lower()
    col = (column or "").strip().lower()
    sem, log = parsed.semantic_type, parsed.logical_representation
    reasons: list[str] = []

    # A temporal declared type must never surface as an identifier / numeric_string.
    if any(dt.startswith(t) for t in _TEMPORAL_DECLARED) and (
            sem in _IDENTIFIER_SEMANTICS or log in ("numeric_string", "time")):
        reasons.append(f"declared type '{dt}' is temporal but sample parsed as {log}/{sem}")
    # A numeric measure declared type must never surface as an identifier.
    elif any(t in dt for t in _NUMERIC_MEASURE_DECLARED) and sem in _IDENTIFIER_SEMANTICS:
        reasons.append(f"declared type '{dt}' is a numeric measure but sample parsed as an identifier")
    # A column named like an identifier must never surface as a numeric measure.
    elif any(col.endswith(s) for s in _IDENTIFIER_NAME_SUFFIXES) and sem in _MEASURE_SEMANTICS:
        reasons.append(f"column '{col}' is named like an identifier but sample parsed as a measure")

    if not reasons:
        return parsed
    prior = f" ({parsed.diagnostic})" if parsed.diagnostic else ""
    return dataclasses.replace(parsed, logical_representation=None, semantic_type=None,
                               diagnostic="withheld parser evidence: " + "; ".join(reasons) + prior)
```

- [ ] **Step 4: Thread declared_type + column into the evidence writer**

In `src/featuregen/overlay/upload/ingest.py`, change `_write_glossary_parser_evidence` (line 613) to
accept `declared_type` + `column` and reconcile before writing:
```python
def _write_glossary_parser_evidence(
    conn, *, logical_ref: str, description: str, declared_type: str, column: str, snapshot_id: str
) -> None:
    parsed = reconcile_profile(parse_sample_profile(description or ""),
                               declared_type=declared_type, column=column)
    present: set[str] = set()
    ...  # unchanged write loop over parsed.logical_representation / parsed.semantic_type
```
Add `reconcile_profile` to the `sample_parser` import. At the call site (line 736-744) pass the
record's declared type + parsed column (both in scope in the loop at `ingest.py:708-744`):
```python
                _write_glossary_parser_evidence(
                    conn, logical_ref=logical_ref, description=rec.definition,
                    declared_type=rec.declared_type, column=parse_ref(logical_ref)[1],
                    snapshot_id=snapshot_id)
```
(Use the same `parse_ref` already imported for the loop; if the column is already bound in the loop, reuse that local.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/featuregen/overlay/upload/test_parser_reconciliation.py -q`
Expected: PASS. Then `pytest tests/featuregen/overlay/upload/test_glossary_ingest_e2e.py -q` (evidence path intact).

- [ ] **Step 6: Commit**

```bash
git add src/featuregen/overlay/upload/sample_parser.py src/featuregen/overlay/upload/ingest.py tests/featuregen/overlay/upload/test_parser_reconciliation.py
git commit -m "fix(ingest): reconcile parser evidence vs declared type + column name; withhold on contradiction (MF-1)"
```

---

### Task 4: MF-7 — Meaning-preserving definition truncation

**Files:**
- Modify: `src/featuregen/overlay/upload/enrich.py` (`_MAX_DEFINITION_LEN`, boundary truncation in `_concept_metadata`)
- Modify: `src/featuregen/overlay/upload/enrich_llm.py` (key-aware egress in `_item_egress_ok` / `_column_profile_ok`)
- Modify: `src/featuregen/overlay/upload/table_synth.py` (`_descriptor` uses the same cap + truncation)
- Test: `tests/featuregen/overlay/upload/test_definition_truncation.py`

**Interfaces:**
- Produces: `bounded_definition(text: str, limit: int) -> str` in `enrich.py` (word/sentence-boundary truncation); `_MAX_DEFINITION_LEN = 600`; egress limit lookup `_max_len_for(key: str) -> int`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/featuregen/overlay/upload/test_definition_truncation.py
from featuregen.overlay.upload.enrich import bounded_definition, _MAX_DEFINITION_LEN
from featuregen.overlay.upload.enrich_llm import _item_egress_ok, _column_profile_ok


def test_medium_definition_survives_whole():
    text = "The customer's primary settlement account identifier used for regulatory reporting. " * 4
    text = text.strip()[:500]
    out = bounded_definition(text, _MAX_DEFINITION_LEN)
    assert out == text  # <= 600, untouched


def test_long_definition_truncates_on_word_boundary():
    text = "word " * 400  # 2000 chars
    out = bounded_definition(text, _MAX_DEFINITION_LEN)
    assert len(out) <= _MAX_DEFINITION_LEN
    assert not out.endswith("wor")  # no mid-token cut
    assert out.split()[-1] == "word"


def test_egress_allows_business_definition_up_to_600():
    meta = {"table": "t", "column": "c", "business_definition": "x" * 600}
    assert _item_egress_ok(meta) is True
    meta_bad = {"table": "t", "column": "c", "business_definition": "x" * 601}
    assert _item_egress_ok(meta_bad) is False


def test_egress_other_scalars_still_capped_at_200():
    assert _item_egress_ok({"table": "t", "column": "c", "term_name": "x" * 201}) is False


def test_column_profile_business_definition_up_to_600():
    assert _column_profile_ok({"column": "c", "type": "unknown",
                               "business_definition": "y" * 600}) is True
    assert _column_profile_ok({"column": "c", "type": "unknown",
                               "business_definition": "y" * 601}) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/featuregen/overlay/upload/test_definition_truncation.py -q`
Expected: FAIL (`bounded_definition`/`_MAX_DEFINITION_LEN` missing; egress caps at 200).

- [ ] **Step 3: Implement bounded truncation + key-aware egress**

In `src/featuregen/overlay/upload/enrich.py`:
```python
# Larger bound for a SANITIZED business definition specifically. The 200-char default cut every real
# definition mid-sentence; sanitized definitions are the intended metadata payload, so allow a bigger
# but still-bounded window with word-boundary truncation. Second boundary remains the batch token budget.
_MAX_DEFINITION_LEN = 600


def bounded_definition(text: str, limit: int) -> str:
    """Trim `text` to <= `limit` chars on a word boundary (prefer a sentence end within the window)."""
    if len(text) <= limit:
        return text
    window = text[:limit]
    cut = window.rfind(". ")
    if cut >= limit // 2:          # a sentence break in the back half → keep whole sentences
        return window[:cut + 1]
    sp = window.rfind(" ")
    return window[:sp] if sp > 0 else window
```
Change the `business_definition` line in `_concept_metadata` (line 213) from `val[:_MAX_META_LEN]` for
that key to the larger bound. Replace the loop body so `business_definition` uses
`bounded_definition(..., _MAX_DEFINITION_LEN)` and the others keep `[:_MAX_META_LEN]`:
```python
        meta_defn = strip_sample_values(rec.definition)
        if meta_defn:
            meta["business_definition"] = bounded_definition(meta_defn, _MAX_DEFINITION_LEN)
        for key, val in (("term_name", rec.term_name), ("data_domain", rec.domain),
                         ("bian_path", rec.bian_path), ("fibo_path", rec.fibo_path)):
            if val:
                meta[key] = val[:_MAX_META_LEN]
```
In `src/featuregen/overlay/upload/enrich_llm.py`, make the egress guard key-aware:
```python
_MAX_LEN_DEFAULT = 200
_MAX_LEN_BY_KEY = {"business_definition": 600}


def _max_len_for(key: str) -> int:
    return _MAX_LEN_BY_KEY.get(key, _MAX_LEN_DEFAULT)
```
In `_item_egress_ok` (line 460) replace the scalar/list length checks with `len(v) > _max_len_for(k)`
(and list items `len(x) <= _max_len_for(k)`). In `_column_profile_ok` (line 452) replace
`len(v) <= 200` with `len(v) <= _max_len_for(k)` iterating `desc.items()`.

In `src/featuregen/overlay/upload/table_synth.py` `_descriptor` (line 33-36), replace `cleaned[:200]`
with `bounded_definition(cleaned, 600)` (import `bounded_definition` from `.enrich`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/featuregen/overlay/upload/test_definition_truncation.py -q`
Expected: PASS. Then `pytest tests/featuregen/overlay/upload/test_enrich_llm.py -q` (egress regressions).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/enrich.py src/featuregen/overlay/upload/enrich_llm.py src/featuregen/overlay/upload/table_synth.py tests/featuregen/overlay/upload/test_definition_truncation.py
git commit -m "fix(enrich): meaning-preserving definition truncation (600, word-boundary) + key-aware egress (MF-7)"
```

---

### Task 5: MF-2 — Pass B receives the complete FTR sidecar

**Files:**
- Modify: `src/featuregen/overlay/upload/table_synth.py` (`_descriptor`, `assemble_table_items` signature, `_COLUMN_PROFILE_KEYS` — note this frozenset is defined in `enrich_llm.py:443`)
- Modify: `src/featuregen/overlay/upload/enrich_llm.py` (`_COLUMN_PROFILE_KEYS` + `_column_profile_ok` admit new keys)
- Modify: `src/featuregen/overlay/upload/ingest.py` (call site threads the glossary records map)
- Test: `tests/featuregen/overlay/upload/test_passb_sidecar.py`

**Interfaces:**
- Consumes: `GlossaryRecord` (`glossary_reader.py:64-102` — `declared_type`, `definition`, `term_name`, `term_type`, `domain`, `process_path`, parser facets), `bounded_definition` (Task 4), `parse_ref`.
- Produces: `assemble_table_items(rows, *, concepts, definitions, records: dict[tuple[str,str], GlossaryRecord] | None)`; each descriptor may carry `type`(=declared_type), `business_definition`, `term_type`, `domain`, `process_path`, `semantic_type`.

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/overlay/upload/test_passb_sidecar.py
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.glossary_reader import GlossaryRecord
from featuregen.overlay.upload.table_synth import assemble_table_items
from featuregen.overlay.upload.enrich_llm import _column_profile_ok


def _row(table, col):
    return CanonicalRow(source="s", table=table, column=col, type="unknown", definition="")


def _rec(table, col, **kw):
    base = dict(logical_ref=f"s.{table}.{col}", term_name=f"{col} term", definition="A settled amount.",
                domain="Payments", synonyms=(), bian_path="", fibo_path="", is_table=False,
                term_type="measure", process_path="Payments>Settlement", physical_fqn=f"{table}.{col}",
                declared_type="double")
    base.update(kw)
    return GlossaryRecord(**base)


def test_descriptor_carries_full_sidecar():
    rows = [_row("txn", "fee_amt")]
    records = {("txn", "fee_amt"): _rec("txn", "fee_amt")}
    items = assemble_table_items(rows, concepts=None, definitions=None, records=records)
    prof = items[0].metadata["column_profiles"][0]
    assert prof["type"] == "double"                 # declared type, not "unknown"
    assert prof["business_definition"] == "A settled amount."
    assert prof["term_type"] == "measure"
    assert prof["domain"] == "Payments"
    assert prof["process_path"] == "Payments>Settlement"
    assert _column_profile_ok(prof) is True         # egress allows the new keys


def test_no_records_falls_back_to_row_type():
    rows = [_row("txn", "id")]
    items = assemble_table_items(rows, concepts=None, definitions=None, records=None)
    prof = items[0].metadata["column_profiles"][0]
    assert prof["type"] == "unknown"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/featuregen/overlay/upload/test_passb_sidecar.py -q`
Expected: FAIL (`records` kwarg absent; keys not in profile / not allowlisted).

- [ ] **Step 3: Extend the egress allowlist**

In `src/featuregen/overlay/upload/enrich_llm.py` extend `_COLUMN_PROFILE_KEYS` (line 443) with the new
keys:
```python
_COLUMN_PROFILE_KEYS = frozenset({
    "column", "type", "concept", "business_definition",
    "identifier_role", "temporal_role", "semantic_type", "entity",
    "term_type", "domain", "process_path",
})
```
(`_column_profile_ok` already validates every value against `_max_len_for` from Task 4.)

- [ ] **Step 4: Build the descriptor from the sidecar + thread `records`**

In `src/featuregen/overlay/upload/table_synth.py`, replace `_descriptor` and widen
`assemble_table_items`:
```python
def _descriptor(r: CanonicalRow, concept: str | None, definition: str | None,
                rec: "GlossaryRecord | None") -> dict:
    desc: dict = {"column": r.column, "type": (rec.declared_type if rec and rec.declared_type
                                               else (r.type or ""))}
    if concept:
        desc["concept"] = concept
    # Curated sidecar meaning wins; else the Pass A draft (blank columns only). NEVER r.definition.
    src_def = (rec.definition if rec and rec.definition else definition)
    if src_def:
        cleaned = strip_sample_values(src_def)
        if cleaned:
            desc["business_definition"] = bounded_definition(cleaned, 600)
    if rec is not None:
        for key, val in (("term_type", rec.term_type), ("domain", rec.domain),
                         ("process_path", rec.process_path),
                         ("semantic_type", getattr(rec, "semantic_type", None))):
            if val:
                desc[key] = val[:200]
    return desc


def assemble_table_items(rows, *, concepts, definitions,
                         records=None) -> list[BatchItem]:
    concepts = concepts or {}
    definitions = definitions or {}
    records = records or {}
    by_table: dict[str, list[CanonicalRow]] = {}
    for r in rows:
        by_table.setdefault(r.table, []).append(r)
    items = []
    for table, trows in by_table.items():
        profiles = [_descriptor(r, concepts.get(content_hash(r)), definitions.get(content_hash(r)),
                                records.get((r.table, r.column))) for r in trows]
        items.append(BatchItem(ref=table, metadata={"table": table, "column_profiles": profiles}))
    return items
```
Add `from .enrich import bounded_definition` and a `GlossaryRecord` type import (guarded under
`TYPE_CHECKING` to avoid an import cycle if needed).

In `src/featuregen/overlay/upload/ingest.py` at the call site (line 1424), build the records map (the
same `(table, column)` keying used for Pass C at `ingest.py:341-352`) and pass it:
```python
        records = {}
        if glossary is not None:
            for rec in glossary.records:
                t, c = parse_ref(rec.logical_ref)
                records[(t, c)] = rec
        items = assemble_table_items(vr.good, concepts=concepts, definitions=definitions,
                                     records=records)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/featuregen/overlay/upload/test_passb_sidecar.py -q`
Expected: PASS. Then `pytest tests/featuregen/overlay/upload/test_table_synth.py -q`.

- [ ] **Step 6: Commit**

```bash
git add src/featuregen/overlay/upload/table_synth.py src/featuregen/overlay/upload/enrich_llm.py src/featuregen/overlay/upload/ingest.py tests/featuregen/overlay/upload/test_passb_sidecar.py
git commit -m "feat(passb): feed the complete FTR sidecar (declared type, definition, term type, domain, process path) to table synthesis (MF-2)"
```

---

### Task 6: MF-3 — Abstention is a valid outcome, not a failure

**Files:**
- Modify: `src/featuregen/overlay/upload/table_synth.py` (`make_ref_accept` — accept a no-grain/no-as-of synthesis as abstained)
- Modify: `src/featuregen/overlay/upload/ingest.py` (`_enrichment_outcome` — count abstentions as resolved; add abstained count to detail)
- Test: `tests/featuregen/overlay/upload/test_passb_abstention.py`

**Interfaces:**
- Consumes: `make_ref_accept` (`table_synth.py:73-111`), `_enrichment_outcome` (`ingest.py:147-166`), `_propose_table_facts` (already skips `None` grain/availability).
- Produces: `make_ref_accept` returns a valid record (with `grain`/`availability_time` possibly `None`) for any parseable synthesis; a helper `_is_abstention(syn: dict) -> bool`. `_enrichment_outcome(syntheses, expected)` detail includes `"abstained": <n>`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/featuregen/overlay/upload/test_passb_abstention.py
import json
from featuregen.overlay.upload.table_synth import make_ref_accept
from featuregen.overlay.upload.ingest import _enrichment_outcome


def test_role_entity_only_synthesis_is_accepted_as_abstention():
    accept = make_ref_accept(cols=["a", "b"])
    payload = json.dumps({"grain_columns": [], "as_of_column": None,
                          "table_role": "reference", "primary_entity": "customer"})
    value, reason = accept("t", payload)
    assert value is not None                       # accepted (was empty_synthesis → None before)
    out = json.loads(value)
    assert out["grain"] is None and out["availability_time"] is None
    assert out["table_role"] == "reference" and out["primary_entity"] == "customer"


def test_unparseable_is_still_rejected():
    accept = make_ref_accept(cols=["a"])
    value, reason = accept("t", "not json")
    assert value is None


def test_outcome_counts_abstention_as_resolved():
    syntheses = {"t1": {"grain": {"columns": ["id"], "is_unique": True}, "availability_time": None},
                 "t2": {"grain": None, "availability_time": None, "table_role": "reference"}}
    state, reason, detail = _enrichment_outcome(syntheses, 2)
    assert state == "succeeded"
    assert detail["resolved"] == 2
    assert detail["abstained"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/featuregen/overlay/upload/test_passb_abstention.py -q`
Expected: FAIL (empty synthesis returns None; no `abstained` key).

- [ ] **Step 3: Accept abstention in `make_ref_accept`**

In `src/featuregen/overlay/upload/table_synth.py` replace the empty-synthesis rejection (line 101-102):
```python
    # A parseable synthesis with neither grain nor availability is a VALID abstention (some tables
    # genuinely have no single grain / as-of). Retain any role/entity it returned; propose zero facts.
    out = {"grain": grain, "availability_time": availability,
           "table_role": s.get("table_role"), "primary_entity": s.get("primary_entity"),
           "event_or_snapshot": s.get("event_or_snapshot")}
    return json.dumps(out, sort_keys=True), ("valid" if (grain or availability) else "abstained")
```
(The prior `if grain is None and availability is None: return None, "empty_synthesis"` line is removed.
Unparseable/non-object results still return `None` earlier in the function — leave those paths.)

- [ ] **Step 4: Count abstentions in `_enrichment_outcome`**

In `src/featuregen/overlay/upload/ingest.py` `_enrichment_outcome` (line 147), compute an abstained
count and include it; abstained tables are resolved (present in `syntheses`), so `succeeded` holds:
```python
def _enrichment_outcome(result: dict, expected: int):
    abstained = sum(1 for syn in result.values()
                    if syn.get("grain") is None and syn.get("availability_time") is None)
    detail: dict = {"resolved": len(result), "expected": expected, "abstained": abstained}
    unresolved = max(expected - len(result), 0)
    ...  # unchanged: failed if expected and not result; partial if unresolved/internal_failures
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/featuregen/overlay/upload/test_passb_abstention.py -q`
Expected: PASS. Then `pytest tests/featuregen/overlay/upload/test_table_synth.py -q`.

- [ ] **Step 6: Commit**

```bash
git add src/featuregen/overlay/upload/table_synth.py src/featuregen/overlay/upload/ingest.py tests/featuregen/overlay/upload/test_passb_abstention.py
git commit -m "fix(passb): treat no-grain/no-as-of synthesis as a valid abstention, not a stage failure (MF-3)"
```

---

### Task 7: MF-4 — Bound LLM execution time

**Files:**
- Modify: `src/featuregen/intake/llm_claude.py` (`ClaudeConfig.timeout` + pass to `messages.create`)
- Modify: `src/featuregen/overlay/upload/enrich_batch.py` (stage deadline with injectable clock)
- Test: `tests/featuregen/intake/test_claude_timeout.py`, `tests/featuregen/overlay/upload/test_enrich_stage_deadline.py`

**Interfaces:**
- Produces: `ClaudeConfig.timeout: float = 60.0` (env `FEATUREGEN_LLM_TIMEOUT`); `messages.create(..., timeout=self._config.timeout)`. In `enrich_batch`: a `deadline` guard — `run_batched(..., now: Callable[[], float] = time.monotonic, deadline_s: float | None = None)` that stops issuing new chunks past the deadline and reports `timed_out`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/featuregen/intake/test_claude_timeout.py
from featuregen.intake.llm_claude import ClaudeConfig


def test_timeout_default_and_env(monkeypatch):
    assert ClaudeConfig().timeout == 60.0
    monkeypatch.setenv("FEATUREGEN_LLM_TIMEOUT", "12.5")
    assert ClaudeConfig.from_env().timeout == 12.5


def test_messages_create_receives_timeout(monkeypatch):
    from featuregen.intake.llm import LLMRequest
    from featuregen.intake.llm_claude import ClaudeLLM
    captured = {}

    class FakeMessages:
        def create(self, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            raise RuntimeError("stop")

    class FakeClient:
        messages = FakeMessages()

    llm = ClaudeLLM(ClaudeConfig(enabled=True, timeout=7.0))
    monkeypatch.setattr(llm, "_ensure_client", lambda: FakeClient())
    req = LLMRequest(task="t", prompt_id="p", prompt_version=1, inputs={"x": 1},
                     output_schema_id="s", output_schema_version=1, generation_settings={},
                     output_schema={"type": "object", "properties": {}})
    llm.call(req)
    assert captured["timeout"] == 7.0
```

```python
# tests/featuregen/overlay/upload/test_enrich_stage_deadline.py
# A driving clock that jumps past the deadline after the first chunk proves later chunks are skipped
# and the stage reports timed_out — without any real sleeping.
```
(The exact assertion depends on `run_batched`'s current return shape; the implementer writes it against
the real signature discovered in `enrich_batch.py:140-230`, asserting: given a `deadline_s` and a
`now()` that advances past it after chunk 1, the second chunk's `client.call` is never invoked and the
returned report/state carries a `timed_out` marker.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/featuregen/intake/test_claude_timeout.py -q`
Expected: FAIL (`timeout` field absent).

- [ ] **Step 3: Add the client timeout**

In `src/featuregen/intake/llm_claude.py` `ClaudeConfig` add `timeout: float = 60.0` and in `from_env`
`timeout=float(os.environ.get("FEATUREGEN_LLM_TIMEOUT", "60"))`. In `messages.create(...)` add
`timeout=self._config.timeout`.

- [ ] **Step 4: Add the stage deadline**

In `src/featuregen/overlay/upload/enrich_batch.py`, thread an optional monotonic clock + deadline into
`run_batched` (line 140). Before issuing each chunk's call, check `if deadline_s is not None and
now() - start >= deadline_s:` → stop issuing new chunks, mark the batch report `timed_out=True`, break.
Facts already asserted and the rest of ingestion are unaffected (enrichment failure is isolated). The
default `deadline_s=None` preserves current behavior byte-for-byte; wire a concrete stage deadline
(e.g. `enrich_config` env `OVERLAY_ENRICH_STAGE_DEADLINE_S`, default 240s) from the enrich entry points.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/featuregen/intake/test_claude_timeout.py tests/featuregen/overlay/upload/test_enrich_stage_deadline.py -q`
Expected: PASS. Then `pytest tests/featuregen/overlay/upload/test_enrich_batch.py -q`.

- [ ] **Step 6: Commit**

```bash
git add src/featuregen/intake/llm_claude.py src/featuregen/overlay/upload/enrich_batch.py src/featuregen/overlay/upload/enrich_config.py tests/featuregen/intake/test_claude_timeout.py tests/featuregen/overlay/upload/test_enrich_stage_deadline.py
git commit -m "feat(enrich): per-call timeout + stage deadline so a slow provider can't hold the source lock or fail ingest (MF-4)"
```

---

### Task 8: MF-5 — Truthful upload result

**Files:**
- Modify: `src/featuregen/overlay/upload/ingest.py` (`IngestResult` additive count fields + compute on success)
- Modify: `frontend/src/api.ts` (`IngestResult` interface)
- Modify: `frontend/src/screens/IngestResultCallout.tsx` (render the new counts)
- Test: `tests/featuregen/overlay/upload/test_ingest_result_counts.py`, `frontend/src/screens/IngestResultCallout.test.tsx`

**Interfaces:**
- Produces: `IngestResult(..., objects_stored=0, tables=0, columns=0, containment_edges=0, facts_asserted=0, join_candidates=0, passb_proposed=0, passb_abstained=0)` — additive fields, `=0` defaults, appended after `flagged`.

- [ ] **Step 1: Write the failing backend test**

```python
# tests/featuregen/overlay/upload/test_ingest_result_counts.py
# Ingest the synthetic single-table FTR fixture (from Task 11) on a fresh source and assert the
# truthful counts agree: objects_stored == tables + columns; containment_edges == columns;
# facts_asserted == result.asserted; passb_proposed + passb_abstained accounted.
def test_success_result_counts_agree(db, synthetic_ftr_upload):
    result = synthetic_ftr_upload(db, source="ftr_truth")
    assert result.status == "ingested"
    assert result.columns == 126 and result.tables == 1
    assert result.objects_stored == result.tables + result.columns   # 127
    assert result.containment_edges == result.columns                # 126
    assert result.facts_asserted == result.asserted
```
(Depends on Task 11's `synthetic_ftr_upload` fixture; sequence Task 11 before this test runs green, or
gate this test with the fixture. The implementer may land the fixture first.)

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/featuregen/overlay/upload/test_ingest_result_counts.py -q`
Expected: FAIL (fields absent).

- [ ] **Step 3: Add the fields + compute them**

In `src/featuregen/overlay/upload/ingest.py` `IngestResult` (line 135), append:
```python
    objects_stored: int = 0
    tables: int = 0
    columns: int = 0
    containment_edges: int = 0
    facts_asserted: int = 0
    join_candidates: int = 0
    passb_proposed: int = 0
    passb_abstained: int = 0
```
On the success path (line 1619-1621), compute from data already in scope:
```python
    tables = len({r.table for r in vr.good})
    columns = len(vr.good)
    passb_abstained = sum(1 for syn in syntheses.values()
                          if syn.get("grain") is None and syn.get("availability_time") is None)
    passb_proposed = len(syntheses) - passb_abstained
    return IngestResult("ingested", None, asserted, changed_objects, len(vr.quarantined), flagged,
                        objects_stored=tables + columns, tables=tables, columns=columns,
                        containment_edges=columns, facts_asserted=asserted,
                        join_candidates=join_candidate_count, passb_proposed=passb_proposed,
                        passb_abstained=passb_abstained)
```
(`join_candidate_count` — thread the Pass C candidate count out of `_run_pass_c` (`ingest.py:1389`); if
Pass C is off it is 0. If plumbing the count is non-trivial, the implementer returns it from
`_run_pass_c` alongside its existing return.)

- [ ] **Step 4: Write the failing frontend test + extend the type + render**

`frontend/src/api.ts` `IngestResult` (lines 88-100) — add the eight optional numeric fields. In
`frontend/src/screens/IngestResultCallout.tsx` (line 248-250) add a second line rendering
`objects_stored` / `containment_edges` / `join_candidates` / `passb_proposed` / `passb_abstained`.
Add `IngestResultCallout.test.tsx` asserting the new counts render.

- [ ] **Step 5: Run tests to verify they pass**

Run backend: `pytest tests/featuregen/overlay/upload/test_ingest_result_counts.py -q`.
Run frontend (changed file only — full vitest hangs in this env, per project note):
`cd frontend && npx vitest run src/screens/IngestResultCallout.test.tsx`.
Expected: PASS both.

- [ ] **Step 6: Commit**

```bash
git add src/featuregen/overlay/upload/ingest.py frontend/src/api.ts frontend/src/screens/IngestResultCallout.tsx frontend/src/screens/IngestResultCallout.test.tsx tests/featuregen/overlay/upload/test_ingest_result_counts.py
git commit -m "feat(ingest): truthful upload result — objects/edges/facts/join-candidates/Pass B proposed+abstained (MF-5)"
```

---

### Task 9: MF-6 — Protect the dedicated-source limitation

**Files:**
- Modify: `src/featuregen/overlay/upload/ingest.py` (source-kind guard for glossary uploads, near the cross-schema fence at line 1109)
- Test: `tests/featuregen/overlay/upload/test_ftr_source_guard.py`

**Interfaces:**
- Produces: `_source_is_schema_less(conn, catalog_source: str) -> bool` — True iff the source has existing `graph_node` rows and none carry a non-NULL `schema_name`. A `held` `IngestResult` with the actionable message when a glossary upload targets such a source.

- [ ] **Step 1: Write the failing tests**

```python
# tests/featuregen/overlay/upload/test_ftr_source_guard.py
# 1) Seed a schema-less technical source (graph_node rows, schema_name NULL). An FTR upload onto it
#    → result.status == "held" and the message mentions "FTR-only source".
# 2) An FTR upload onto a brand-new source → proceeds (not held for this reason).
# 3) An FTR upload onto an existing FTR source (schema_name set) → proceeds.
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/featuregen/overlay/upload/test_ftr_source_guard.py -q`
Expected: FAIL (no guard).

- [ ] **Step 3: Implement the guard**

In `src/featuregen/overlay/upload/ingest.py`, inside `if glossary is not None:` (line 1109), before
`_cross_schema_conflicts`, add:
```python
        if _source_is_schema_less(conn, catalog_source):
            return IngestResult(
                "held",
                "this FTR upload requires a new or existing FTR-only source; it cannot enrich the "
                f"existing schema-less technical source '{catalog_source}'. Choose a new source name "
                "or an FTR source.",
                0, 0, len(vr.quarantined))
```
Add the helper:
```python
def _source_is_schema_less(conn, catalog_source: str) -> bool:
    row = conn.execute(
        "SELECT count(*) AS n, count(schema_name) AS with_schema "
        "FROM graph_node WHERE catalog_source = %s AND kind = 'column'",
        (catalog_source,)).fetchone()
    n, with_schema = (row["n"], row["with_schema"]) if isinstance(row, dict) else (row[0], row[1])
    return n > 0 and with_schema == 0
```
(Match the existing row-access idiom in `ingest.py` — dict rows vs tuple.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/featuregen/overlay/upload/test_ftr_source_guard.py -q`
Expected: PASS. Then `pytest tests/featuregen/overlay/upload/test_glossary_ingest_e2e.py -q`.

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/ingest.py tests/featuregen/overlay/upload/test_ftr_source_guard.py
git commit -m "feat(ingest): guard FTR uploads onto schema-less technical sources with an actionable held message (MF-6)"
```

---

### Task 10: MF-8a + MF-8b — Evidence-based batch ceilings + real-provider sweep harness

**Files:**
- Modify: `src/featuregen/overlay/upload/enrich_config.py` (`_DEFAULT_MAX_ITEMS`)
- Test: `tests/featuregen/overlay/upload/test_enrich_config.py` (ceiling defaults)
- Create: `tests/eval/test_batch_size_sweep.py` (key-gated sweep harness)
- Create: `tests/eval/contamination.py` (cross-item contamination metric helper)

**Interfaces:**
- Produces: `_DEFAULT_MAX_ITEMS = {"concept":20, "definition":8, "domain":8, "table_synth":4}`. Sweep harness measuring accuracy/abstention/missing+duplicate refs/contamination/latency/cost across the specified batch sizes vs single-item baselines.

- [ ] **Step 1: Write the failing ceiling test**

```python
# tests/featuregen/overlay/upload/test_enrich_config.py
from featuregen.overlay.upload import enrich_config


def test_conservative_default_ceilings():
    assert enrich_config._DEFAULT_MAX_ITEMS == {
        "concept": 20, "definition": 8, "domain": 8, "table_synth": 4}


def test_env_override_still_applies(monkeypatch):
    monkeypatch.setenv("OVERLAY_ENRICH_BATCH_CONCEPT_MAX_ITEMS", "5")
    assert enrich_config.max_items("concept") == 5
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/featuregen/overlay/upload/test_enrich_config.py -q`
Expected: FAIL (defaults are 40/12/20/8).

- [ ] **Step 3: Lower the ceilings**

`src/featuregen/overlay/upload/enrich_config.py:17`:
```python
_DEFAULT_MAX_ITEMS = {"concept": 20, "definition": 8, "domain": 8, "table_synth": 4}
```

- [ ] **Step 4: Build the key-gated sweep harness**

Create `tests/eval/test_batch_size_sweep.py` (`pytestmark = pytest.mark.eval`,
`@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="live provider required")`). For
each task and each batch size in `{concept:[5,10,20,40], definition:[4,8,12], domain:[4,8,20],
table_synth:[1,2,4,8]}`, run the real provider over the gold/synthetic set with
`OVERLAY_ENRICH_BATCH_<T>_MAX_ITEMS` set to that size, plus a single-item baseline (size 1), and record
per size: accuracy vs gold, abstention rate, missing refs, duplicate refs, cross-item contamination
(via `contamination.py`), wall-clock latency, and token cost (from the recorded `llm_call` usage).
Emit a human-readable report (print / write to `tests/eval/reports/`) and assert only that the harness
ran (it is an evidence generator, not a pass/fail gate). Add `tests/eval/contamination.py` implementing
a contamination metric: the rate at which an item's answer echoes a *sibling* item's distinctive tokens
(e.g. a definition reusing another column's term) beyond a baseline.

- [ ] **Step 5: Run the ceiling test (harness runs only with a key)**

Run: `pytest tests/featuregen/overlay/upload/test_enrich_config.py -q` → PASS.
Run (no key): `pytest -m eval tests/eval/test_batch_size_sweep.py -q` → SKIPPED (documents the gate).

- [ ] **Step 6: Commit**

```bash
git add src/featuregen/overlay/upload/enrich_config.py tests/featuregen/overlay/upload/test_enrich_config.py tests/eval/test_batch_size_sweep.py tests/eval/contamination.py
git commit -m "feat(enrich): conservative batch ceilings 20/8/8/4 + key-gated real-provider batch-size sweep harness (MF-8)"
```

---

### Task 11: PG-backed acceptance test on a synthetic FTR fixture

**Files:**
- Create: `tests/featuregen/overlay/upload/fixtures/ftr_sample_synthetic.csv` (committed; no real PII)
- Create: `tests/featuregen/overlay/upload/conftest.py` addition or a fixture module: `synthetic_ftr_upload`
- Create: `tests/featuregen/overlay/upload/test_ftr_phase1_acceptance.py`

**Interfaces:**
- Consumes: the full upload path (`read_ftr_glossary`/`to_glossary_upload`/`ingest_upload`), a `FakeLLM` scripted for concept/definition/domain/Pass B.
- Produces: a `synthetic_ftr_upload(db, *, source)` fixture used here and by Task 8.

- [ ] **Step 1: Build the synthetic fixture**

Create `ftr_sample_synthetic.csv` with the exact 17 FTR headers (the multiset `is_ftr_glossary`
requires — mirror them from `ftr_adapter.py`), 126 column-term rows + 1 table-term row, canonical
`…values such as …` sample clauses in several definitions, definitions exceeding 200 chars, varied
`data_type` values including `timestamp`, `double`, `varchar`, and at least one `Regulatory Term`
term_type. No real customer values — invent innocuous tokens.

- [ ] **Step 2: Write the acceptance test (failing until prior tasks land)**

```python
# tests/featuregen/overlay/upload/test_ftr_phase1_acceptance.py
def test_ftr_sample_accepts_cleanly(db, synthetic_ftr_upload):
    r = synthetic_ftr_upload(db, source="ftr_accept")
    assert r.status == "ingested"
    assert r.columns == 126 and r.tables == 1 and r.quarantined == 0
    # declared types preserved on graph_node
    # no contradictory parser evidence (no identifier asserted for a timestamp/double column)
    # sanitized definitions contain no sample tokens
    # counts agree: objects_stored == 127; containment_edges == 126


def test_reupload_is_deterministic(db, synthetic_ftr_upload):
    a = synthetic_ftr_upload(db, source="ftr_reup")
    b = synthetic_ftr_upload(db, source="ftr_reup")
    assert (a.columns, a.tables) == (b.columns, b.tables)
    assert b.changed_objects == 0        # nothing changed on identical re-upload
```
Add the `synthetic_ftr_upload` fixture that reads the CSV, routes it through the FTR reader, scripts a
`FakeLLM` returning valid concept/definition/domain/Pass B outputs (including at least one abstaining
table), and calls `ingest_upload`. Assert Pass A received sanitized defs + declared types and Pass B
received the complete metadata by inspecting the `FakeLLM`'s captured request inputs.

- [ ] **Step 3: Run to verify it fails, then passes as tasks land**

Run: `pytest tests/featuregen/overlay/upload/test_ftr_phase1_acceptance.py -q`
Expected: PASS once Tasks 1-10 are in (this task is the integration gate; run it last among fixes).

- [ ] **Step 4: Commit**

```bash
git add tests/featuregen/overlay/upload/fixtures/ftr_sample_synthetic.csv tests/featuregen/overlay/upload/test_ftr_phase1_acceptance.py tests/featuregen/overlay/upload/conftest.py
git commit -m "test(ftr): PG-backed Phase-1 acceptance on a synthetic FTR sample (counts, declared types, parser reconciliation, sanitized defs, abstention, determinism)"
```

---

### Task 12: Live canary + final wiring

**Files:**
- Create: `tests/eval/test_anthropic_live_canary.py` (key-gated)
- Modify: `docs/superpowers/plans/2026-07-18-phase1-llm-enrichment-hardening-plan.md` (check boxes as done)

**Interfaces:**
- Consumes: the real Anthropic client via `ClaudeLLM`, the registered schemas, `project_for_anthropic`.

- [ ] **Step 1: Write the key-gated canary**

Create `tests/eval/test_anthropic_live_canary.py` (`pytest.mark.eval` +
`skipif(not ANTHROPIC_API_KEY)`). For each of `overlay_concept_batch`, `overlay_domain_batch`,
`overlay_table_synth_summary_batch`, `overlay_table_synth_batch`: register the canonical schema, build
a minimal real `LLMRequest` through the enrichment path, call the real provider, and assert (a) no
`APIStatusError` / no 400, and (b) the response validates against the canonical schema
(`reg.validate`). This exercises the exact projected wire schema end-to-end.

- [ ] **Step 2: Run (skips without a key)**

Run: `pytest -m eval tests/eval/test_anthropic_live_canary.py -q` → SKIPPED without a key; with
`ANTHROPIC_API_KEY` set against a throwaway config, PASS (no 400).

- [ ] **Step 3: Full backend suite**

Run: `pytest -q`
Expected: PASS (target ≥ prior 2791 + new tests; no regressions).

- [ ] **Step 4: Commit**

```bash
git add tests/eval/test_anthropic_live_canary.py docs/superpowers/plans/2026-07-18-phase1-llm-enrichment-hardening-plan.md
git commit -m "test(eval): key-gated Anthropic live canary across concept/domain/Pass B schemas"
```

---

## Self-Review

**Spec coverage:** Projection linchpin → Tasks 1-2. anthropic dep → Task 2. MF-1 → Task 3. MF-7 →
Task 4. MF-2 → Task 5. MF-3 → Task 6. MF-4 → Task 7. MF-5 → Task 8. MF-6 → Task 9. MF-8a/8b → Task 10.
Static compat test → Task 2. Wire-shape test → Task 2. Local-enforcement test → Task 2. Safe-400 →
Task 2. Live canary → Task 12. PG acceptance on synthetic fixture → Task 11. All spec items mapped.

**Placeholder scan:** Task 7 Step 1 (deadline test) and Task 10 Step 4 (sweep/contamination) describe
behavior the implementer writes against the discovered real signatures rather than fully-inlined code,
because the exact `run_batched` return shape and the contamination metric depend on runtime structures
not safely transcribable from investigation alone. These are flagged as implementer-derived with
explicit assertions to satisfy; every other step carries complete code.

**Type consistency:** `project_for_anthropic`/`provider_incompatibilities`/
`assert_schemas_provider_compatible` (Task 1) used consistently in Task 2. `bounded_definition`
(Task 4) reused in Task 5. `_enrichment_outcome` abstained key (Task 6) reused in Task 8's
`passb_abstained`. `synthetic_ftr_upload` fixture (Task 11) consumed by Task 8 — Task 11 must land
before Task 8's test runs green (note in Task 8 Step 1).

**Sequencing note:** Task 11's fixture is depended on by Task 8. Land Task 11's fixture+conftest early
(or run Task 8's count test after Task 11). The acceptance assertions in Task 11 fully pass only after
Tasks 1-10.
