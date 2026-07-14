from __future__ import annotations

from datetime import UTC, datetime

from tests.featuregen._helpers import mint_test_identity
from tests.featuregen.overlay.upload.test_bridge_candidates import _two_catalog_customer

from featuregen.contracts.envelopes import Command
from featuregen.overlay._lifecycle import _cas_target
from featuregen.overlay.commands import confirm_fact
from featuregen.overlay.identity import EntityBridgeRef, fact_key
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.bridge_candidates import derive_bridge_candidates
from featuregen.overlay.upload.bridge_projection import (
    active_bridges,
    demote_bridge_edges,
    project_verified_bridge,
)
from featuregen.overlay.upload.bridge_propose import propose_bridge
from featuregen.overlay.upload.enrich_llm import _ENRICH_ACTOR
from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter

_NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def _ref(db) -> EntityBridgeRef:
    cand = derive_bridge_candidates(db)[0]
    return EntityBridgeRef(cand.entity_id, cand.left_ref, cand.right_ref)


def _confirm(db, ref: EntityBridgeRef) -> None:
    key = fact_key(ref, "entity_bridge")
    admin = mint_test_identity(subject="user:admin1", role_claims=("platform-admin",))
    target = _cas_target(fold_overlay_state(load_fact(db, key)))
    res = confirm_fact(db, Command(
        "confirm_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "entity_bridge", "use_case": None, "target_event_id": target},
        admin, f"confirm-{target}"))
    assert res.accepted, res.denied_reason


def _propose_confirm(db) -> EntityBridgeRef:
    ensure_upload_catalog_adapter()
    _two_catalog_customer(db)
    ref = _ref(db)
    propose_bridge(db, derive_bridge_candidates(db)[0], actor=_ENRICH_ACTOR, now=_NOW)
    _confirm(db, ref)
    return ref


def test_single_confirm_verifies_a_bridge(db):
    ref = _propose_confirm(db)
    # single-confirmer: ONE platform-admin confirmation reaches VERIFIED
    assert fold_overlay_state(load_fact(db, fact_key(ref, "entity_bridge"))).status == "VERIFIED"


def test_project_verified_bridge_writes_the_edge(db):
    ref = _propose_confirm(db)
    assert project_verified_bridge(db, ref, now=_NOW) == "projected"
    row = db.execute(
        "SELECT entity_id, left_catalog_source, right_catalog_source, status FROM entity_bridge_edge "
        "WHERE fact_key = %s", (fact_key(ref, "entity_bridge"),)).fetchone()
    assert row == ("customer", "core", "crm", "VERIFIED")
    active = active_bridges(db)
    assert len(active) == 1 and active[0].entity_id == "customer"


def test_unverified_bridge_does_not_project(db):
    ensure_upload_catalog_adapter()
    _two_catalog_customer(db)
    ref = _ref(db)
    propose_bridge(db, derive_bridge_candidates(db)[0], actor=_ENRICH_ACTOR, now=_NOW)   # DRAFT only
    assert project_verified_bridge(db, ref, now=_NOW) == "pending"
    assert active_bridges(db) == ()


def test_demote_removes_a_projected_bridge(db):
    ref = _propose_confirm(db)
    project_verified_bridge(db, ref, now=_NOW)
    assert demote_bridge_edges(db, fact_key(ref, "entity_bridge")) == 1
    assert active_bridges(db) == ()


def test_bridge_events_skip_the_single_source_read_models(db):
    # Draining the GENERIC overlay projection over a bridge event must not halt it (a bridge ref has
    # no single catalog_source; pre-fix, _catalog_source KeyErrors and the fail-closed runner marks
    # the aggregate degraded and stops advancing), and must create no overlay_proposal/_state rows —
    # since 3B.3.0 the dependency index IS maintained (endpoint wiring for drift-staling).
    from featuregen.overlay.projection import OverlayProjection
    from featuregen.projections.runner import projection_lag, run_projection
    ensure_upload_catalog_adapter()
    _two_catalog_customer(db)
    key = propose_bridge(db, derive_bridge_candidates(db)[0], actor=_ENRICH_ACTOR, now=_NOW)
    while run_projection(db, OverlayProjection()) >= 500:
        pass
    # The projection must advance PAST the bridge event (no fail-closed halt, no degraded marker) …
    assert projection_lag(db, "overlay") == 0
    assert db.execute(
        "SELECT count(*) FROM projection_degraded WHERE projection_name = 'overlay'"
    ).fetchone()[0] == 0
    # … and leave no single-source read-model rows for the two-source bridge fact. The dependency
    # index is the 3B.3.0 exception: both endpoints (table + identifier column) are indexed there.
    assert db.execute("SELECT count(*) FROM overlay_proposal WHERE fact_key=%s", (key,)).fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM overlay_fact_dependency WHERE fact_key=%s", (key,)).fetchone()[0] == 4


