import pytest

from featuregen.contracts import Command
from featuregen.identity.build import build_human_identity
from featuregen.overlay.commands import (
    OverlayCommandError,
    get_task_proposal,
    propose_fact,
)
from featuregen.overlay.identity import CatalogObjectRef, display_object_ref, fact_key

ALICE = build_human_identity(subject="user:alice", role_claims=("data_owner",))
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
