from psycopg.rows import dict_row

from featuregen.contracts import Command
from featuregen.identity.build import build_human_identity
from featuregen.overlay.commands import confirm_fact, propose_fact, reject_fact
from featuregen.overlay.identity import CatalogObjectRef, fact_key
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact

ALICE = build_human_identity(subject="user:alice", role_claims=("data_owner",))
BOB = build_human_identity(subject="user:bob", role_claims=("data_owner",))
COMPLIANCE = build_human_identity(subject="user:carol", role_claims=("compliance",))
ADMIN = build_human_identity(subject="user:admin", role_claims=("platform-admin",))


def _orders() -> CatalogObjectRef:
    return CatalogObjectRef("pg:core", "table", "sales", "orders")


def _propose(db, *, fact_type="grain", value=None, use_case=None, actor=BOB, key="p"):
    value = value or {"columns": ["order_id"], "is_unique": True}
    args = {"ref": _orders(), "fact_type": fact_type, "proposed_value": value}
    if use_case is not None:
        args["use_case"] = use_case
    res = propose_fact(
        db,
        Command("propose_fact", "overlay_fact", None, args, actor, key),
    )
    assert res.accepted, res.denied_reason
    return res.produced_event_ids[0]  # the DRAFT (target) event id


def _confirm_cmd(*, fact_type="grain", use_case=None, target, actor=ALICE, key="c"):
    args = {"ref": _orders(), "fact_type": fact_type, "target_event_id": target}
    if use_case is not None:
        args["use_case"] = use_case
    return Command("confirm_fact", "overlay_fact", None, args, actor, key)


def test_owner_confirms_draft_to_verified(db, catalog):
    catalog.set_owner(_orders(), "user:alice")
    draft = _propose(db)  # proposed by BOB (four-eyes ok)
    res = confirm_fact(db, _confirm_cmd(target=draft))
    assert res.accepted is True
    key = fact_key(_orders(), "grain")
    assert fold_overlay_state(load_fact(db, key)).status == "VERIFIED"
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT status FROM human_tasks WHERE fact_key=%s", (key,))
        assert cur.fetchone()["status"] == "cancelled"
        # confirming arms exactly one overlay_expiry timer on the fact-key stream (decision 5)
        cur.execute(
            "SELECT count(*) AS n FROM timers WHERE kind='overlay_expiry' AND aggregate_id=%s", (key,)
        )
        assert cur.fetchone()["n"] == 1


def test_platform_admin_confirms_governance_queue_task(db, catalog):
    # No owner recorded -> governance queue; a platform-admin may confirm the fallback task so it
    # is not stuck forever (decision 7). Proposed by BOB, so four-eyes is satisfied.
    draft = _propose(db)
    res = confirm_fact(db, _confirm_cmd(target=draft, actor=ADMIN))
    assert res.accepted is True, res.denied_reason
    key = fact_key(_orders(), "grain")
    assert fold_overlay_state(load_fact(db, key)).status == "VERIFIED"


def test_wrong_role_is_denied(db, catalog):
    catalog.set_owner(_orders(), "user:alice")
    draft = _propose(db)
    res = confirm_fact(db, _confirm_cmd(target=draft, actor=COMPLIANCE))
    assert res.accepted is False
    assert "authority" in res.denied_reason


def test_stale_target_event_id_is_denied(db, catalog):
    catalog.set_owner(_orders(), "user:alice")
    _propose(db)
    res = confirm_fact(db, _confirm_cmd(target="evt_does_not_exist"))
    assert res.accepted is False
    assert "stale" in res.denied_reason


def test_reject_marks_rejected_and_records_fingerprint(db, catalog):
    catalog.set_owner(_orders(), "user:alice")
    draft = _propose(db)
    res = reject_fact(
        db,
        Command(
            "reject_fact",
            "overlay_fact",
            None,
            {"ref": _orders(), "fact_type": "grain", "target_event_id": draft, "reason": "wrong key"},
            ALICE,
            "r",
        ),
    )
    assert res.accepted is True
    key = fact_key(_orders(), "grain")
    stream = load_fact(db, key)
    assert fold_overlay_state(stream).status == "REJECTED"
    rej = next(e for e in stream if e.type == "OVERLAY_FACT_REJECTED")
    assert rej.payload["retired_fingerprint"] is not None


def test_data_owner_cannot_confirm_policy_tag(db, catalog):
    catalog.set_owner(_orders(), "user:alice")
    draft = _propose(
        db,
        fact_type="policy_tag",
        value={"decision": "deny", "basis": "PII"},
        use_case="ads",
        actor=BOB,
    )
    # ALICE is a data_owner, not Compliance → denied by the fine authority check (SP-0 SoD posture)
    res = confirm_fact(db, _confirm_cmd(fact_type="policy_tag", use_case="ads", target=draft, actor=ALICE))
    assert res.accepted is False
    assert "authority" in res.denied_reason
    # Compliance can confirm it
    ok = confirm_fact(
        db, _confirm_cmd(fact_type="policy_tag", use_case="ads", target=draft, actor=COMPLIANCE, key="c2")
    )
    assert ok.accepted is True


def test_confirm_with_malformed_override_value_is_rejected(db, catalog):
    """pin 17: the confirmer may override the value (e.g. a REVERIFY/STALE correction), but the
    FINAL value is validated with `validate_fact_value` BEFORE OVERLAY_FACT_CONFIRMED is appended —
    a malformed override is rejected and nothing is persisted."""
    catalog.set_owner(_orders(), "user:alice")
    draft = _propose(db)  # valid grain proposed by BOB
    bad = Command(
        "confirm_fact",
        "overlay_fact",
        None,
        # `is_unique` must be a bool and `columns` a non-empty list — this override is malformed.
        {"ref": _orders(), "fact_type": "grain", "target_event_id": draft, "value": {"columns": [], "is_unique": "yes"}},
        ALICE,
        "c-bad",
    )
    res = confirm_fact(db, bad)
    assert res.accepted is False
    assert "invalid confirmed value" in res.denied_reason
    key = fact_key(_orders(), "grain")
    # still awaiting confirmation — no CONFIRMED event was written
    assert fold_overlay_state(load_fact(db, key)).status == "DRAFT"
