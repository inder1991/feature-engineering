"""The money guard's durable half: reserve worst case, settle actuals, and sweep what neither.

**Why reserve rather than count afterwards.** A ceiling checked after the call has already been paid.
The reservation is taken BEFORE the provider is reached, on the pre-dispatch connection that is the
only seam seeing every PHYSICAL attempt — ``drive_structured_call`` retries and repairs beneath the
logical call, so a count taken at the logical layer under-counts by exactly the retries.

**Why not one mutable counter.** An earlier design put ``calls_consumed`` on the authorization and
required it to be updated in the same transaction as the dispatch audit. That is impossible:
``record_dispatch`` deliberately opens its own connection and commits independently, so the audit
survives a caller rollback. Two append-only event tables answer the same question without needing a
transaction that cannot exist.

▲ **And a reservation is a lifecycle state only a live worker can leave**, so it expires and is
swept. Without that, a crash between reserve and settle leaves worst-case cost reserved for ever and
the budget silently shrinks until the authorization is exhausted by work that never happened — the
same defect this codebase already has in three other lifecycle tables (§15.1).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from featuregen.canonical import jcs_sha256

__all__ = [
    "SpendExhausted",
    "SpendRemaining",
    "authorize_spend",
    "reconcile_expired_spend",
    "remaining_spend",
    "reservation_for_dispatch",
    "reserve_spend",
    "settle_spend",
    "sweep_expired_reservations",
]

#: How long a reservation may stand unsettled before the sweep reclaims it. Deliberately generous
#: relative to a provider call and deliberately finite: the cost of it being too long is a budget
#: that recovers slowly, and of it being too short is double-spending a call still in flight.
DEFAULT_RESERVATION_TTL_SECONDS = 900


class SpendExhausted(RuntimeError):
    """This authorization cannot cover the requested work. A refusal, never a smaller call."""


@dataclass(frozen=True, slots=True)
class SpendRemaining:
    calls: int
    tokens: int
    cost: Decimal


def authorize_spend(
    conn, *, action: str, actor_subject: str, job_identity: str, member_identities,
    provider_contract_hash: str, max_calls: int, max_tokens: int, currency: str,
    max_cost, pricing_version: str, expires_at,
) -> str:
    """Record a ceiling. Idempotent on its content, so a redelivered request cannot re-authorize.

    ``expires_at`` is required by the schema, not defaulted here: an approval granted inside a
    triage window must not still authorize a spend three months later, and a default would pick a
    number nobody agreed to.
    """
    import json

    members = list(member_identities)
    idempotency = jcs_sha256({
        "action": action, "actor_subject": actor_subject, "job_identity": job_identity,
        "member_identities": members, "provider_contract_hash": provider_contract_hash,
        "max_calls": max_calls, "max_tokens": max_tokens, "currency": currency,
        "max_cost": str(max_cost), "pricing_version": pricing_version,
    })
    authorization_id = f"sa-{idempotency[:32]}"
    conn.execute(
        "INSERT INTO llm_spend_authorization_revision (spend_authorization_id, action, "
        "actor_subject, job_identity, member_identities, provider_contract_hash, max_calls, "
        "max_tokens, currency, max_cost, pricing_version, idempotency_identity, expires_at) "
        "VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (idempotency_identity) DO NOTHING",
        (authorization_id, action, actor_subject, job_identity, json.dumps(members),
         provider_contract_hash, max_calls, max_tokens, currency, max_cost, pricing_version,
         idempotency, expires_at))
    row = conn.execute(
        "SELECT spend_authorization_id FROM llm_spend_authorization_revision "
        "WHERE idempotency_identity = %s", (idempotency,)).fetchone()
    return row[0]


def _committed(conn, spend_authorization_id: str, now):
    """What is already spent or promised: settled actuals plus UNEXPIRED unsettled reservations.

    ▲ Settled actuals are used where they exist and the reservation elsewhere — never both, and
    never the reservation once an actual is known. Counting the worst case after the real cost is
    recorded would shrink a budget by work that came in under it.
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(COALESCE(s.actual_calls,  r.reserved_calls)),  0), "
        "       COALESCE(SUM(COALESCE(s.actual_tokens, r.reserved_tokens)), 0), "
        "       COALESCE(SUM(COALESCE(s.actual_cost,   r.reserved_cost)),   0) "
        "  FROM llm_spend_reservation r "
        "  LEFT JOIN llm_spend_settlement s ON s.reservation_id = r.reservation_id "
        " WHERE r.spend_authorization_id = %s "
        "   AND (s.reservation_id IS NOT NULL OR r.expires_at > %s)",
        (spend_authorization_id, now)).fetchone()
    return int(row[0]), int(row[1]), Decimal(row[2])


