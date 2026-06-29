from __future__ import annotations

from typing import Any

from featuregen.state_machine.engine import GUARD_FAILED, TRANSITION_REJECTED

WORKFLOW_VERSION_MIGRATED = "WORKFLOW_VERSION_MIGRATED"
FEATURE_LIFECYCLE_VERSION_MIGRATED = "FEATURE_LIFECYCLE_VERSION_MIGRATED"

_OWNER = "featuregen-state-machine"

_GUARD_FAILED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["from_state", "to_state", "trigger", "guard"],
    "properties": {
        "from_state": {"type": "string"},
        "to_state": {"type": "string"},
        "trigger": {"type": "string"},
        "guard": {
            "type": "object",
            "required": ["guard_expr", "passed", "resolved_inputs", "per_predicate"],
            "properties": {
                "guard_expr": {"type": "string"},
                "passed": {"type": "boolean"},
                "resolved_inputs": {"type": "object"},
                "per_predicate": {"type": "object"},
            },
        },
    },
}

_TRANSITION_REJECTED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["from_state", "trigger", "reason"],
    "properties": {
        "from_state": {"type": "string"},
        "trigger": {"type": "string"},
        "reason": {"type": "string"},
    },
}

_MIGRATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["from_table_version", "to_table_version", "current_state"],
    "properties": {
        "from_table_version": {"type": "integer"},
        "to_table_version": {"type": "integer"},
        "current_state": {"type": "string"},
    },
}


def register_state_machine_event_types(registry: Any) -> None:
    """Register the four Phase-03 event types (version 1) into an event registry."""
    registry.register_schema(GUARD_FAILED, 1, _GUARD_FAILED_SCHEMA, _OWNER)
    registry.register_schema(TRANSITION_REJECTED, 1, _TRANSITION_REJECTED_SCHEMA, _OWNER)
    registry.register_schema(WORKFLOW_VERSION_MIGRATED, 1, _MIGRATION_SCHEMA, _OWNER)
    registry.register_schema(FEATURE_LIFECYCLE_VERSION_MIGRATED, 1, _MIGRATION_SCHEMA, _OWNER)
