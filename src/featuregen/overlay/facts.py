from __future__ import annotations

from collections.abc import Mapping

import jsonschema


class FactValidationError(Exception):
    """Raised by `validate_fact_value` when a fact value fails its per-type schema or the use_case
    rule (overview pin 9). Overlay-owned — every phase imports THIS, never SP-0's
    `SchemaValidationError` (which is the event-registry's error)."""


# ---- fact types (§3.3) ----
AVAILABILITY_TIME = "availability_time"
GRAIN = "grain"
SCD_EFFECTIVE_DATING = "scd_effective_dating"
APPROVED_JOIN = "approved_join"
POLICY_TAG = "policy_tag"

DATA_FACT_TYPES = frozenset({AVAILABILITY_TIME, GRAIN, SCD_EFFECTIVE_DATING, APPROVED_JOIN})
POLICY_FACT_TYPES = frozenset({POLICY_TAG})

_CATALOG_OBJECT_REF_SCHEMA = {
    "type": "object",
    "required": ["catalog_source", "object_kind", "schema", "table"],
    "properties": {
        "catalog_source": {"type": "string"},
        "object_kind": {"type": "string"},
        "schema": {"type": "string"},
        "table": {"type": "string"},
        "column": {"type": ["string", "null"]},
    },
    "additionalProperties": False,
}

