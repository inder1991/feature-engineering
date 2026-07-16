"""#10 honest authority attribution — the upload auto-confirm records a `source_declared`
authority basis instead of FABRICATING a `data_owner` confirmer for whoever dropped the file.

Four invariants under test:
1. an upload (any actor — here a platform-admin) writes `authority_basis=source_declared` +
   `origin_type` + the actor's REAL role_claims, and NO confirmer entry at all;
2. the OPERATIONAL projection is unchanged — the fact folds/serves VERIFIED exactly as before
   (resolve_fact, overlay_fact_state, graph flags); only the recorded provenance differs;
3. a LEGACY-shaped CONFIRMED event (confirmers, no authority_basis) still validates, folds and
   projects exactly as today — never retroactively reclassified as source_declared;
4. a genuine dual-owner human join confirm still writes real `confirmers` and no authority_basis.
"""
from datetime import UTC, datetime, timedelta

from tests.featuregen._helpers import mint_test_identity

from featuregen.contracts import Command
from featuregen.overlay import facts
from featuregen.overlay.commands import confirm_fact, propose_fact
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.identity import (
    ApprovedJoinRef,
    CatalogObjectRef,
    ColumnPair,
    fact_key,
    proposal_fingerprint,
)
from featuregen.overlay.projection import OverlayProjection
from featuregen.overlay.resolve import resolve_fact
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import append_overlay_event, load_fact
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.upload_catalog import UploadCatalog, table_ref
from featuregen.projections.runner import run_projection

ADMIN = mint_test_identity(subject="user:admin", role_claims=("platform-admin",))
NOW = datetime(2026, 7, 16, tzinfo=UTC)
SOURCE = "deposits"


def _seal_config():
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def _rows():
    return [
        CanonicalRow(SOURCE, "accounts", "id", "integer", is_grain=True),
        CanonicalRow(SOURCE, "accounts", "posted_at", "timestamp", as_of=True),
        CanonicalRow(SOURCE, "accounts", "balance", "numeric"),
    ]


def _confirmed_events(db, fk):
    return [e for e in load_fact(db, fk) if e.type == facts.OVERLAY_FACT_CONFIRMED]


def test_upload_records_source_declared_basis_not_a_fabricated_data_owner(db):
    _seal_config()
    res = ingest_upload(db, SOURCE, _rows(), actor=ADMIN, now=NOW)
    assert res.status == "ingested"

    for fact_type in ("grain", "availability_time"):
        fk = fact_key(table_ref(SOURCE, "accounts"), fact_type)
        confirmed = _confirmed_events(db, fk)
        assert len(confirmed) == 1
        payload = confirmed[0].payload
        # the honest basis: the SOURCE declared the fact; the uploader did not vouch for it
        assert payload["authority_basis"] == facts.AUTHORITY_SOURCE_DECLARED
        assert payload["origin_type"] == "upload"
        # the actor's REAL role_claims, never a synthesized single role
        assert payload["role_claims"] == ["platform-admin"]
        # NO confirmer entry at all — a platform-admin upload must not mint a data_owner
        assert "confirmers" not in payload

        st = fold_overlay_state(load_fact(db, fk))
        assert st.status == "VERIFIED"
        assert st.confirmers == []
        assert st.authority_basis == facts.AUTHORITY_SOURCE_DECLARED
        assert st.origin_type == "upload"
        assert st.role_claims == ["platform-admin"]


def test_source_declared_fact_projects_verified_identically(db):
    """Operational outcome unchanged: the fact serves VERIFIED with the declared value through
    resolve_fact, the overlay_fact_state read model, and the graph grain/as-of flags — the same
    surfaces the pre-#10 fabricated-confirmer auto-confirm produced."""
    _seal_config()
    rows = _rows()
    assert ingest_upload(db, SOURCE, rows, actor=ADMIN, now=NOW).status == "ingested"

    cat = UploadCatalog(SOURCE, rows)
    grain = resolve_fact(db, cat, table_ref(SOURCE, "accounts"), "grain", now=NOW)
    assert grain.status == "VERIFIED"
    assert grain.value == {"columns": ["id"], "is_unique": True}
    avail = resolve_fact(db, cat, table_ref(SOURCE, "accounts"), "availability_time", now=NOW)
    assert avail.status == "VERIFIED"
    assert avail.value == {"column": "posted_at", "basis": "posted_at"}

    fk = fact_key(table_ref(SOURCE, "accounts"), "grain")
    row = db.execute(
        "SELECT status, value, confirmers FROM overlay_fact_state WHERE fact_key = %s", (fk,)
    ).fetchone()
    assert row[0] == "VERIFIED"
    assert row[1] == {"columns": ["id"], "is_unique": True}
    assert row[2] == []  # honest: nobody vouched — no fabricated confirmer in the read model

    # the graph flags (the operational edge/flag surface) are marked exactly as before
    flags = dict(db.execute(
        "SELECT column_name, is_grain FROM graph_node WHERE catalog_source = %s "
        "AND table_name = 'accounts' AND kind = 'column'", (SOURCE,)).fetchall())
    assert flags["id"] is True


