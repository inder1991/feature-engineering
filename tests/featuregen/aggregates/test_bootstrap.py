from __future__ import annotations

from featuregen.aggregates.bootstrap import register_phase06_event_schemas
from featuregen.aggregates.events import EVENT_SCHEMAS
from featuregen.events.registry import event_registry


def _valid_value(prop: dict):
    """A schema-valid value for one tightened property (enum/typed)."""
    enum = prop.get("enum")
    if enum:
        return enum[0]
    declared = prop.get("type")
    types = declared if isinstance(declared, list) else [declared]
    if "integer" in types:
        return 1
    if "array" in types:
        return []
    if "object" in types:
        return {}
    return "x"


def test_bootstrap_registers_every_type_into_real_event_registry():
    register_phase06_event_schemas()
    # A valid sample for every Phase-06 type validates against the REAL registry the runtime
    # `append_event` uses — proving the production path registered each schema (an unregistered
    # type would raise here, not validate). Samples respect each schema's enum/type constraints.
    for type_name, schema in EVENT_SCHEMAS.items():
        props = schema.get("properties", {})
        sample = {key: _valid_value(props.get(key, {})) for key in schema.get("required", [])}
        event_registry().validate(type_name, 1, sample)  # no SchemaValidationError


def test_bootstrap_is_idempotent():
    # calling twice must not raise (e.g. duplicate-registration error)
    register_phase06_event_schemas()
    register_phase06_event_schemas()
