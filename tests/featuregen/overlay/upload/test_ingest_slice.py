from datetime import datetime, timedelta, timezone

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.resolve import resolve_fact
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.upload_catalog import UploadCatalog, table_ref


def _actor() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _seal_config():
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def test_slice_ingest_serve_drift_and_brake(db):
    _seal_config()
    now = datetime(2026, 7, 5, tzinfo=timezone.utc)
    source = "deposits"

    # Upload 1: accounts(id grain, posted_at as-of) + a second table so a later drop is small.
    rows1 = [
        CanonicalRow(source, "accounts", "id", "integer", is_grain=True),
        CanonicalRow(source, "accounts", "posted_at", "timestamp", as_of=True),
        CanonicalRow(source, "accounts", "balance", "numeric"),
        CanonicalRow(source, "customers", "cust_id", "integer", is_grain=True),
    ]
    res1 = ingest_upload(db, source, rows1, actor=_actor(), now=now)
    assert res1.status == "ingested"

    # The graph is materialized on a successful ingest.
    node_count = db.execute(
        "SELECT count(*) FROM graph_node WHERE catalog_source='deposits'").fetchone()[0]
    assert node_count > 0

    cat1 = UploadCatalog(source, rows1)
    grain = resolve_fact(db, cat1, table_ref(source, "accounts"), "grain", now=now)
    assert grain.status == "VERIFIED"
    assert grain.value == {"columns": ["id"], "is_unique": True}
    avail = resolve_fact(db, cat1, table_ref(source, "accounts"), "availability_time", now=now)
    assert avail.status == "VERIFIED"

    # Upload 2: posted_at dropped -> availability_time fact STALE, grain still served.
    rows2 = [
        CanonicalRow(source, "accounts", "id", "integer", is_grain=True),
        CanonicalRow(source, "accounts", "balance", "numeric"),
        CanonicalRow(source, "customers", "cust_id", "integer", is_grain=True),
    ]
    res2 = ingest_upload(db, source, rows2, actor=_actor(), now=now)
    assert res2.status == "ingested"
    assert res2.staled >= 1

    cat2 = UploadCatalog(source, rows2)
    avail2 = resolve_fact(db, cat2, table_ref(source, "accounts"), "availability_time", now=now)
    assert avail2.value is None                       # fail-closed
    assert avail2.status in ("STALE", "REVERIFY")
    grain2 = resolve_fact(db, cat2, table_ref(source, "accounts"), "grain", now=now)
    assert grain2.status == "VERIFIED"                # unaffected fact still served

    # Upload 3: truncated (only accounts.id) -> brake holds, nothing changes.
    rows3 = [CanonicalRow(source, "accounts", "id", "integer", is_grain=True)]
    res3 = ingest_upload(db, source, rows3, actor=_actor(), now=now)
    assert res3.status == "held"


def test_enrichment_failure_does_not_abort_ingest(db):
    _seal_config()
    now = datetime(2026, 7, 5, tzinfo=timezone.utc)

    class _Boom:
        def call(self, request):
            raise RuntimeError("provider down")

    rows = [CanonicalRow("s", "accounts", "id", "integer", is_grain=True)]
    res = ingest_upload(db, "s", rows, actor=_actor(), now=now, client=_Boom())
    assert res.status == "ingested"   # advisory enrichment failure must not abort the upload's facts
    assert res.asserted >= 1
