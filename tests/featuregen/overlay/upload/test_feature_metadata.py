from datetime import UTC, datetime, timedelta

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.search import search


def _actor():
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _seal():
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def test_feature_metadata_surfaced_and_entity_searchable(db):
    _seal()
    now = datetime(2026, 7, 5, tzinfo=UTC)
    rows = [
        CanonicalRow("deposits", "accounts", "balance", "numeric", definition="ledger balance",
                     additivity="semi_additive", unit="cents", currency="USD", entity="Account"),
    ]
    assert ingest_upload(db, "deposits", rows, actor=_actor(), now=now).status == "ingested"

    # metadata surfaces on the search hit (feature-building needs it to aggregate correctly)
    hit = next(h for h in search(db, "balance", now=now) if h.column == "balance")
    assert hit.additivity == "semi_additive"   # do not SUM a balance over time
    assert hit.unit == "cents" and hit.currency == "USD"
    assert hit.entity == "Account"

    # the entity is searchable
    assert any(h.column == "balance" for h in search(db, "account", now=now))