def test_bridge_endpoints_land_in_the_dependency_index(db):
    from featuregen.overlay.projection import OverlayProjection, dependents_of
    from featuregen.projections.runner import run_projection
    ensure_upload_catalog_adapter()
    _two_catalog_customer(db)
    key = propose_bridge(db, derive_bridge_candidates(db)[0], actor=_ENRICH_ACTOR, now=_NOW)
    while run_projection(db, OverlayProjection()) >= 500:
        pass
    deps = set(db.execute(
        "SELECT catalog_source, ref_object FROM overlay_fact_dependency WHERE fact_key = %s",
        (key,)).fetchall())
    assert deps == {
        ("core", "public.customer_master"), ("crm", "public.customers"),
        ("core", "public.customer_master.customer_id"), ("crm", "public.customers.customer_id")}
    # the reverse index drift-staling reads finds the bridge from EITHER catalog side
    assert key in dependents_of(db, "core", "public.customer_master.customer_id")
    assert key in dependents_of(db, "crm", "public.customers.customer_id")
    # still NO single-source read-model rows for the two-source bridge
    assert db.execute("SELECT count(*) FROM overlay_proposal WHERE fact_key=%s", (key,)).fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM overlay_fact_state WHERE fact_key=%s", (key,)).fetchone()[0] == 0
    # Confirm-through-projection (3B.3.0 review): drain the bridge's CONFIRMED through the generic
    # overlay projection and PROVE it is a no-op here — the CONFIRMED branch re-derives dependencies
    # only from an overlay_proposal row, which a bridge never has, so the 4 endpoint rows survive
    # intact and still no single-source read-model rows appear.
    _confirm(db, _ref(db))
    assert fold_overlay_state(load_fact(db, key)).status == "VERIFIED"
    while run_projection(db, OverlayProjection()) >= 500:
        pass
    assert db.execute("SELECT count(*) FROM overlay_fact_dependency WHERE fact_key=%s", (key,)).fetchone()[0] == 4
    assert db.execute("SELECT count(*) FROM overlay_proposal WHERE fact_key=%s", (key,)).fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM overlay_fact_state WHERE fact_key=%s", (key,)).fetchone()[0] == 0


def test_catalog_drift_stales_a_verified_bridge(db):
    # END-TO-END 3B.3.0: a catalog change on a bridged catalog stales the VERIFIED bridge through the
    # REAL drift machinery — detect_catalog_changes diffs the re-uploaded catalog against the
    # overlay_catalog_object snapshot, walks dependents_of over the 3B.3.0 dependency index, and
    # _stale_one appends OVERLAY_FACT_STALED. Driven exactly as the upload ingest does (ingest.py:
    # detect_catalog_changes(conn, UploadCatalog(...), open_reverify=False)); no hand-appended STALED.
    from featuregen.overlay.catalog_changes import detect_catalog_changes
    from featuregen.overlay.projection import OverlayProjection
    from featuregen.overlay.upload.canonical import CanonicalRow
    from featuregen.overlay.upload.upload_catalog import UploadCatalog
    from featuregen.projections.runner import projection_lag, run_projection

    ref = _propose_confirm(db)                       # propose + single-confirm -> VERIFIED
    key = fact_key(ref, "entity_bridge")
    while run_projection(db, OverlayProjection()) >= 500:   # index the bridge's endpoints (3B.3.0)
        pass
    assert project_verified_bridge(db, ref, now=_NOW) == "projected"

    # Establish the drift baseline for the 'core' catalog (the first scan diffs against an empty
    # snapshot -> adds only, stales nothing).
    rows_v1 = [CanonicalRow("core", "customer_master", "customer_id", "integer", is_grain=True),
               CanonicalRow("core", "customer_master", "segment", "text")]
    detect_catalog_changes(db, UploadCatalog("core", rows_v1), actor=_ENRICH_ACTOR,
                           now=_NOW, open_reverify=False)
    assert fold_overlay_state(load_fact(db, key)).status == "VERIFIED"

    # Re-upload DROPS the bridged endpoint column customer_master.customer_id -> the drift scan must
    # find the bridge via its dependency-index endpoint and STALE it.
    rows_v2 = [CanonicalRow("core", "customer_master", "segment", "text")]
    changes = detect_catalog_changes(db, UploadCatalog("core", rows_v2), actor=_ENRICH_ACTOR,
                                     now=_NOW, open_reverify=False)
    assert any(c.kind == "drop" and c.object_ref == "public.customer_master.customer_id"
               for c in changes)
    # the bridge is STALED end-to-end (fold status flips off VERIFIED) …
    assert fold_overlay_state(load_fact(db, key)).status == "STALE"
    # … the generic projection drains the bridge's STALED without halting …
    while run_projection(db, OverlayProjection()) >= 500:
        pass
    assert projection_lag(db, "overlay") == 0
    # … and the bridge projection reflects it: the edge is demoted on the next project.
    assert project_verified_bridge(db, ref, now=_NOW) == "pending"
    assert active_bridges(db) == ()
