from __future__ import annotations

from featuregen.overlay.catalog import CatalogObject
from featuregen.overlay.identity import CatalogObjectRef
from featuregen.overlay.upload.canonical import CanonicalRow

_SCHEMA = "public"


def table_ref(catalog_source: str, table: str) -> CatalogObjectRef:
    return CatalogObjectRef(catalog_source=catalog_source, object_kind="table",
                            schema=_SCHEMA, table=table, column=None)


def _table_object_ref(table: str) -> str:
    return f"{_SCHEMA}.{table}"


def _column_object_ref(table: str, column: str) -> str:
    return f"{_SCHEMA}.{table}.{column}"


class UploadCatalog:
    def __init__(self, catalog_source: str, rows: list[CanonicalRow]) -> None:
        self.catalog_source = catalog_source
        self._rows = rows

    def _objects(self) -> dict[str, CatalogObject]:
        objs: dict[str, CatalogObject] = {}
        for r in self._rows:
            t_ref = _table_object_ref(r.table)
            objs.setdefault(t_ref, CatalogObject(
                object_ref=t_ref, object_kind="table", schema=_SCHEMA,
                table=r.table, column=None, data_type=None, native_oid=None))
            c_ref = _column_object_ref(r.table, r.column)
            # Safety metadata a re-upload can change; folded into the drift fingerprint so a change to
            # any of them (e.g. a public->pii or additive->non_additive reclassification) stales the
            # column's dependents, not just a raw data_type change.
            safety = "|".join((r.sensitivity, str(r.is_grain), str(r.as_of), r.as_of_basis,
                               r.cardinality, r.additivity, r.unit, r.currency, r.entity))
            objs[c_ref] = CatalogObject(
                object_ref=c_ref, object_kind="column", schema=_SCHEMA,
                table=r.table, column=r.column, data_type=r.type, native_oid=None,
                safety_fingerprint=safety)
        return objs

    def list_objects(self):
        return list(self._objects().values())

    def fingerprint(self):
        return self._objects()

    def get_fact(self, ref, fact_type, use_case=None):
        return None

    def owner_of(self, ref):
        return None
