from datetime import UTC, datetime, timedelta

import pytest

from featuregen.contracts import Command
from featuregen.identity.build import build_human_identity
from featuregen.overlay.commands import (
    OverlayCommandError,
    confirm_fact,
    get_task_proposal,
    propose_fact,
    reject_fact,
)
from featuregen.overlay.freshness import fire_due_overlay_expiries
from featuregen.overlay.identity import CatalogObjectRef, display_object_ref, fact_key
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact

ALICE = build_human_identity(subject="user:alice", role_claims=("data_owner",))
BOB = build_human_identity(subject="user:bob", role_claims=("data_owner",))
MALLORY = build_human_identity(subject="user:mallory", role_claims=("data_scientist",))


def _orders():
    return CatalogObjectRef("pg:core", "table", "sales", "orders")


def _propose_and_task(db):
    res = propose_fact(
        db,
        Command(
            "propose_fact",
            "overlay_fact",
            None,
            {
                "ref": _orders(),
                "fact_type": "grain",
                "proposed_value": {"columns": ["order_id"], "is_unique": True},
            },
            build_human_identity(subject="user:bob", role_claims=("data_owner",)),
            "p",
        ),
    )
    assert res.accepted
    draft = res.produced_event_ids[0]
    # No projection needed: get_task_proposal reads the CAS target from human_tasks and prior_value
    # from the event stream (finding 1) — both synchronous.
    key = fact_key(_orders(), "grain")
    row = db.execute(
        "SELECT task_id FROM human_tasks WHERE fact_key=%s AND status='open'", (key,)
    ).fetchone()
    return row[0], draft


def test_assignee_can_read_proposal(db, catalog):
    catalog.set_owner(_orders(), "user:alice")
    task_id, draft = _propose_and_task(db)
    out = get_task_proposal(db, task_id, ALICE)
    assert out["fact_type"] == "grain"
    assert out["object_ref"] == display_object_ref(_orders())
    assert out["proposed_value"] == {"columns": ["order_id"], "is_unique": True}
    assert out["use_case"] is None
    assert out["prior_value"] is None  # a fresh DRAFT has no prior value
    assert out["target_event_id"] == draft  # the CAS target for a fresh DRAFT is the draft event id
    assert out["evidence"] is None


def test_non_assignee_is_denied(db, catalog):
    catalog.set_owner(_orders(), "user:alice")
    task_id, _ = _propose_and_task(db)
    with pytest.raises(OverlayCommandError):
        get_task_proposal(db, task_id, MALLORY)


def _cmd(action, args, actor, key):
    return Command(action, "overlay_fact", None, args, actor, key)


def test_reproposed_after_rejected_has_no_stale_prior_value(db, catalog):
    """I1: PROPOSED->CONFIRMED->EXPIRED(REVERIFY)->REJECTED->re-PROPOSED. The retired VERIFIED value
    flows into prior_value on EXPIRED and is retained through REJECTED; the fresh re-proposal must
    reset it, so get_task_proposal shows NO stale prior_value to the confirming authority on the new
    DRAFT (the fold must mirror the projection's PROPOSED reset)."""
    catalog.set_owner(_orders(), "user:alice")
    key = fact_key(_orders(), "grain")
    value = {"columns": ["order_id"], "is_unique": True}

    # PROPOSED (bob) -> CONFIRMED (alice, owner) -> VERIFIED
    p = _cmd("propose_fact", {"ref": _orders(), "fact_type": "grain", "proposed_value": value}, BOB, "p1")
    draft = propose_fact(db, p).produced_event_ids[0]
    c = confirm_fact(db, _cmd("confirm_fact", {"ref": _orders(), "fact_type": "grain", "target_event_id": draft}, ALICE, "c1"))
    assert c.accepted, c.denied_reason
    confirmed_id = c.produced_event_ids[0]

    # fire the future-dated expiry timer -> REVERIFY (prior_value = the verified value)
    assert fire_due_overlay_expiries(db, now=datetime.now(UTC) + timedelta(days=200)) == 1
    assert fold_overlay_state(load_fact(db, key)).status == "REVERIFY"

    # REJECT under REVERIFY (CAS target is the confirmed_event_id) -> REJECTED, prior_value retained
    r = reject_fact(db, _cmd("reject_fact", {"ref": _orders(), "fact_type": "grain", "target_event_id": confirmed_id, "reason": "retire"}, ALICE, "r1"))
    assert r.accepted, r.denied_reason
    assert fold_overlay_state(load_fact(db, key)).prior_value == value  # retired value still folded

    # re-PROPOSE with a DIFFERENT fingerprint -> a fresh DRAFT
    rp = _cmd("propose_fact", {"ref": _orders(), "fact_type": "grain", "proposed_value": {"columns": ["order_id", "tenant"], "is_unique": True}}, BOB, "p2")
    assert propose_fact(db, rp).accepted

    # the fold (and thus get_task_proposal) must show NO stale prior value on the fresh DRAFT
    assert fold_overlay_state(load_fact(db, key)).prior_value is None
    task_id = db.execute(
        "SELECT task_id FROM human_tasks WHERE fact_key=%s AND status='open'", (key,)
    ).fetchone()[0]
    out = get_task_proposal(db, task_id, ALICE)
    assert out["prior_value"] is None
