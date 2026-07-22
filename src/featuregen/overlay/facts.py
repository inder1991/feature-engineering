from __future__ import annotations

from collections.abc import Mapping

import jsonschema


class FactValidationError(Exception):
    """Raised by `validate_fact_value` when a fact value fails its per-type schema or the use_case
    rule. Overlay-owned — every overlay module imports THIS, never SP-0's
    `SchemaValidationError` (which is the event-registry's error)."""


# ---- fact types (§3.3) ----
AVAILABILITY_TIME = "availability_time"
GRAIN = "grain"
SCD_EFFECTIVE_DATING = "scd_effective_dating"
APPROVED_JOIN = "approved_join"
ENTITY_BRIDGE = "entity_bridge"
# Delivery E governed semantic fact types: a column IS a business entity / a measure's currency is
# that column. Human-confirmed, column-referent, single-source facts (Delivery E depends on these).
ENTITY_ASSIGNMENT = "entity_assignment"
CURRENCY_BINDING = "currency_binding"
POLICY_TAG = "policy_tag"

DATA_FACT_TYPES = frozenset(
    {
        AVAILABILITY_TIME,
        GRAIN,
        SCD_EFFECTIVE_DATING,
        APPROVED_JOIN,
        ENTITY_BRIDGE,
        ENTITY_ASSIGNMENT,
        CURRENCY_BINDING,
    }
)
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
        # basis "event_time_plus_lag" is undefined without a lag, so mandate lag_hours for it.
        "if": {"properties": {"basis": {"const": "event_time_plus_lag"}}},
        "then": {"required": ["lag_hours"]},
    },
    GRAIN: {
        "type": "object",
        "required": ["columns", "is_unique"],
        "properties": {
            "columns": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "uniqueItems": True,
            },
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
        # endpoint refs — NOT flat `from_columns`/`to_columns`. This mirrors
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
                "uniqueItems": True,
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
    ENTITY_BRIDGE: {
        # A cross-catalog identity bridge: the SAME entity via an identifier column in two DISTINCT
        # catalogs. Symmetric in (left_ref, right_ref); cross-catalog is enforced in the write gate.
        "type": "object",
        "required": ["entity_id", "left_ref", "right_ref"],
        "properties": {
            "entity_id": {"type": "string"},
            "left_ref": _CATALOG_OBJECT_REF_SCHEMA,
            "right_ref": _CATALOG_OBJECT_REF_SCHEMA,
        },
        "additionalProperties": False,
    },
    ENTITY_ASSIGNMENT: {
        # A column IS this business entity (Delivery E). `entity_id` must be a member of the closed
        # `known_entities()` vocabulary — enforced in the WRITE GATE (identity.join_write_error), NOT
        # here, so the governed vocabulary can never drift into a duplicated literal set in this
        # schema. NO target ref and NO free value beyond `entity_id` (additionalProperties False);
        # `use_case` is PROHIBITED (a data fact — validate_fact_value enforces the use_case rule).
        "type": "object",
        "required": ["entity_id"],
        "properties": {"entity_id": {"type": "string", "minLength": 1}},
        "additionalProperties": False,
    },
    CURRENCY_BINDING: {
        # This measure's currency is that column (Delivery E). value = {currency_column:
        # CatalogObjectRef}. The target currency column must live in the SAME source/schema/table as
        # the subject measure and reference a concrete column — enforced in the WRITE GATE. NO free
        # value (additionalProperties False); `use_case` is PROHIBITED (a data fact).
        "type": "object",
        "required": ["currency_column"],
        "properties": {"currency_column": _CATALOG_OBJECT_REF_SCHEMA},
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
    is OPTIONAL on the signature — data-fact callers omit it. Raises FactValidationError on
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

# ---- authority basis (#10 honest authority attribution) ----
# A first-class basis for a CONFIRMED fact's authority, DISTINCT from a human confirmer entry:
# `source_declared` means the fact is authoritative because the ingested source declared it (the
# upload catalog treats the source as the system of record) — NOT because a human owner vouched.
# Written ONLY by the auto-confirm paths (upload / connector sync / quarantine resolution); the
# genuine human confirm paths keep writing real `confirmers`.
AUTHORITY_SOURCE_DECLARED = "source_declared"
# READ-side label only (OverlayState.authority_provenance) for pre-#10 events, which used the same
# confirmer shape for genuine human confirms AND upload auto-confirms with no discriminator. Such
# events are NEVER retroactively reclassified — and this value is never written to an event.
AUTHORITY_LEGACY_UNSPECIFIED = "legacy_unspecified"
# Where a source-declared fact entered the system.
ORIGIN_TYPES = ("upload", "connector", "resolution")


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

def _confirmed_schema() -> dict:
    """OVERLAY_FACT_CONFIRMED payload schema. Extended ADDITIVELY for #10 under schema_version 1
    (the established pattern for optional fields here — see `note`/`category`): `confirmers` is no
    longer unconditionally required; instead the `oneOf` authority rule demands EITHER real human
    `confirmers` OR the complete source-declared triple (`authority_basis` + `origin_type` +
    `role_claims`) — never both, never neither. Every pre-#10 event (confirmers, no
    authority_basis) satisfies the first arm unchanged, so existing events still validate and
    replay without an upcaster."""
    schema = _evt(
        {
            "value": {"type": "object"},
            "confirmers": {"type": "array", "items": _CONFIRMER},
            # #10 source-declared authority (all three or none — enforced by the oneOf below):
            "authority_basis": {"type": "string", "enum": [AUTHORITY_SOURCE_DECLARED]},
            "origin_type": {"type": "string", "enum": list(ORIGIN_TYPES)},
            "role_claims": {"type": "array", "items": {"type": "string"}},
            "expires_at": _NSTR,
            "confirms_event_id": _STR,
            "note": _NSTR,  # optional approver note (confirmation surface); absent pre-feature
        },
        ["value", "confirms_event_id"],
    )
    schema["oneOf"] = [
        # human confirmation (and every legacy pre-#10 event): confirmers, no authority basis
        {"required": ["confirmers"], "not": {"required": ["authority_basis"]}},
        # source-declared: the complete honest-attribution triple, and NO confirmer entry
        {"required": ["authority_basis", "origin_type", "role_claims"],
         "not": {"required": ["confirmers"]}},
    ]
    return schema


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
            "proposed_by": _STR,  # the actor subject STRING, not an object
            # `source_uploader` (optional; additive like `note`/`category`): the uploading HUMAN
            # principal behind an ingest-authored SERVICE proposal (semantic bindings / Pass B
            # grain-availability). Confirm-side four-eyes bars this subject from confirming a
            # value their own upload declared (program-audit F2/F10). NOT required — pre-existing
            # PROPOSED events lack the key entirely.
            "source_uploader": _STR,
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
        # `note` (optional, nullable): the confirmer's free-text note for the OTHER approver
        # (confirmation surface). NOT required — pre-existing events lack the key entirely.
        {"by_owner": _STR, "role": _STR, "draft_event_id": _STR, "note": _NSTR},
        ["by_owner", "role", "draft_event_id"],
    ),
    OVERLAY_FACT_CONFIRMED: _confirmed_schema(),
    OVERLAY_FACT_REJECTED: _evt(
        {
            "rejected_by": _STR,
            "reason": _NSTR,
            # `category` (optional, nullable): first-class structured reject category (Task 5
            # review) — a reliable analytics key, unlike the polymorphic free-text `reason`.
            # NOT required — pre-existing REJECTED events lack the key entirely.
            "category": _NSTR,
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
