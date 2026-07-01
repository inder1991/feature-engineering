# SP-2 — Phase 2 — Feature Contract data model + catalog + fold (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Global Constraints + Shared Contract:** see [sp2-00-overview.md](2026-07-01-sp2-00-overview.md) (authoritative). Where a signature here and the overview disagree, **the overview wins**. Spec sections: **§4** (+**§4.5**, **§4.6**), **§5.4**, **§11** of [the SP-2 design spec](../architecture/2026-07-01-sp2-intake-clarification-design.md).

---

**Prerequisite:** Phase 1 ([sp2-01](2026-07-01-sp2-01-sp0-extensions.md)) is merged. Specifically this phase relies on:
- The **`feature_contract` aggregate** admitted by migration `0508_feature_contract_events.sql`, and the twelve SP-2 FC event-type **constants + schemas** registered from `src/featuregen/intake/events.py` (`register_sp2_event_types`). This phase imports the event-type **string constants** from `featuregen.intake.events` — `INTENT_SUBMITTED`, `DRAFT_CONTRACT_PRODUCED`, `CONTRACT_CRITIQUED`, `FIELD_AUTO_RESOLVED`, `CLARIFICATION_REQUESTED`, `CLARIFICATION_ANSWERED`, `CONTRACT_REFINED`, `MINIMUM_CONTRACT_VALIDATED`, `CONTRACT_CONFIRMED`, `USE_CASE_ONBOARDING_REQUESTED`, `INTENT_REJECTED`, `LLM_CALL_RECORDED`.
- SP-0's document registry (`document_type_registry` table) exists (base migrations) so `DocumentSchemaRegistry(conn)` can register/validate contract content-schemas.

This phase builds SP-2's **content-schema + catalog + fold** foundation. It reads SP-0's `documents/draft.py` (`UNKNOWN`, `INTAKE_MODES`, `RAW_INPUT_CLASSIFICATIONS`, `register_draft_schemas`) and `documents/registry.py` (`DocumentSchemaRegistry`) — it writes **no** events and opens **no** tasks (that is P4–P8). The **fold** (`fold_feature_contract_state`) is built HERE, early, so every later command phase (P4–P8) gates on it inline (mirroring `overlay/confirmation_commands.py`), **not** on `state_machine/engine.py` (built-but-unused) and **not** on `run_workflow_state` (unwired scaffold).

**New package + test dirs:** `src/featuregen/intake/` and `tests/featuregen/intake/`. Tasks 2.1–2.4 (`contract.py`), 2.5 (`state.py`), 2.6–2.7 (`banking_catalog.py`) are independent except 2.2 consumes the schema constants of 2.1, 2.3 consumes 2.2's reshape, and 2.7 consumes 2.6's reader. Implement in numeric order.

---

### Task 2.1: `intake/contract.py` — closed-enum vocabularies + authoritative content-schemas + tagged `calculation_method`

**Files:**
- Create: `src/featuregen/intake/__init__.py`
- Create: `src/featuregen/intake/contract.py`
- Create: `tests/featuregen/intake/__init__.py`
- Test: `tests/featuregen/intake/test_contract_schemas.py`

**Interfaces:**
- Consumes: `featuregen.documents.draft.{UNKNOWN, INTAKE_MODES, RAW_INPUT_CLASSIFICATIONS}` (SP-0, verbatim reuse — overview Shared Contract); `jsonschema` (stdlib-adjacent, already a runtime dep).
- Produces:
  ```python
  # closed enum vocabularies (§4.0 — the only permitted members; extended only by a schema bump + upcaster)
  INTAKE_MODES: tuple[str, ...]                 # re-export of SP-0 ("hypothesis","definition")
  RAW_INPUT_CLASSIFICATIONS: tuple[str, ...]    # re-export of SP-0 ("contains_pii","clean","unscanned")
  UNKNOWN: str                                  # re-export of SP-0 sentinel "UNKNOWN"
  OBSERVATION_INTENT_KINDS: tuple[str, ...]     # ("point_in_time","as_of_event")
  METHOD_KINDS: tuple[str, ...]                 # ("rolling_aggregate","point_snapshot","ratio","distribution_divergence")
  SCORE_SOURCES: tuple[str, ...]                # ("llm","default","catalog")
  ROUTED_TO: tuple[str, ...]                    # ("human","auto")
  DRAFT_STATUS: str                             # "NEEDS_CLARIFICATION"
  CONFIRMED_STATUS: str                         # "CONFIRMED"
  DRAFT_CONTRACT_SCHEMA_VERSION: int            # 1
  ASSUMPTION_LEDGER_SCHEMA_VERSION: int         # 1
  CONFIRMED_CONTRACT_SCHEMA_VERSION: int        # 1
  DRAFT_CONTENT_SCHEMA: dict                    # authoritative DRAFT_CONTRACT content-schema (§4.1)
  CONFIRMED_CONTRACT_JSON_SCHEMA: dict          # authoritative CONFIRMED_CONTRACT content-schema (§4.2), tagged calc-method
  ASSUMPTION_LEDGER_CONTENT_SCHEMA: dict        # authoritative ASSUMPTION_LEDGER content-schema (§4.3), `assumptions` array
  ```

- [ ] **Step 1: Write the failing test**

Create `tests/featuregen/intake/__init__.py` (empty) and `tests/featuregen/intake/test_contract_schemas.py`:

```python
import jsonschema
import pytest

from featuregen.intake import contract


def test_closed_enum_vocabularies_are_the_authoritative_members():
    assert contract.OBSERVATION_INTENT_KINDS == ("point_in_time", "as_of_event")
    assert contract.METHOD_KINDS == (
        "rolling_aggregate", "point_snapshot", "ratio", "distribution_divergence",
    )
    assert contract.SCORE_SOURCES == ("llm", "default", "catalog")
    assert contract.ROUTED_TO == ("human", "auto")
    assert contract.DRAFT_STATUS == "NEEDS_CLARIFICATION"
    assert contract.CONFIRMED_STATUS == "CONFIRMED"


def test_unknown_and_mode_vocabularies_are_reused_from_sp0_verbatim():
    from featuregen.documents import draft as sp0_draft

    assert contract.UNKNOWN is sp0_draft.UNKNOWN
    assert contract.INTAKE_MODES == sp0_draft.INTAKE_MODES
    assert contract.RAW_INPUT_CLASSIFICATIONS == sp0_draft.RAW_INPUT_CLASSIFICATIONS


def test_all_three_content_schemas_are_valid_json_schemas():
    for schema in (
        contract.DRAFT_CONTENT_SCHEMA,
        contract.CONFIRMED_CONTRACT_JSON_SCHEMA,
        contract.ASSUMPTION_LEDGER_CONTENT_SCHEMA,
    ):
        # raises SchemaError if the schema itself is malformed
        jsonschema.Draft202012Validator.check_schema(schema)


def test_draft_schema_forbids_inline_raw_input_and_pins_status_const():
    props = contract.DRAFT_CONTENT_SCHEMA["properties"]
    assert props["status"] == {"const": "NEEDS_CLARIFICATION"}
    assert contract.DRAFT_CONTENT_SCHEMA["not"] == {"required": ["raw_input"]}
    assert "proposed_feature_name" in contract.DRAFT_CONTENT_SCHEMA["required"]
    # in the Draft, calculation_method is a STRING label (§4.1)
    assert props["feature_semantics"]["properties"]["calculation_method"] == {"type": "string"}


def test_confirmed_calculation_method_is_versioned_and_kind_discriminated():
    defs = contract.CONFIRMED_CONTRACT_JSON_SCHEMA["$defs"]
    cm = defs["calculation_method"]
    assert cm["required"] == ["method_version", "chosen"]
    variants = defs["method_variant"]["oneOf"]
    kinds = {v["properties"]["kind"]["const"] for v in variants}
    assert kinds == set(contract.METHOD_KINDS)
    assert contract.CONFIRMED_CONTRACT_JSON_SCHEMA["properties"]["status"] == {"const": "CONFIRMED"}


def test_ledger_top_level_array_is_assumptions_with_value_item_field():
    schema = contract.ASSUMPTION_LEDGER_CONTENT_SCHEMA
    assert schema["required"] == ["request_id", "assumptions"]
    item = schema["properties"]["assumptions"]["items"]
    assert item["required"] == ["field", "value", "rationale"]           # SP-0 required set
    assert item["properties"]["source"] == {"enum": ["default", "catalog", "llm"]}


def test_confirmed_rolling_aggregate_body_validates_and_a_bad_kind_is_rejected():
    # Validate the calculation_method against a root that carries the shared $defs (the tagged
    # method_variant / filter refs live at the schema root, so an isolated sub-schema cannot resolve
    # them). Reuse the real $defs via a spread so this exercises the shipped definitions.
    root = {**contract.CONFIRMED_CONTRACT_JSON_SCHEMA,
            "required": ["calculation_method"],
            "properties": {"calculation_method": {"$ref": "#/$defs/calculation_method"}}}
    good = {
        "method_version": 1,
        "chosen": {"kind": "rolling_aggregate", "aggregation": "count", "window": "90d",
                   "filter": {"concept": "declined card authorization",
                              "predicate": "card_authorizations.auth_result = 'D'"}},
        "considered": [{"kind": "rolling_aggregate", "aggregation": "count", "window": "90d"}],
    }
    jsonschema.validate({"calculation_method": good}, root)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {"calculation_method": {"method_version": 1, "chosen": {"kind": "made_up"}}}, root
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/featuregen/intake/test_contract_schemas.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'featuregen.intake'`

- [ ] **Step 3: Write minimal implementation**

Create `src/featuregen/intake/__init__.py` (empty). Create `src/featuregen/intake/contract.py`:

