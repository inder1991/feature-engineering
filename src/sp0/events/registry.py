from __future__ import annotations

from typing import Any, Mapping, Optional

import jsonschema

from sp0.contracts import SchemaValidationError, Upcaster


class EventSchemaRegistry:
    """Event-type registry (§3.3): versioned JSON schemas, stepwise upcasters,
    deprecate/withdraw lifecycle, pinnable snapshot id."""

    def __init__(self) -> None:
        self._schemas: dict[tuple[str, int], dict[str, Any]] = {}
        self._owners: dict[tuple[str, int], str] = {}
        self._status: dict[tuple[str, int], str] = {}
        self._upcasters: dict[tuple[str, int], Upcaster] = {}

    def register_schema(
        self,
        type_name: str,
        schema_version: int,
        json_schema: Mapping[str, Any],
        owner: str,
        *,
        status: str = "active",
    ) -> None:
        key = (type_name, schema_version)
        self._schemas[key] = dict(json_schema)
        self._owners[key] = owner
        self._status[key] = status

    def validate(self, type_name: str, schema_version: int, body: Mapping[str, Any]) -> None:
        key = (type_name, schema_version)
        schema = self._schemas.get(key)
        if schema is None:
            raise SchemaValidationError(f"no schema registered for {type_name}@v{schema_version}")
        try:
            jsonschema.validate(instance=dict(body), schema=schema)
        except jsonschema.ValidationError as exc:
            raise SchemaValidationError(
                f"{type_name}@v{schema_version}: {exc.message}"
            ) from exc


_REGISTRY: Optional[EventSchemaRegistry] = None


def event_registry() -> EventSchemaRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = EventSchemaRegistry()
    return _REGISTRY


def reset_event_registry() -> None:
    global _REGISTRY
    _REGISTRY = EventSchemaRegistry()
