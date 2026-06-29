from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import jsonschema
from psycopg.types.json import Jsonb

from featuregen.contracts import DbConn, SchemaValidationError, Upcaster


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
                raise SchemaValidationError(f"missing upcaster {type_name} v{v}->v{v + 1} (poison)")
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
            raise SchemaValidationError(f"{type_name}@v{schema_version}: {exc.message}") from exc

    def assert_writable(self, type_name: str, schema_version: int) -> None:
        """Block NEW writes at a non-active version (§3.3): `deprecated` => no new
        writes; `withdrawn` => upcast-only. Deprecated/withdrawn versions stay
        READABLE via validate()/upcast() for in-flight docs. Producers call this
        before writing a new document body at (type_name, schema_version)."""
        row = self._conn.execute(
            "SELECT status FROM document_type_registry WHERE type_name=%s AND schema_version=%s",
            (type_name, schema_version),
        ).fetchone()
        if row is None:
            raise SchemaValidationError(f"unregistered type {type_name}@v{schema_version}")
        if row[0] != "active":
            raise SchemaValidationError(
                f"{type_name}@v{schema_version} is {row[0]}: no new writes "
                f"(deprecated => no new writes; withdrawn => upcast-only) (§3.3)"
            )

    def snapshot_version(self) -> str:
        """Pinnable doc-registry snapshot id ('docs@vN') recorded in provenance for
        replay determinism (§3.3/§8). `contents` is exactly {type_name:
        max_active_version} (matches the shared-contract DDL — no extra keys).
        Idempotent: an unchanged active set returns the existing snapshot id."""
        contents = self._active_max_versions()
        existing = self._conn.execute(
            "SELECT snapshot_id FROM registry_snapshots WHERE registry='docs' AND contents = %s",
            (Jsonb(contents),),
        ).fetchone()
        if existing:
            return existing[0]
        n = (
            self._conn.execute(
                "SELECT count(*) FROM registry_snapshots WHERE registry='docs'"
            ).fetchone()[0]
            + 1
        )
        snapshot_id = f"docs@v{n}"
        self._conn.execute(
            "INSERT INTO registry_snapshots (snapshot_id, registry, contents) "
            "VALUES (%s, 'docs', %s)",
            (snapshot_id, Jsonb(contents)),
        )
        return snapshot_id

    def _active_max_versions(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT type_name, max(schema_version) FROM document_type_registry "
            "WHERE status='active' GROUP BY type_name"
        ).fetchall()
        return {name: ver for name, ver in rows}

    def _load_schema(self, type_name: str, schema_version: int) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT json_schema FROM document_type_registry "
            "WHERE type_name=%s AND schema_version=%s",
            (type_name, schema_version),
        ).fetchone()
        if row is None:
            raise SchemaValidationError(f"unregistered type {type_name}@v{schema_version}")
        return row[0]
