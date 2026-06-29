from __future__ import annotations

import pytest

from featuregen.contracts import SchemaValidationError
from featuregen.events.registry import (
    EventSchemaRegistry,
    event_registry,
    hydrate_event_registry,
    persist_event_schemas,
    reset_event_registry,
)


def test_hydrate_reconstitutes_schemas_from_db(conn):
    seed = EventSchemaRegistry()
    seed.register_schema(
        "RUN_STARTED",
        1,
        {"type": "object", "required": ["x"], "properties": {"x": {"type": "string"}}},
        owner="featuregen",
    )
    persist_event_schemas(conn, seed)

    # Simulate a fresh process: the in-memory singleton knows nothing yet.
    reset_event_registry()
    with pytest.raises(SchemaValidationError):
        event_registry().validate("RUN_STARTED", 1, {"x": "ok"})

    # Hydrate from the durable table -> validation works again, constraints intact.
    hydrate_event_registry(conn)
    event_registry().validate("RUN_STARTED", 1, {"x": "ok"})
    with pytest.raises(SchemaValidationError):
        event_registry().validate("RUN_STARTED", 1, {})  # missing required 'x'


def test_hydrate_preserves_status(conn):
    seed = EventSchemaRegistry()
    seed.register_schema("T", 1, {"type": "object"}, owner="o")
    seed.set_status("T", 1, "deprecated")
    persist_event_schemas(conn, seed)

    reset_event_registry()
    hydrate_event_registry(conn)
    with pytest.raises(SchemaValidationError):
        event_registry().assert_writable("T", 1)  # deprecated status survived the round-trip
