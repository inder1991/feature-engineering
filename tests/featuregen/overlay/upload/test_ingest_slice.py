from datetime import UTC, datetime, timedelta

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.intake.llm import FakeLLM, FakeResponse
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
    now = datetime(2026, 7, 5, tzinfo=UTC)
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
    assert res2.changed_objects >= 1

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


def test_case_whitespace_variant_reupload_is_one_identity(db):
    # #1: a re-upload differing ONLY in case / trailing whitespace on table+column is the SAME
    # catalog: ONE graph node per column (no case-variant twin), NO drift (changed_objects == 0),
    # and no false fact re-assertion. Pre-fix the raw refs split identity: the brake saw 0% overlap
    # (held) and the snapshot diff reported mass drift.
    _seal_config()
    now = datetime(2026, 7, 5, tzinfo=UTC)
    source = "deposits"
    rows1 = [
        CanonicalRow(source, "accounts", "id", "integer", is_grain=True),
        CanonicalRow(source, "accounts", "balance", "numeric"),
    ]
    assert ingest_upload(db, source, rows1, actor=_actor(), now=now).status == "ingested"

    rows2 = [
        CanonicalRow(source, "Accounts ", "ID ", "integer", is_grain=True),
        CanonicalRow(source, " accounts", "Balance", "numeric"),
    ]
    res2 = ingest_upload(db, source, rows2, actor=_actor(), now=now)
    assert res2.status == "ingested"          # same catalog — not held as a wrong source
    assert res2.changed_objects == 0          # no false drift/staling
    assert res2.asserted == 0                 # grain fact value unchanged -> no re-assertion
    refs = sorted(r[0] for r in db.execute(
        "SELECT object_ref FROM graph_node WHERE catalog_source = %s AND kind = 'column'",
        (source,)).fetchall())
    assert refs == ["public.accounts.balance", "public.accounts.id"]   # ONE node per column


def test_two_as_of_columns_conflict_surfaced_same_either_row_order(db):
    # #17: ONE table declaring TWO as_of columns used to silently assert whichever row came first
    # (reordering equivalent CSV rows changed the availability fact, no conflict reported). Now the
    # ambiguity quarantines both as_of rows — NO availability_time fact is asserted — and the
    # surfaced conflict is identical regardless of row order. A single as_of column still asserts
    # availability normally (test_slice_ingest_serve_drift_and_brake above).
    from featuregen.overlay.upload.review_queue import list_quarantine
    _seal_config()
    now = datetime(2026, 7, 5, tzinfo=UTC)

    def _rows(src, flipped):
        pair = [
            CanonicalRow(src, "accounts", "posted_at", "timestamp", as_of=True),
            CanonicalRow(src, "accounts", "ingested_at", "timestamp", as_of=True),
        ]
        return [CanonicalRow(src, "accounts", "id", "integer", is_grain=True),
                *(reversed(pair) if flipped else pair)]

    surfaced = []
    for src, flipped in (("s1", False), ("s2", True)):
        rows = _rows(src, flipped)
        res = ingest_upload(db, src, rows, actor=_actor(), now=now)
        assert res.status == "ingested"        # the unambiguous rows still ingest
        assert res.quarantined == 2            # both as_of rows surfaced, not silently resolved
        avail = resolve_fact(db, UploadCatalog(src, rows), table_ref(src, "accounts"),
                             "availability_time", now=now)
        assert avail.value is None             # no silently-picked availability basis
        surfaced.append(sorted((q.raw["column"], q.reason) for q in list_quarantine(db, src)))
    assert surfaced[0] == surfaced[1]          # row order changes NOTHING the reviewer sees


def test_enrichment_failure_does_not_abort_ingest(db):
    _seal_config()
    now = datetime(2026, 7, 5, tzinfo=UTC)

    class _Boom:
        def call(self, request):
            raise RuntimeError("provider down")

    rows = [CanonicalRow("s", "accounts", "id", "integer", is_grain=True)]
    res = ingest_upload(db, "s", rows, actor=_actor(), now=now, client=_Boom())
    assert res.status == "ingested"   # advisory enrichment failure must not abort the upload's facts
    assert res.asserted >= 1


def test_drift_skipped_when_projection_lags(db, monkeypatch):
    # If the overlay projection is behind (poison-halted), the upload must NOT run drift detection —
    # doing so would stale nothing yet advance the snapshot, laundering a dropped/changed column.
    from featuregen.overlay.upload import ingest as ingest_mod
    _seal_config()
    now = datetime(2026, 7, 5, tzinfo=UTC)
    called: list[bool] = []
    monkeypatch.setattr(ingest_mod, "projection_lag", lambda conn, name: 1)          # pretend halted
    monkeypatch.setattr(ingest_mod, "detect_catalog_changes",
                        lambda *a, **k: called.append(True) or [])
    rows = [CanonicalRow("deposits", "accounts", "id", "integer", is_grain=True)]
    res = ingest_upload(db, "deposits", rows, actor=_actor(), now=now)
    assert res.status == "ingested"   # the upload's facts still assert
    assert res.changed_objects == 0   # drift deferred
    assert called == []               # detect_catalog_changes was NOT run (laundering avoided)


def test_safety_metadata_change_is_drift(db):
    # A re-upload that reclassifies a column's SAFETY metadata (additive -> non_additive) is a
    # type_change, so its dependents get staled — a data_type-only fingerprint would miss it.
    _seal_config()
    now = datetime(2026, 7, 5, tzinfo=UTC)
    ingest_upload(db, "s", [CanonicalRow("s", "t", "amt", "numeric", additivity="additive")],
                  actor=_actor(), now=now)
    res = ingest_upload(db, "s", [CanonicalRow("s", "t", "amt", "numeric", additivity="non_additive")],
                        actor=_actor(), now=now)
    assert res.changed_objects >= 1   # the additivity flip registered as a type_change


def test_fingerprint_backward_compatible_without_safety():
    # An adapter that supplies no safety metadata keeps the EXACT data_type-only fingerprint (no mass
    # false-drift on existing snapshots for the non-upload catalog adapters).
    import hashlib

    from featuregen.overlay.catalog import CatalogObject
    from featuregen.overlay.catalog_changes import _type_fingerprint
    obj = CatalogObject("public.t.c", "column", "public", "t", "c", "numeric", None)
    assert _type_fingerprint(obj) == hashlib.sha256(b"column|numeric").hexdigest()


def test_domain_failure_does_not_discard_concepts(db, monkeypatch):
    # A domain enrichment blow-up must not null out concepts/definitions (spec C1). Stub
    # classify_domains to raise and assert the concept enrichment still reached the graph.
    from featuregen.overlay.upload import ingest as ing
    monkeypatch.setattr(ing, "classify_domains",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    captured: dict = {}
    real_build = ing.build_graph

    def spy(conn, src, rows, concepts, definitions, domains):
        captured.update(concepts=concepts, domains=domains)
        return real_build(conn, src, rows, concepts, definitions, domains)

    monkeypatch.setattr(ing, "build_graph", spy)
    rows = [CanonicalRow("deposits", "accounts", "balance", "numeric")]
    client = FakeLLM(script={
        "overlay.enrich.concept": FakeResponse(output={"concept": "monetary_stock"}),
        "overlay.enrich.definition": FakeResponse(output={"definition": "the balance"})})
    now = datetime(2026, 7, 5, tzinfo=UTC)
    ing.ingest_upload(db, "deposits", rows, actor=_actor(), now=now, client=client)
    assert captured["concepts"] and captured["domains"] is None   # concepts survived; only domains lost
