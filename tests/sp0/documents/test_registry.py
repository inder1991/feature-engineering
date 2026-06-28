from __future__ import annotations

import pytest

from sp0.contracts import SchemaValidationError
from sp0.documents.registry import DocumentSchemaRegistry

_SCHEMA = {
    "type": "object",
    "required": ["x"],
    "properties": {"x": {"type": "integer"}},
    "additionalProperties": False,
}


def test_validate_accepts_conforming_body(db):
    reg = DocumentSchemaRegistry(db)
    reg.register_schema("FEATURE_PLAN", 1, _SCHEMA, owner="sp0")
    reg.validate("FEATURE_PLAN", 1, {"x": 7})  # no raise


def test_validate_rejects_nonconforming_body(db):
    reg = DocumentSchemaRegistry(db)
    reg.register_schema("FEATURE_PLAN", 1, _SCHEMA, owner="sp0")
    with pytest.raises(SchemaValidationError):
        reg.validate("FEATURE_PLAN", 1, {"x": "not-an-int"})


def test_validate_unregistered_type_raises(db):
    reg = DocumentSchemaRegistry(db)
    with pytest.raises(SchemaValidationError):
        reg.validate("FEATURE_PLAN", 99, {"x": 1})


def test_upcast_chains_stepwise(db):
    reg = DocumentSchemaRegistry(db)
    reg.register_upcaster("DQ_REPORT", 1, 2, lambda b: {**b, "v2": True})
    reg.register_upcaster("DQ_REPORT", 2, 3, lambda b: {**b, "v3": True})
    out = reg.upcast("DQ_REPORT", {"v1": True}, 1, 3)
    assert out == {"v1": True, "v2": True, "v3": True}


def test_upcast_missing_step_is_poison(db):
    reg = DocumentSchemaRegistry(db)
    reg.register_upcaster("DQ_REPORT", 1, 2, lambda b: {**b, "v2": True})
    with pytest.raises(SchemaValidationError):
        reg.upcast("DQ_REPORT", {"v1": True}, 1, 3)


def test_upcaster_must_be_stepwise():
    reg = DocumentSchemaRegistry.__new__(DocumentSchemaRegistry)
    reg._upcasters = {}
    with pytest.raises(ValueError):
        reg.register_upcaster("DQ_REPORT", 1, 3, lambda b: b)
