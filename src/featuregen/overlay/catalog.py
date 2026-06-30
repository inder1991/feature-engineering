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
