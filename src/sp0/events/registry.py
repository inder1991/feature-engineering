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

    def assert_evolution_complete(self) -> None:
        """§3.3 load-time enforcement: a breaking schema bump REQUIRES a stepwise upcaster.
        For every type, each consecutive registered version pair that is not backward-compatible
        must have a registered upcaster for every step between them; otherwise raise
        SchemaValidationError (a load-time error, never a lazy read-time poison)."""
        by_type: dict[str, list[int]] = {}
        for (type_name, version) in self._schemas:
            by_type.setdefault(type_name, []).append(version)
        for type_name, versions in by_type.items():
            versions.sort()
            for prev, nxt in zip(versions, versions[1:]):
                if is_backward_compatible(self._schemas[(type_name, prev)],
                                          self._schemas[(type_name, nxt)]):
                    continue  # additive bump: no upcaster required
                for step in range(prev, nxt):
                    if (type_name, step) not in self._upcasters:
                        raise SchemaValidationError(
                            f"breaking schema bump {type_name} v{prev}->v{nxt} requires a "
                            f"stepwise upcaster {type_name} v{step}->v{step + 1}"
                        )


def _types_of(spec: Mapping[str, Any]) -> set[str]:
    t = spec.get("type")
    if t is None:
        return set()
    return set(t) if isinstance(t, list) else {t}


def _type_compatible(old_spec: Mapping[str, Any], new_spec: Mapping[str, Any]) -> bool:
    old_types = _types_of(old_spec)
    new_types = _types_of(new_spec)
    if not old_types or not new_types:
        return True  # unconstrained on either side: not a narrowing we track
    return old_types <= new_types  # widening (superset) is compatible


def _enum_compatible(old_spec: Mapping[str, Any], new_spec: Mapping[str, Any]) -> bool:
    old_enum = old_spec.get("enum")
    new_enum = new_spec.get("enum")
    if old_enum is None and new_enum is None:
        return True
    if old_enum is None and new_enum is not None:
        return False  # adding an enum constraint narrows
    if old_enum is not None and new_enum is None:
        return True  # dropping the enum constraint widens
    return set(old_enum) <= set(new_enum)  # adding values is compatible


def is_backward_compatible(old_schema: Mapping[str, Any], new_schema: Mapping[str, Any]) -> bool:
    """§3.3 backward-compat rule: compatible iff the new schema only adds optional
    fields, widens types, or adds enum values; anything else is breaking."""
    old_props: Mapping[str, Any] = old_schema.get("properties", {})
    new_props: Mapping[str, Any] = new_schema.get("properties", {})
    old_required = set(old_schema.get("required", []))
    new_required = set(new_schema.get("required", []))

    if new_required - old_required:
        return False  # a newly-required field breaks old writers
    if set(old_props) - set(new_props):
        return False  # removing a known property breaks old readers
    for name, old_spec in old_props.items():
        new_spec = new_props[name]
        if not _type_compatible(old_spec, new_spec):
            return False
        if not _enum_compatible(old_spec, new_spec):
            return False
    return True


_REGISTRY: Optional[EventSchemaRegistry] = None


def event_registry() -> EventSchemaRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = EventSchemaRegistry()
    return _REGISTRY


def reset_event_registry() -> None:
    global _REGISTRY
    _REGISTRY = EventSchemaRegistry()
