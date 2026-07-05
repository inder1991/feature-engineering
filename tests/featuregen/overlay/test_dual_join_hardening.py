from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.featuregen._helpers import mint_test_identity
from tests.featuregen.overlay._helpers import StubCatalog

from featuregen.contracts import Command
from featuregen.overlay import facts
from featuregen.overlay.catalog import CatalogObject, register_catalog_adapter
from featuregen.overlay.commands import confirm_fact, propose_fact
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.identity import ApprovedJoinRef, CatalogObjectRef, ColumnPair, fact_key
from featuregen.overlay.projection import OverlayProjection
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import append_overlay_event, load_fact
from featuregen.projections.runner import run_projection

EVE = mint_test_identity(subject="user:eve", role_claims=("data_owner",))
ALICE = mint_test_identity(subject="user:alice", role_claims=("data_owner",))
BOB = mint_test_identity(subject="user:bob", role_claims=("data_owner",))

ORDERS = CatalogObjectRef("pg:core", "table", "sales", "orders")
CUSTOMERS = CatalogObjectRef("pg:core", "table", "sales", "customers")
REF = ApprovedJoinRef(ORDERS, CUSTOMERS, (ColumnPair("customer_id", "id"),), "N:1")
KEY = fact_key(REF, "approved_join")
VALUE = {
    "from_ref": {"catalog_source": "pg:core", "object_kind": "table", "schema": "sales",
                 "table": "orders", "column": None},
    "to_ref": {"catalog_source": "pg:core", "object_kind": "table", "schema": "sales",
               "table": "customers", "column": None},
    "column_pairs": [{"from_col": "customer_id", "to_col": "id"}],
    "cardinality": "N:1",
}


def _config(**over):
    base = dict(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.0, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(minutes=60),
        profiler_require_restricted_role=False,
    )
    base.update(over)
    register_overlay_config(OverlayConfig(**base))


_JOIN_OBJECTS = [
    CatalogObject("sales.orders", "table", "sales", "orders", None, None, "1"),
    CatalogObject("sales.customers", "table", "sales", "customers", None, None, "2"),
    CatalogObject("sales.orders.customer_id", "column", "sales", "orders", "customer_id",
                  "bigint", "1:1"),
    CatalogObject("sales.customers.id", "column", "sales", "customers", "id", "bigint", "2:1"),
]


def _adapter(*, objects=None):
    # Default to the join's referents present (a real join's tables exist); tests that need a
    # dropped referent pass objects=[] to re-register an empty catalog.
    cat = StubCatalog(
        objects=list(_JOIN_OBJECTS if objects is None else objects), catalog_source="pg:core"
    )
    cat.set_owner(ORDERS, "user:alice")
    cat.set_owner(CUSTOMERS, "user:bob")
    register_catalog_adapter(cat)
    return cat


def _confirm(db, actor, target, key):
    return confirm_fact(db, Command(
        "confirm_fact", "overlay_fact", None,
        {"ref": REF, "fact_type": "approved_join", "target_event_id": target}, actor, key,
    ))


def _verify_dual(db):
    draft = propose_fact(db, Command(
        "propose_fact", "overlay_fact", None,
        {"ref": REF, "fact_type": "approved_join", "proposed_value": VALUE}, EVE, "p",
    )).produced_event_ids[0]
    _confirm(db, ALICE, draft, "c1")
    r = _confirm(db, BOB, draft, "c2")
    assert r.accepted, r.denied_reason
    return draft


def test_dual_join_uses_configurable_ttl(db):
    # MAJOR #6: the dual-owner join path honored a hardcoded 180d, ignoring OverlayConfig.
    _config(ttl_by_fact_type={"approved_join": timedelta(days=90)})
    _adapter()
    _verify_dual(db)
    confirmed = next(e for e in load_fact(db, KEY) if e.type == "OVERLAY_FACT_CONFIRMED")
    exp = datetime.fromisoformat(confirmed.payload["expires_at"])
    horizon = exp - datetime.now(UTC)
    assert timedelta(days=88) < horizon < timedelta(days=92)  # ~90d config, not 180d


