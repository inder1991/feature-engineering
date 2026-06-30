from datetime import UTC, datetime, timedelta

import pytest
from psycopg.rows import dict_row

from featuregen.contracts import Command, ConcurrencyError
from featuregen.identity.build import build_human_identity, build_service_identity
from featuregen.overlay.commands import confirm_fact, propose_fact, reject_fact
from featuregen.overlay.freshness import fire_due_overlay_expiries
from featuregen.overlay.identity import CatalogObjectRef, fact_key
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact

ALICE = build_human_identity(subject="user:alice", role_claims=("data_owner",))
BOB = build_human_identity(subject="user:bob", role_claims=("data_owner",))
COMPLIANCE = build_human_identity(subject="user:carol", role_claims=("compliance",))
ADMIN = build_human_identity(subject="user:admin", role_claims=("platform-admin",))
# A non-human (profiler) principal, attested + granted platform-admin so it WOULD clear the
# authority/four-eyes checks if it were human — isolating the `actor_kind != "human"` guard.
PROFILER = build_service_identity(
    subject="service:profiler", role_claims=("platform-admin",), attestation="deploy-sig-abc"
)


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


def _reject_cmd(*, fact_type="grain", use_case=None, target, actor=ALICE, reason="no", key="r"):
    args = {
        "ref": _orders(),
        "fact_type": fact_type,
        "target_event_id": target,
        "reason": reason,
    }
    if use_case is not None:
        args["use_case"] = use_case
    return Command("reject_fact", "overlay_fact", None, args, actor, key)


def _has_event(db, fact_type, event_type) -> bool:
    return any(e.type == event_type for e in load_fact(db, fact_key(_orders(), fact_type)))


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


def test_reverify_no_override_keeps_last_verified_override_not_original(db, catalog):
    """P1b: a re-verify confirm with NO override must re-affirm the LAST VERIFIED value, not silently
    revert to the original cycle-1 proposal. Cycle 1: BOB proposes V0, ALICE confirms with an
    OVERRIDE V'. After expiry (REVERIFY) the only PROPOSED on the stream is still the cycle-1 V0
    draft. A no-override re-confirm previously defaulted the value to proposed_value (V0), silently
    discarding the human correction V' that state.prior_value still holds. The fix defaults to
    state.prior_value on REVERIFY/STALE, so the confirmed value stays V'."""
    catalog.set_owner(_orders(), "user:alice")
    v0 = {"columns": ["order_id"], "is_unique": True}
    vprime = {"columns": ["customer_id"], "is_unique": True}
    draft = _propose(db, value=v0)  # proposer = BOB
    res = confirm_fact(
        db,
        Command(
            "confirm_fact",
            "overlay_fact",
            None,
            {"ref": _orders(), "fact_type": "grain", "target_event_id": draft, "value": vprime},
            ALICE,
            "c1",
        ),
    )
    assert res.accepted, res.denied_reason
    key = fact_key(_orders(), "grain")
    st = fold_overlay_state(load_fact(db, key))
    assert st.status == "VERIFIED" and st.value == vprime
    confirmed_id = st.confirmed_event_id
    # expire the fact (confirm armed a +180d overlay_expiry timer; fire it from the future)
    fired = fire_due_overlay_expiries(db, now=datetime.now(UTC) + timedelta(days=181))
    assert fired == 1
    st = fold_overlay_state(load_fact(db, key))
    assert st.status == "REVERIFY" and st.prior_value == vprime
    # cycle 2: re-confirm with NO override (CAS on the confirmed event id)
    res = confirm_fact(db, _confirm_cmd(target=confirmed_id, actor=ALICE, key="c2"))
    assert res.accepted, res.denied_reason
    st = fold_overlay_state(load_fact(db, key))
    # PRE-FIX this assert fails: value reverts to v0. POST-FIX value stays vprime.
    assert st.value == vprime, f"re-verify silently reverted override to {st.value}"


# --- security-guard DENIAL paths (each test must FAIL if its guard were removed) ---------------


@pytest.mark.parametrize(
    ("handler", "verb", "event_type"),
    [
        (confirm_fact, "confirm_fact", "OVERLAY_FACT_CONFIRMED"),
        (reject_fact, "reject_fact", "OVERLAY_FACT_REJECTED"),
    ],
)
def test_non_human_actor_is_denied(db, catalog, handler, verb, event_type):
    """confirm_fact / reject_fact are human-only (§6.3). No owner is recorded -> governance queue,
    and PROFILER holds platform-admin + is attested, so it would clear the authority and four-eyes
    checks if it were human. The ONLY thing blocking it is the `actor_kind != "human"` guard: drop
    that guard and `accepted` flips to True (and the event is written), so this genuinely covers it.
    """
    draft = _propose(db, actor=BOB)  # human proposer -> four-eyes would be satisfied for PROFILER
    cmd = Command(
        verb,
        "overlay_fact",
        None,
        {"ref": _orders(), "fact_type": "grain", "target_event_id": draft, "reason": "x"},
        PROFILER,
        "svc",
    )
    res = handler(db, cmd)
    assert res.accepted is False
    assert "human" in res.denied_reason
    # nothing was appended: no terminal CONFIRMED/REJECTED event on the fact stream
    assert not _has_event(db, "grain", event_type)