def test_connector_origin_is_recorded_when_passed(db):
    _seal_config()
    res = ingest_upload(db, SOURCE, _rows(), actor=ADMIN, now=NOW, origin_type="connector")
    assert res.status == "ingested"
    fk = fact_key(table_ref(SOURCE, "accounts"), "grain")
    payload = _confirmed_events(db, fk)[0].payload
    assert payload["authority_basis"] == facts.AUTHORITY_SOURCE_DECLARED
    assert payload["origin_type"] == "connector"


def test_legacy_confirmed_event_still_validates_folds_and_projects_unreclassified(db):
    """A pre-#10 CONFIRMED event (confirmers present, no authority_basis) must still be accepted
    by the extended schema at the store boundary, fold to VERIFIED via the confirmer path, and
    project with its confirmers intact — NEVER relabeled as source_declared."""
    ref = CatalogObjectRef(SOURCE, "table", "public", "accounts")
    fk = fact_key(ref, "grain")
    value = {"columns": ["id"], "is_unique": True}
    draft = append_overlay_event(db, fact_key=fk, type=facts.OVERLAY_FACT_PROPOSED,
        actor=ADMIN, expected_version=0, payload={
            "catalog_object_ref": {"catalog_source": SOURCE, "object_kind": "table",
                                   "schema": "public", "table": "accounts"},
            "object_ref": "public.accounts", "fact_type": "grain",
            "proposed_value": value, "proposal_fingerprint": proposal_fingerprint(value),
            "proposed_by": ADMIN.subject})
    # the exact legacy auto-confirm shape (what _assert_fact wrote pre-#10)
    append_overlay_event(db, fact_key=fk, type=facts.OVERLAY_FACT_CONFIRMED,
        actor=ADMIN, expected_version=1, payload={
            "value": value, "confirmers": [{"subject": ADMIN.subject, "role": "data_owner"}],
            "expires_at": None, "confirms_event_id": draft.event_id})

    st = fold_overlay_state(load_fact(db, fk))
    assert st.status == "VERIFIED"
    assert st.confirmers == [{"subject": ADMIN.subject, "role": "data_owner"}]
    assert st.authority_basis is None          # NOT reclassified
    assert st.origin_type is None
    assert st.role_claims == []
    assert st.authority_provenance == facts.AUTHORITY_LEGACY_UNSPECIFIED

    run_projection(db, OverlayProjection(), batch=500)
    row = db.execute(
        "SELECT status, confirmers FROM overlay_fact_state WHERE fact_key = %s", (fk,)).fetchone()
    assert row[0] == "VERIFIED"
    assert row[1] == [{"subject": ADMIN.subject, "role": "data_owner"}]  # preserved verbatim


def test_genuine_dual_owner_join_confirm_is_unchanged(db, catalog):
    """The two-human approved_join confirm still records BOTH real confirmers and carries no
    source-declared markers — only the upload auto-confirm path changed."""
    alice = mint_test_identity(subject="user:alice", role_claims=("data_owner",))
    bob = mint_test_identity(subject="user:bob", role_claims=("data_owner",))
    eve = mint_test_identity(subject="user:eve", role_claims=("data_owner",))
    orders = CatalogObjectRef("pg:core", "table", "sales", "orders")
    customers = CatalogObjectRef("pg:core", "table", "sales", "customers")
    ref = ApprovedJoinRef(orders, customers, (ColumnPair("customer_id", "id"),), "N:1")
    value = {
        "from_ref": {"catalog_source": "pg:core", "object_kind": "table",
                     "schema": "sales", "table": "orders", "column": None},
        "to_ref": {"catalog_source": "pg:core", "object_kind": "table",
                   "schema": "sales", "table": "customers", "column": None},
        "column_pairs": [{"from_col": "customer_id", "to_col": "id"}],
        "cardinality": "N:1",
    }
    catalog.set_owner(orders, "user:alice")
    catalog.set_owner(customers, "user:bob")
    res = propose_fact(db, Command("propose_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "approved_join", "proposed_value": value}, eve, "p"))
    assert res.accepted, res.denied_reason
    draft = res.produced_event_ids[0]
    args = {"ref": ref, "fact_type": "approved_join", "target_event_id": draft}
    assert confirm_fact(db, Command("confirm_fact", "overlay_fact", None, args, alice, "c1")).accepted
    assert confirm_fact(db, Command("confirm_fact", "overlay_fact", None, args, bob, "c2")).accepted

    fk = fact_key(ref, "approved_join")
    stream = load_fact(db, fk)
    confirmed = next(e for e in stream if e.type == facts.OVERLAY_FACT_CONFIRMED)
    assert {(c["subject"], c["role"]) for c in confirmed.payload["confirmers"]} == {
        ("user:alice", "data_owner_from"), ("user:bob", "data_owner_to")}
    assert "authority_basis" not in confirmed.payload
    assert "origin_type" not in confirmed.payload
    assert "role_claims" not in confirmed.payload
    st = fold_overlay_state(stream)
    assert st.status == "VERIFIED"
    assert st.authority_basis is None
    assert len(st.confirmers) == 2
