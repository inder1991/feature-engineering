"""Task 4.2 — `propose_fact` (validation + replacement semantics + per-side human-gate tasks).

SCOPE NOTE: `confirm_fact`/`reject_fact` land in Task 4.3, so the two replacement-semantics tests
seed the prior VERIFIED / REJECTED state by appending `OVERLAY_FACT_CONFIRMED` /
`OVERLAY_FACT_REJECTED` directly (the same events those handlers will emit). This exercises
`propose_fact`'s decision-6 denial + REJECTED-stickiness in isolation, without coupling Task 4.2 to
unbuilt handlers.
"""
import pytest
from psycopg.rows import dict_row
from tests.featuregen._helpers import mint_test_identity

from featuregen.contracts import Command, ConcurrencyError
from featuregen.overlay.commands import propose_fact
from featuregen.overlay.identity import (
    ApprovedJoinRef,
    CatalogObjectRef,
    ColumnPair,
    fact_key,
    proposal_fingerprint,
)
from featuregen.overlay.store import append_overlay_event


def _orders() -> CatalogObjectRef:
    return CatalogObjectRef("pg:core", "table", "sales", "orders")


def _propose_cmd(*, ref, fact_type, value, use_case=None, actor=None, key="k1"):
    actor = actor or mint_test_identity(subject="user:alice", role_claims=("data_owner",))
    args = {"ref": ref, "fact_type": fact_type, "proposed_value": value}
    if use_case is not None:
        args["use_case"] = use_case
    return Command(
        action="propose_fact",
        aggregate="overlay_fact",
        aggregate_id=None,
        args=args,
        actor=actor,
        idempotency_key=key,
    )


def test_propose_creates_draft_and_data_owner_task(db, catalog):
    catalog.set_owner(_orders(), "user:alice")
    cmd = _propose_cmd(
        ref=_orders(), fact_type="grain", value={"columns": ["order_id"], "is_unique": True}
    )
    res = propose_fact(db, cmd)
    assert res.accepted is True
    key = fact_key(_orders(), "grain")
    assert res.aggregate_id == key
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT type FROM events WHERE overlay_fact_id=%s ORDER BY stream_version", (key,)
        )
        assert [r["type"] for r in cur.fetchall()] == ["OVERLAY_FACT_PROPOSED"]
        cur.execute(
            "SELECT gate, eligible_assignees FROM human_tasks WHERE fact_key=%s AND status='open'",
            (key,),
        )
        row = cur.fetchone()
        assert row["gate"] == "OVERLAY_DATA_OWNER"
        assert row["eligible_assignees"]["subject"] == "user:alice"


def test_duplicate_fingerprint_is_denied(db, catalog):
    catalog.set_owner(_orders(), "user:alice")
    value = {"columns": ["order_id"], "is_unique": True}
    assert propose_fact(
        db, _propose_cmd(ref=_orders(), fact_type="grain", value=value, key="k1")
    ).accepted
    dup = propose_fact(db, _propose_cmd(ref=_orders(), fact_type="grain", value=value, key="k2"))
    assert dup.accepted is False
    assert "duplicate" in dup.denied_reason


def test_policy_tag_opens_compliance_task(db, catalog):
    cmd = _propose_cmd(
        ref=_orders(),
        fact_type="policy_tag",
        value={"decision": "restricted", "basis": "PII review 2026-06"},
        use_case="marketing",
    )
    res = propose_fact(db, cmd)
    assert res.accepted is True
    key = fact_key(_orders(), "policy_tag", "marketing")
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT gate, eligible_assignees FROM human_tasks WHERE fact_key=%s", (key,))
        row = cur.fetchone()
        assert row["gate"] == "OVERLAY_COMPLIANCE"
        assert row["eligible_assignees"] == {"role": "compliance"}


def test_propose_on_verified_fact_is_denied(db, catalog):
    """Replacement semantics (decision 6): a VERIFIED fact stays usable until its own re-verify
    flow replaces it — a fresh proposal must NOT regress it to DRAFT. (Task 4.3 emits the
    confirmation; here we seed OVERLAY_FACT_CONFIRMED directly to reach VERIFIED.)"""
    catalog.set_owner(_orders(), "user:alice")
    bob = mint_test_identity(subject="user:bob", role_claims=("data_owner",))
    value = {"columns": ["order_id"], "is_unique": True}
    p = propose_fact(
        db, _propose_cmd(ref=_orders(), fact_type="grain", value=value, actor=bob, key="p1")
    )
    assert p.accepted
    draft = p.produced_event_ids[0]
    key = fact_key(_orders(), "grain")
    alice = mint_test_identity(subject="user:alice", role_claims=("data_owner",))
    append_overlay_event(
        db,
        fact_key=key,
        type="OVERLAY_FACT_CONFIRMED",
        payload={
            "value": value,
            "confirmers": [{"subject": "user:alice", "role": "data_owner"}],
            "confirms_event_id": draft,
        },
        actor=alice,
    )  # now VERIFIED
    again = propose_fact(
        db,
        _propose_cmd(
            ref=_orders(),
            fact_type="grain",
            value={"columns": ["order_id", "tenant"], "is_unique": True},
            key="p2",
        ),
    )
    assert again.accepted is False
    assert "non-terminal" in again.denied_reason


