from __future__ import annotations

from typing import Any, Mapping

import jsonschema
from psycopg.types.json import Jsonb

from sp0.contracts import DbConn, SchemaValidationError, Upcaster


class DocumentSchemaRegistry:
    """Document/artifact SchemaRegistry over document_type_registry (§3.7).

    Construct per-connection: DocumentSchemaRegistry(conn). This cycle (6.1)
    ships register_schema + validate + the private _load_schema only. Chained
    reader-upcasters are added in cycle 6.2; snapshot_version + the deprecation
    lifecycle (assert_writable, _active_max_versions) are added in cycle 6.3."""

    def __init__(self, conn: DbConn) -> None:
        self._conn = conn
        self._upcasters: dict[tuple[str, int, int], Upcaster] = {}

    def register_schema(
        self,
        type_name: str,
        schema_version: int,
        json_schema: Mapping[str, Any],
        owner: str,
        *,
        status: str = "active",
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO document_type_registry
                (type_name, schema_version, json_schema, owner, status)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (type_name, schema_version) DO UPDATE
                SET json_schema = EXCLUDED.json_schema,
                    owner = EXCLUDED.owner,
                    status = EXCLUDED.status
            """,
            (type_name, schema_version, Jsonb(dict(json_schema)), owner, status),
        )

    def register_upcaster(
        self, type_name: str, from_version: int, to_version: int, upcaster: Upcaster
    ) -> None:
        if to_version != from_version + 1:
            raise ValueError("upcasters must be stepwise: to_version == from_version + 1")
        self._upcasters[(type_name, from_version, to_version)] = upcaster

    def upcast(
        self, type_name: str, body: Mapping[str, Any], from_version: int, to_version: int
    ) -> Mapping[str, Any]:
        if to_version < from_version:
            raise ValueError("cannot downcast")
        current: Mapping[str, Any] = dict(body)
        for v in range(from_version, to_version):
            step = self._upcasters.get((type_name, v, v + 1))
            if step is None:
                raise SchemaValidationError(
                    f"missing upcaster {type_name} v{v}->v{v + 1} (poison)"
                )
            current = dict(step(current))
        return current

    def validate(self, type_name: str, schema_version: int, body: Mapping[str, Any]) -> None:
        """Validate body against the registered schema. STATUS-AGNOSTIC by design:
        deprecated/withdrawn versions stay READABLE for in-flight docs (§3.3); the
        "no new writes" rule is enforced separately by assert_writable (cycle 6.3)."""
        schema = self._load_schema(type_name, schema_version)
        try:
            jsonschema.validate(instance=dict(body), schema=schema)
        except jsonschema.ValidationError as exc:
            raise SchemaValidationError(
                f"{type_name}@v{schema_version}: {exc.message}"
            ) from exc

    def _load_schema(self, type_name: str, schema_version: int) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT json_schema FROM document_type_registry "
            "WHERE type_name=%s AND schema_version=%s",
            (type_name, schema_version),
        ).fetchone()
        if row is None:
            raise SchemaValidationError(
                f"unregistered type {type_name}@v{schema_version}"
            )
        return row[0]
