from __future__ import annotations

import pytest

from featuregen.contracts import SchemaValidationError
from featuregen.events.registry import EventSchemaRegistry


def _reg() -> EventSchemaRegistry:
    reg = EventSchemaRegistry()
    reg.register_upcaster("T", 1, 2, lambda b: {**b, "added_v2": True})
    reg.register_upcaster("T", 2, 3, lambda b: {**b, "added_v3": 1})
    return reg


def test_upcast_chains_stepwise():
    out = _reg().upcast("T", {"orig": 1}, 1, 3)
    assert out == {"orig": 1, "added_v2": True, "added_v3": 1}


def test_upcast_noop_when_versions_equal():
    out = _reg().upcast("T", {"orig": 1}, 3, 3)
    assert out == {"orig": 1}


def test_upcast_missing_step_is_poison_error():
    reg = EventSchemaRegistry()
    reg.register_upcaster("T", 1, 2, lambda b: b)
    # no 2->3 registered
    with pytest.raises(SchemaValidationError):
        reg.upcast("T", {"x": 1}, 1, 3)


def test_register_upcaster_must_be_stepwise():
    reg = EventSchemaRegistry()
    with pytest.raises(ValueError):
        reg.register_upcaster("T", 1, 3, lambda b: b)


def test_upcast_cannot_downcast():
    with pytest.raises(SchemaValidationError):
        _reg().upcast("T", {"x": 1}, 3, 1)
