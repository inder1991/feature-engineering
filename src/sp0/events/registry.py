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

    def register_upcaster(
        self,
        type_name: str,
        from_version: int,
        to_version: int,
        upcaster: Upcaster,
    ) -> None:
        if to_version != from_version + 1:
            raise ValueError(
                f"upcaster must be stepwise vN->vN+1, got {from_version}->{to_version}"
            )
        self._upcasters[(type_name, from_version)] = upcaster

    def upcast(
        self,
        type_name: str,
        body: Mapping[str, Any],
        from_version: int,
        to_version: int,
    ) -> Mapping[str, Any]:
        if to_version < from_version:
            raise SchemaValidationError(
                f"cannot downcast {type_name} {from_version}->{to_version}"
            )
        current: dict[str, Any] = dict(body)
        version = from_version
        while version < to_version:
            step = self._upcasters.get((type_name, version))
            if step is None:
                raise SchemaValidationError(
                    f"missing upcaster {type_name} {version}->{version + 1}"
                )
            current = dict(step(current))
            version += 1
        return current


_REGISTRY: Optional[EventSchemaRegistry] = None


def event_registry() -> EventSchemaRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = EventSchemaRegistry()
    return _REGISTRY


def reset_event_registry() -> None:
    global _REGISTRY
    _REGISTRY = EventSchemaRegistry()