```python
from __future__ import annotations

from typing import Any

# UNKNOWN + the mode / classification vocabularies are SP-0's — reused VERBATIM (overview Shared
# Contract). Any UNKNOWN-valued semantic field MUST be listed in open_fields (§4.0).
from featuregen.documents.draft import INTAKE_MODES, RAW_INPUT_CLASSIFICATIONS, UNKNOWN

__all__ = [
    "INTAKE_MODES", "RAW_INPUT_CLASSIFICATIONS", "UNKNOWN",
    "OBSERVATION_INTENT_KINDS", "METHOD_KINDS", "SCORE_SOURCES", "ROUTED_TO",
    "DRAFT_STATUS", "CONFIRMED_STATUS",
    "DRAFT_CONTRACT_SCHEMA_VERSION", "ASSUMPTION_LEDGER_SCHEMA_VERSION",
    "CONFIRMED_CONTRACT_SCHEMA_VERSION",
    "DRAFT_CONTENT_SCHEMA", "CONFIRMED_CONTRACT_JSON_SCHEMA", "ASSUMPTION_LEDGER_CONTENT_SCHEMA",
]

# ---- closed enum vocabularies (§4.0) ----
OBSERVATION_INTENT_KINDS: tuple[str, ...] = ("point_in_time", "as_of_event")
METHOD_KINDS: tuple[str, ...] = ("rolling_aggregate", "point_snapshot", "ratio", "distribution_divergence")
SCORE_SOURCES: tuple[str, ...] = ("llm", "default", "catalog")
ROUTED_TO: tuple[str, ...] = ("human", "auto")
DRAFT_STATUS = "NEEDS_CLARIFICATION"
CONFIRMED_STATUS = "CONFIRMED"

DRAFT_CONTRACT_SCHEMA_VERSION = 1
ASSUMPTION_LEDGER_SCHEMA_VERSION = 1
CONFIRMED_CONTRACT_SCHEMA_VERSION = 1

# ---- DRAFT_CONTRACT authoritative content-schema (§4.1) ----
# calculation_method is a STRING label in the Draft (the faithful method name, or "UNKNOWN");
# it is reshaped into the tagged structure at confirmation (assemble_confirmed, Task 2.3).
DRAFT_CONTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "request_id", "intake_mode", "raw_input_ref", "raw_input_classification",
        "proposed_feature_name", "feature_semantics", "field_scores",
        "open_fields", "assumption_ledger_ref", "provenance", "status",
    ],
    "not": {"required": ["raw_input"]},          # raw text is never inline (§9.4)
    "additionalProperties": True,                # SP-0 owns + wraps the envelope
    "properties": {
        "request_id": {"type": "string"},
        "intake_mode": {"enum": list(INTAKE_MODES)},
        "raw_input_ref": {"type": "string", "pattern": "^(blob|doc)_"},
        "raw_input_classification": {"enum": list(RAW_INPUT_CLASSIFICATIONS)},
        "assumption_ledger_ref": {"type": "string", "pattern": "^doc_"},
        "proposed_feature_name": {"type": "string"},
        "feature_semantics": {
            "type": "object",
            "required": ["entity", "entity_grain", "observation_intent",
                         "calculation_method", "windows", "filters"],
            "properties": {
                "entity": {"type": "string"},
                "entity_grain": {"type": "array", "items": {"type": "string"}},
                "observation_intent": {"$ref": "#/$defs/observation_intent"},
                "calculation_method": {"type": "string"},   # faithful label, or "UNKNOWN"
                "windows": {"type": "array"},
                "filters": {"type": "array"},
                "target_definition": {"type": "string"},
            },
        },
        "field_scores": {"type": "object", "additionalProperties": {"$ref": "#/$defs/field_score"}},
        "open_fields": {"type": "array", "items": {"type": "string"}},
        "open_questions": {"type": "array", "items": {"$ref": "#/$defs/open_question"}},
        "provenance": {"type": "object", "required": ["schema_version"]},
        "status": {"const": DRAFT_STATUS},
    },
    "$defs": {
        "field_score": {
            "type": "object",
            "required": ["ambiguity", "confidence", "source"],
            "properties": {
                "ambiguity": {"type": "number"},
                "confidence": {"type": "number"},
                "source": {"enum": list(SCORE_SOURCES)},
            },
        },
        "observation_intent": {
            "type": "object",
            "required": ["kind"],
            "properties": {
                "kind": {"enum": list(OBSERVATION_INTENT_KINDS)},
                "as_of_field": {"type": "string"},
                "rule": {"type": "string"},
            },
        },
        "open_question": {
            "type": "object",
            "required": ["field", "question", "blocks_progress", "routed_to"],
            "properties": {"routed_to": {"enum": list(ROUTED_TO)}},
        },
    },
}

# ---- CONFIRMED_CONTRACT authoritative content-schema (§4.2) — tagged calculation_method ----
CONFIRMED_CONTRACT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "feature_name", "intake_mode", "raw_input_ref", "raw_input_classification",
        "entity", "entity_key", "feature_grain", "observation_intent",
        "calculation_method", "assumption_ledger_ref", "requires_independent_validation",
        "confirmation", "provenance", "status",
    ],
    "additionalProperties": True,
    "properties": {
        "feature_name": {"type": "string"},
        "intake_mode": {"enum": list(INTAKE_MODES)},
        "raw_input_ref": {"type": "string", "pattern": "^(blob|doc)_"},
        "raw_input_classification": {"enum": list(RAW_INPUT_CLASSIFICATIONS)},
        "entity": {"type": "string"},
        "entity_key": {"type": "string"},
        "feature_grain": {"type": "array", "items": {"type": "string"}},
        "observation_intent": {"$ref": "#/$defs/observation_intent"},
        "calculation_method": {"$ref": "#/$defs/calculation_method"},
        "target": {"type": ["object", "null"]},
        "assumption_ledger_ref": {"type": "string", "pattern": "^doc_"},
        "requires_independent_validation": {"type": "boolean"},
        "confirmation": {"type": "object", "required": ["confirmed_by", "confirmed_at"]},
        "provenance": {"type": "object", "required": ["derived_from", "schema_version"]},
        "status": {"const": CONFIRMED_STATUS},
    },
    "$defs": {
        "observation_intent": {
            "type": "object",
            "required": ["kind"],
            "properties": {"kind": {"enum": list(OBSERVATION_INTENT_KINDS)}},
        },
        "calculation_method": {
            "type": "object",
            "required": ["method_version", "chosen"],
            "properties": {
                "method_version": {"type": "integer"},
                "chosen": {"$ref": "#/$defs/method_variant"},
                "considered": {"type": "array", "items": {"$ref": "#/$defs/method_variant"}},
            },
        },
        "method_variant": {
            "type": "object",
            "required": ["kind"],
            "oneOf": [
                {"required": ["kind", "aggregation", "window"],
                 "properties": {"kind": {"const": "rolling_aggregate"},
                                "aggregation": {"type": "string"}, "window": {"type": "string"},
                                "filter": {"$ref": "#/$defs/filter"}}},
                {"required": ["kind", "field"],
                 "properties": {"kind": {"const": "point_snapshot"}, "field": {"type": "string"},
                                "filter": {"$ref": "#/$defs/filter"}}},
                {"required": ["kind", "numerator", "denominator"],
                 "properties": {"kind": {"const": "ratio"}, "numerator": {}, "denominator": {},
                                "window": {"type": "string"}}},
                {"required": ["kind", "measure", "window", "baseline_window"],
                 "properties": {"kind": {"const": "distribution_divergence"},
                                "measure": {"type": "string"}, "window": {"type": "string"},
                                "baseline_window": {"type": "string"}}},
            ],
        },
        "filter": {
            "type": "object",
            "properties": {"concept": {"type": "string"}, "predicate": {"type": "string"}},
        },
    },
}

# ---- ASSUMPTION_LEDGER authoritative content-schema (§4.3) — extends SP-0's registered schema ----
# Top-level array is `assumptions` (SP-0's required name, documents/draft.py:47 — NOT `entries`);
# each item value field is `value` (NOT `chosen_value`).
ASSUMPTION_LEDGER_CONTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["request_id", "assumptions"],
    "additionalProperties": True,
    "properties": {
        "request_id": {"type": "string"},
        "assumptions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["field", "value", "rationale"],   # SP-0 required set
                "properties": {
                    "field": {"type": "string"},
                    "value": {},                               # any JSON — the chosen value
                    "rationale": {"type": "string"},
                    "source": {"enum": ["default", "catalog", "llm"]},   # §4.3 closed enum (same members as SCORE_SOURCES)
                    "ambiguity": {"type": "number"},
                    "confidence": {"type": "number"},
                    "auto_resolved_at": {"type": "string"},
                },
            },
        },
    },
}
```

> Note: the ledger's `source` closed enum has the same three members as the field-score `source` (`SCORE_SOURCES`); it is spelled out here in the §4.3 order (`default | catalog | llm`) because a JSON-Schema `enum` is an ordered list and the Task-2.1 test asserts that exact list.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/featuregen/intake/test_contract_schemas.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/intake/__init__.py src/featuregen/intake/contract.py \
        tests/featuregen/intake/__init__.py tests/featuregen/intake/test_contract_schemas.py
git commit -m "feat(intake): Feature Contract closed-enum vocabularies + authoritative Draft/Confirmed/Ledger content-schemas + tagged calculation_method"
```

---

### Task 2.2: `intake/contract.py` — `validate_semantics` (semantic validation + the UNKNOWN-in-open_fields rule)

**Files:**
- Modify: `src/featuregen/intake/contract.py`
- Test: `tests/featuregen/intake/test_validate_semantics.py`

**Interfaces:**
- Consumes: `DRAFT_CONTENT_SCHEMA`, `CONFIRMED_CONTRACT_JSON_SCHEMA`, `ASSUMPTION_LEDGER_CONTENT_SCHEMA`, `UNKNOWN` (Task 2.1); `jsonschema`.
- Produces:
  ```python
  class ContractSemanticError(Exception)          # intake-owned (mirrors overlay's FactValidationError)
  def validate_semantics(body: Mapping, *, stage: str) -> None
        # stage ∈ {"DRAFT_CONTRACT","CONFIRMED_CONTRACT","ASSUMPTION_LEDGER"} (SP-0 Stage values)
        # SP-2 SEMANTIC validation (§4.0): SP-0 owns the envelope + required-field presence
        # (documents/draft.py::validate_draft); THIS runs the semantic content-schema + closed enums
        # + the UNKNOWN-listed-in-open_fields rule. Raises ContractSemanticError on any violation.
  ```

- [ ] **Step 1: Write the failing test**

Create `tests/featuregen/intake/test_validate_semantics.py`:

```python
import pytest

from featuregen.intake import contract
from featuregen.intake.contract import ContractSemanticError, validate_semantics


def _draft(open_fields=None, calc="rolling_count", predicate="card_authorizations.auth_result = 'D'"):
    return {
        "request_id": "req_1",
        "intake_mode": "definition",
        "raw_input_ref": "blob_01H",
        "raw_input_classification": "clean",
        "proposed_feature_name": "declined_card_auth_count_90d",
        "assumption_ledger_ref": "doc_led1",
        "feature_semantics": {
            "entity": "customer",
            "entity_grain": ["customer_id", "as_of_date"],
            "observation_intent": {"kind": "point_in_time", "as_of_field": "as_of_date"},
            "calculation_method": calc,
            "windows": [{"name": "lookback", "value": "90d"}],
            "filters": [{"concept": "declined card authorization", "predicate": predicate}],
        },
        "field_scores": {"entity": {"ambiguity": 0.05, "confidence": 0.97, "source": "llm"}},
        "open_fields": open_fields if open_fields is not None else [],
        "provenance": {"schema_version": 1, "llm_call_refs": ["llmc_1"]},
        "status": "NEEDS_CLARIFICATION",
    }


def test_valid_draft_passes():
    validate_semantics(_draft(), stage="DRAFT_CONTRACT")


def test_draft_missing_semantic_block_field_is_rejected():
    body = _draft()
    del body["feature_semantics"]["calculation_method"]
    with pytest.raises(ContractSemanticError):
        validate_semantics(body, stage="DRAFT_CONTRACT")


def test_draft_closed_enum_violation_is_rejected():
    body = _draft()
    body["feature_semantics"]["observation_intent"]["kind"] = "made_up"
    with pytest.raises(ContractSemanticError):
        validate_semantics(body, stage="DRAFT_CONTRACT")


def test_draft_wrong_status_const_is_rejected():
    body = _draft()
    body["status"] = "CONFIRMED"
    with pytest.raises(ContractSemanticError):
        validate_semantics(body, stage="DRAFT_CONTRACT")