def remaining_spend(conn, spend_authorization_id: str, *, now) -> SpendRemaining:
    """What is still available under this authorization."""
    row = conn.execute(
        "SELECT max_calls, max_tokens, max_cost FROM llm_spend_authorization_revision "
        "WHERE spend_authorization_id = %s", (spend_authorization_id,)).fetchone()
    if row is None:
        raise SpendExhausted(
            f"no spend authorization {spend_authorization_id!r}: no authorization means no outbox "
            f"work, and certainly no provider call")
    used_calls, used_tokens, used_cost = _committed(conn, spend_authorization_id, now)
    return SpendRemaining(
        calls=row[0] - used_calls, tokens=row[1] - used_tokens,
        cost=Decimal(row[2]) - used_cost)


def reserve_spend(
    conn, *, spend_authorization_id: str, calls: int, tokens: int, cost, now,
    ttl_seconds: int = DEFAULT_RESERVATION_TTL_SECONDS, dispatch_ref: str | None = None,
) -> str:
    """Reserve WORST CASE for one attempt, or refuse. Returns the reservation id.

    ▲ **The authorization row is LOCKED for the read-and-insert.** Two workers reading "remaining"
    concurrently would otherwise both find room and both proceed, and both would be within budget
    individually while the pair was not.

    Raises:
        SpendExhausted: the ceiling cannot cover this attempt, or the authorization has expired. A
            refusal, never a quietly smaller call — truncating the work to fit the budget produces a
            result nobody can tell apart from a complete one.
    """
    # ▲ THE EXPIRY COMPARISON HAPPENS IN POSTGRES, and this is a correction with a lesson. An
    # earlier version compared `str(expires_at) <= str(now)` — a tz-aware datetime rendered in the
    # SESSION timezone with a space separator, against an ISO string with 'T'. On the same calendar
    # date the space sorts before 'T', so a valid authorization expiring later today was refused —
    # and under a positive-offset session timezone the mirror case let an EXPIRED one spend real
    # money: the money guard failing OPEN. Every OTHER time comparison in this module already ran in
    # SQL; only the Python-side one was broken. Timestamps are compared where they are typed.
    locked = conn.execute(
        "SELECT expires_at, expires_at <= %s::timestamptz "
        "FROM llm_spend_authorization_revision "
        "WHERE spend_authorization_id = %s FOR UPDATE",
        (now, spend_authorization_id)).fetchone()
    if locked is None:
        raise SpendExhausted(
            f"no spend authorization {spend_authorization_id!r}: no authorization means no "
            f"provider call")
    if locked[1]:
        raise SpendExhausted(
            f"spend authorization {spend_authorization_id} expired at {locked[0]}. An approval "
            f"granted in a triage window does not authorize work months later")

    left = remaining_spend(conn, spend_authorization_id, now=now)
    if calls > left.calls or tokens > left.tokens or Decimal(cost) > left.cost:
        raise SpendExhausted(
            f"spend authorization {spend_authorization_id} cannot cover this attempt: asked "
            f"{calls} calls / {tokens} tokens / {cost}, and {left.calls} calls / {left.tokens} "
            f"tokens / {left.cost} remain. The job STOPS — a truncated critic loop presented as a "
            f"finished result is worse than a refusal")

    reservation_id = f"rsv-{jcs_sha256({'a': spend_authorization_id, 'n': str(now), 'c': calls, 't': tokens, 'x': str(cost), 'd': dispatch_ref})[:32]}"
    conn.execute(
        "INSERT INTO llm_spend_reservation (reservation_id, spend_authorization_id, dispatch_ref, "
        "reserved_calls, reserved_tokens, reserved_cost, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s::timestamptz + make_interval(secs => %s))",
        (reservation_id, spend_authorization_id, dispatch_ref, calls, tokens, cost, now,
         ttl_seconds))
    return reservation_id


