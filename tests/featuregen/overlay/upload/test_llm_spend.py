"""The money guard's durable half.

▲ The case worth reading first is `test_A_CRASHED_RESERVATION_DOES_NOT_SHRINK_THE_BUDGET_FOR_EVER`.
It is the defect this design had until it was reviewed against its own rule about lifecycle tables.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from featuregen.overlay.upload.llm_spend import (
    SpendExhausted,
    authorize_spend,
    remaining_spend,
    reserve_spend,
    settle_spend,
    sweep_expired_reservations,
)

NOW = "2026-08-23T00:00:00Z"
LATER = "2026-08-23T01:00:00Z"


def _authorize(db, *, calls=4, tokens=100_000, cost="5.00", expires="2099-01-01T00:00:00Z"):
    return authorize_spend(
        db, action="AUTHOR_FORMULA", actor_subject="user:sam", job_identity="job-1",
        member_identities=["m1"], provider_contract_hash="pc-1", max_calls=calls,
        max_tokens=tokens, currency="USD", max_cost=cost, pricing_version="price-v1",
        expires_at=expires)


# ══ THE CEILING ════════════════════════════════════════════════════════════════════════════════
def test_A_RESERVATION_TAKES_WORST_CASE_BEFORE_THE_CALL(db):
    """A ceiling checked after the call has already been paid."""
    auth = _authorize(db)

    reserve_spend(db, spend_authorization_id=auth, calls=1, tokens=10_000, cost="1.00", now=NOW)

    left = remaining_spend(db, auth, now=NOW)
    assert (left.calls, left.tokens, left.cost) == (3, 90_000, Decimal("4.00"))


def test_SETTLING_UNDER_THE_RESERVATION_GIVES_THE_BUDGET_BACK(db):
    """▲ Actuals replace the worst case; they are never added to it. Counting both would shrink a
    budget by work that came in under it."""
    auth = _authorize(db)
    reservation = reserve_spend(
        db, spend_authorization_id=auth, calls=1, tokens=10_000, cost="1.00", now=NOW)

    settle_spend(db, reservation, actual_calls=1, actual_tokens=2_000, actual_cost="0.20")

    left = remaining_spend(db, auth, now=NOW)
    assert (left.calls, left.tokens, left.cost) == (3, 98_000, Decimal("4.80"))


@pytest.mark.parametrize("over,why", [
    (dict(calls=99), "calls without tokens permits one enormous call"),
    (dict(tokens=999_999), "tokens without calls permits an unbounded loop of small ones"),
    (dict(cost="99.00"), "a currency ceiling is the one a person actually approved"),
])
def test_EVERY_AXIS_REFUSES_INDEPENDENTLY(db, over, why):
    auth = _authorize(db)
    ask = {"calls": 1, "tokens": 1_000, "cost": "0.10", **over}

    with pytest.raises(SpendExhausted, match="cannot cover this attempt"):
        reserve_spend(db, spend_authorization_id=auth, now=NOW, **ask)


def test_AN_EXHAUSTED_AUTHORIZATION_STOPS_THE_JOB(db):
    """▲ A refusal, never a quietly smaller call. A truncated critic loop presented as a finished
    result is indistinguishable from a complete one, which is worse than stopping."""
    auth = _authorize(db, calls=1)
    reserve_spend(db, spend_authorization_id=auth, calls=1, tokens=1_000, cost="0.10", now=NOW)

    with pytest.raises(SpendExhausted, match="The job STOPS"):
        reserve_spend(db, spend_authorization_id=auth, calls=1, tokens=1_000, cost="0.10", now=NOW)


def test_AN_EXPIRED_AUTHORIZATION_AUTHORIZES_NOTHING(db):
    """An approval granted inside a triage window must not still authorize work months later."""
    auth = _authorize(db, expires="2026-01-01T00:00:00Z")

    with pytest.raises(SpendExhausted, match="expired"):
        reserve_spend(db, spend_authorization_id=auth, calls=1, tokens=1_000, cost="0.10", now=NOW)


def test_NO_AUTHORIZATION_MEANS_NO_CALL(db):
    with pytest.raises(SpendExhausted, match="no spend authorization"):
        reserve_spend(db, spend_authorization_id="sa-nobody-approved", calls=1, tokens=1,
                      cost="0.01", now=NOW)


# ══ THE LIFECYCLE GAP THIS DESIGN NEARLY SHIPPED ═══════════════════════════════════════════════
def test_A_CRASHED_RESERVATION_DOES_NOT_SHRINK_THE_BUDGET_FOR_EVER(db):
    """▲ A reservation is a lifecycle state only a live worker can leave — the same shape as the
    three other tables in this codebase that wedge permanently.

    A crash between reserve and settle would otherwise leave worst-case cost reserved for ever, and
    the authorization would be exhausted by work that never happened. The budget recovers by
    ARITHMETIC: an expired unsettled reservation stops counting.
    """
    auth = _authorize(db, calls=1)
    reserve_spend(db, spend_authorization_id=auth, calls=1, tokens=1_000, cost="0.10", now=NOW,
                  ttl_seconds=60)

    assert remaining_spend(db, auth, now=NOW).calls == 0          # while it is live
    assert remaining_spend(db, auth, now=LATER).calls == 1        # once it has aged out


def test_THE_SWEEP_COUNTS_WITHOUT_DELETING(db):
    """▲ Nothing is deleted: the row is the record that this platform promised that money and then
    lost the worker. A non-zero count is a SIGNAL — reconcile against the dispatch outcome before
    assuming the money was never spent, because assuming it was not buys the tokens twice."""
    auth = _authorize(db)
    reserve_spend(db, spend_authorization_id=auth, calls=1, tokens=1_000, cost="0.10", now=NOW,
                  ttl_seconds=60)

    assert sweep_expired_reservations(db, now=NOW) == 0
    assert sweep_expired_reservations(db, now=LATER) == 1
    assert db.execute("SELECT count(*) FROM llm_spend_reservation").fetchone()[0] == 1


def test_A_SETTLED_RESERVATION_IS_NEVER_SWEPT(db):
    """Settled means accounted for. Sweeping it would recover a budget that really was spent."""
    auth = _authorize(db)
    reservation = reserve_spend(
        db, spend_authorization_id=auth, calls=1, tokens=1_000, cost="0.10", now=NOW,
        ttl_seconds=60)
    settle_spend(db, reservation, actual_calls=1, actual_tokens=900, actual_cost="0.09")

    assert sweep_expired_reservations(db, now=LATER) == 0
    # And it still counts against the budget, because it really was spent.
    assert remaining_spend(db, auth, now=LATER).calls == 3


# ══ IDEMPOTENCY AND IMMUTABILITY ═══════════════════════════════════════════════════════════════
def test_AUTHORIZING_THE_SAME_CEILING_TWICE_IS_ONE_AUTHORIZATION(db):
    """So a redelivered request cannot re-authorize the same spend under a new id."""
    assert _authorize(db) == _authorize(db)
    assert db.execute(
        "SELECT count(*) FROM llm_spend_authorization_revision").fetchone()[0] == 1


def test_A_CEILING_CANNOT_BE_RAISED_AFTER_THE_FACT(db):
    """A ceiling that can be raised later is not a ceiling. Record a new authorization instead."""
    import psycopg

    auth = _authorize(db)

    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        db.execute("UPDATE llm_spend_authorization_revision SET max_cost = 999 "
                   "WHERE spend_authorization_id = %s", (auth,))


def test_A_SETTLEMENT_CANNOT_BE_REWRITTEN(db):
    import psycopg

    auth = _authorize(db)
    reservation = reserve_spend(
        db, spend_authorization_id=auth, calls=1, tokens=1_000, cost="0.10", now=NOW)
    settle_spend(db, reservation, actual_calls=1, actual_tokens=900, actual_cost="0.09")

    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        db.execute("UPDATE llm_spend_settlement SET actual_cost = 0 WHERE reservation_id = %s",
                   (reservation,))


def test_EXPIRY_IS_COMPARED_AS_INSTANTS_not_as_strings(db):
    """▲ Workflow finding W1: the expiry check was `str(expires_at) <= str(now)` — a tz-aware
    datetime rendered in the SESSION timezone with a space separator, lexically compared to an ISO
    string with 'T'. On the same calendar date the space (0x20) sorts before 'T' (0x54), so a
    perfectly valid authorization expiring LATER TODAY was refused as expired — and under a
    positive-offset session timezone the mirror case let an EXPIRED authorization spend real money.
    The existing tests only used dates differing in the first ten characters, where lexical order
    coincidentally matches instant order."""
    auth = _authorize(db, expires="2026-08-23T12:00:00Z")   # valid for twelve more hours

    reservation = reserve_spend(
        db, spend_authorization_id=auth, calls=1, tokens=1_000, cost="0.10",
        now="2026-08-23T00:00:00Z")

    assert reservation
    # And one minute AFTER expiry, same calendar date, it refuses.
    with pytest.raises(SpendExhausted, match="expired"):
        reserve_spend(db, spend_authorization_id=auth, calls=1, tokens=1_000, cost="0.10",
                      now="2026-08-23T12:01:00Z")


def test_A_RESERVATION_CANNOT_BE_EDITED_OR_DELETED(db):
    """▲ Workflow finding W2: the ledger trigger's own error text claimed reservations are
    append-only, but the trigger was attached only to settlements. A mutable reservation IS the
    overspend: shrink its expires_at while the call is in flight and the worst-case amount frees for
    a concurrent worker — two calls under one ceiling, with no record."""
    import psycopg

    auth = _authorize(db)
    reservation = reserve_spend(
        db, spend_authorization_id=auth, calls=1, tokens=1_000, cost="0.10", now=NOW)

    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        db.execute("UPDATE llm_spend_reservation SET expires_at = now() "
                   "WHERE reservation_id = %s", (reservation,))


def test_a_DIFFERENT_VALIDITY_WINDOW_is_a_DIFFERENT_approval(db):
    """Task 5 review C-2: expiry is INSIDE the idempotency identity. Outside it, every ceiling
    was a LIFETIME budget per (actor, subject, contract) — once expired, a re-mint returned the
    same expired row for ever and the subject became permanently unauthorable."""
    common = dict(action="AUTHOR_FORMULA", actor_subject="user:sam", job_identity="job-c2",
                  member_identities=["m"], provider_contract_hash="sha256:c", max_calls=5,
                  max_tokens=1000, currency="USD", max_cost="1.00", pricing_version="p@1")
    first = authorize_spend(db, **common, expires_at="2026-12-01T00:00:00Z")
    same = authorize_spend(db, **common, expires_at="2026-12-01T00:00:00Z")
    renewed = authorize_spend(db, **common, expires_at="2026-12-02T00:00:00Z")

    assert first == same, "the identical approval is ONE row"
    assert renewed != first, "a new validity window is a NEW bounded approval — the renewal"