def test_draft_unknown_calc_method_must_be_listed_in_open_fields():
    # calculation_method == UNKNOWN but NOT in open_fields → rejected (§4.0)
    body = _draft(open_fields=[], calc=contract.UNKNOWN)
    with pytest.raises(ContractSemanticError):
        validate_semantics(body, stage="DRAFT_CONTRACT")
    # listing it makes the Draft valid
    body_ok = _draft(open_fields=["calculation_method"], calc=contract.UNKNOWN)
    validate_semantics(body_ok, stage="DRAFT_CONTRACT")


def test_draft_unknown_filter_predicate_requires_an_open_fields_entry():
    body = _draft(open_fields=[], predicate=contract.UNKNOWN)
    with pytest.raises(ContractSemanticError):
        validate_semantics(body, stage="DRAFT_CONTRACT")
    body_ok = _draft(open_fields=["filters.declined_status_encoding"], predicate=contract.UNKNOWN)
    validate_semantics(body_ok, stage="DRAFT_CONTRACT")


def _confirmed():
    return {
        "feature_name": "declined_card_auth_count_90d",
        "intake_mode": "definition",
        "raw_input_ref": "blob_01H",
        "raw_input_classification": "clean",
        "entity": "customer",
        "entity_key": "customer_id",
        "feature_grain": ["customer_id", "as_of_date"],
        "observation_intent": {"kind": "point_in_time"},
        "calculation_method": {"method_version": 1,
                               "chosen": {"kind": "rolling_aggregate", "aggregation": "count",
                                          "window": "90d"},
                               "considered": []},
        "target": None,
        "assumption_ledger_ref": "doc_led1",
        "requires_independent_validation": False,
        "confirmation": {"confirmed_by": "user:raj", "confirmed_at": "2026-07-01T10:22:41Z"},
        "provenance": {"derived_from": ["doc_draft1"], "schema_version": 1},
        "status": "CONFIRMED",
    }


def test_valid_confirmed_passes_and_bad_method_variant_is_rejected():
    validate_semantics(_confirmed(), stage="CONFIRMED_CONTRACT")
    body = _confirmed()
    body["calculation_method"]["chosen"] = {"kind": "rolling_aggregate"}  # missing aggregation/window
    with pytest.raises(ContractSemanticError):
        validate_semantics(body, stage="CONFIRMED_CONTRACT")


def test_ledger_source_enum_is_closed():
    ok = {"request_id": "req_1", "assumptions": [
        {"field": "entity_grain", "value": ["customer_id", "as_of_date"],
         "rationale": "platform default", "source": "default"}]}
    validate_semantics(ok, stage="ASSUMPTION_LEDGER")
    bad = {"request_id": "req_1", "assumptions": [
        {"field": "x", "value": 1, "rationale": "r", "source": "guess"}]}
    with pytest.raises(ContractSemanticError):
        validate_semantics(bad, stage="ASSUMPTION_LEDGER")


def test_unknown_stage_is_rejected():
    with pytest.raises(ContractSemanticError):
        validate_semantics({}, stage="MAPPED_CONTRACT")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/featuregen/intake/test_validate_semantics.py -v`
Expected: FAIL with `ImportError: cannot import name 'ContractSemanticError'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/featuregen/intake/contract.py` (add the imports at the top, and the code at the end):

```python
# add to the existing top-of-file imports:
from collections.abc import Mapping

import jsonschema

# add to __all__:
#   "ContractSemanticError", "validate_semantics"


class ContractSemanticError(Exception):
    """Raised by `validate_semantics` when a contract body fails SP-2 SEMANTIC validation — the
    content-schema, a closed-enum violation, or the UNKNOWN-listed-in-open_fields rule (§4.0).
    Intake-owned (mirrors overlay's FactValidationError); every SP-2 phase imports THIS. SP-0's
    envelope/required-field validation (documents/draft.py::validate_draft) is separate and
    complementary — it raises SP-0's DraftValidationError."""


_CONTENT_SCHEMAS: dict[str, dict] = {
    "DRAFT_CONTRACT": DRAFT_CONTENT_SCHEMA,
    "CONFIRMED_CONTRACT": CONFIRMED_CONTRACT_JSON_SCHEMA,
    "ASSUMPTION_LEDGER": ASSUMPTION_LEDGER_CONTENT_SCHEMA,
}


def validate_semantics(body: Mapping[str, Any], *, stage: str) -> None:
    """SP-2 semantic validation for a contract body at `stage` (§4.0). Validates the authoritative
    content-schema (required semantic fields + closed enums + the tagged calculation_method), then —
    for a Draft — enforces the UNKNOWN-listed-in-open_fields rule: any semantic field carrying the
    UNKNOWN sentinel MUST appear in open_fields (§4.0). Raises ContractSemanticError on any
    violation. `stage` ∈ the three SP-2 content stages; any other stage is a programmer error."""
    schema = _CONTENT_SCHEMAS.get(stage)
    if schema is None:
        raise ContractSemanticError(f"no SP-2 semantic content-schema for stage {stage!r}")
    try:
        jsonschema.validate(instance=dict(body), schema=schema)
    except jsonschema.ValidationError as exc:
        raise ContractSemanticError(f"{stage} semantic invalid: {exc.message}") from exc
    if stage == "DRAFT_CONTRACT":
        _assert_unknowns_listed(body)


