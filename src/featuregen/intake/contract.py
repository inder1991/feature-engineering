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
