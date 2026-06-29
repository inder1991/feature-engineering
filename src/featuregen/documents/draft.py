from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from featuregen.contracts import SchemaValidationError

RAW_INPUT_CLASSIFICATIONS: tuple[str, ...] = ("contains_pii", "clean", "unscanned")
INTAKE_MODES: tuple[str, ...] = ("hypothesis", "definition")
UNKNOWN = "UNKNOWN"  # §3.5 sentinel: an unresolved Draft field; MUST be in open_fields

DRAFT_CONTRACT_SCHEMA_VERSION = 1
ASSUMPTION_LEDGER_SCHEMA_VERSION = 1

_DRAFT_REQUIRED = (
    "request_id",
    "intake_mode",
    "raw_input_ref",
    "raw_input_classification",
    "open_fields",
    "assumption_ledger_ref",
    "status",
)

DRAFT_CONTRACT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": list(_DRAFT_REQUIRED),
    "not": {"required": ["raw_input"]},  # raw_input MUST NOT be inline (§9)
    "properties": {
        "request_id": {"type": "string"},
        "intake_mode": {"enum": list(INTAKE_MODES)},
        "raw_input_ref": {"type": "string"},
        "raw_input_classification": {"enum": list(RAW_INPUT_CLASSIFICATIONS)},
        "hypothesis": {"type": "string"},
        "target": {"type": "string"},
        "entity": {"type": "string"},
        "feature_concept": {"type": "string"},
        "source_concepts": {"type": "array", "items": {"type": "string"}},
        "candidate_calculations": {"type": "array", "items": {"type": "string"}},
        "open_fields": {"type": "array", "items": {"type": "string"}},
        "assumption_ledger_ref": {"type": "string"},
        "status": {"type": "string"},
    },
    "additionalProperties": True,
}

ASSUMPTION_LEDGER_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["request_id", "assumptions"],
    "properties": {
        "request_id": {"type": "string"},
        "assumptions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["field", "value", "rationale"],
                "properties": {
                    "field": {"type": "string"},
                    "value": {},
                    "rationale": {"type": "string"},
                },
            },
        },
    },
    "additionalProperties": True,
}


class DraftValidationError(SchemaValidationError):
    """Raised when a Draft body violates the normative §3.5 Draft schema."""


def validate_draft(body: Mapping[str, Any]) -> None:
    """SP-0 envelope + required-field validation for a Draft (§3.5). Semantic
    validation is SP-2's. raw_input is never inline (§9) — reference only."""
    if "raw_input" in body:
        raise DraftValidationError(
            "raw_input must never be inline; use raw_input_ref + classification (§9)"
        )
    missing = [k for k in _DRAFT_REQUIRED if k not in body]
    if missing:
        raise DraftValidationError(f"Draft missing required fields: {missing}")
    if body["raw_input_classification"] not in RAW_INPUT_CLASSIFICATIONS:
        raise DraftValidationError(
            f"invalid raw_input_classification: {body['raw_input_classification']!r}"
        )
    if body["intake_mode"] not in INTAKE_MODES:
        raise DraftValidationError(f"invalid intake_mode: {body['intake_mode']!r}")
    if not isinstance(body["open_fields"], list):
        raise DraftValidationError("open_fields must be a list")
    # §3.5: unresolved fields are UNKNOWN and listed in open_fields. Any field whose
    # value is the UNKNOWN sentinel must therefore appear in open_fields.
    open_fields = body["open_fields"]
    unknown_unlisted = [
        k for k, v in body.items() if isinstance(v, str) and v == UNKNOWN and k not in open_fields
    ]
    if unknown_unlisted:
        raise DraftValidationError(
            f"fields set to UNKNOWN must be listed in open_fields: {unknown_unlisted} (§3.5)"
        )
    if not body.get("assumption_ledger_ref"):
        raise DraftValidationError("assumption_ledger_ref is required")


def draft_has_open_fields(body: Mapping[str, Any]) -> bool:
    """True if the Draft still has unresolved fields (cannot pass Gate #1, §3.5)."""
    return bool(body.get("open_fields"))


def register_draft_schemas(registry) -> None:
    """Register DRAFT_CONTRACT + ASSUMPTION_LEDGER in the document registry (§3.7)."""
    registry.register_schema(
        "DRAFT_CONTRACT",
        DRAFT_CONTRACT_SCHEMA_VERSION,
        DRAFT_CONTRACT_JSON_SCHEMA,
        owner="featuregen",
    )
    registry.register_schema(
        "ASSUMPTION_LEDGER",
        ASSUMPTION_LEDGER_SCHEMA_VERSION,
        ASSUMPTION_LEDGER_JSON_SCHEMA,
        owner="featuregen",
    )
