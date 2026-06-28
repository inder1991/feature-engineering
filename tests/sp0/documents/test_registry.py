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
