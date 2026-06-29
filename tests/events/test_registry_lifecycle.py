from __future__ import annotations

import pytest

from featuregen.contracts import SchemaValidationError
from featuregen.events.registry import EventSchemaRegistry


def _reg() -> EventSchemaRegistry:
    reg = EventSchemaRegistry()
    reg.register_schema("T", 1, {"type": "object"}, owner="o")
    reg.register_schema("T", 2, {"type": "object"}, owner="o")
    reg.register_upcaster("T", 1, 2, lambda b: {**b, "v2": True})
    return reg


def test_active_version_is_writable():
    _reg().assert_writable("T", 2)


def test_deprecated_version_blocks_new_writes():
    reg = _reg()
    reg.set_status("T", 1, "deprecated")
    with pytest.raises(SchemaValidationError):
        reg.assert_writable("T", 1)


def test_withdrawn_version_blocks_writes_but_stays_readable():
    reg = _reg()
    reg.set_status("T", 1, "withdrawn")
    with pytest.raises(SchemaValidationError):
        reg.assert_writable("T", 1)
    # in-flight v1 body still upcasts to v2
    assert reg.upcast("T", {"orig": 1}, 1, 2) == {"orig": 1, "v2": True}


def test_set_status_unknown_version_raises():
    with pytest.raises(SchemaValidationError):
        _reg().set_status("T", 99, "deprecated")


def test_assert_writable_unknown_version_raises():
    with pytest.raises(SchemaValidationError):
        _reg().assert_writable("T", 99)
