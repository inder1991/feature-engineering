"""Shared overlay-test doubles and builders.

Imported by conftest (which re-exports `StubCatalog` and exposes the `catalog` fixture) and by the
individual overlay test modules. Centralizes the several per-file CatalogAdapter doubles that had
drifted into near-duplicates (finding CQ14).
"""

from featuregen.overlay.catalog import CatalogFact, CatalogObject
from featuregen.overlay.identity import CatalogObjectRef, display_object_ref


class StubCatalog:
    """In-memory CatalogAdapter test double (stands in for the real FixtureCatalog/PostgresCatalog so
    overlay tests stay decoupled from their constructors).

    Covers the union of what the per-file variants needed:

      * ``objects`` -> ``list_objects()`` / ``fingerprint()`` (profiler scans)
      * ``owners``  -> ``owner_of()``; accepted either as ``set_owner(ref, subject)`` (keyed on the
                       display object_ref string) or as a constructor dict keyed on ``(schema, table)``
                       (the profiler tests) — both keyings are honoured on lookup.
      * ``fact``    -> a constant ``get_fact()`` return (resolve tests, which only ever call get_fact).
    """

    def __init__(self, objects=None, owners=None, fact: CatalogFact | None = None) -> None:
        self._objects = list(objects or [])
        self._owners = dict(owners or {})
        self._fact = fact

    def set_owner(self, ref, subject: str) -> None:
        self._owners[display_object_ref(ref)] = subject

    def owner_of(self, ref):
        key = display_object_ref(ref)
        if key in self._owners:
            return self._owners[key]
        return self._owners.get((ref.schema, ref.table))

    def get_fact(self, ref, fact_type, use_case=None):
        return self._fact

    def list_objects(self):
        return list(self._objects)

    def fingerprint(self):
        return {o.object_ref: o for o in self._objects}


def catalog_columns(ref: CatalogObjectRef, specs):
    """Build the column ``CatalogObject`` list for ``ref`` from ``(name, data_type)`` pairs."""
    return [
        CatalogObject(
            object_ref=f"{ref.schema}.{ref.table}.{name}",
            object_kind="column",
            schema=ref.schema,
            table=ref.table,
            column=name,
            data_type=data_type,
            native_oid=None,
        )
        for name, data_type in specs
    ]