def _assert_unknowns_listed(body: Mapping[str, Any]) -> None:
    """Every UNKNOWN-valued semantic field must be listed in open_fields (§4.0). Covers the
    top-level feature_semantics string fields by their own name, and a UNKNOWN filter predicate by
    requiring at least one `filters`-prefixed open_fields path (the dotted encoding, e.g.
    `filters.declined_status_encoding`, §4.1)."""
    open_fields = set(body.get("open_fields") or ())
    fs = body.get("feature_semantics") or {}
    unlisted: list[str] = []
    for name in ("entity", "calculation_method", "target_definition"):
        val = fs.get(name)
        if isinstance(val, str) and val == UNKNOWN and name not in open_fields:
            unlisted.append(name)
    for i, filt in enumerate(fs.get("filters") or ()):
        if isinstance(filt, Mapping) and filt.get("predicate") == UNKNOWN:
            if not any(str(of).startswith("filters") for of in open_fields):
                unlisted.append(f"filters[{i}].predicate")
    if unlisted:
        raise ContractSemanticError(
            f"UNKNOWN fields must be listed in open_fields: {unlisted} (§4.0)"
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/featuregen/intake/test_validate_semantics.py -v`
Expected: PASS (10 passed)

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/intake/contract.py tests/featuregen/intake/test_validate_semantics.py
git commit -m "feat(intake): validate_semantics — SP-2 semantic content validation + UNKNOWN-in-open_fields rule"
```

---

### Task 2.3: `intake/contract.py` — `assemble_confirmed` (Draft→Confirmed renames + `calculation_method` reshape)

**Files:**
- Modify: `src/featuregen/intake/contract.py`
- Test: `tests/featuregen/intake/test_assemble_confirmed.py`

**Interfaces:**
- Consumes: `UNKNOWN`, `CONFIRMED_STATUS`, `validate_semantics` (Tasks 2.1/2.2).
- Produces:
  ```python
  def reshape_calculation_method(feature_semantics: Mapping, *, chosen_method: Mapping | None = None) -> dict
        # Draft string label + windows[0].value + filters[0] → tagged {method_version, chosen, considered}
        # (§4.2). rolling_* labels auto-reshape; any other method REQUIRES an explicit chosen_method
        # (the tagged variant, e.g. the selected hypothesis candidate). Raises ContractSemanticError
        # if the method is still UNKNOWN / un-reshapable.
  def assemble_confirmed(draft_body: Mapping, *, confirmation: Mapping, derived_from: Sequence[str],
                         feature_name: str | None = None, chosen_method: Mapping | None = None,
                         requires_independent_validation: bool = False,
                         target: Mapping | None = None, schema_version: int = 1) -> dict
        # deterministic Draft→Confirmed persistence (§4.2): entity_grain → feature_grain (+ derived
        # entity_key = grain[0]); proposed_feature_name → feature_name (feature_name overrides for a
        # Gate #1 edit); string calculation_method → tagged structure. Returns a CONFIRMED_CONTRACT
        # body (status=CONFIRMED). The CALLER (P7) validates it with validate_semantics.
  ```

- [ ] **Step 1: Write the failing test**

Create `tests/featuregen/intake/test_assemble_confirmed.py`:

```python
import pytest

from featuregen.intake.contract import (
    ContractSemanticError,
    assemble_confirmed,
    reshape_calculation_method,
    validate_semantics,
)


def _draft_semantics(calc="rolling_count", predicate="card_authorizations.auth_result = 'D'"):
    return {
        "entity": "customer",
        "entity_grain": ["customer_id", "as_of_date"],
        "observation_intent": {"kind": "point_in_time", "as_of_field": "as_of_date"},
        "calculation_method": calc,
        "windows": [{"name": "lookback", "value": "90d"}],
        "filters": [{"concept": "declined card authorization", "predicate": predicate}],
    }


def _draft_body():
    return {
        "request_id": "req_1", "intake_mode": "definition",
        "raw_input_ref": "blob_01H", "raw_input_classification": "clean",
        "proposed_feature_name": "declined_card_auth_count_90d",
        "assumption_ledger_ref": "doc_led1",
        "feature_semantics": _draft_semantics(),
        "field_scores": {}, "open_fields": [],
        "provenance": {"schema_version": 1, "llm_call_refs": ["llmc_1"]},
        "status": "NEEDS_CLARIFICATION",
    }


def test_reshape_rolling_count_matches_the_tagged_shape():
    cm = reshape_calculation_method(_draft_semantics())
    assert cm["method_version"] == 1
    assert cm["chosen"] == {
        "kind": "rolling_aggregate", "aggregation": "count", "window": "90d",
        "filter": {"concept": "declined card authorization",
                   "predicate": "card_authorizations.auth_result = 'D'"},
    }
    assert cm["considered"] == [cm["chosen"]]


def test_reshape_unknown_method_fails_closed():
    with pytest.raises(ContractSemanticError):
        reshape_calculation_method(_draft_semantics(calc="UNKNOWN"))


def test_reshape_non_rolling_label_requires_explicit_chosen_method():
    with pytest.raises(ContractSemanticError):
        reshape_calculation_method(_draft_semantics(calc="jensen_shannon"))
    # ...but an explicit tagged variant is accepted verbatim (hypothesis-mode candidate)
    chosen = {"kind": "distribution_divergence", "measure": "jensen_shannon",
              "window": "30d", "baseline_window": "180d"}
    cm = reshape_calculation_method(_draft_semantics(calc="jensen_shannon"), chosen_method=chosen)
    assert cm["chosen"] == chosen


def test_assemble_confirmed_renames_and_reshapes_deterministically_and_validates():
    confirmation = {"confirmed_by": "user:raj", "confirmed_at": "2026-07-01T10:22:41Z",
                    "selected_candidate": None, "rejected_candidates": [], "human_edits": []}
    confirmed = assemble_confirmed(
        _draft_body(), confirmation=confirmation, derived_from=["doc_draft1"],
    )
    # Draft→Confirmed renames (§4.2)
    assert confirmed["feature_name"] == "declined_card_auth_count_90d"
    assert confirmed["feature_grain"] == ["customer_id", "as_of_date"]
    assert confirmed["entity_key"] == "customer_id"       # derived: grain[0]
    assert confirmed["target"] is None
    assert confirmed["requires_independent_validation"] is False
    assert confirmed["status"] == "CONFIRMED"
    assert confirmed["provenance"]["derived_from"] == ["doc_draft1"]
    assert confirmed["provenance"]["llm_call_refs"] == ["llmc_1"]   # carried from the Draft
    # the assembled body is a valid CONFIRMED_CONTRACT
    validate_semantics(confirmed, stage="CONFIRMED_CONTRACT")


def test_assemble_confirmed_honours_a_gate1_feature_name_edit_and_risk_flag():
    confirmation = {"confirmed_by": "user:raj", "confirmed_at": "2026-07-01T10:22:41Z"}
    confirmed = assemble_confirmed(
        _draft_body(), confirmation=confirmation, derived_from=["doc_draft1"],
        feature_name="declined_auth_cnt_90d", requires_independent_validation=True,
    )
    assert confirmed["feature_name"] == "declined_auth_cnt_90d"
    assert confirmed["requires_independent_validation"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/featuregen/intake/test_assemble_confirmed.py -v`
Expected: FAIL with `ImportError: cannot import name 'assemble_confirmed'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/featuregen/intake/contract.py` (add `Sequence` to the `collections.abc` import; add both names to `__all__`):

```python
# extend the collections.abc import to: from collections.abc import Mapping, Sequence

# add to __all__: "reshape_calculation_method", "assemble_confirmed"

# label → (kind, aggregation) for the deterministic definition-mode reshape (§4.2). Any method not
# here (point_snapshot / ratio / distribution_divergence, or a hypothesis candidate) MUST arrive as
# an explicit tagged `chosen_method`.
_ROLLING_AGGREGATIONS: dict[str, str] = {
    "rolling_count": "count",
    "rolling_sum": "sum",
    "rolling_avg": "avg",
    "rolling_average": "avg",
    "rolling_mean": "avg",
}


def reshape_calculation_method(
    feature_semantics: Mapping[str, Any], *, chosen_method: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Reshape the Draft's string calculation_method (+ windows/filters) into the tagged, versioned
    structure SP-3 consumes deterministically (§4.2). If `chosen_method` (a tagged method_variant) is
    supplied — hypothesis-mode candidate selection, or any non-rolling method — it is used verbatim.
    Otherwise a `rolling_*` label is reshaped to a `rolling_aggregate` variant using windows[0].value
    and filters[0]. Fails closed (ContractSemanticError) on an UNKNOWN / un-reshapable method."""
    if chosen_method is not None:
        chosen: dict[str, Any] = dict(chosen_method)
    else:
        label = feature_semantics.get("calculation_method")
        if not isinstance(label, str) or label == UNKNOWN:
            raise ContractSemanticError("calculation_method must be resolved before assembly")
        aggregation = _ROLLING_AGGREGATIONS.get(label)
        if aggregation is None:
            raise ContractSemanticError(
                f"cannot auto-reshape calculation_method {label!r}; pass an explicit chosen_method"
            )
        windows = feature_semantics.get("windows") or ()
        window = windows[0].get("value") if windows and isinstance(windows[0], Mapping) else None
        if not window:
            raise ContractSemanticError("rolling_aggregate requires a window from windows[0].value")
        chosen = {"kind": "rolling_aggregate", "aggregation": aggregation, "window": window}
        filters = feature_semantics.get("filters") or ()
        if filters and isinstance(filters[0], Mapping):
            f0 = filters[0]
            filt: dict[str, Any] = {}
            if f0.get("concept"):
                filt["concept"] = f0["concept"]
            predicate = f0.get("predicate")
            if predicate and predicate != UNKNOWN:
                filt["predicate"] = predicate
            if filt:
                chosen["filter"] = filt
    return {"method_version": 1, "chosen": chosen, "considered": [dict(chosen)]}


def assemble_confirmed(
    draft_body: Mapping[str, Any],
    *,
    confirmation: Mapping[str, Any],
    derived_from: Sequence[str],
    feature_name: str | None = None,
    chosen_method: Mapping[str, Any] | None = None,
    requires_independent_validation: bool = False,
    target: Mapping[str, Any] | None = None,
    schema_version: int = 1,
) -> dict[str, Any]:
    """Deterministically assemble a CONFIRMED_CONTRACT body from a final Draft body (§4.2). Applies
    the Draft→Confirmed renames — entity_grain → feature_grain (+ derived entity_key = grain[0]) and
    proposed_feature_name → feature_name (overridden by a Gate #1 `feature_name` edit) — and reshapes
    the string calculation_method into the tagged structure. Envelope fields (raw_input_ref /
    raw_input_classification / intake_mode / observation_intent / assumption_ledger_ref) are carried
    forward unchanged. The caller (P7 confirm_contract) validates the result with validate_semantics
    before freezing the document."""
    fs = draft_body["feature_semantics"]
    grain = list(fs["entity_grain"])
    method = reshape_calculation_method(fs, chosen_method=chosen_method)
    draft_prov = draft_body.get("provenance") or {}
    return {
        "feature_name": feature_name or draft_body["proposed_feature_name"],
        "intake_mode": draft_body["intake_mode"],
        "raw_input_ref": draft_body["raw_input_ref"],
        "raw_input_classification": draft_body["raw_input_classification"],
        "entity": fs["entity"],
        "entity_key": grain[0] if grain else fs["entity"],
        "feature_grain": grain,
        "observation_intent": dict(fs["observation_intent"]),
        "calculation_method": method,
        "target": dict(target) if target is not None else None,
        "assumption_ledger_ref": draft_body["assumption_ledger_ref"],
        "requires_independent_validation": bool(requires_independent_validation),
        "confirmation": dict(confirmation),
        "provenance": {
            "derived_from": list(derived_from),
            "llm_call_refs": list(draft_prov.get("llm_call_refs", ())),
            "schema_version": schema_version,
        },
        "status": CONFIRMED_STATUS,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/featuregen/intake/test_assemble_confirmed.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/intake/contract.py tests/featuregen/intake/test_assemble_confirmed.py
git commit -m "feat(intake): assemble_confirmed — deterministic Draft→Confirmed renames + calculation_method reshape"
```

---

### Task 2.4: `intake/contract.py` — `register_contract_schemas` + the reader-upcaster seam

**Files:**
- Modify: `src/featuregen/intake/contract.py`
- Test: `tests/featuregen/intake/test_register_contract_schemas.py`

**Interfaces:**
- Consumes: `featuregen.documents.registry.DocumentSchemaRegistry(conn)` (SP-0 — `register_schema(type, ver, schema, owner, *, status)`, `validate(type, ver, body)`, `assert_writable(type, ver)`, `register_upcaster(type, from, to, fn)`, `upcast(type, body, from, to)`); `featuregen.documents.draft.register_draft_schemas(registry)` (SP-0 — registers the envelope DRAFT_CONTRACT@1 + ASSUMPTION_LEDGER@1); the `db` fixture (`tests/featuregen/conftest.py`).
- Produces:
  ```python
  CONTRACT_SCHEMA_OWNER: str                     # "featuregen-intake"
  def register_contract_schemas(registry) -> None
        # Registers CONFIRMED_CONTRACT@1 (the genuinely NEW registered document type — SP-0 registers
        # no confirmed schema) and re-affirms SP-0's DRAFT_CONTRACT@1 + ASSUMPTION_LEDGER@1 via
        # register_draft_schemas (idempotent; ADDS nothing divergent — additive per §2.1). Sets up the
        # (currently empty) reader-upcaster chain: v1 is the base, so there is nothing to upcast yet;
        # the first real chained reader-upcaster ships with the first schema-version bump (§4.0).
  ```

- [ ] **Step 1: Write the failing test**

Create `tests/featuregen/intake/test_register_contract_schemas.py`:

```python
import pytest

from featuregen.documents.registry import DocumentSchemaRegistry
from featuregen.intake import contract
from featuregen.intake.contract import register_contract_schemas


def _confirmed_body():
    return {
        "feature_name": "declined_card_auth_count_90d", "intake_mode": "definition",
        "raw_input_ref": "blob_01H", "raw_input_classification": "clean",
        "entity": "customer", "entity_key": "customer_id",
        "feature_grain": ["customer_id", "as_of_date"],
        "observation_intent": {"kind": "point_in_time"},
        "calculation_method": {"method_version": 1,
                               "chosen": {"kind": "rolling_aggregate", "aggregation": "count",
                                          "window": "90d"}, "considered": []},
        "target": None, "assumption_ledger_ref": "doc_led1",
        "requires_independent_validation": False,
        "confirmation": {"confirmed_by": "user:raj", "confirmed_at": "2026-07-01T10:22:41Z"},
        "provenance": {"derived_from": ["doc_draft1"], "schema_version": 1},
        "status": "CONFIRMED",
    }


def test_registers_all_three_content_stages_and_confirmed_is_writable(db):
    reg = DocumentSchemaRegistry(db)
    register_contract_schemas(reg)
    # CONFIRMED_CONTRACT is the NEW registered type; DRAFT/LEDGER re-affirmed (additive)
    reg.assert_writable("CONFIRMED_CONTRACT", contract.CONFIRMED_CONTRACT_SCHEMA_VERSION)
    reg.assert_writable("DRAFT_CONTRACT", contract.DRAFT_CONTRACT_SCHEMA_VERSION)
    reg.assert_writable("ASSUMPTION_LEDGER", contract.ASSUMPTION_LEDGER_SCHEMA_VERSION)


def test_registered_confirmed_schema_validates_a_good_body_and_rejects_a_bad_one(db):
    from featuregen.contracts import SchemaValidationError

    reg = DocumentSchemaRegistry(db)
    register_contract_schemas(reg)
    reg.validate("CONFIRMED_CONTRACT", 1, _confirmed_body())
    bad = _confirmed_body()
    del bad["calculation_method"]
    with pytest.raises(SchemaValidationError):
        reg.validate("CONFIRMED_CONTRACT", 1, bad)


def test_register_is_idempotent(db):
    reg = DocumentSchemaRegistry(db)
    register_contract_schemas(reg)
    register_contract_schemas(reg)   # must not raise (ON CONFLICT DO UPDATE, additive)
    reg.validate("CONFIRMED_CONTRACT", 1, _confirmed_body())


def test_upcaster_chain_is_total_at_v1_identity_read(db):
    # v1 is the base version — reading a v1 body "through the chain to the current version" is the
    # identity projection; the registry supports from==to. The first real upcaster ships with the
    # first schema-version bump (§4.0).
    reg = DocumentSchemaRegistry(db)
    register_contract_schemas(reg)
    body = _confirmed_body()
    assert reg.upcast("CONFIRMED_CONTRACT", body, 1, 1) == dict(body)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/featuregen/intake/test_register_contract_schemas.py -v`
Expected: FAIL with `ImportError: cannot import name 'register_contract_schemas'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/featuregen/intake/contract.py` (add the name to `__all__`):

```python
# add to __all__: "CONTRACT_SCHEMA_OWNER", "register_contract_schemas"

from featuregen.documents.draft import register_draft_schemas

CONTRACT_SCHEMA_OWNER = "featuregen-intake"


def register_contract_schemas(registry) -> None:
    """Register SP-2's Feature Contract content-schemas in SP-0's document registry (§2.1 #3, §4.0).
    Registers the genuinely NEW CONFIRMED_CONTRACT@1 (SP-0 registers no confirmed schema) and
    re-affirms SP-0's envelope DRAFT_CONTRACT@1 + ASSUMPTION_LEDGER@1 via register_draft_schemas so
    all three content stages are present after a single call. ADDITIVE (§2.1): register_schema uses
    ON CONFLICT DO UPDATE, and the DRAFT/LEDGER re-affirmation writes SP-0's own schemas back
    unchanged — no existing row is rewritten with a divergent schema. Idempotent. SP-2's *stricter*
    Draft/Ledger SEMANTIC constraints are enforced separately by validate_semantics (§4.0) — SP-0's
    registered envelope schemas stay the registry's Draft/Ledger schema of record.

    Reader-upcasters (§4.0): every schema is versioned; each version bump ships a total, chained,
    pure reader-upcaster registered via registry.register_upcaster(...). v1 is the base, so there is
    nothing to register yet — a v1 read through the chain is the identity projection."""
    register_draft_schemas(registry)   # SP-0 DRAFT_CONTRACT@1 + ASSUMPTION_LEDGER@1 (idempotent)
    registry.register_schema(
        "CONFIRMED_CONTRACT",
        CONFIRMED_CONTRACT_SCHEMA_VERSION,
        CONFIRMED_CONTRACT_JSON_SCHEMA,
        CONTRACT_SCHEMA_OWNER,
    )
    # No upcasters at v1. First bump: registry.register_upcaster("CONFIRMED_CONTRACT", 1, 2, _v1_to_v2)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/featuregen/intake/test_register_contract_schemas.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/intake/contract.py tests/featuregen/intake/test_register_contract_schemas.py
git commit -m "feat(intake): register_contract_schemas — CONFIRMED_CONTRACT@1 + additive Draft/Ledger re-affirm + upcaster seam"
```

---

### Task 2.5: `intake/state.py` — `FeatureContractStatus` + `FeatureContractState` + `fold_feature_contract_state`

**Files:**
- Create: `src/featuregen/intake/state.py`
- Test: `tests/featuregen/intake/test_state.py`

**Interfaces:**
- Consumes: the SP-2 FC event-type **string constants** from `featuregen.intake.events` (P1) — `INTENT_SUBMITTED`, `DRAFT_CONTRACT_PRODUCED`, `FIELD_AUTO_RESOLVED`, `CLARIFICATION_ANSWERED`, `CONTRACT_REFINED`, `MINIMUM_CONTRACT_VALIDATED`, `CONTRACT_CONFIRMED`, `INTENT_REJECTED`, `USE_CASE_ONBOARDING_REQUESTED`, `LLM_CALL_RECORDED` (+ `CONTRACT_CRITIQUED`, `CLARIFICATION_REQUESTED`, folded as no-ops). Stream items expose `.type`, `.event_id`, `.payload` (SP-0 `EventEnvelope`).
- Produces:
  ```python
  class FeatureContractStatus(str, Enum):        # closed lifecycle vocabulary (overview §4.6)
      NEEDS_CLARIFICATION; MINIMUM_CONTRACT_VALIDATED; CONFIRMED
      OUT_OF_SCOPE; PROHIBITED_DATA_CLASS; NEEDS_USE_CASE_ONBOARDING
  TERMINAL_STATUSES: frozenset[FeatureContractStatus]   # CONFIRMED, OUT_OF_SCOPE, PROHIBITED_DATA_CLASS
  @dataclass(frozen=True)
  class FeatureContractState:
      status: FeatureContractStatus | None
      open_fields: tuple[str, ...]
      request_id / run_id / intake_mode / draft_doc_id / assumption_ledger_ref /
      confirmed_doc_id / candidate_doc_ids / catalog_version / classification /
      matched_class / confirmed_by / llm_call_refs   # folded provenance
      # + properties: is_terminal, is_confirmed, mcv_passed
  def fold_feature_contract_state(stream: Iterable) -> FeatureContractState
        # mirrors overlay/state.py::fold_overlay_state — folds the feature_contract stream to the
        # authoritative status, with a NO-REGRESSION guard (a fold at/past terminal/confirmed refuses
        # a conflicting re-advance). NEVER a projection row.
  ```

- [ ] **Step 1: Write the failing test**

Create `tests/featuregen/intake/test_state.py`:

```python
from dataclasses import dataclass

from featuregen.intake import events
from featuregen.intake.state import (
    FeatureContractState,
    FeatureContractStatus,
    fold_feature_contract_state,
)


@dataclass
class _Evt:
    type: str
    event_id: str
    payload: dict


def _submitted(eid="evt_sub"):
    return _Evt(events.INTENT_SUBMITTED, eid,
                {"request_id": "req_1", "run_id": "run_1", "intake_mode": "definition",
                 "catalog_version": "banking-cat@1"})


def _produced(open_fields=("filters.declined_status_encoding",), candidates=()):
    return _Evt(events.DRAFT_CONTRACT_PRODUCED, "evt_prod",
                {"draft_doc_id": "doc_draft1", "assumption_ledger_ref": "doc_led1",
                 "open_fields": list(open_fields), "candidate_doc_ids": list(candidates)})


def test_empty_stream_is_unopened():
    st = fold_feature_contract_state([])
    assert st.status is None
    assert st.open_fields == ()
    assert not st.is_terminal


def test_submit_then_draft_is_needs_clarification_with_open_fields():
    st = fold_feature_contract_state([_submitted(), _produced()])
    assert st.status is FeatureContractStatus.NEEDS_CLARIFICATION
    assert st.open_fields == ("filters.declined_status_encoding",)
    assert st.request_id == "req_1"
    assert st.run_id == "run_1"
    assert st.intake_mode == "definition"
    assert st.draft_doc_id == "doc_draft1"
    assert st.assumption_ledger_ref == "doc_led1"
    assert st.catalog_version == "banking-cat@1"
    assert not st.mcv_passed


def test_answering_and_auto_resolving_clears_open_fields():
    answered = _Evt(events.CLARIFICATION_ANSWERED, "evt_ans",
                    {"field": "filters.declined_status_encoding"})
    st = fold_feature_contract_state([_submitted(), _produced(), answered])
    assert st.open_fields == ()
    resolved = _Evt(events.FIELD_AUTO_RESOLVED, "evt_ar", {"field": "entity_grain"})
    st2 = fold_feature_contract_state(
        [_submitted(), _produced(open_fields=("entity_grain",)), resolved])
    assert st2.open_fields == ()


def test_mcv_then_confirm_advances_status():
    mcv = _Evt(events.MINIMUM_CONTRACT_VALIDATED, "evt_mcv", {})
    st_mcv = fold_feature_contract_state([_submitted(), _produced(open_fields=()), mcv])
    assert st_mcv.status is FeatureContractStatus.MINIMUM_CONTRACT_VALIDATED
    assert st_mcv.mcv_passed
    conf = _Evt(events.CONTRACT_CONFIRMED, "evt_conf",
                {"confirmed_doc_id": "doc_conf1", "confirmed_by": "user:raj"})
    st_conf = fold_feature_contract_state([_submitted(), _produced(open_fields=()), mcv, conf])
    assert st_conf.status is FeatureContractStatus.CONFIRMED
    assert st_conf.is_confirmed
    assert st_conf.confirmed_doc_id == "doc_conf1"
    assert st_conf.confirmed_by == "user:raj"


def test_edit_reopening_a_field_drops_back_from_mcv_to_needs_clarification():
    mcv = _Evt(events.MINIMUM_CONTRACT_VALIDATED, "evt_mcv", {})
    refined = _Evt(events.CONTRACT_REFINED, "evt_ref",
                   {"draft_doc_id": "doc_draft2", "open_fields": ["calculation_method"]})
    st = fold_feature_contract_state([_submitted(), _produced(open_fields=()), mcv, refined])
    assert st.status is FeatureContractStatus.NEEDS_CLARIFICATION
    assert st.open_fields == ("calculation_method",)
    assert st.draft_doc_id == "doc_draft2"


def test_intent_rejected_folds_to_the_carried_classification():
    rej = _Evt(events.INTENT_REJECTED, "evt_rej",
               {"classification": "PROHIBITED_DATA_CLASS", "matched_class": "protected_attribute",
                "catalog_version": "banking-cat@1"})
    st = fold_feature_contract_state([_submitted(), rej])
    assert st.status is FeatureContractStatus.PROHIBITED_DATA_CLASS
    assert st.is_terminal
    assert st.matched_class == "protected_attribute"
    assert st.classification == "PROHIBITED_DATA_CLASS"


def test_onboarding_request_parks_the_contract():
    onb = _Evt(events.USE_CASE_ONBOARDING_REQUESTED, "evt_onb", {"catalog_version": "banking-cat@1"})
    st = fold_feature_contract_state([_submitted(), onb])
    assert st.status is FeatureContractStatus.NEEDS_USE_CASE_ONBOARDING


def test_no_regression_guard_locks_confirmed_and_terminal_states():
    # a stray re-advance AFTER CONFIRMED must be ignored (mirrors overlay's defensive fold)
    mcv = _Evt(events.MINIMUM_CONTRACT_VALIDATED, "evt_mcv", {})
    conf = _Evt(events.CONTRACT_CONFIRMED, "evt_conf", {"confirmed_doc_id": "doc_conf1"})
    stray_refine = _Evt(events.CONTRACT_REFINED, "evt_ref2", {"open_fields": ["x"]})
    st = fold_feature_contract_state(
        [_submitted(), _produced(open_fields=()), mcv, conf, stray_refine])
    assert st.status is FeatureContractStatus.CONFIRMED
    assert st.open_fields == ()
    # a stray DRAFT after a terminal rejection must not re-open the contract
    rej = _Evt(events.INTENT_REJECTED, "evt_rej", {"classification": "OUT_OF_SCOPE"})
    st2 = fold_feature_contract_state([_submitted(), rej, _produced()])
    assert st2.status is FeatureContractStatus.OUT_OF_SCOPE


def test_llm_call_refs_accrete_even_after_confirmation():
    conf = _Evt(events.CONTRACT_CONFIRMED, "evt_conf", {"confirmed_doc_id": "doc_conf1"})
    llm = _Evt(events.LLM_CALL_RECORDED, "evt_llm", {"llm_call_ref": "llmc_9"})
    st = fold_feature_contract_state([_submitted(), conf, llm])
    assert st.llm_call_refs == ("llmc_9",)
    assert st.status is FeatureContractStatus.CONFIRMED
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/featuregen/intake/test_state.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'featuregen.intake.state'`

- [ ] **Step 3: Write minimal implementation**

Create `src/featuregen/intake/state.py`:

```python
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum

from featuregen.intake import events


class FeatureContractStatus(str, Enum):
    """The closed Feature Contract lifecycle vocabulary (overview §4.6, spec §11). FOLDED from the
    feature_contract event stream — never a stored enum, never a projection row."""

    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    MINIMUM_CONTRACT_VALIDATED = "MINIMUM_CONTRACT_VALIDATED"
    CONFIRMED = "CONFIRMED"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"                             # terminal (banking-boundary)
    PROHIBITED_DATA_CLASS = "PROHIBITED_DATA_CLASS"          # terminal (blocked-class)
    NEEDS_USE_CASE_ONBOARDING = "NEEDS_USE_CASE_ONBOARDING"  # park / hold


# CONFIRMED + the two banking-boundary rejections are no-regression-locked (a later, conflicting
# event never moves the fold off them). NEEDS_USE_CASE_ONBOARDING is a park that a governance flow
# (out of SP-2 scope) may later resume — so it is NOT locked here.
TERMINAL_STATUSES: frozenset[FeatureContractStatus] = frozenset({
    FeatureContractStatus.CONFIRMED,
    FeatureContractStatus.OUT_OF_SCOPE,
    FeatureContractStatus.PROHIBITED_DATA_CLASS,
})


@dataclass(frozen=True)
class FeatureContractState:
    """The authoritative folded state of the feature_contract aggregate — the value every SP-2
    command handler gates on inline before appending (spec §11), mirroring OverlayState."""

    status: FeatureContractStatus | None = None
    open_fields: tuple[str, ...] = ()
    request_id: str | None = None
    run_id: str | None = None
    intake_mode: str | None = None
    draft_doc_id: str | None = None
    assumption_ledger_ref: str | None = None
    confirmed_doc_id: str | None = None
    candidate_doc_ids: tuple[str, ...] = ()
    catalog_version: str | None = None
    classification: str | None = None
    matched_class: str | None = None
    confirmed_by: str | None = None
    llm_call_refs: tuple[str, ...] = ()

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_confirmed(self) -> bool:
        return self.status is FeatureContractStatus.CONFIRMED

    @property
    def mcv_passed(self) -> bool:
        return self.status in (
            FeatureContractStatus.MINIMUM_CONTRACT_VALIDATED,
            FeatureContractStatus.CONFIRMED,
        )


def fold_feature_contract_state(stream: Iterable) -> FeatureContractState:
    """Fold a feature_contract event stream (stream_version ASC) into the current lifecycle state
    (spec §4.6, §11). Mirrors overlay/state.py::fold_overlay_state — each item exposes `.type`,
    `.event_id`, `.payload`. It is the AUTHORITATIVE state for command decisions, never a projection.

    NO-REGRESSION GUARD (spec §11): once the fold reaches a no-regression-locked status (CONFIRMED /
    OUT_OF_SCOPE / PROHIBITED_DATA_CLASS) only LLM_CALL_RECORDED accretes provenance; every other
    event is ignored, so a stray/duplicate/late event can never regress or re-advance a locked
    contract. MINIMUM_CONTRACT_VALIDATED ↔ NEEDS_CLARIFICATION is intentionally two-way: a
    CONTRACT_REFINED that re-opens a field drops MCV back to NEEDS_CLARIFICATION (that is refinement,
    not regression past a lock)."""
    status: FeatureContractStatus | None = None
    open_fields: tuple[str, ...] = ()
    request_id = run_id = intake_mode = None
    draft_doc_id = assumption_ledger_ref = confirmed_doc_id = None
    candidate_doc_ids: tuple[str, ...] = ()
    catalog_version = classification = matched_class = confirmed_by = None
    llm_call_refs: tuple[str, ...] = ()

    for event in stream:
        t = event.type
        p = event.payload
        if status in TERMINAL_STATUSES:
            if t == events.LLM_CALL_RECORDED:
                llm_call_refs = llm_call_refs + (p["llm_call_ref"],)
            continue
        if t == events.INTENT_SUBMITTED:
            status = FeatureContractStatus.NEEDS_CLARIFICATION
            request_id = p.get("request_id")
            run_id = p.get("run_id")
            intake_mode = p.get("intake_mode")
            catalog_version = p.get("catalog_version", catalog_version)
        elif t == events.DRAFT_CONTRACT_PRODUCED:
            draft_doc_id = p.get("draft_doc_id")
            assumption_ledger_ref = p.get("assumption_ledger_ref")
            open_fields = tuple(p.get("open_fields") or ())
            candidate_doc_ids = tuple(p.get("candidate_doc_ids") or ())
        elif t == events.CONTRACT_REFINED:
            draft_doc_id = p.get("draft_doc_id", draft_doc_id)
            open_fields = tuple(p.get("open_fields") or ())
            if status is FeatureContractStatus.MINIMUM_CONTRACT_VALIDATED and open_fields:
                status = FeatureContractStatus.NEEDS_CLARIFICATION
        elif t in (events.FIELD_AUTO_RESOLVED, events.CLARIFICATION_ANSWERED):
            resolved = p.get("field")
            if resolved is not None:
                open_fields = tuple(f for f in open_fields if f != resolved)
        elif t == events.MINIMUM_CONTRACT_VALIDATED:
            status = FeatureContractStatus.MINIMUM_CONTRACT_VALIDATED
        elif t == events.CONTRACT_CONFIRMED:
            status = FeatureContractStatus.CONFIRMED
            confirmed_doc_id = p.get("confirmed_doc_id")
            confirmed_by = p.get("confirmed_by")
        elif t == events.INTENT_REJECTED:
            classification = p.get("classification")
            status = FeatureContractStatus(classification)   # OUT_OF_SCOPE | PROHIBITED_DATA_CLASS
            matched_class = p.get("matched_class")
            catalog_version = p.get("catalog_version", catalog_version)
        elif t == events.USE_CASE_ONBOARDING_REQUESTED:
            status = FeatureContractStatus.NEEDS_USE_CASE_ONBOARDING
            catalog_version = p.get("catalog_version", catalog_version)
        elif t == events.LLM_CALL_RECORDED:
            llm_call_refs = llm_call_refs + (p["llm_call_ref"],)
        # CONTRACT_CRITIQUED / CLARIFICATION_REQUESTED: doubt/question shadows — no status change.

    return FeatureContractState(
        status=status, open_fields=open_fields, request_id=request_id, run_id=run_id,
        intake_mode=intake_mode, draft_doc_id=draft_doc_id,
        assumption_ledger_ref=assumption_ledger_ref, confirmed_doc_id=confirmed_doc_id,
        candidate_doc_ids=candidate_doc_ids, catalog_version=catalog_version,
        classification=classification, matched_class=matched_class, confirmed_by=confirmed_by,
        llm_call_refs=llm_call_refs,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/featuregen/intake/test_state.py -v`
Expected: PASS (9 passed)

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/intake/state.py tests/featuregen/intake/test_state.py
git commit -m "feat(intake): FeatureContractStatus + FeatureContractState + fold_feature_contract_state (no-regression guard)"
```

---

### Task 2.6: `intake/banking_catalog.py` — the read-only `BankingDomainCatalog` reference reader

**Files:**
- Create: `src/featuregen/intake/banking_catalog.py`
- Test: `tests/featuregen/intake/test_banking_catalog_reader.py`

**Interfaces:**
- Consumes: nothing (pure stdlib `json`). Reads the SP-0-governed `banking-domain-catalog.seed.json` shape — top keys `catalog_version`, `data_classes`, `entities`, `use_cases[]` (each `{use_case, status, blocked_data_classes, allowed_data_classes, target?, ...}`). SP-2 **reads only**; never writes; never grounding (Decision D8).
- Produces:
  ```python
  @dataclass(frozen=True)
  class BankingDomainCatalog:
      version: str | None
      banking_entities: frozenset[str]; banking_terms: frozenset[str]
      allowed_use_cases: frozenset[str]; out_of_scope_use_cases: frozenset[str]
      out_of_scope_terms: frozenset[str]
      blocked_data_classes: frozenset[str]; blocked_terms: Mapping[str, str]   # surface term -> data class
      sensitive_proxy_terms: frozenset[str]
      use_case_terms: Mapping[str, tuple[str, ...]]; predictive_markers: frozenset[str]
      scoped_use_cases: frozenset[str]
      owner: str | None; effective_date: str | None; provenance: str | None
      @property
      def available(self) -> bool                       # bool(version) — the fail-closed gate (§4.5(b))
      @classmethod
      def from_seed(cls, seed: Mapping) -> "BankingDomainCatalog"
  def load_banking_catalog(path) -> BankingDomainCatalog   # json.load → from_seed
  ```

- [ ] **Step 1: Write the failing test**

Create `tests/featuregen/intake/test_banking_catalog_reader.py`:

```python
from featuregen.intake.banking_catalog import BankingDomainCatalog

_SEED = {
    "catalog_version": "0.1.0-draft",
    "data_classes": ["transactions", "balances", "protected_attribute", "geolocation"],
    "entities": ["customer", "account"],
    "use_cases": [
        {"use_case": "retail_churn", "status": "active",
         "blocked_data_classes": ["protected_attribute"],
         "allowed_data_classes": ["transactions", "balances"],
         "target": {"name": "churn", "definition": "no txn for 90d"}},
        {"use_case": "behavioral_credit_scoring", "status": "active",
         "blocked_data_classes": ["protected_attribute"],
         "target": {"name": "credit risk"}},
        {"use_case": "card_fraud_realtime", "status": "out_of_scope",
         "blocked_data_classes": ["protected_attribute"]},
    ],
}


def test_from_seed_maps_version_scope_and_use_cases():
    cat = BankingDomainCatalog.from_seed(_SEED)
    assert cat.version == "0.1.0-draft"
    assert cat.available
    assert "retail_churn" in cat.allowed_use_cases
    assert "behavioral_credit_scoring" in cat.allowed_use_cases
    assert "card_fraud_realtime" in cat.out_of_scope_use_cases
    assert "card_fraud_realtime" not in cat.allowed_use_cases


def test_from_seed_derives_blocked_classes_and_surface_terms():
    cat = BankingDomainCatalog.from_seed(_SEED)
    assert cat.blocked_data_classes == frozenset({"protected_attribute"})
    # protected_attribute expands to protected-attribute surface terms → each maps back to the class
    assert cat.blocked_terms["race"] == "protected_attribute"
    assert cat.blocked_terms["gender"] == "protected_attribute"


def test_from_seed_builds_banking_terms_and_use_case_terms():
    cat = BankingDomainCatalog.from_seed(_SEED)
    assert "customer" in cat.banking_entities
    assert "customer" in cat.banking_terms and "transactions" in cat.banking_terms
    assert "churn" in cat.use_case_terms["retail_churn"]
    assert "credit risk" in cat.use_case_terms["behavioral_credit_scoring"]


def test_geolocation_data_class_seeds_a_sensitive_proxy_term():
    cat = BankingDomainCatalog.from_seed(_SEED)
    assert "zip code" in cat.sensitive_proxy_terms


def test_missing_version_is_unavailable_fail_closed_gate():
    cat = BankingDomainCatalog.from_seed({"catalog_version": "", "use_cases": []})
    assert cat.available is False
    empty = BankingDomainCatalog(version=None)
    assert empty.available is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/featuregen/intake/test_banking_catalog_reader.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'featuregen.intake.banking_catalog'`

- [ ] **Step 3: Write minimal implementation**

Create `src/featuregen/intake/banking_catalog.py`:

```python
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field

# Surface terms per blocked data class — the deterministic lexical expansion the intake screen
# matches against raw intent text (§5.4). `protected_attribute` is the platform-wide blocked class
# in the seed (every use-case blocks it); these are the protected characteristics it covers.
_PROTECTED_ATTRIBUTE_TERMS: tuple[str, ...] = (
    "race", "ethnicity", "gender", "sex", "religion", "age", "disability",
    "marital status", "national origin", "sexual orientation",
)
_CLASS_SURFACE_TERMS: dict[str, tuple[str, ...]] = {
    "protected_attribute": _PROTECTED_ATTRIBUTE_TERMS,
}
# Data classes that are sensitive PROXIES (route to clarification / compliance review, NOT a block,
# §4.5, §6.2), and their raw-text surface terms.
_PROXY_TERMS_BY_CLASS: dict[str, tuple[str, ...]] = {
    "geolocation": ("zip code", "postal code", "neighbourhood", "neighborhood"),
    "demographics": ("age band", "income bracket"),
    "device": (),
}
_DEFAULT_PREDICTIVE_MARKERS: tuple[str, ...] = (
    "predict", "prediction", "propensity", "likelihood", "more likely",
    "higher risk", "score for", "who will", "which customers",
)
_DEFAULT_OUT_OF_SCOPE_TERMS: tuple[str, ...] = (
    "netflix", "e-commerce", "cart abandonment", "streaming", "movie",
)


@dataclass(frozen=True)
class BankingDomainCatalog:
    """Read-only, SP-0-governed banking-boundary / blocked-class reference data (§4.5). SP-2 READS
    only — never writes, never grounding (Decision D8). Term-sets are the deterministic lexical
    surfaces classify_intent matches raw intent text against (§5.4)."""

    version: str | None
    banking_entities: frozenset[str] = frozenset()
    banking_terms: frozenset[str] = frozenset()
    allowed_use_cases: frozenset[str] = frozenset()
    out_of_scope_use_cases: frozenset[str] = frozenset()
    out_of_scope_terms: frozenset[str] = frozenset()
    blocked_data_classes: frozenset[str] = frozenset()
    blocked_terms: Mapping[str, str] = field(default_factory=dict)
    sensitive_proxy_terms: frozenset[str] = frozenset()
    use_case_terms: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    predictive_markers: frozenset[str] = frozenset()
    scoped_use_cases: frozenset[str] = frozenset()
    owner: str | None = None
    effective_date: str | None = None
    provenance: str | None = None

    @property
    def available(self) -> bool:
        """The fail-closed gate (§4.5(b)): an absent/unversioned catalog is UNAVAILABLE — the intake
        screen must never auto-pass against it."""
        return bool(self.version)

    @classmethod
    def from_seed(cls, seed: Mapping) -> "BankingDomainCatalog":
        """Map the SP-0-governed banking-domain-catalog seed shape → the reader dataclass. Optional
        seed keys override the built-in surface defaults: `sensitive_proxy_hints`,
        `out_of_scope_examples`, `predictive_markers`, `scoped_use_cases`."""
        use_cases = list(seed.get("use_cases") or ())
        allowed = frozenset(
            u["use_case"] for u in use_cases if u.get("status", "active") == "active"
        )
        oos_uc = frozenset(
            u["use_case"] for u in use_cases if u.get("status") == "out_of_scope"
        )
        blocked_classes = frozenset(
            c for u in use_cases for c in (u.get("blocked_data_classes") or ())
        )
        blocked_terms: dict[str, str] = {}
        for cls_name in blocked_classes:
            for term in _CLASS_SURFACE_TERMS.get(cls_name, (cls_name,)):
                blocked_terms[term] = cls_name

        entities = frozenset(seed.get("entities") or ())
        data_classes = frozenset(seed.get("data_classes") or ())
        use_case_terms: dict[str, tuple[str, ...]] = {}
        for u in use_cases:
            name = u["use_case"]
            terms = [name.replace("_", " ")]
            target_name = (u.get("target") or {}).get("name")
            if target_name:
                terms.append(target_name)
            use_case_terms[name] = tuple(dict.fromkeys(terms))

        banking_terms = frozenset(
            set(entities)
            | set(data_classes)
            | {t for terms in use_case_terms.values() for t in terms}
        )
        proxy_terms: set[str] = set(seed.get("sensitive_proxy_hints") or ())
        for c in data_classes:
            proxy_terms.update(_PROXY_TERMS_BY_CLASS.get(c, ()))

        return cls(
            version=seed.get("catalog_version") or seed.get("version"),
            banking_entities=entities,
            banking_terms=banking_terms,
            allowed_use_cases=allowed,
            out_of_scope_use_cases=oos_uc,
            out_of_scope_terms=frozenset(
                seed.get("out_of_scope_examples") or _DEFAULT_OUT_OF_SCOPE_TERMS
            ),
            blocked_data_classes=blocked_classes,
            blocked_terms=blocked_terms,
            sensitive_proxy_terms=frozenset(proxy_terms),
            use_case_terms=use_case_terms,
            predictive_markers=frozenset(
                seed.get("predictive_markers") or _DEFAULT_PREDICTIVE_MARKERS
            ),
            scoped_use_cases=frozenset(seed.get("scoped_use_cases") or ()),
            owner=seed.get("owner"),
            effective_date=seed.get("effective_date"),
            provenance=seed.get("source") or seed.get("provenance"),
        )


def load_banking_catalog(path: str | os.PathLike) -> BankingDomainCatalog:
    """Load the read-only banking-domain-catalog seed JSON at `path` into a BankingDomainCatalog.
    A thin, side-effect-free reader (Decision D8): open → json.load → from_seed."""
    with open(path, encoding="utf-8") as fh:
        seed = json.load(fh)
    return BankingDomainCatalog.from_seed(seed)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/featuregen/intake/test_banking_catalog_reader.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/intake/banking_catalog.py tests/featuregen/intake/test_banking_catalog_reader.py
git commit -m "feat(intake): read-only BankingDomainCatalog reference reader (from_seed + load_banking_catalog)"
```

---

### Task 2.7: `intake/banking_catalog.py` — `IntakeOutcome` + `IntakeClassification` + deterministic `classify_intent`

**Files:**
- Modify: `src/featuregen/intake/banking_catalog.py`
- Test: `tests/featuregen/intake/test_classify_intent.py`

**Interfaces:**
- Consumes: `BankingDomainCatalog` (Task 2.6).
- Produces:
  ```python
  class IntakeOutcome(str, Enum):
      OUT_OF_SCOPE; PROHIBITED_DATA_CLASS; SENSITIVE_PROXY_CLARIFY
      AMBIGUOUS_CLARIFY; NEEDS_USE_CASE_ONBOARDING; CLEAR
  @dataclass(frozen=True)
  class IntakeClassification:
      outcome: IntakeOutcome; catalog_version: str | None; reason: str
      matched_class: str | None; matched_use_case: str | None
      # + properties: is_clear, blocks (OUT_OF_SCOPE|PROHIBITED_DATA_CLASS), needs_clarification
  def classify_intent(intent: str, *, product: str | None = None, region: str | None = None,
                      catalog: BankingDomainCatalog | None) -> IntakeClassification
        # deterministic (NOT the LLM's call, §5.4). TOTAL + fail-closed + precedence
        # most-restrictive-wins (PROHIBITED_DATA_CLASS > OUT_OF_SCOPE > sensitive-proxy > ambiguous),
        # exactly one outcome, catalog `version` stamped on EVERY outcome incl. CLEAR (§4.5 a/b/c/e).
  ```
  > `OUT_OF_SCOPE` / `PROHIBITED_DATA_CLASS` map to the P4 `reject_intent` terminal outcome (→ `INTENT_REJECTED` carrying `classification` = `outcome.value`); `NEEDS_USE_CASE_ONBOARDING` maps to the P4 onboarding park. The values deliberately match `FeatureContractStatus` (Task 2.5) so the fold can do `FeatureContractStatus(payload["classification"])`. **Catalog-unavailable fails closed to `AMBIGUOUS_CLARIFY`** (never CLEAR, `catalog_version=None`, `reason="catalog_unavailable_fail_closed"`); P4 treats a catalog-unavailable `AMBIGUOUS_CLARIFY` as the §4.5(b) manual-review park.

- [ ] **Step 1: Write the failing test**

Create `tests/featuregen/intake/test_classify_intent.py`:

```python
import pytest

from featuregen.intake.banking_catalog import (
    BankingDomainCatalog,
    IntakeClassification,
    IntakeOutcome,
    classify_intent,
)

CAT = BankingDomainCatalog(
    version="banking-cat@1",
    banking_entities=frozenset({"customer", "account", "card"}),
    banking_terms=frozenset({"customer", "account", "card", "authorization", "authorizations",
                             "transaction", "credit", "balance", "mortgage"}),
    allowed_use_cases=frozenset({"behavioral_credit_scoring", "retail_churn"}),
    out_of_scope_use_cases=frozenset({"card_fraud_realtime"}),
    out_of_scope_terms=frozenset({"netflix", "cart abandonment"}),
    blocked_data_classes=frozenset({"protected_attribute"}),
    blocked_terms={"race": "protected_attribute", "gender": "protected_attribute"},
    sensitive_proxy_terms=frozenset({"zip code", "postal code"}),
    use_case_terms={"behavioral_credit_scoring": ("credit risk", "credit score"),
                    "retail_churn": ("churn",),
                    "card_fraud_realtime": ("real-time card fraud",)},
    predictive_markers=frozenset({"predict", "propensity", "more likely"}),
    scoped_use_cases=frozenset({"retail_churn"}),
)


def test_prohibited_data_class_is_the_most_restrictive_outcome():
    r = classify_intent("predict churn using race for netflix subscribers", catalog=CAT)
    assert r.outcome is IntakeOutcome.PROHIBITED_DATA_CLASS   # race dominates netflix + churn
    assert r.matched_class == "protected_attribute"
    assert r.catalog_version == "banking-cat@1"
    assert r.blocks


def test_out_of_scope_example_term():
    r = classify_intent("predict which netflix shows a customer will watch", catalog=CAT)
    assert r.outcome is IntakeOutcome.OUT_OF_SCOPE
    assert r.catalog_version == "banking-cat@1"


def test_out_of_scope_when_no_banking_concept_present():
    r = classify_intent("rank the best pizza toppings", catalog=CAT)
    assert r.outcome is IntakeOutcome.OUT_OF_SCOPE


def test_out_of_scope_use_case_is_rejected():
    r = classify_intent("build a real-time card fraud model", catalog=CAT)
    assert r.outcome is IntakeOutcome.OUT_OF_SCOPE
    assert r.matched_use_case == "card_fraud_realtime"


def test_sensitive_proxy_routes_to_clarification_not_a_block():
    r = classify_intent("credit risk score using the customer's zip code", catalog=CAT)
    assert r.outcome is IntakeOutcome.SENSITIVE_PROXY_CLARIFY
    assert r.needs_clarification
    assert not r.blocks


def test_missing_product_region_for_a_scoped_use_case_is_ambiguous():
    r = classify_intent("predict churn for these customers", catalog=CAT)
    assert r.outcome is IntakeOutcome.AMBIGUOUS_CLARIFY
    ok = classify_intent("predict churn for these customers", product="cards", region="UK",
                         catalog=CAT)
    assert ok.outcome is IntakeOutcome.CLEAR
    assert ok.matched_use_case == "retail_churn"


def test_in_scope_known_use_case_is_clear():
    r = classify_intent("build a credit risk score for customers", catalog=CAT)
    assert r.outcome is IntakeOutcome.CLEAR
    assert r.is_clear
    assert r.matched_use_case == "behavioral_credit_scoring"
    assert r.catalog_version == "banking-cat@1"


def test_plain_banking_feature_definition_is_clear():
    r = classify_intent(
        "90-day rolling count of declined card authorizations per customer", catalog=CAT)
    assert r.outcome is IntakeOutcome.CLEAR


def test_in_scope_unknown_use_case_routes_to_onboarding():
    r = classify_intent("predict which customers will prepay their mortgage early", catalog=CAT)
    assert r.outcome is IntakeOutcome.NEEDS_USE_CASE_ONBOARDING
    assert r.catalog_version == "banking-cat@1"


def test_catalog_unavailable_fails_closed_never_clear():
    for cat in (None, BankingDomainCatalog(version=None)):
        r = classify_intent("build a credit risk score for customers", catalog=cat)
        assert r.outcome is IntakeOutcome.AMBIGUOUS_CLARIFY
        assert r.catalog_version is None
        assert not r.is_clear


def test_every_outcome_stamps_the_catalog_version_when_catalog_is_available():
    intents = [
        "predict churn using race",                              # prohibited
        "predict which netflix shows to watch for a customer",   # out of scope
        "credit risk score using zip code",                      # proxy
        "predict churn for customers",                           # ambiguous (scoped, no product/region)
        "build a credit risk score for customers",               # clear
        "predict which customers will prepay their mortgage",    # onboarding
    ]
    for text in intents:
        r = classify_intent(text, catalog=CAT)
        assert isinstance(r, IntakeClassification)
        assert r.catalog_version == "banking-cat@1"              # §4.5(c): version on EVERY outcome
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/featuregen/intake/test_classify_intent.py -v`
Expected: FAIL with `ImportError: cannot import name 'classify_intent'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/featuregen/intake/banking_catalog.py` (add `from enum import Enum` and `from collections.abc import Iterable` to the top imports):

```python
# extend top imports with:
#   from collections.abc import Iterable, Mapping
#   from enum import Enum


class IntakeOutcome(str, Enum):
    """The deterministic intake-classification outcomes (§4.5, §5.4). Exactly one is produced per
    intent (most-restrictive-wins). OUT_OF_SCOPE / PROHIBITED_DATA_CLASS / NEEDS_USE_CASE_ONBOARDING
    share their string values with FeatureContractStatus so the fold can map them directly."""

    OUT_OF_SCOPE = "OUT_OF_SCOPE"                             # terminal / park (banking boundary)
    PROHIBITED_DATA_CLASS = "PROHIBITED_DATA_CLASS"          # terminal block (blocked class)
    SENSITIVE_PROXY_CLARIFY = "SENSITIVE_PROXY_CLARIFY"      # non-terminal → clarification / review
    AMBIGUOUS_CLARIFY = "AMBIGUOUS_CLARIFY"                  # non-terminal → clarification
    NEEDS_USE_CASE_ONBOARDING = "NEEDS_USE_CASE_ONBOARDING"  # in-scope, unknown use-case → park
    CLEAR = "CLEAR"                                          # pass


@dataclass(frozen=True)
class IntakeClassification:
    """One deterministic classification outcome + its audit/MRM provenance. `catalog_version` is
    stamped on EVERY outcome incl. CLEAR (§4.5(c)); it is None only when the catalog was unavailable
    (the fail-closed case, §4.5(b))."""

    outcome: IntakeOutcome
    catalog_version: str | None
    reason: str
    matched_class: str | None = None
    matched_use_case: str | None = None

    @property
    def is_clear(self) -> bool:
        return self.outcome is IntakeOutcome.CLEAR

    @property
    def blocks(self) -> bool:
        return self.outcome in (IntakeOutcome.OUT_OF_SCOPE, IntakeOutcome.PROHIBITED_DATA_CLASS)

    @property
    def needs_clarification(self) -> bool:
        return self.outcome in (
            IntakeOutcome.SENSITIVE_PROXY_CLARIFY,
            IntakeOutcome.AMBIGUOUS_CLARIFY,
        )


def _first_match(text: str, terms: Iterable[str]) -> str | None:
    """First (deterministically ordered) term that occurs in the lowercased intent, or None."""
    for term in sorted(terms):
        if term and term in text:
            return term
    return None


def _match_use_case(text: str, catalog: BankingDomainCatalog) -> str | None:
    """The first (deterministically ordered) known use-case any of whose keyword terms occurs."""
    for use_case in sorted(catalog.use_case_terms):
        if any(term and term in text for term in catalog.use_case_terms[use_case]):
            return use_case
    return None


def classify_intent(
    intent: str,
    *,
    product: str | None = None,
    region: str | None = None,
    catalog: BankingDomainCatalog | None,
) -> IntakeClassification:
    """Deterministic intake banking-boundary classifier over the read-only BankingDomainCatalog
    (§4.5, §5.4) — NOT the LLM's call. TOTAL and fail-closed: it returns exactly one outcome for any
    input under most-restrictive-wins precedence (PROHIBITED_DATA_CLASS > OUT_OF_SCOPE >
    sensitive-proxy > ambiguous), and stamps the catalog `version` on every outcome incl. CLEAR
    (§4.5 a/c). Completeness rules: (b) an unavailable/unversioned catalog fails closed to
    AMBIGUOUS_CLARIFY (never CLEAR); (e) a scoped use-case missing product/region → AMBIGUOUS_CLARIFY."""
    # (b) fail-closed on an absent / unversioned catalog — never auto-pass.
    if catalog is None or not catalog.available:
        return IntakeClassification(
            IntakeOutcome.AMBIGUOUS_CLARIFY, None, "catalog_unavailable_fail_closed"
        )
    version = catalog.version
    text = f" {intent.lower()} "

    # 1. PROHIBITED_DATA_CLASS — most restrictive; dominates everything.
    hit = _first_match(text, catalog.blocked_terms)
    if hit is not None:
        return IntakeClassification(
            IntakeOutcome.PROHIBITED_DATA_CLASS, version,
            f"blocked data class matched: {hit}", matched_class=catalog.blocked_terms[hit],
        )

    use_case = _match_use_case(text, catalog)

    # 2. OUT_OF_SCOPE — explicit example term, an out-of-scope use-case, or no banking concept at all.
    oos_term = _first_match(text, catalog.out_of_scope_terms)
    if oos_term is not None:
        return IntakeClassification(
            IntakeOutcome.OUT_OF_SCOPE, version, f"out-of-scope example matched: {oos_term}"
        )
    if use_case is not None and use_case in catalog.out_of_scope_use_cases:
        return IntakeClassification(
            IntakeOutcome.OUT_OF_SCOPE, version, f"use-case out of scope: {use_case}",
            matched_use_case=use_case,
        )
    if _first_match(text, catalog.banking_terms) is None:
        return IntakeClassification(
            IntakeOutcome.OUT_OF_SCOPE, version, "no banking entity / data / concept"
        )

    # 3. SENSITIVE_PROXY_CLARIFY — a proxy hint is a doubt to review, never a standalone block.
    proxy = _first_match(text, catalog.sensitive_proxy_terms)
    if proxy is not None:
        return IntakeClassification(
            IntakeOutcome.SENSITIVE_PROXY_CLARIFY, version,
            f"sensitive-proxy hint matched: {proxy}",
        )

    # 4. AMBIGUOUS_CLARIFY — (e) a scoped use-case whose product/region context is missing.
    if use_case is not None and use_case in catalog.scoped_use_cases and (
        product is None or region is None
    ):
        return IntakeClassification(
            IntakeOutcome.AMBIGUOUS_CLARIFY, version,
            f"missing product/region for scoped use-case {use_case}", matched_use_case=use_case,
        )

    # 5. CLEAR (known use-case) / NEEDS_USE_CASE_ONBOARDING (in-scope banking, unknown use-case).
    if use_case is not None:
        return IntakeClassification(
            IntakeOutcome.CLEAR, version, f"in banking scope: {use_case}", matched_use_case=use_case
        )
    if _first_match(text, catalog.predictive_markers) is not None:
        return IntakeClassification(
            IntakeOutcome.NEEDS_USE_CASE_ONBOARDING, version, "in-scope banking, unknown use-case"
        )
    return IntakeClassification(IntakeOutcome.CLEAR, version, "in banking scope: feature definition")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/featuregen/intake/test_classify_intent.py -v`
Expected: PASS (11 passed)

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/intake/banking_catalog.py tests/featuregen/intake/test_classify_intent.py
git commit -m "feat(intake): deterministic classify_intent — total, fail-closed, most-restrictive-wins, version-stamped"
```

---

## Phase 2 exit criteria

- [ ] `uv run pytest tests/featuregen/intake -v` is green (all 7 test modules).
- [ ] `contract.py` exports: the closed-enum vocabularies (`OBSERVATION_INTENT_KINDS`, `METHOD_KINDS`, `SCORE_SOURCES`, `ROUTED_TO`) + `UNKNOWN`/`INTAKE_MODES`/`RAW_INPUT_CLASSIFICATIONS` re-exported from SP-0; the three authoritative content-schema constants (`DRAFT_CONTENT_SCHEMA`, `CONFIRMED_CONTRACT_JSON_SCHEMA`, `ASSUMPTION_LEDGER_CONTENT_SCHEMA`) with the tagged/versioned `calculation_method`; `validate_semantics(body, *, stage)`; `assemble_confirmed(...)` + `reshape_calculation_method(...)`; `register_contract_schemas(registry)` (CONFIRMED_CONTRACT@1 + additive Draft/Ledger re-affirm + the v1 upcaster seam).
- [ ] `state.py` exports `FeatureContractStatus` (the 6 lifecycle values), `FeatureContractState`, and **`fold_feature_contract_state(stream)`** with the no-regression guard — the authoritative fold every later command phase (P4–P8) gates on inline (mirroring `overlay/confirmation_commands.py`); it is **not** `state_machine/engine.py` and **not** `run_workflow_state`.
- [ ] `banking_catalog.py` exports the read-only `BankingDomainCatalog` reader (`from_seed` / `load_banking_catalog`) and the deterministic `classify_intent(...) -> IntakeClassification` + `IntakeOutcome` — total, fail-closed, precedence most-restrictive-wins, version stamped on every outcome.
- [ ] No events appended, no tasks opened, no authz seeded — those are P3–P8. This phase is pure content-model + fold + reference reader.

**Downstream consumers (do not drift these symbols):** P3 registers `CONFIRMED_CONTRACT`'s LLM output-schema against `register_contract_schemas`; P4 (`submit_intent`) uses `classify_intent` + `validate_semantics(stage="DRAFT_CONTRACT")` + emits the Draft/Ledger docs; P5 (`mcv.py`) folds `fold_feature_contract_state` and validates the Draft; P7 (`confirm_contract`) calls `assemble_confirmed` + `validate_semantics(stage="CONFIRMED_CONTRACT")`; P8 re-uses `fold_feature_contract_state` for the inline lifecycle guards + `get_contract` read model.
