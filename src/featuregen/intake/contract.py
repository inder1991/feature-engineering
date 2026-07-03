"""SP-2 Feature-Contract content-schema + assembly (§4.2, Decision 4).

DELIBERATELY MINIMAL (Decision 4 — "minimum-viable contract content-schema, not maximal"): the CONFIRMED
contract carries intent semantics at the fidelity intake can establish WITHOUT grounding — filter
predicates as raw strings, a closed-but-small calculation_method vocabulary (METHOD_KINDS), untyped ratio
components, and a mode-dependent target (None for a definition-mode feature with no target). It is an
AUDITED INTENT LOCK, NOT an executable artifact.

HARD DOWNSTREAM DEPENDENCY (N10): a CONFIRMED contract MUST NOT be mapped / compiled / executed until SP-3
grounding applies its normalization gate (typed predicates, resolved concepts, validated method + target).
SP-2's `validate_semantics` enforces ONLY the intake-level floor (§4.2) — it is NOT SP-3 grounding.
Invariants: "no confirmed contract → no execution" (SP-2) AND "no SP-3 normalization → no execution" (SP-3).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import jsonschema

# UNKNOWN + the mode / classification vocabularies are SP-0's — reused VERBATIM (overview Shared
# Contract). Any UNKNOWN-valued semantic field MUST be listed in open_fields (§4.0).
from featuregen.documents.draft import (
    INTAKE_MODES,
    RAW_INPUT_CLASSIFICATIONS,
    UNKNOWN,
    register_draft_schemas,
)

__all__ = [
    "INTAKE_MODES", "RAW_INPUT_CLASSIFICATIONS", "UNKNOWN",
    "OBSERVATION_INTENT_KINDS", "METHOD_KINDS", "SCORE_SOURCES", "ROUTED_TO",
    "DRAFT_STATUS", "CONFIRMED_STATUS",
    "DRAFT_CONTRACT_SCHEMA_VERSION", "ASSUMPTION_LEDGER_SCHEMA_VERSION",
    "CONFIRMED_CONTRACT_SCHEMA_VERSION",
    "DRAFT_CONTENT_SCHEMA", "CONFIRMED_CONTRACT_JSON_SCHEMA", "ASSUMPTION_LEDGER_CONTENT_SCHEMA",
    "ContractSemanticError", "validate_semantics",
    "reshape_calculation_method", "assemble_confirmed",
    "CONTRACT_SCHEMA_OWNER", "register_contract_schemas",
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
