from datetime import UTC, datetime

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.planner.contracts import CatalogOmissionReason
from featuregen.overlay.upload.planner.scope import resolve_catalog_scope

_NOW = datetime(2026, 7, 14, tzinfo=UTC)


def _seed(db, source, *, watermark=True):
    build_graph(db, source, [CanonicalRow(source, "t", "id", "integer", is_grain=True)],
                concepts={content_hash(CanonicalRow(source, "t", "id", "integer", is_grain=True)): "customer_id"})
    if watermark:
        db.execute("INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, head_seq) "
                   "VALUES (%s, %s, 'r', 5) ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
                   (source, _NOW, _NOW))


def test_scope_orders_and_stamps_readable_catalogs(db):
    _seed(db, "core")
    _seed(db, "crm")
    scope = resolve_catalog_scope(db, roles=(), target_entity="customer", now=_NOW)
    assert scope.authorized_catalog_sources == ("core", "crm")           # deterministically sorted
    assert {s.catalog_source for s in scope.catalog_state_stamps} == {"core", "crm"}
    assert all(s.head_seq == 5 for s in scope.catalog_state_stamps)
    assert scope.scope_id and scope.read_scope_policy_version


def test_catalog_without_watermark_is_omitted(db):
    _seed(db, "core")
    _seed(db, "nowm", watermark=False)
    scope = resolve_catalog_scope(db, roles=(), target_entity="customer", now=_NOW)
    assert scope.authorized_catalog_sources == ("core",)
    assert any(o.catalog_source == "nowm" and o.reason is CatalogOmissionReason.no_usable_state_stamp
               for o in scope.omitted_catalog_sources)


def test_scope_id_is_deterministic(db):
    _seed(db, "core")
    a = resolve_catalog_scope(db, roles=(), target_entity="customer", now=_NOW)
    b = resolve_catalog_scope(db, roles=(), target_entity="customer", now=_NOW)
    assert a.scope_id == b.scope_id
