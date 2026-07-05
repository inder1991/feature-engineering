"""Regression tests for the review's BLOCKER (B1) and M1 — the value-change re-upload path."""
from datetime import datetime, timedelta, timezone

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.resolve import resolve_fact
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.upload_catalog import UploadCatalog, table_ref


def _actor():
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _seal():
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


NOW = datetime(2026, 7, 5, tzinfo=timezone.utc)


def _grain(db, source, table, now=NOW):
    return resolve_fact(db, UploadCatalog(source, []), table_ref(source, table), "grain", now=now)


def test_reupload_value_change_updates_served_grain(db):
    """B1: a re-upload that CHANGES the grain must update the served value, not serve the stale one."""
    _seal()
    src = "deposits"
    # Upload 1: grain = [id]
    rows1 = [CanonicalRow(src, "accounts", "id", "integer", is_grain=True),
             CanonicalRow(src, "customers", "cust_id", "integer", is_grain=True)]
    assert ingest_upload(db, src, rows1, actor=_actor(), now=NOW).status == "ingested"
    assert _grain(db, src, "accounts").value == {"columns": ["id"], "is_unique": True}

    # Upload 2: grain becomes composite [id, branch_id]
    rows2 = [CanonicalRow(src, "accounts", "id", "integer", is_grain=True),
             CanonicalRow(src, "accounts", "branch_id", "integer", is_grain=True),
             CanonicalRow(src, "customers", "cust_id", "integer", is_grain=True)]
    assert ingest_upload(db, src, rows2, actor=_actor(), now=NOW).status == "ingested"
    assert _grain(db, src, "accounts").value == {"columns": ["id", "branch_id"], "is_unique": True}


def test_reupload_recovers_a_staled_fact(db):
    """M1: drop a column -> fact STALE; re-add it -> the fact recovers to VERIFIED (not stuck)."""
    _seal()
    src = "deposits"
    ref = table_ref(src, "accounts")
    full = [CanonicalRow(src, "accounts", "id", "integer", is_grain=True),
            CanonicalRow(src, "accounts", "posted_at", "timestamp", as_of=True),
            CanonicalRow(src, "accounts", "balance", "numeric"),
            CanonicalRow(src, "customers", "cust_id", "integer", is_grain=True)]
    dropped = [r for r in full if r.column != "posted_at"]

    assert ingest_upload(db, src, full, actor=_actor(), now=NOW).status == "ingested"
    assert resolve_fact(db, UploadCatalog(src, []), ref, "availability_time", now=NOW).status == "VERIFIED"

    # Drop posted_at -> availability_time STALEs (fail-closed).
    assert ingest_upload(db, src, dropped, actor=_actor(), now=NOW).status == "ingested"
    assert resolve_fact(db, UploadCatalog(src, []), ref, "availability_time", now=NOW).value is None

    # Re-add posted_at -> the fact recovers to VERIFIED.
    assert ingest_upload(db, src, full, actor=_actor(), now=NOW).status == "ingested"
    recovered = resolve_fact(db, UploadCatalog(src, []), ref, "availability_time", now=NOW)
    assert recovered.status == "VERIFIED"
    assert recovered.value == {"column": "posted_at", "basis": "posted_at"}


def test_multi_source_rows_quarantined_not_crash(db):
    """M5: a foreign-source row (same table.column) must be quarantined, not crash the ingest."""
    _seal()
    rows = [CanonicalRow("deposits", "accounts", "id", "integer", is_grain=True),
            CanonicalRow("cards", "accounts", "id", "integer")]   # foreign source -> dup object_ref
    res = ingest_upload(db, "deposits", rows, actor=_actor(), now=NOW)
    assert res.status == "ingested"          # no UniqueViolation / rollback
    assert res.quarantined == 1              # the 'cards' row is quarantined
    assert _grain(db, "deposits", "accounts").value == {"columns": ["id"], "is_unique": True}