# ---- per-type value schemas (§3.3) ----
FACT_VALUE_SCHEMAS: dict[str, dict] = {
    AVAILABILITY_TIME: {
        "type": "object",
        "required": ["column", "basis"],
        "properties": {
            "column": {"type": "string"},
            "basis": {
                "type": "string",
                "enum": ["posted_at", "ingested_at", "event_time_plus_lag"],
            },
            "lag_hours": {"type": "number"},
        },
        "additionalProperties": False,
    },
    GRAIN: {
        "type": "object",
        "required": ["columns", "is_unique"],
        "properties": {
            "columns": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "is_unique": {"type": "boolean"},
        },
        "additionalProperties": False,
    },
    SCD_EFFECTIVE_DATING: {
        "type": "object",
        "required": ["valid_from", "valid_to"],
        "properties": {
            "valid_from": {"type": "string"},
            "valid_to": {"type": ["string", "null"]},
            "current_flag": {"type": "string"},
        },
        "additionalProperties": False,
    },
    APPROVED_JOIN: {
        # Reconciled on `column_pairs` (ordered list of {from_col,to_col}) plus the two structured
        # endpoint refs — NOT flat `from_columns`/`to_columns` (decision 7). This mirrors
        # `ApprovedJoinRef` so dependency extraction can index both tables and all paired columns
        # from the value alone, without ever parsing the display "from -> to" string.
        "type": "object",
        "required": ["from_ref", "to_ref", "column_pairs", "cardinality"],
        "properties": {
            "from_ref": _CATALOG_OBJECT_REF_SCHEMA,
            "to_ref": _CATALOG_OBJECT_REF_SCHEMA,
            "column_pairs": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["from_col", "to_col"],
                    "properties": {
                        "from_col": {"type": "string"},
                        "to_col": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            "cardinality": {"type": "string", "enum": ["1:1", "1:N", "N:1"]},
        },
        "additionalProperties": False,
    },
    POLICY_TAG: {
        "type": "object",
        "required": ["decision", "basis"],
        "properties": {
            "decision": {"type": "string", "enum": ["allow", "deny", "restricted"]},
            "sensitivity": {"type": "string"},
            "basis": {"type": "string"},
        },
        "additionalProperties": False,
    },
}


def validate_fact_value(fact_type: str, value: Mapping, use_case: str | None = None) -> None:
    """Validate a proposed/confirmed value against its per-type schema and enforce the use_case
    rule (§3.3): `use_case` REQUIRED for policy_tag, PROHIBITED for the four data facts. `use_case`
    is OPTIONAL on the signature (pin 9) — data-fact callers omit it. Raises FactValidationError on
    any violation (caller raises before append)."""
    schema = FACT_VALUE_SCHEMAS.get(fact_type)
    if schema is None:
        raise FactValidationError(f"unknown fact_type {fact_type!r}")
    if fact_type in POLICY_FACT_TYPES:
        if not use_case:
            raise FactValidationError(f"{fact_type} requires a use_case")
    elif use_case is not None:
        raise FactValidationError(f"{fact_type} prohibits a use_case")
    try:
        jsonschema.validate(instance=dict(value), schema=schema)
    except jsonschema.ValidationError as exc:
        raise FactValidationError(f"{fact_type} value invalid: {exc.message}") from exc


# ---- event types (§3.2; OVERLAY_ prefix per Shared Contract) ----
OVERLAY_FACT_PROPOSED = "OVERLAY_FACT_PROPOSED"
OVERLAY_FACT_PARTIALLY_CONFIRMED = "OVERLAY_FACT_PARTIALLY_CONFIRMED"
OVERLAY_FACT_CONFIRMED = "OVERLAY_FACT_CONFIRMED"
OVERLAY_FACT_REJECTED = "OVERLAY_FACT_REJECTED"
OVERLAY_FACT_EXPIRED = "OVERLAY_FACT_EXPIRED"
OVERLAY_FACT_STALED = "OVERLAY_FACT_STALED"

OVERLAY_EVENT_SCHEMA_VERSION = 1
OVERLAY_OWNER = "featuregen-overlay"


def _evt(properties: Mapping, required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


_STR = {"type": "string"}
_NSTR = {"type": ["string", "null"]}
_CONFIRMER = {
    "type": "object",
    "required": ["subject", "role"],
    "properties": {"subject": {"type": "string"}, "role": {"type": "string"}},
    "additionalProperties": False,
}

OVERLAY_EVENT_SCHEMAS: dict[str, dict] = {
    OVERLAY_FACT_PROPOSED: _evt(
        {
            "catalog_object_ref": {"type": "object"},
            "object_ref": _STR,
            "fact_type": _STR,
            "use_case": _NSTR,
            "proposed_value": {"type": "object"},
            "proposal_fingerprint": _STR,
            "evidence_ref": _NSTR,
            "proposed_by": _STR,  # the actor subject STRING (pin 11), not an object
        },
        [
            "catalog_object_ref",
            "object_ref",
            "fact_type",
            "proposed_value",
            "proposal_fingerprint",
            "proposed_by",
        ],
    ),
    OVERLAY_FACT_PARTIALLY_CONFIRMED: _evt(
        {"by_owner": _STR, "role": _STR, "draft_event_id": _STR},
        ["by_owner", "role", "draft_event_id"],
    ),
    OVERLAY_FACT_CONFIRMED: _evt(
        {
            "value": {"type": "object"},
            "confirmers": {"type": "array", "items": _CONFIRMER},
            "expires_at": _NSTR,
            "confirms_event_id": _STR,
        },
        ["value", "confirmers", "confirms_event_id"],
    ),
    OVERLAY_FACT_REJECTED: _evt(
        {
            "rejected_by": _STR,
            "reason": _NSTR,
            "target_event_id": _STR,
            "retired_fingerprint": _NSTR,
        },
        ["rejected_by", "target_event_id"],
    ),
    OVERLAY_FACT_EXPIRED: _evt(
        {"expires_confirmed_event_id": _STR},
        ["expires_confirmed_event_id"],
    ),
    OVERLAY_FACT_STALED: _evt(
        {"catalog_change_ref": _STR, "stales_confirmed_event_id": _STR},
        ["catalog_change_ref", "stales_confirmed_event_id"],
    ),
}


def register_overlay_event_types(registry) -> None:
    """Register the 6 OVERLAY_FACT_* event schemas (schema_version=1) so append_event validation
    passes (Global Constraint: every new event type MUST be registered before any append)."""
    for type_name, schema in OVERLAY_EVENT_SCHEMAS.items():
        registry.register_schema(
            type_name, OVERLAY_EVENT_SCHEMA_VERSION, schema, owner=OVERLAY_OWNER, status="active"
        )