def settle_spend(
    conn, reservation_id: str, *, actual_calls: int, actual_tokens: int, actual_cost,
) -> None:
    """Record what the attempt actually cost. Idempotent on the reservation."""
    conn.execute(
        "INSERT INTO llm_spend_settlement (reservation_id, actual_calls, actual_tokens, "
        "actual_cost) VALUES (%s, %s, %s, %s) ON CONFLICT (reservation_id) DO NOTHING",
        (reservation_id, actual_calls, actual_tokens, actual_cost))


def reservation_for_dispatch(conn, dispatch_ref: str) -> str | None:
    """The reservation bound to one physical dispatch, or ``None`` for an unmetered call."""
    row = conn.execute(
        "SELECT reservation_id FROM llm_spend_reservation WHERE dispatch_ref = %s",
        (dispatch_ref,)).fetchone()
    return None if row is None else row[0]


def reconcile_expired_spend(conn, *, now) -> dict[str, int]:
    """Settle expired unsettled reservations AGAINST THE DISPATCH OUTCOME — owner ruling 2026-08-23
    item 3: reconciled before budget release, never assumed.

    ▲ The budget already released by ARITHMETIC the moment the reservation expired unsettled
    (`_committed` stops counting it) — so the reconciler's real job is the opposite of releasing:
    **re-charging** the budget for money that WAS plausibly spent. Per expired unsettled reservation
    with a dispatch:

    * ``response_received`` — the provider answered; the money is real. Settle at WORST CASE
      (the actuals died with the worker) so the budget re-charges conservatively.
    * **no outcome row** — the worker died between egress and the outcome write, so spend is
      UNKNOWABLE. Settle at worst case too: assuming it was never spent is the opposite error,
      and it buys the tokens twice.
    * ``transport_failed`` — the provider was never reached past transport. Settle at ZERO,
      explicitly, so the ledger says "checked and unspent" rather than merely "expired".

    Reservations with NO dispatch ref are left to expiry arithmetic — they were never bound to an
    egress, so there is no outcome to reconcile against.
    """
    rows = conn.execute(
        "SELECT r.reservation_id, r.reserved_calls, r.reserved_tokens, r.reserved_cost, o.outcome "
        "  FROM llm_spend_reservation r "
        "  LEFT JOIN llm_spend_settlement s ON s.reservation_id = r.reservation_id "
        "  LEFT JOIN llm_dispatch_outcome o ON o.dispatch_ref = r.dispatch_ref "
        " WHERE s.reservation_id IS NULL AND r.expires_at <= %s AND r.dispatch_ref IS NOT NULL",
        (now,)).fetchall()
    tallies = {"recharged_worst_case": 0, "released_transport_failed": 0}
    for reservation_id, calls, tokens, cost, outcome in rows:
        if outcome == "transport_failed":
            settle_spend(conn, reservation_id, actual_calls=0, actual_tokens=0, actual_cost=0)
            tallies["released_transport_failed"] += 1
        else:
            settle_spend(conn, reservation_id, actual_calls=calls, actual_tokens=tokens,
                         actual_cost=cost)
            tallies["recharged_worst_case"] += 1
    return tallies


def sweep_expired_reservations(conn, *, now) -> int:
    """Count reservations that have aged out unsettled — the gauge the sweep exists for.

    ▲ **Nothing is deleted.** `_committed` already ignores an expired unsettled reservation, so the
    budget recovers by ARITHMETIC rather than by a write; deleting the row would destroy the record
    that the platform once promised that money and then lost the worker.

    ▲ **And a non-zero count is a SIGNAL, not noise.** Every one is a provider call this platform
    reserved for and cannot account for — either the worker died, or a settlement path is missing.
    Reconcile against `llm_dispatch_outcome` before assuming the money was never spent: assuming it
    was not is the opposite error, and it buys the tokens twice.
    """
    row = conn.execute(
        "SELECT count(*) FROM llm_spend_reservation r "
        "  LEFT JOIN llm_spend_settlement s ON s.reservation_id = r.reservation_id "
        " WHERE s.reservation_id IS NULL AND r.expires_at <= %s", (now,)).fetchone()
    return int(row[0])
