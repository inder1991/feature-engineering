from datetime import UTC, datetime, timedelta

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.ingest import (
    dismiss_quarantine_row,
    ingest_upload,
    resolve_quarantine_row,
)
from featuregen.overlay.upload.review_queue import list_quarantine


def _actor():
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _seal():
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def test_quarantine_persisted_and_cleared_on_reupload(db):
    _seal()
    now = datetime(2026, 7, 5, tzinfo=UTC)

    # Upload 1: one good row + one bad (blank column) -> ingested, 1 quarantined + persisted.
    rows1 = [
        CanonicalRow("deposits", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("deposits", "accounts", "", "text"),  # blank column -> quarantined
    ]
    res1 = ingest_upload(db, "deposits", rows1, actor=_actor(), now=now)
    assert res1.status == "ingested" and res1.quarantined == 1

    q = list_quarantine(db, "deposits")
    assert len(q) == 1
    assert q[0].row_index == 1
    assert "missing" in q[0].reason
    assert q[0].raw["table"] == "accounts"      # raw row is captured for the reviewer

    # Upload 2: the row is fixed -> quarantine for the source is cleared.
    rows2 = [
        CanonicalRow("deposits", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("deposits", "accounts", "name", "text"),
    ]
    assert ingest_upload(db, "deposits", rows2, actor=_actor(), now=now).status == "ingested"
    assert list_quarantine(db, "deposits") == []


def _quarantine_one_bad(db):
    _seal()
    now = datetime(2026, 7, 5, tzinfo=UTC)
    rows = [
        CanonicalRow("deposits", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("deposits", "accounts", "", "text"),   # blank column -> quarantined
    ]
    ingest_upload(db, "deposits", rows, actor=_actor(), now=now)
    q = list_quarantine(db, "deposits")
    assert len(q) == 1
    return q[0].row_index


def test_resolve_quarantine_row_adds_to_catalog_and_clears(db):
    idx = _quarantine_one_bad(db)
    resolved, reason = resolve_quarantine_row(db, "deposits", idx, {"column": "name"}, actor=_actor())
    assert resolved and reason == ""
    assert list_quarantine(db, "deposits") == []                    # left the queue
    assert db.execute(                                              # entered the catalog
        "SELECT 1 FROM graph_node WHERE catalog_source = 'deposits' AND object_ref = %s",
        ("public.accounts.name",)).fetchone() is not None


def test_resolve_still_invalid_keeps_the_row(db):
    idx = _quarantine_one_bad(db)
    resolved, reason = resolve_quarantine_row(db, "deposits", idx, {"column": ""}, actor=_actor())
    assert not resolved and reason                                  # surfaced why
    assert len(list_quarantine(db, "deposits")) == 1               # still quarantined


def test_resolve_rejects_a_column_already_in_the_catalog(db):
    idx = _quarantine_one_bad(db)
    # 'id' already exists in the catalog -> resolving the blank row to 'id' is a conflict
    resolved, reason = resolve_quarantine_row(db, "deposits", idx, {"column": "id"}, actor=_actor())
    assert not resolved and "already" in reason


def test_resolve_recognizes_case_space_variant_of_existing_column(db):
    # #7: the resolution path's "already in the catalog?" check must use the SAME normalized ref as
    # the main ingest path — a case/trailing-space variant of an existing column IS that column, not
    # a new one to add as a twin node.
    idx = _quarantine_one_bad(db)
    resolved, reason = resolve_quarantine_row(db, "deposits", idx, {"column": "ID "}, actor=_actor())
    assert not resolved and "already" in reason
    twins = db.execute(
        "SELECT count(*) FROM graph_node WHERE catalog_source = 'deposits' AND kind = 'column' "
        "AND lower(btrim(column_name)) = 'id'").fetchone()[0]
    assert twins == 1                                   # no case-variant twin was added


def test_resolve_cannot_bypass_the_large_change_brake(db):
    # #4: an all-quarantined WRONG-SOURCE upload is held by the brake — but resolving it row-by-row
    # used to bypass the brake entirely, letting a reviewer contaminate the catalog with an unrelated
    # schema one object at a time. The cumulative resolved additions must re-trip the source brake.
    _seal()
    now = datetime(2026, 7, 5, tzinfo=UTC)
    rows = [
        CanonicalRow("deposits", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("deposits", "accounts", "balance", "numeric"),
        CanonicalRow("deposits", "accounts", "posted_at", "timestamp", as_of=True),
        CanonicalRow("deposits", "customers", "cust_id", "integer", is_grain=True),
    ]
    assert ingest_upload(db, "deposits", rows, actor=_actor(), now=now).status == "ingested"

    foreign = [CanonicalRow("crm", t, c, "text") for t, c in [
        ("leads", "lead_id"), ("leads", "stage"),
        ("tickets", "ticket_id"), ("tickets", "priority")]]
    held = ingest_upload(db, "deposits", foreign, actor=_actor(), now=now)
    assert held.status == "held"                      # the upload path is braked...
    q = list_quarantine(db, "deposits")
    assert len(q) == 4

    outcomes = [resolve_quarantine_row(db, "deposits", item.row_index,
                                       {"source": "deposits"}, actor=_actor())
                for item in q]
    refused = [reason for resolved, reason in outcomes if not resolved]
    assert refused and "brake" in refused[0]          # ...and row-by-row resolution is too
    # The brake fired BEFORE the whole foreign schema landed in the graph.
    foreign_cols = db.execute(
        "SELECT count(*) FROM graph_node WHERE catalog_source = 'deposits' AND kind = 'column' "
        "AND table_name IN ('leads', 'tickets')").fetchone()[0]
    assert foreign_cols < 4
    assert list_quarantine(db, "deposits")            # refused rows stay queued for review


def test_dismiss_quarantine_row(db):
    idx = _quarantine_one_bad(db)
    assert dismiss_quarantine_row(db, "deposits", idx) is True
    assert list_quarantine(db, "deposits") == []
    assert dismiss_quarantine_row(db, "deposits", 999) is False    # unknown row


def test_resolve_grain_column_reconciles_the_grain_fact(db):
    from featuregen.overlay.identity import fact_key
    from featuregen.overlay.state import fold_overlay_state
    from featuregen.overlay.store import load_fact
    from featuregen.overlay.upload.upload_catalog import table_ref
    _seal()
    now = datetime(2026, 7, 5, tzinfo=UTC)
    rows = [
        CanonicalRow("deposits", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("deposits", "accounts", "cust_id", "", is_grain=True),   # blank type -> quarantined
    ]
    ingest_upload(db, "deposits", rows, actor=_actor(), now=now)
    idx = list_quarantine(db, "deposits")[0].row_index
    resolved, _ = resolve_quarantine_row(db, "deposits", idx, {"type": "integer"}, actor=_actor())
    assert resolved
    # the table's grain fact now covers BOTH grain columns, not just the one that ingested cleanly
    state = fold_overlay_state(load_fact(db, fact_key(table_ref("deposits", "accounts"), "grain")))
    assert set(state.value["columns"]) == {"id", "cust_id"}
