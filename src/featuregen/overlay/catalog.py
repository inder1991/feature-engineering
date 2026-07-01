"""Catalog adapter: structural metadata + per-fact authority boundary (SP-1 design §4).

The catalog supplies object existence/columns/types/native-oid via ``list_objects`` and
``fingerprint`` (structural facts, NOT overlay fact types), and answers ML-fact queries via
``get_fact`` only when it genuinely records that fact authoritatively. Authoritativeness is a
property of each returned ``CatalogFact`` (per object/fact/use_case), not a global set.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from psycopg.rows import dict_row

from featuregen.contracts.db import DbConn
from featuregen.overlay.identity import CatalogObjectRef, display_object_ref


@dataclass(frozen=True, slots=True)
class CatalogObject:
    """A structural catalog object (table or column). Not an ML fact."""

    object_ref: str
    object_kind: str
    schema: str
    table: str
    column: str | None
    data_type: str | None
    native_oid: str | None


@dataclass(frozen=True, slots=True)
class CatalogFact:
    """An ML fact the catalog records, with its per-fact authority flag."""

    value: object
    authoritative: bool


@runtime_checkable
class CatalogAdapter(Protocol):
    def list_objects(self) -> Iterable[CatalogObject]: ...

    def get_fact(
        self, ref: CatalogObjectRef, fact_type: str, use_case: str | None = None
    ) -> CatalogFact | None: ...

    def owner_of(self, ref: CatalogObjectRef) -> str | None: ...

    def fingerprint(self) -> Mapping[str, CatalogObject]: ...


class FixtureCatalog:
    """In-memory ``CatalogAdapter`` test double.

    Facts and owners are keyed by ``display_object_ref(ref)`` so that lookups use the exact same
    normalized identity the real adapters expose via ``CatalogObject.object_ref``.
    """

    def __init__(self, catalog_source: str = "fixture") -> None:
        self._catalog_source = catalog_source
        self._objects: dict[str, CatalogObject] = {}
        self._facts: dict[tuple[str, str, str | None], CatalogFact] = {}
        self._owners: dict[str, str] = {}

    def add_object(self, obj: CatalogObject) -> None:
        self._objects[obj.object_ref] = obj

    def set_fact(
        self,
        ref: CatalogObjectRef,
        fact_type: str,
        value: object,
        *,
        authoritative: bool,
        use_case: str | None = None,
    ) -> None:
        key = (display_object_ref(ref), fact_type, use_case)
        self._facts[key] = CatalogFact(value=value, authoritative=authoritative)

    def set_owner(self, ref: CatalogObjectRef, owner: str) -> None:
        self._owners[display_object_ref(ref)] = owner

    def list_objects(self) -> Iterable[CatalogObject]:
        return tuple(self._objects.values())

    def get_fact(
        self, ref: CatalogObjectRef, fact_type: str, use_case: str | None = None
    ) -> CatalogFact | None:
        return self._facts.get((display_object_ref(ref), fact_type, use_case))

    def owner_of(self, ref: CatalogObjectRef) -> str | None:
        return self._owners.get(display_object_ref(ref))

    def fingerprint(self) -> Mapping[str, CatalogObject]:
        return dict(self._objects)


class PostgresCatalog:
    """Reference ``CatalogAdapter`` over a live PostgreSQL connection.

    Structural metadata only: existence/columns/types from ``information_schema`` and stable
    native object ids from ``pg_catalog`` (for rename detection, design §8) —
    a table's ``native_oid`` is its ``pg_class.oid``; a column's ``native_oid`` is the composite
    ``"<table_oid>:<attnum>"`` (``pg_attribute.attnum`` is not reused on rename, so column
    identity survives renames). It records NONE of the five ML fact types, so ``get_fact``
    returns ``None`` for all of them and the overlay owns those facts. ``owner_of`` returns
    ``None`` (ownership is not recorded here).
    """

    def __init__(
        self,
        conn: DbConn,
        *,
        catalog_source: str = "pg:core",
        schemas: tuple[str, ...] = ("public",),
    ) -> None:
        self._conn = conn
        self._catalog_source = catalog_source
        self._schemas = schemas

    def list_objects(self) -> Iterable[CatalogObject]:
        schemas = list(self._schemas)
        objects: list[CatalogObject] = []
        with self._conn.cursor(row_factory=dict_row) as cur:
            # Tables/views with their stable native oid from pg_catalog.
            cur.execute(
                """
                SELECT n.nspname AS sch, c.relname AS tbl, c.oid::text AS oid
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind IN ('r', 'p', 'v', 'm')
                  AND n.nspname = ANY(%s)
                ORDER BY n.nspname, c.relname
                """,
                (schemas,),
            )
            for row in cur.fetchall():
                ref = CatalogObjectRef(
                    catalog_source=self._catalog_source,
                    object_kind="table",
                    schema=row["sch"],
                    table=row["tbl"],
                )
                objects.append(
                    CatalogObject(
                        object_ref=display_object_ref(ref),
                        object_kind="table",
                        schema=row["sch"],
                        table=row["tbl"],
                        column=None,
                        data_type=None,
                        native_oid=row["oid"],
                    )
                )
            # Columns with their information_schema data types, plus a stable composite
            # native_oid of "<table_oid>:<attnum>" (design §8). attnum is
            # assigned at column creation and is NOT reused on rename, so the column's identity
            # survives a rename — letting change detection track renames instead of degrading
            # them to drop/add. The table oid (pg_class.oid) and attnum (pg_attribute.attnum)
            # come from pg_catalog; data_type stays from information_schema for stable spelling.
            cur.execute(
                """
                SELECT isc.table_schema AS sch, isc.table_name AS tbl,
                       isc.column_name AS col, isc.data_type AS dtype,
                       c.oid::text AS table_oid, a.attnum AS attnum
                FROM information_schema.columns isc
                JOIN pg_catalog.pg_namespace n ON n.nspname = isc.table_schema
                JOIN pg_catalog.pg_class c
                  ON c.relname = isc.table_name AND c.relnamespace = n.oid
                JOIN pg_catalog.pg_attribute a
                  ON a.attrelid = c.oid AND a.attname = isc.column_name
                WHERE isc.table_schema = ANY(%s)
                ORDER BY isc.table_schema, isc.table_name, isc.ordinal_position
                """,
                (schemas,),
            )
            for row in cur.fetchall():
                ref = CatalogObjectRef(
                    catalog_source=self._catalog_source,
                    object_kind="column",
                    schema=row["sch"],
                    table=row["tbl"],
                    column=row["col"],
                )
                objects.append(
                    CatalogObject(
                        object_ref=display_object_ref(ref),
                        object_kind="column",
                        schema=row["sch"],
                        table=row["tbl"],
                        column=row["col"],
                        data_type=row["dtype"],
                        native_oid=f"{row['table_oid']}:{row['attnum']}",
                    )
                )
        return objects

    def get_fact(
        self, ref: CatalogObjectRef, fact_type: str, use_case: str | None = None
    ) -> CatalogFact | None:
        # information_schema records none of the five ML fact types authoritatively.
        return None

    def owner_of(self, ref: CatalogObjectRef) -> str | None:
        return None

    def fingerprint(self) -> Mapping[str, CatalogObject]:
        return {obj.object_ref: obj for obj in self.list_objects()}


# --- Single-source overlay catalog adapter accessor ---------------------------------------
# The process-wide adapter shared by ``propose_fact`` and ``run_profiler``. Mirrors the SP-0
# ``register_command_authorizer`` / ``current_authorizer`` module-global pattern. This is the ONLY
# holder for the overlay catalog adapter — downstream phases call ``current_catalog_adapter()``.
_CATALOG_ADAPTER: CatalogAdapter | None = None


def register_catalog_adapter(adapter: CatalogAdapter) -> None:
    """Register the process-wide overlay ``CatalogAdapter`` (last writer wins)."""
    global _CATALOG_ADAPTER
    _CATALOG_ADAPTER = adapter


def current_catalog_adapter() -> CatalogAdapter:
    """Return the registered overlay ``CatalogAdapter``.

    Fails closed: raises ``RuntimeError`` if no adapter has been registered, so callers never
    resolve facts against a missing catalog.
    """
    if _CATALOG_ADAPTER is None:
        raise RuntimeError(
            "no catalog adapter registered; call register_catalog_adapter(...) "
            "(register_overlay() does this in production)"
        )
    return _CATALOG_ADAPTER


def _clear_catalog_adapter() -> None:
    """Test-only reset of the module-global adapter (call from the overlay conftest)."""
    global _CATALOG_ADAPTER
    _CATALOG_ADAPTER = None
