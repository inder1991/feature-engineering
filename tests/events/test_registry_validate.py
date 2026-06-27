from __future__ import annotations

import pytest

from sp0.contracts import SchemaValidationError
from sp0.events.registry import EventSchemaRegistry, event_registry, reset_event_registry

SCHEMA = {
    "type": "object",
    "required": ["confirmed_contract_ref"],
    "properties": {"confirmed_contract_ref": {"type": "string"}},
    "additionalProperties": True,
}


def test_validate_accepts_conforming_payload():
    reg = EventSchemaRegistry()
    reg.register_schema("CONTRACT_CONFIRMED", 1, SCHEMA, owner="sp2")
    reg.validate("CONTRACT_CONFIRMED", 1, {"confirmed_contract_ref": "doc_1"})


def test_validate_rejects_missing_required_field():
    reg = EventSchemaRegistry()
    reg.register_schema("CONTRACT_CONFIRMED", 1, SCHEMA, owner="sp2")
    with pytest.raises(SchemaValidationError):
        reg.validate("CONTRACT_CONFIRMED", 1, {"other": 1})


def test_validate_unknown_type_raises():
    reg = EventSchemaRegistry()
    with pytest.raises(SchemaValidationError):
        reg.validate("NOPE", 1, {})


def test_singleton_is_process_global_and_resettable():
    event_registry().register_schema("X", 1, {"type": "object"}, owner="o")
    assert event_registry() is event_registry()
    reset_event_registry()
    with pytest.raises(SchemaValidationError):
        event_registry().validate("X", 1, {})
