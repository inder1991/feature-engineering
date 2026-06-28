from __future__ import annotations

from sp0.aggregates.bootstrap import register_phase06_event_schemas
from sp0.aggregates.events import EVENT_SCHEMAS
from sp0.events.registry import event_registry


def test_bootstrap_registers_every_type_into_real_event_registry():
    register_phase06_event_schemas()
    # A valid sample for every Phase-06 type validates against the REAL registry the runtime
    # `append_event` uses — proving the production path registered each schema (an unregistered
    # type would raise here, not validate).
    for type_name, schema in EVENT_SCHEMAS.items():
        sample = {key: "x" for key in schema.get("required", [])}
        event_registry().validate(type_name, 1, sample)  # no SchemaValidationError


def test_bootstrap_is_idempotent():
    # calling twice must not raise (e.g. duplicate-registration error)
    register_phase06_event_schemas()
    register_phase06_event_schemas()
