from datetime import datetime, timedelta, timezone

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


def _ingest(db, now):
    rows = [
        CanonicalRow("deposits", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("deposits", "accounts", "posted_at", "timestamp", as_of=True),
        CanonicalRow("deposits", "accounts", "balance", "numeric",
                     definition="customer ledger balance"),
    ]
    assert ingest_upload(db, "deposits", rows, actor=_actor(), now=now).status == "ingested"


def test_search_finds_by_name_and_definition(db):
    _seal()
    now = datetime(2026, 7, 5, tzinfo=timezone.utc)
    _ingest(db, now)

    # 'balance' matches the column name.
    hits = search(db, "balance", now=now)
    assert any(h.object_ref == "public.accounts.balance" for h in hits)

    # 'customer' matches only the definition of balance.
    hits2 = search(db, "customer", now=now)
    assert any(h.column == "balance" for h in hits2)


def test_grain_column_outranks_plain_on_name(db):
    _seal()
    now = datetime(2026, 7, 5, tzinfo=timezone.utc)
    _ingest(db, now)
    hits = search(db, "id", now=now)
    assert hits and hits[0].object_ref == "public.accounts.id"
    assert hits[0].is_grain is True


def test_stale_source_excluded(db):
    _seal()
    now = datetime(2026, 7, 5, tzinfo=timezone.utc)
    _ingest(db, now)
    # Query far in the future -> the source's watermark is older than the 24h SLA -> excluded.
    later = now + timedelta(days=3)
    assert search(db, "balance", now=later) == []


def test_search_uses_llm_concept(db):
    from featuregen.intake.llm import FakeLLM, FakeResponse
    _seal()
    now = datetime(2026, 7, 5, tzinfo=timezone.utc)
    rows = [CanonicalRow("deposits", "accounts", "bal", "numeric")]  # cryptic name, no definition
    client = FakeLLM(script={"overlay.enrich.concept":
                             FakeResponse(output={"concept": "monetary_amount"})})
    assert ingest_upload(db, "deposits", rows, actor=_actor(), now=now,
                         client=client).status == "ingested"
    # 'monetary' finds the cryptic 'bal' column only via its LLM-assigned concept.
    hits = search(db, "monetary", now=now)
    assert any(h.column == "bal" for h in hits)
    assert next(h for h in hits if h.column == "bal").concept == "monetary_amount"
