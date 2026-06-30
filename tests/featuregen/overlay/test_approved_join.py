from featuregen.contracts import Command
from featuregen.identity.build import build_human_identity
from featuregen.overlay.commands import confirm_fact, propose_fact, reject_fact
from featuregen.overlay.identity import ApprovedJoinRef, CatalogObjectRef, ColumnPair, fact_key
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact

ALICE = build_human_identity(subject="user:alice", role_claims=("data_owner",))
BOB = build_human_identity(subject="user:bob", role_claims=("data_owner",))
EVE = build_human_identity(subject="user:eve", role_claims=("data_owner",))
ADMIN = build_human_identity(subject="user:admin", role_claims=("platform-admin",))
ADMIN2 = build_human_identity(subject="user:admin2", role_claims=("platform-admin",))


def _orders():
    return CatalogObjectRef("pg:core", "table", "sales", "orders")


def _customers():
    return CatalogObjectRef("pg:core", "table", "sales", "customers")


def _ref():
    return ApprovedJoinRef(_orders(), _customers(), (ColumnPair("customer_id", "id"),), "N:1")


def _value():
    return {
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


def _propose(db):
    res = propose_fact(
        db,
        Command(
            "propose_fact",
            "overlay_fact",
            None,
            {"ref": _ref(), "fact_type": "approved_join", "proposed_value": _value()},
            EVE,  # proposer distinct from both owners
            "p",
        ),
    )
    assert res.accepted, res.denied_reason
    return res.produced_event_ids[0]


def _confirm(db, *, target, actor, key):
    return confirm_fact(
        db,
        Command(
            "confirm_fact",
            "overlay_fact",
            None,
            {"ref": _ref(), "fact_type": "approved_join", "target_event_id": target},
            actor,
            key,
        ),
    )


def test_two_step_verify_records_both_approvers(db, catalog):
    catalog.set_owner(_orders(), "user:alice")
    catalog.set_owner(_customers(), "user:bob")
    draft = _propose(db)
    key = fact_key(_ref(), "approved_join")

    first = _confirm(db, target=draft, actor=ALICE, key="c1")
    assert first.accepted is True
    assert fold_overlay_state(load_fact(db, key)).status == "PARTIALLY_CONFIRMED"

    second = _confirm(db, target=draft, actor=BOB, key="c2")
    assert second.accepted is True
    stream = load_fact(db, key)
    assert fold_overlay_state(stream).status == "VERIFIED"
    confirmed = next(e for e in stream if e.type == "OVERLAY_FACT_CONFIRMED")
    subjects = {c["subject"] for c in confirmed.payload["confirmers"]}
    assert subjects == {"user:alice", "user:bob"}


def test_one_confirm_is_insufficient(db, catalog):
    catalog.set_owner(_orders(), "user:alice")
    catalog.set_owner(_customers(), "user:bob")
    draft = _propose(db)
    _confirm(db, target=draft, actor=ALICE, key="c1")
    # same owner trying to also satisfy the second side
    again = _confirm(db, target=draft, actor=ALICE, key="c2")
    assert again.accepted is False
    assert "other owner" in again.denied_reason


def test_same_owner_both_sides_single_confirm_verifies(db, catalog):
    catalog.set_owner(_orders(), "user:alice")
    catalog.set_owner(_customers(), "user:alice")
    draft = _propose(db)
    res = _confirm(db, target=draft, actor=ALICE, key="c1")
    assert res.accepted is True
    assert fold_overlay_state(load_fact(db, fact_key(_ref(), "approved_join"))).status == "VERIFIED"


def test_either_owner_reject_marks_rejected(db, catalog):
    catalog.set_owner(_orders(), "user:alice")
    catalog.set_owner(_customers(), "user:bob")
    draft = _propose(db)
    res = reject_fact(
        db,
        Command(
            "reject_fact",
            "overlay_fact",
            None,
            {"ref": _ref(), "fact_type": "approved_join", "target_event_id": draft, "reason": "no"},
            BOB,
            "r",
        ),
    )
    assert res.accepted is True
    assert fold_overlay_state(load_fact(db, fact_key(_ref(), "approved_join"))).status == "REJECTED"


def test_four_eyes_proposer_cannot_confirm(db, catalog):
    # EVE proposes; making EVE also an owner must not let her confirm her own join (§6.5).
    catalog.set_owner(_orders(), "user:eve")
    catalog.set_owner(_customers(), "user:bob")
    draft = _propose(db)
    res = _confirm(db, target=draft, actor=EVE, key="c1")
    assert res.accepted is False
    assert "proposer" in res.denied_reason


def test_mixed_known_and_governance_owner_plus_admin_verifies(db, catalog):
    # One known owner (alice) + one unknown/governance side -> the known owner and a
    # platform-admin together verify (decision 7).
    catalog.set_owner(_orders(), "user:alice")  # to_ref (customers) owner is unknown
    draft = _propose(db)
    key = fact_key(_ref(), "approved_join")

    first = _confirm(db, target=draft, actor=ALICE, key="c1")
    assert first.accepted is True
    assert fold_overlay_state(load_fact(db, key)).status == "PARTIALLY_CONFIRMED"

    second = _confirm(db, target=draft, actor=ADMIN, key="c2")
    assert second.accepted is True
    stream = load_fact(db, key)
    assert fold_overlay_state(stream).status == "VERIFIED"
    confirmed = next(e for e in stream if e.type == "OVERLAY_FACT_CONFIRMED")
    subjects = {c["subject"] for c in confirmed.payload["confirmers"]}
    assert subjects == {"user:alice", "user:admin"}


def test_mixed_two_admins_cannot_bypass_known_owner(db, catalog):
    # The known owner's side must be confirmed by the owner: two platform-admins must NOT verify
    # a join that has a known owner (side-coverage guard, finding 3).
    catalog.set_owner(_orders(), "user:alice")  # customers owner unknown
    draft = _propose(db)
    key = fact_key(_ref(), "approved_join")

    first = _confirm(db, target=draft, actor=ADMIN, key="c1")
    assert first.accepted is True
    second = _confirm(db, target=draft, actor=ADMIN2, key="c2")
    assert second.accepted is False
    assert "known owner" in second.denied_reason
    assert fold_overlay_state(load_fact(db, key)).status == "PARTIALLY_CONFIRMED"