def test_repropose_after_reject_requires_new_fingerprint(db, catalog):
    """After REJECTED, the same fingerprint is sticky-denied; a DIFFERENT fingerprint is allowed.
    (Task 4.3 emits the rejection; here we seed OVERLAY_FACT_REJECTED directly.)"""
    catalog.set_owner(_orders(), "user:alice")
    bob = mint_test_identity(subject="user:bob", role_claims=("data_owner",))
    value = {"columns": ["order_id"], "is_unique": True}
    p = propose_fact(
        db, _propose_cmd(ref=_orders(), fact_type="grain", value=value, actor=bob, key="p1")
    )
    assert p.accepted
    draft = p.produced_event_ids[0]
    key = fact_key(_orders(), "grain")
    alice = mint_test_identity(subject="user:alice", role_claims=("data_owner",))
    append_overlay_event(
        db,
        fact_key=key,
        type="OVERLAY_FACT_REJECTED",
        payload={
            "rejected_by": "user:alice",
            "reason": "wrong key",
            "target_event_id": draft,
            "retired_fingerprint": proposal_fingerprint(value),
        },
        actor=alice,
    )  # now REJECTED
    same = propose_fact(
        db, _propose_cmd(ref=_orders(), fact_type="grain", value=value, key="p2")
    )
    assert same.accepted is False
    assert "rejected" in same.denied_reason
    diff = propose_fact(
        db,
        _propose_cmd(
            ref=_orders(),
            fact_type="grain",
            value={"columns": ["order_id", "tenant"], "is_unique": True},
            key="p3",
        ),
    )
    assert diff.accepted is True


def test_mixed_owner_join_opens_owner_and_governance_tasks(db, catalog):
    """approved_join with one known owner + one unknown owner opens TWO tasks: one for the known
    owner and one routed to the platform-admin/governance queue (decision 7) — the known owner is
    never folded onto the governance task."""
    a = _orders()
    b = CatalogObjectRef("pg:core", "table", "sales", "customers")
    catalog.set_owner(a, "user:alice")  # b's owner is unknown
    ref = ApprovedJoinRef(a, b, (ColumnPair("customer_id", "id"),), "N:1")
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
    res = propose_fact(db, _propose_cmd(ref=ref, fact_type="approved_join", value=value, key="j1"))
    assert res.accepted is True, res.denied_reason
    key = fact_key(ref, "approved_join")
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT eligible_assignees FROM human_tasks WHERE fact_key=%s AND status='open'",
            (key,),
        )
        rows = [r["eligible_assignees"] for r in cur.fetchall()]
    assert sorted(r["role"] for r in rows) == ["data_owner", "platform-admin"]
    owner_task = next(r for r in rows if r["role"] == "data_owner")
    assert owner_task["subject"] == "user:alice"
    gov_task = next(r for r in rows if r["role"] == "platform-admin")
    assert "subject" not in gov_task  # NOT collapsed to the known owner


# --- I4: re-propose-after-REJECTED append is CAS-pinned to the observed head -------------------


def _reach_rejected(db, value):
    """Drive `orders`/grain to REJECTED (PROPOSED then a direct OVERLAY_FACT_REJECTED seed, the
    same event reject_fact emits) and return the fact_key. Stream is at stream_version 2."""
    bob = mint_test_identity(subject="user:bob", role_claims=("data_owner",))
    p = propose_fact(
        db, _propose_cmd(ref=_orders(), fact_type="grain", value=value, actor=bob, key="p1")
    )
    assert p.accepted
    draft = p.produced_event_ids[0]
    key = fact_key(_orders(), "grain")
    alice = mint_test_identity(subject="user:alice", role_claims=("data_owner",))
    append_overlay_event(
        db,
        fact_key=key,
        type="OVERLAY_FACT_REJECTED",
        payload={
            "rejected_by": "user:alice",
            "reason": "wrong key",
            "target_event_id": draft,
            "retired_fingerprint": proposal_fingerprint(value),
        },
        actor=alice,
    )  # now REJECTED at stream_version 2
    return key


def test_repropose_after_rejected_pins_expected_version(db, catalog, occ_spy):
    """I4: the re-propose-after-REJECTED path (the only non-fresh propose that proceeds) must pin
    its OVERLAY_FACT_PROPOSED append to the observed head (stream_version 2: PROPOSED + REJECTED),
    NOT expected_version=None. With None, two concurrent re-proposals both append -> duplicate
    DRAFT + duplicate human-gate task sets + fold/projection divergence."""
    catalog.set_owner(_orders(), "user:alice")
    _reach_rejected(db, {"columns": ["order_id"], "is_unique": True})
    diff = propose_fact(
        db,
        _propose_cmd(
            ref=_orders(),
            fact_type="grain",
            value={"columns": ["order_id", "tenant"], "is_unique": True},
            key="p3",
        ),
    )
    assert diff.accepted is True
    assert occ_spy["OVERLAY_FACT_PROPOSED"] == 2  # pinned to the observed head & non-None


def test_concurrent_repropose_after_rejected_collides(db, catalog, inject_concurrent_append):
    """I4 (genuine concurrency): with the fact REJECTED, a concurrent re-propose lands a fresh
    OVERLAY_FACT_PROPOSED out-of-band between this handler's load and its own append. Pinned to the
    observed head, the handler's append must raise ConcurrencyError rather than append a duplicate
    DRAFT."""
    catalog.set_owner(_orders(), "user:alice")
    _reach_rejected(db, {"columns": ["order_id"], "is_unique": True})
    inject_concurrent_append("OVERLAY_FACT_PROPOSED")
    with pytest.raises(ConcurrencyError):
        propose_fact(
            db,
            _propose_cmd(
                ref=_orders(),
                fact_type="grain",
                value={"columns": ["order_id", "tenant"], "is_unique": True},
                key="p3",
            ),
        )
