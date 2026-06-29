from __future__ import annotations

from featuregen.events.registry import is_backward_compatible

V1 = {
    "type": "object",
    "required": ["a"],
    "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
}


def test_add_optional_field_is_compatible():
    v2 = {
        "type": "object",
        "required": ["a"],
        "properties": {
            "a": {"type": "string"},
            "b": {"type": "integer"},
            "c": {"type": "boolean"},
        },
    }
    assert is_backward_compatible(V1, v2) is True


def test_widen_type_is_compatible():
    v2 = {
        "type": "object",
        "required": ["a"],
        "properties": {"a": {"type": "string"}, "b": {"type": ["integer", "number"]}},
    }
    assert is_backward_compatible(V1, v2) is True


def test_add_enum_value_is_compatible():
    old = {"type": "object", "properties": {"e": {"enum": ["x"]}}}
    new = {"type": "object", "properties": {"e": {"enum": ["x", "y"]}}}
    assert is_backward_compatible(old, new) is True


def test_new_required_field_is_breaking():
    v2 = {
        "type": "object",
        "required": ["a", "c"],
        "properties": {
            "a": {"type": "string"},
            "b": {"type": "integer"},
            "c": {"type": "string"},
        },
    }
    assert is_backward_compatible(V1, v2) is False


def test_removed_property_is_breaking():
    v2 = {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}}
    assert is_backward_compatible(V1, v2) is False


def test_narrowed_type_is_breaking():
    old = {"type": "object", "properties": {"b": {"type": ["integer", "number"]}}}
    new = {"type": "object", "properties": {"b": {"type": "integer"}}}
    assert is_backward_compatible(old, new) is False


def test_removed_enum_value_is_breaking():
    old = {"type": "object", "properties": {"e": {"enum": ["x", "y"]}}}
    new = {"type": "object", "properties": {"e": {"enum": ["x"]}}}
    assert is_backward_compatible(old, new) is False


# ── assert_evolution_complete: §3.3 breaking-bump => mandatory upcaster (active enforcement)
import pytest  # noqa: E402

from featuregen.contracts import SchemaValidationError  # noqa: E402
from featuregen.events.registry import EventSchemaRegistry  # noqa: E402

_BREAKING_V2 = {
    "type": "object",
    "required": ["a", "c"],  # new required field 'c' breaks old writers
    "properties": {"a": {"type": "string"}, "c": {"type": "string"}},
}


def test_evolution_complete_passes_for_compatible_chain():
    reg = EventSchemaRegistry()
    reg.register_schema("T", 1, V1, owner="o")
    reg.register_schema(
        "T",
        2,
        {
            "type": "object",
            "required": ["a"],
            "properties": {
                "a": {"type": "string"},
                "b": {"type": "integer"},
                "d": {"type": "boolean"},
            },
        },  # add optional field => compatible
        owner="o",
    )
    reg.assert_evolution_complete()  # no upcaster needed; does not raise


def test_evolution_breaking_bump_without_upcaster_raises():
    reg = EventSchemaRegistry()
    reg.register_schema("T", 1, V1, owner="o")
    reg.register_schema("T", 2, _BREAKING_V2, owner="o")  # breaking, no 1->2 upcaster
    with pytest.raises(SchemaValidationError):
        reg.assert_evolution_complete()


def test_evolution_breaking_bump_with_upcaster_passes():
    reg = EventSchemaRegistry()
    reg.register_schema("T", 1, V1, owner="o")
    reg.register_schema("T", 2, _BREAKING_V2, owner="o")
    reg.register_upcaster("T", 1, 2, lambda b: {**b, "c": "backfilled"})
    reg.assert_evolution_complete()  # mandatory upcaster present => does not raise