def test_dual_join_stale_reconfirm_blocked_when_referent_missing(db):
    # BLOCKER #2: a drift-STALEd dual join must not re-VERIFY while a referent is gone.
    _config()
    _adapter()  # referents present for the initial verify
    _verify_dual(db)
    confirmed = next(e for e in load_fact(db, KEY) if e.type == "OVERLAY_FACT_CONFIRMED")
    append_overlay_event(
        db, fact_key=KEY, type=facts.OVERLAY_FACT_STALED, actor=EVE,
        payload={"catalog_change_ref": "drop:sales.customers",
                 "stales_confirmed_event_id": confirmed.event_id},
    )
    run_projection(db, OverlayProjection())
    _adapter(objects=[])  # the referent is now DROPPED -> re-register an empty catalog
    blocked = _confirm(db, ALICE, confirmed.event_id, "reconf")  # first STALE re-confirm
    assert not blocked.accepted
    assert "stale re-confirm blocked" in (blocked.denied_reason or "")


def test_dual_join_stale_reconfirm_allowed_when_referents_present(db):
    _config()
    _adapter()  # referents present throughout
    _verify_dual(db)
    confirmed = next(e for e in load_fact(db, KEY) if e.type == "OVERLAY_FACT_CONFIRMED")
    append_overlay_event(
        db, fact_key=KEY, type=facts.OVERLAY_FACT_STALED, actor=EVE,
        payload={"catalog_change_ref": "typecheck", "stales_confirmed_event_id": confirmed.event_id},
    )
    run_projection(db, OverlayProjection())
    ok = _confirm(db, ALICE, confirmed.event_id, "reconf")  # referents present -> proceeds (partial)
    assert ok.accepted, ok.denied_reason


def test_verified_dual_join_cannot_renew_in_place(db):
    # MAJOR #6: a within-grace VERIFIED dual join must NOT regress to PARTIALLY_CONFIRMED.
    _config()
    _adapter()
    exp = (datetime.now(UTC) + timedelta(days=2)).isoformat()  # within the 14d grace window
    draft = append_overlay_event(
        db, fact_key=KEY, type=facts.OVERLAY_FACT_PROPOSED, actor=EVE, expected_version=0,
        payload={"catalog_object_ref": VALUE, "object_ref": "sales.orders -> sales.customers",
                 "fact_type": "approved_join", "proposed_value": VALUE,
                 "proposal_fingerprint": "fp", "proposed_by": "user:eve"},
    )
    append_overlay_event(
        db, fact_key=KEY, type=facts.OVERLAY_FACT_PARTIALLY_CONFIRMED, actor=ALICE,
        payload={"by_owner": "user:alice", "role": "data_owner_from", "draft_event_id": draft.event_id},
    )
    confirmed = append_overlay_event(
        db, fact_key=KEY, type=facts.OVERLAY_FACT_CONFIRMED, actor=BOB,
        payload={"value": VALUE, "confirmers": [{"subject": "user:alice", "role": "data_owner_from"},
                                                {"subject": "user:bob", "role": "data_owner_to"}],
                 "expires_at": exp, "confirms_event_id": draft.event_id},
    )
    run_projection(db, OverlayProjection())
    assert fold_overlay_state(load_fact(db, KEY)).status == "VERIFIED"

    denied = _confirm(db, BOB, confirmed.event_id, "renew")
    assert not denied.accepted
    assert "cannot renew in place" in (denied.denied_reason or "")


def test_dual_join_referent_vanishing_between_confirms_is_blocked(db):
    # SP-1.5 re-review #2: a referent dropped BETWEEN the two owners' re-confirms must block the
    # second (VERIFY-producing) confirm — the first-partial gate alone is not enough.
    _config()
    _adapter()  # referents present for the initial verify + first re-confirm
    _verify_dual(db)
    confirmed = next(e for e in load_fact(db, KEY) if e.type == "OVERLAY_FACT_CONFIRMED")
    append_overlay_event(
        db, fact_key=KEY, type=facts.OVERLAY_FACT_STALED, actor=EVE,
        payload={"catalog_change_ref": "typecheck", "stales_confirmed_event_id": confirmed.event_id},
    )
    run_projection(db, OverlayProjection())

    r1 = _confirm(db, ALICE, confirmed.event_id, "re1")  # first re-confirm (referents present)
    assert r1.accepted, r1.denied_reason
    _adapter(objects=[])  # a referent is DROPPED between the two owners' confirms
    r2 = _confirm(db, BOB, confirmed.event_id, "re2")  # second (VERIFY) confirm must be blocked
    assert not r2.accepted
    assert "join re-confirm blocked" in (r2.denied_reason or "")
