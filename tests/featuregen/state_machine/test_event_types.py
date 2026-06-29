from __future__ import annotations

import pytest

from featuregen.contracts import SchemaValidationError
from featuregen.events.registry import event_registry
from featuregen.state_machine.engine import GUARD_FAILED, TRANSITION_REJECTED
from featuregen.state_machine.event_types import (
    FEATURE_LIFECYCLE_VERSION_MIGRATED,
    WORKFLOW_VERSION_MIGRATED,
    register_state_machine_event_types,
)


class _RecordingRegistry:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str]] = []

    def register_schema(self, type_name, schema_version, json_schema, owner, *, status="active"):
        self.calls.append((type_name, schema_version, owner))


def test_registers_all_four_types_at_v1() -> None:
    rec = _RecordingRegistry()
    register_state_machine_event_types(rec)
    registered = {(t, v) for (t, v, _owner) in rec.calls}
    assert registered == {
        (GUARD_FAILED, 1),
        (TRANSITION_REJECTED, 1),
        (WORKFLOW_VERSION_MIGRATED, 1),
        (FEATURE_LIFECYCLE_VERSION_MIGRATED, 1),
    }
    assert all(owner == "featuregen-state-machine" for (_t, _v, owner) in rec.calls)


def test_guard_failed_schema_accepts_engine_payload() -> None:
    # Registered into the shared registry by the autouse conftest fixture (per test).
    event_registry().validate(
        GUARD_FAILED,
        1,
        {
            "from_state": "CONFIRMED_CONTRACT",
            "to_state": "MAPPING_REVIEW_FAILED",
            "trigger": "MAPPING_COMPLETED",
            "guard": {
                "guard_expr": "confirmed_contract_exists",
                "passed": False,
                "resolved_inputs": {"confirmed_contract_ref": "doc_1"},
                "per_predicate": {"confirmed_contract_exists": False},
            },
        },
    )


def test_guard_failed_schema_rejects_missing_guard() -> None:
    with pytest.raises(SchemaValidationError):
        event_registry().validate(
            GUARD_FAILED,
            1,
            {"from_state": "A", "to_state": "B", "trigger": "T"},
        )


def test_transition_rejected_schema_validates() -> None:
    event_registry().validate(
        TRANSITION_REJECTED,
        1,
        {"from_state": "A", "trigger": "T", "reason": "no_matching_transition"},
    )
    with pytest.raises(SchemaValidationError):
        event_registry().validate(TRANSITION_REJECTED, 1, {"from_state": "A"})


def test_migration_schema_validates() -> None:
    event_registry().validate(
        WORKFLOW_VERSION_MIGRATED,
        1,
        {"from_table_version": 1, "to_table_version": 2, "current_state": "DRAFT"},
    )
    with pytest.raises(SchemaValidationError):
        event_registry().validate(WORKFLOW_VERSION_MIGRATED, 1, {"to_table_version": 2})
