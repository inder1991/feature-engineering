from __future__ import annotations

import logging

from featuregen.overlay.catalog import (
    CatalogAdapter,
    CatalogObject,
    current_catalog_adapter,
    register_catalog_adapter,
)
from featuregen.overlay.identity import CatalogObjectRef
from featuregen.overlay.upload.canonical import CanonicalRow

logger = logging.getLogger(__name__)

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


class UploadContextAdapter(CatalogAdapter):
    """A stateless catalog adapter for the upload request/worker context.

    The upload flow has no external ownership registry, so ``owner_of`` returns ``None`` — which
    routes every governed fact (grain/availability proposals) to the platform-admin governance
    queue, the documented fail-safe (mirrors ``PostgresCatalog.owner_of``). ``get_fact`` returns
    ``None`` (the ML fact types are recorded in the overlay, not this catalog). ``list_objects`` /
    ``fingerprint`` are unused on the propose/confirm/expiry path, so they are empty here; the
    per-upload ``UploadCatalog`` still owns drift fingerprinting. Stateless ⇒ safe to register once
    process-wide with no clobber hazard.

    NOT production owner routing: ``owner_of->None`` sends every grain/availability confirmation
    task to the platform-admin governance queue rather than to a data-owner/table-steward. That is a
    correct fail-safe for a proof-of-concept HITL loop, but data-owner-specific routing requires a
    richer adapter (structural-provider fusion, Phase 3/4)."""

    # REQUIRED protocol member (catalog.py:48). Registering this adapter at worker startup un-skips
    # the drift poller (_run_drift_scan reads adapter.catalog_source every tick); without this it
    # would AttributeError each tick. A reserved sentinel source that no real UploadCatalog uses +
    # an empty fingerprint() means detect_catalog_changes diffs {} against an equally-empty prior
    # snapshot → zero changes → drift is INERT (no false stales, no per-tick error).
    catalog_source = "upload:context"

    def list_objects(self):
        return []

    def fingerprint(self):
        return {}

    def get_fact(self, ref, fact_type, use_case=None):
        return None

    def owner_of(self, ref):
        return None


def ensure_upload_catalog_adapter() -> None:
    """Register a process-wide :class:`UploadContextAdapter` iff none is registered yet.

    Idempotent and forward-safe: a deployment that registers a richer adapter (with real ownership)
    wins — this NEVER clobbers an already-registered adapter — and a second call is a no-op. Called
    at ``ingest_upload`` entry (the single upload chokepoint) and at worker startup (for the
    expiry/renewal pollers). Emits a counter/log when the fallback is installed so a missing
    production ownership adapter is visible, not silent."""
    from featuregen.runtime.observability import counters
    try:
        current_catalog_adapter()
    except RuntimeError:
        register_catalog_adapter(UploadContextAdapter())
        counters.incr("overlay.catalog_adapter.upload_context_fallback_registered")
        logger.info("registered UploadContextAdapter fallback (owner_of->None; governance-queue "
                    "routing). Not production owner routing — see Phase 3/4.")
