from __future__ import annotations

import hashlib
import json as _json
from collections.abc import Mapping
from typing import Any

import jsonschema
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from featuregen.contracts import SchemaValidationError, Upcaster
from featuregen.contracts.db import DbConn


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
            raise SchemaValidationError(f"{type_name}@v{schema_version}: {exc.message}") from exc

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
            raise SchemaValidationError(f"cannot downcast {type_name} {from_version}->{to_version}")
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

    def set_status(self, type_name: str, schema_version: int, status: str) -> None:
        key = (type_name, schema_version)
        if key not in self._status:
            raise SchemaValidationError(f"unknown schema {type_name}@v{schema_version}")
        if status not in ("active", "deprecated", "withdrawn"):
            raise SchemaValidationError(f"invalid status {status!r}")
        self._status[key] = status

    def assert_writable(self, type_name: str, schema_version: int) -> None:
        status = self._status.get((type_name, schema_version))
        if status is None:
            raise SchemaValidationError(f"unknown schema {type_name}@v{schema_version}")
        if status != "active":
            raise SchemaValidationError(
                f"{type_name}@v{schema_version} is {status}; no new writes allowed"
            )

    def assert_evolution_complete(self) -> None:
        """§3.3 load-time enforcement: a breaking schema bump REQUIRES a stepwise upcaster.
        For every type, each consecutive registered version pair that is not backward-compatible
        must have a registered upcaster for every step between them; otherwise raise
        SchemaValidationError (a load-time error, never a lazy read-time poison)."""
        by_type: dict[str, list[int]] = {}
        for type_name, version in self._schemas:
            by_type.setdefault(type_name, []).append(version)
        for type_name, versions in by_type.items():
            versions.sort()
            for prev, nxt in zip(versions, versions[1:]):
                if is_backward_compatible(
                    self._schemas[(type_name, prev)], self._schemas[(type_name, nxt)]
                ):
                    continue  # additive bump: no upcaster required
                for step in range(prev, nxt):
                    if (type_name, step) not in self._upcasters:
                        raise SchemaValidationError(
                            f"breaking schema bump {type_name} v{prev}->v{nxt} requires a "
                            f"stepwise upcaster {type_name} v{step}->v{step + 1}"
                        )

    def snapshot_version(self) -> str:
        """Content-addressed pinnable snapshot id over the {type_name: max_active_version} map.
        Identical registry states yield the SAME id; distinct states yield DISTINCT ids — so a
        provenance-pinned id resolves to exactly one {type: version} map (§3.3 determinism)."""
        canonical = _json.dumps(self.max_active_versions(), sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        return f"events@{digest}"

    def max_active_versions(self) -> dict[str, int]:
        """{type_name: highest active schema_version} for the snapshot contents."""
        out: dict[str, int] = {}
        for (type_name, version), status in self._status.items():
            if status == "active":
                out[type_name] = max(out.get(type_name, 0), version)
        return out

    def all_schemas(self) -> list[tuple[str, int, dict[str, Any], str, str]]:
        """(type_name, schema_version, json_schema, owner, status) for every registration."""
        return [
            (t, v, self._schemas[(t, v)], self._owners[(t, v)], self._status[(t, v)])
            for (t, v) in self._schemas
        ]


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


_REGISTRY: EventSchemaRegistry | None = None


def event_registry() -> EventSchemaRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = EventSchemaRegistry()
    return _REGISTRY


def reset_event_registry() -> None:
    global _REGISTRY
    _REGISTRY = EventSchemaRegistry()


def persist_event_schemas(conn: DbConn, registry: EventSchemaRegistry) -> None:
    """Durably record the in-memory schemas in event_type_registry (idempotent upsert).
    Enforces the §3.3 breaking-bump rule FIRST: assert_evolution_complete() raises before any
    write if a breaking schema bump lacks its mandatory upcaster."""
    registry.assert_evolution_complete()
    with conn.cursor() as cur:
        for type_name, version, json_schema, owner, status in registry.all_schemas():
            cur.execute(
                """
                INSERT INTO event_type_registry
                    (type_name, schema_version, json_schema, owner, status)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (type_name, schema_version)
                DO UPDATE SET json_schema = EXCLUDED.json_schema,
                              owner = EXCLUDED.owner,
                              status = EXCLUDED.status
                """,
                (type_name, version, Jsonb(json_schema), owner, status),
            )


def persist_registry_snapshot(conn: DbConn, registry: EventSchemaRegistry) -> str:
    """Write {type_name: max_active_version} under the content-addressed snapshot id; return it.
    Because the id is derived from the same contents, ON CONFLICT re-writes identical contents
    (no cross-state overwrite)."""
    snapshot_id = registry.snapshot_version()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO registry_snapshots (snapshot_id, registry, contents)
            VALUES (%s, 'events', %s)
            ON CONFLICT (snapshot_id)
            DO UPDATE SET contents = EXCLUDED.contents, captured_at = now()
            """,
            (snapshot_id, Jsonb(registry.max_active_versions())),
        )
    return snapshot_id


def load_registry_snapshot(conn: DbConn, snapshot_id: str) -> dict[str, int]:
    """Resolve a pinned snapshot id back to its {type_name: schema_version} map so a replay can
    drive upcast-on-read deterministically (§3.3/§8). Raises SchemaValidationError if unknown."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT contents FROM registry_snapshots WHERE snapshot_id = %s",
            (snapshot_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise SchemaValidationError(f"unknown registry snapshot {snapshot_id!r}")
    return {str(k): int(v) for k, v in row["contents"].items()}


def hydrate_event_registry(conn: DbConn) -> EventSchemaRegistry:
    """Reconstitute the process-global registry singleton's SCHEMAS from event_type_registry
    (resets then reloads), so a fresh process can validate/append without re-declaring every
    schema by hand. Upcasters are code: they are re-registered at import, not hydrated."""
    reset_event_registry()
    reg = event_registry()
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT type_name, schema_version, json_schema, owner, status "
            "FROM event_type_registry ORDER BY type_name, schema_version"
        )
        rows = cur.fetchall()
    for r in rows:
        reg.register_schema(
            r["type_name"],
            r["schema_version"],
            r["json_schema"],
            r["owner"],
            status=r["status"],
        )
    return reg
