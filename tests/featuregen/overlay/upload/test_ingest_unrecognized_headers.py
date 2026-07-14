"""Unrecognized-headers ingest fix: an upload whose rows ALL quarantine (headers never mapped to
table/column/type, or a glossary whose FQNs all failed to resolve) must return an HONEST "rejected"
— never "ingested" with asserted=0 — and must NEVER reach build_graph, which would wipe an existing
graph for the source. The rows still carry a source (readers set it from the upload param), so the
"no row has a source" structural error does NOT catch this case.
"""
from datetime import UTC, datetime, timedelta

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.review_queue import list_quarantine


def _actor() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _seal_config():
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


_NOW = datetime(2026, 7, 14, tzinfo=UTC)


def _unrecognized_rows(src):
    """Rows with a source but NO table/column/type — what a reader emits when headers didn't map."""
    return [CanonicalRow(source=src, table="", column="", type="")]


def test_all_quarantine_returns_rejected_not_ingested(db):
    _seal_config()
    res = ingest_upload(db, "src", _unrecognized_rows("src"), actor=_actor(), now=_NOW)
    assert res.status == "rejected"            # honest, not "ingested"
    assert res.asserted == 0
    assert res.quarantined >= 1
    assert "quarantin" in (res.reason or "").lower() or "recogni" in (res.reason or "").lower()
    # The quarantine is persisted (like the brake's held path) so the rows are reviewable.
    assert len(list_quarantine(db, "src")) == 1


def test_all_quarantine_preserves_existing_graph(db, monkeypatch):
    _seal_config()
    from featuregen.overlay.upload import ingest as ingest_mod

    # 1. Ingest a good catalog UNDER PROJECTION LAG -> build_graph materializes the graph, but
    #    detect_catalog_changes is skipped, so the overlay_catalog_object snapshot is NOT advanced.
    #    This is the real production skew where the next upload's large-change brake sees "first
    #    upload" and does NOT hold — the all-quarantine upload reaches build_graph unbraked.
    monkeypatch.setattr(ingest_mod, "projection_lag", lambda conn, name: 1)
    good = [CanonicalRow(source="src", table="t", column="c", type="text")]
    res1 = ingest_upload(db, "src", good, actor=_actor(), now=_NOW)
    assert res1.status == "ingested"
    monkeypatch.undo()

    before = db.execute(
        "SELECT count(*) FROM graph_node WHERE catalog_source='src'").fetchone()[0]
    assert before > 0

    # 2. Re-upload an all-quarantine file for the SAME source.
    res2 = ingest_upload(db, "src", _unrecognized_rows("src"), actor=_actor(), now=_NOW)
    after = db.execute(
        "SELECT count(*) FROM graph_node WHERE catalog_source='src'").fetchone()[0]
    assert after == before                     # graph UNCHANGED (not wiped by build_graph)
    assert res2.status == "rejected"


def test_partial_upload_still_ingests_good_rows(db):
    _seal_config()
    rows = [CanonicalRow(source="src2", table="t", column="c", type="text"),
            CanonicalRow(source="src2", table="", column="", type="")]   # one good, one bad
    res = ingest_upload(db, "src2", rows, actor=_actor(), now=_NOW)
    assert res.status == "ingested"            # partial success still ingests
    assert res.quarantined == 1
    nodes = db.execute(
        "SELECT count(*) FROM graph_node WHERE catalog_source='src2'").fetchone()[0]
    assert nodes > 0                           # the good row reached the graph


def test_brake_held_takes_precedence_over_all_quarantine_rejection(db):
    _seal_config()
    # With an ADVANCED snapshot (normal good upload), an all-quarantine re-upload has 0% overlap:
    # the large-change brake HOLDS it. The all-quarantine branch sits AFTER the brake, so a held
    # upload must still report "held" (either way the graph is preserved — no build_graph).
    good = [CanonicalRow(source="src3", table="t", column="c", type="text")]
    assert ingest_upload(db, "src3", good, actor=_actor(), now=_NOW).status == "ingested"
    before = db.execute(
        "SELECT count(*) FROM graph_node WHERE catalog_source='src3'").fetchone()[0]
    res = ingest_upload(db, "src3", _unrecognized_rows("src3"), actor=_actor(), now=_NOW)
    assert res.status == "held"
    after = db.execute(
        "SELECT count(*) FROM graph_node WHERE catalog_source='src3'").fetchone()[0]
    assert after == before
