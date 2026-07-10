from datetime import UTC, datetime, timedelta

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.read_scope import allowed_sensitivities
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


def test_allowed_sensitivities_maps_roles():
    assert allowed_sensitivities(()) == []
    assert allowed_sensitivities({"pii_reader"}) == ["pii"]
    assert set(allowed_sensitivities({"pii_reader", "restricted_reader"})) == {"pii", "restricted"}


def test_pii_node_hidden_without_role_visible_with_role(db):
    _seal()
    now = datetime(2026, 7, 5, tzinfo=UTC)
    rows = [
        CanonicalRow("deposits", "accounts", "ssn_hash", "text", sensitivity="pii"),
        CanonicalRow("deposits", "accounts", "balance", "numeric"),
    ]
    assert ingest_upload(db, "deposits", rows, actor=_actor(), now=now).status == "ingested"

    # No role -> the PII column is excluded from search entirely.
    open_hits = {h.object_ref for h in search(db, "ssn_hash", now=now).hits}
    assert "public.accounts.ssn_hash" not in open_hits

    # With the pii_reader role -> visible, and its sensitivity is surfaced.
    priv = search(db, "ssn_hash", now=now, roles={"pii_reader"}).hits
    hit = next(h for h in priv if h.object_ref == "public.accounts.ssn_hash")
    assert hit.sensitivity == "pii"

    # A non-sensitive column is visible either way.
    assert any(h.column == "balance" for h in search(db, "balance", now=now).hits)
