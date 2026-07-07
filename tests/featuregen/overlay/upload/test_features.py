from datetime import UTC, datetime, timedelta

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.features import (
    FeatureSpec,
    feature_freshness,
    features_affected_by,
    register_feature,
)
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.join_path import find_join_path


def _actor():
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _seal():
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def test_register_feature_and_drift_impact(db):
    fid = register_feature(db, FeatureSpec(
        name="avg_balance_90d", description="90-day average balance",
        grain_table="accounts", aggregation="avg_90d", as_of_column="posted_at",
        derives_from=(("deposits", "public.accounts.balance"),
                      ("deposits", "public.accounts.posted_at"))))
    assert fid.startswith("feat")
    # drift impact: which features break if accounts.balance changes?
    assert features_affected_by(db, "deposits", "public.accounts.balance") == [fid]
    assert features_affected_by(db, "deposits", "public.accounts.unused") == []


def test_feature_freshness_follows_stalest_source(db):
    _seal()
    now = datetime(2026, 7, 5, tzinfo=UTC)
    rows = [CanonicalRow("deposits", "accounts", "balance", "numeric")]
    ingest_upload(db, "deposits", rows, actor=_actor(), now=now)   # writes a fresh watermark
    fid = register_feature(db, FeatureSpec(
        name="bal", derives_from=(("deposits", "public.accounts.balance"),)))
    assert feature_freshness(db, fid, now=now).fresh is True
    # 3 days later the source watermark is beyond the 24h window -> the feature is stale.
    later = now + timedelta(days=3)
    fr = feature_freshness(db, fid, now=later)
    assert fr.fresh is False and fr.stale_sources == ["deposits"]


def test_find_join_path_direct_and_multihop(db):
    # transactions -> accounts -> customers (two joins).
    rows = [
        CanonicalRow("bank", "transactions", "acct_id", "integer",
                     joins_to="accounts.account_id", cardinality="N:1"),
        CanonicalRow("bank", "accounts", "account_id", "integer", is_grain=True),
        CanonicalRow("bank", "accounts", "cust_id", "integer",
                     joins_to="customers.customer_id", cardinality="N:1"),
        CanonicalRow("bank", "customers", "customer_id", "integer", is_grain=True),
    ]
    build_graph(db, "bank", rows)

    assert find_join_path(db, "bank", "accounts", "accounts") == []
    direct = find_join_path(db, "bank", "transactions", "accounts")
    assert direct is not None and len(direct) == 1
    multi = find_join_path(db, "bank", "transactions", "customers")
    assert multi is not None and len(multi) == 2
    assert find_join_path(db, "bank", "transactions", "nowhere") is None
