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


def _propose_confirm(db) -> EntityBridgeRef:
    ensure_upload_catalog_adapter()
    _two_catalog_customer(db)
    ref = _ref(db)
    propose_bridge(db, derive_bridge_candidates(db)[0], actor=_ENRICH_ACTOR, now=_NOW)
    key = fact_key(ref, "entity_bridge")
    admin = mint_test_identity(subject="user:admin1", role_claims=("platform-admin",))
    target = _cas_target(fold_overlay_state(load_fact(db, key)))
    res = confirm_fact(db, Command(
        "confirm_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "entity_bridge", "use_case": None, "target_event_id": target},
        admin, f"confirm-{target}"))
    assert res.accepted, res.denied_reason
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


def test_bridge_events_are_skipped_by_the_overlay_projection(db):
    # Draining the GENERIC overlay projection over a bridge event must not halt it (a bridge ref has
    # no single catalog_source; pre-fix, _catalog_source KeyErrors and the fail-closed runner marks
    # the aggregate degraded and stops advancing), and must create no overlay read-model rows —
    # bridge drift/expire integration is 3B.3.
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
    # … and leave no single-source read-model rows for the two-source bridge fact.
    assert db.execute("SELECT count(*) FROM overlay_proposal WHERE fact_key=%s", (key,)).fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM overlay_fact_dependency WHERE fact_key=%s", (key,)).fetchone()[0] == 0