def test_four_eyes_proposer_cannot_confirm_own_draft(db, catalog):
    """Four-eyes SoD (§6.5): the proposer may not confirm the same fact. ALICE is BOTH the owner
    (so the authority check passes) AND the proposer here, so `proposer_ne_confirmer` is the only
    remaining blocker — remove it and ALICE self-confirms to VERIFIED.

    Contrast: test_owner_confirms_draft_to_verified is the happy path with a DIFFERENT confirmer
    (BOB proposes, ALICE confirms -> accepted)."""
    catalog.set_owner(_orders(), "user:alice")
    draft = _propose(db, actor=ALICE)  # proposed by the owner herself
    res = confirm_fact(db, _confirm_cmd(target=draft, actor=ALICE))
    assert res.accepted is False
    assert "four-eyes" in res.denied_reason
    # the authority check did NOT mask this: a non-owner confirmer would have failed on "authority".
    stream = load_fact(db, fact_key(_orders(), "grain"))
    assert not any(e.type == "OVERLAY_FACT_CONFIRMED" for e in stream)
    assert fold_overlay_state(stream).status == "DRAFT"


def test_reject_stale_target_event_id_is_denied(db, catalog):
    """reject_fact CAS: a `target_event_id` that is not the current head is denied as stale and
    nothing is appended (mirror of test_stale_target_event_id_is_denied for confirm)."""
    catalog.set_owner(_orders(), "user:alice")
    _propose(db)  # a real DRAFT exists, but we reject against a bogus (superseded) target
    res = reject_fact(db, _reject_cmd(target="evt_does_not_exist", actor=ALICE))
    assert res.accepted is False
    assert "stale" in res.denied_reason
    assert not _has_event(db, "grain", "OVERLAY_FACT_REJECTED")


def test_reject_wrong_authority_is_denied(db, catalog):
    """reject_fact authority: BOB is a human data_owner but NOT the resolved owner of `orders`
    (ALICE is), so `_actor_is_authority` returns False and the rejection is denied with nothing
    appended. Remove the authority check and BOB's rejection would succeed."""
    catalog.set_owner(_orders(), "user:alice")
    draft = _propose(db)  # proposed by BOB; rejected by BOB below (a non-owner human)
    res = reject_fact(db, _reject_cmd(target=draft, actor=BOB))
    assert res.accepted is False
    assert "authority" in res.denied_reason
    assert not _has_event(db, "grain", "OVERLAY_FACT_REJECTED")


# --- C2: command appends are CAS-pinned to the observed head (lost-update guard) ---------------


def test_confirm_pins_expected_version_to_observed_head(db, catalog, occ_spy):
    """C2: confirm_fact's OVERLAY_FACT_CONFIRMED append is version-pinned to the head it folded
    against (the DRAFT at stream_version 1), NOT expected_version=None. Without pinning, two
    concurrent confirmers both pass the read-compare CAS and both append (double-CONFIRMED /
    confirm-vs-reject lost-update)."""
    catalog.set_owner(_orders(), "user:alice")
    draft = _propose(db)  # DRAFT at stream_version 1
    res = confirm_fact(db, _confirm_cmd(target=draft, actor=ALICE))
    assert res.accepted is True
    assert occ_spy["OVERLAY_FACT_CONFIRMED"] == 1  # pinned & non-None


def test_reject_pins_expected_version_to_observed_head(db, catalog, occ_spy):
    """C2: reject_fact's OVERLAY_FACT_REJECTED append is version-pinned to the observed head."""
    catalog.set_owner(_orders(), "user:alice")
    draft = _propose(db)  # DRAFT at stream_version 1
    res = reject_fact(db, _reject_cmd(target=draft, actor=ALICE))
    assert res.accepted is True
    assert occ_spy["OVERLAY_FACT_REJECTED"] == 1  # pinned & non-None


def test_confirm_append_collides_with_concurrent_confirm(db, catalog, inject_concurrent_append):
    """C2 (genuine concurrency): a confirmer lands OVERLAY_FACT_CONFIRMED out-of-band between this
    handler's fold and its own append. Because the handler pins its append to the head it folded
    against, it must now raise ConcurrencyError — NOT silently land a second CONFIRMED one
    stream_version higher (the lost-update the expected_version=None bug allowed)."""
    catalog.set_owner(_orders(), "user:alice")
    draft = _propose(db)  # DRAFT at stream_version 1
    inject_concurrent_append("OVERLAY_FACT_CONFIRMED")
    with pytest.raises(ConcurrencyError):
        confirm_fact(db, _confirm_cmd(target=draft, actor=ALICE))
