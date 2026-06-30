from featuregen.contracts import Command
from featuregen.identity.build import build_human_identity, build_service_identity
from featuregen.overlay.commands import enter_fact
from featuregen.overlay.identity import ApprovedJoinRef, CatalogObjectRef, ColumnPair, fact_key
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact

ALICE = build_human_identity(subject="user:alice", role_claims=("data_owner",))
SVC = build_service_identity(subject="service:profiler", role_claims=("overlay",), attestation="sig")


def _orders() -> CatalogObjectRef:
    return CatalogObjectRef("pg:core", "table", "sales", "orders")


def _customers() -> CatalogObjectRef:
    return CatalogObjectRef("pg:core", "table", "sales", "customers")


def _enter(*, ref, fact_type, value, use_case=None, actor=ALICE, key="e"):
    args = {"ref": ref, "fact_type": fact_type, "proposed_value": value}
    if use_case is not None:
        args["use_case"] = use_case
    return Command("enter_fact", "overlay_fact", None, args, actor, key)


def test_owner_direct_enters_grain_to_verified(db, catalog):
    catalog.set_owner(_orders(), "user:alice")
    res = enter_fact(
        db, _enter(ref=_orders(), fact_type="grain", value={"columns": ["order_id"], "is_unique": True})
    )
    assert res.accepted is True
    assert len(res.produced_event_ids) == 2  # PROPOSED + CONFIRMED
    stream = load_fact(db, fact_key(_orders(), "grain"))
    assert fold_overlay_state(stream).status == "VERIFIED"
    confirmed = next(e for e in stream if e.type == "OVERLAY_FACT_CONFIRMED")
    assert confirmed.payload["confirmers"][0]["subject"] == "user:alice"


def test_service_cannot_self_confirm(db, catalog):
    catalog.set_owner(_orders(), "service:profiler")
    res = enter_fact(
        db,
        _enter(
            ref=_orders(),
            fact_type="grain",
            value={"columns": ["order_id"], "is_unique": True},
            actor=SVC,
        ),
    )
    assert res.accepted is False
    assert "human" in res.denied_reason


def test_dual_owner_join_direct_entry_rejected(db, catalog):
    catalog.set_owner(_orders(), "user:alice")
    catalog.set_owner(_customers(), "user:bob")  # two distinct owners → dual
    ref = ApprovedJoinRef(_orders(), _customers(), (ColumnPair("customer_id", "id"),), "N:1")
    value = {
        "from_ref": {
            "catalog_source": "pg:core",
            "object_kind": "table",
            "schema": "sales",
            "table": "orders",
            "column": None,
        },
        "to_ref": {
            "catalog_source": "pg:core",
            "object_kind": "table",
            "schema": "sales",
            "table": "customers",
            "column": None,
        },
        "column_pairs": [{"from_col": "customer_id", "to_col": "id"}],
        "cardinality": "N:1",
    }
    res = enter_fact(db, _enter(ref=ref, fact_type="approved_join", value=value))
    assert res.accepted is False
    assert "dual-owner" in res.denied_reason


def test_same_owner_join_direct_entry_to_verified(db, catalog):
    # One principal owns BOTH sides → Authority.dual is False, so the single both-roles direct-entry
    # path runs and folds the join to VERIFIED (finding 4 / decision 7).
    catalog.set_owner(_orders(), "user:alice")
    catalog.set_owner(_customers(), "user:alice")  # same owner on both sides → not dual
    ref = ApprovedJoinRef(_orders(), _customers(), (ColumnPair("customer_id", "id"),), "N:1")
    value = {
        "from_ref": {
            "catalog_source": "pg:core",
            "object_kind": "table",
            "schema": "sales",
            "table": "orders",
            "column": None,
        },
        "to_ref": {
            "catalog_source": "pg:core",
            "object_kind": "table",
            "schema": "sales",
            "table": "customers",
            "column": None,
        },
        "column_pairs": [{"from_col": "customer_id", "to_col": "id"}],
        "cardinality": "N:1",
    }
    res = enter_fact(db, _enter(ref=ref, fact_type="approved_join", value=value))
    assert res.accepted is True
    assert len(res.produced_event_ids) == 2  # PROPOSED + CONFIRMED
    stream = load_fact(db, fact_key(ref, "approved_join"))
    assert fold_overlay_state(stream).status == "VERIFIED"
    confirmed = next(e for e in stream if e.type == "OVERLAY_FACT_CONFIRMED")
    confirmers = confirmed.payload["confirmers"]
    # The same principal is recorded under BOTH side roles so audit attribution matches a
    # two-owner join (finding 4 / decision 7).
    assert {"subject": "user:alice", "role": "data_owner_from"} in confirmers
    assert {"subject": "user:alice", "role": "data_owner_to"} in confirmers
