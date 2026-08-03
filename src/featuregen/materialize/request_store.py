"""Phase G §3.2 — ``materialization_request``: a run acquires DURABLE IDENTITY before any work.

**The hole this closes.** A materialization run used to acquire no database identity until someone
appended an event, and :func:`~featuregen.materialize.control_plane.fold_run_status` raises on an
empty stream. A crash between "we decided to run" and the first append therefore left *zero trace*:
nobody could tell a run had ever been requested. The request row exists **before** any work begins,
so the crash leaves a leased, un-advanced request the reconciler can find.

**Two kinds of record, and why they must not be conflated.**

* The control plane (migrations ``1034``/``1044``, :mod:`featuregen.materialize.control_plane`) is
  **immutable evidence** — append-only, one terminal event per run, ordering-triggered. Nothing in
  this module writes to it, reads it for a verdict, or alters it in any way.
* ``materialization_request`` is a **mutable coordination record** — who asked, for what, under
  which flag state, is it still being worked, has its lease expired. It is UPDATEd (accept, lease
  renewal, terminal link) and carries no append-only guard, because a row that could not be updated
  could not carry a lease.

So this module makes no append-only claim and offers no fold: the request row says what is *being
attempted*, and the plane says what *happened*. A reader who needs the second must read the plane.

**Every refusal here is a ``ValueError``, deliberately.** §14's four closed vocabularies answer
governed questions — a compilation refusal is decided from governed metadata, a publication refusal
against the target environment. "This request is already accepted", "committed does not move to
running" and "a naive timestamp cannot be compared to a lease" are none of those: they are calls
assembled wrongly, and :class:`~featuregen.materialize.codes.CompilationRefusalCode` has no member
for one (the same reasoning as ``ir.py``, ``physical_types.py`` and ``contract.py``). Typing them
into that enum would force a handler back to comparing raw strings.

**Concurrency.** Every state change is a single conditional UPDATE whose ``WHERE`` names the states
it is legal from, so two workers racing to accept or advance one request cannot both win: the loser
matches no row and is refused. Nothing here is a read-modify-write, and ``updated_at`` is stamped by
the database (a touch trigger in ``1053``) rather than by each writer remembering to set it.
"""
from __future__ import annotations

import datetime as _datetime
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from psycopg.types.json import Jsonb

from featuregen.contracts.db import DbConn

__all__ = [
    "LEGAL_LIFECYCLE_TRANSITIONS",
    "MaterializationRequestV1",
    "RequestLifecycle",
    "accept_request",
    "advance_lifecycle",
    "expired_requests",
    "read_request",
    "record_request",
    "renew_lease",
]


class RequestLifecycle(StrEnum):
    """The CLOSED lifecycle of a materialization request — the four states Phase G §3.2 ships.

    Migration ``1053`` carries the same set as a ``CHECK``, so a state invented at a call site is
    refused by the database rather than stored as a lifecycle nothing can advance. These states are
    the *coordination* view of a run; the run's own status stays folded from the plane's events.
    """

    #: Recorded, not yet claimed. No lease, no generation, no run.
    REQUESTED = "requested"
    #: Claimed by a worker, which holds a lease. Compilation may be in progress.
    ACCEPTED = "accepted"
    #: A run was prepared and named — ``run_id`` is set, and the plane's event stream has begun.
    RUNNING = "running"
    #: TERMINAL. The run reached its end and its evidence is in the plane.
    COMMITTED = "committed"
    #: TERMINAL. The attempt is over without a committed run (including reconciled abandonment).
    FAILED = "failed"

    def is_terminal(self) -> bool:
        """Whether the request is over. A terminal request is never reconciled again."""
        return not LEGAL_LIFECYCLE_TRANSITIONS[self]


#: The ONE definition of what may move where. Read by the test suite as a set rather than described
#: in prose, so an edge added here without a reason fails a test that names the shipped set.
#:
#: ``requested → accepted → running → committed | failed`` is Phase G §3.2's set, plus ONE edge it
#: does not name: ``accepted → failed``. §3.3's reconciler acts on an *expired lease*, and a lease
#: exists in exactly two states — ``accepted`` and ``running``. A worker that dies between claiming
#: a request and preparing a run leaves it ``accepted``, and without this edge the reconciler would
#: have to first move it to ``running`` — recording that a run started when none ever did — in order
#: to fail it. Terminal is terminal, nothing moves backwards, and there is no self-transition.
LEGAL_LIFECYCLE_TRANSITIONS: Mapping[RequestLifecycle, frozenset[RequestLifecycle]] = {
    RequestLifecycle.REQUESTED: frozenset({RequestLifecycle.ACCEPTED}),
    RequestLifecycle.ACCEPTED: frozenset({RequestLifecycle.RUNNING, RequestLifecycle.FAILED}),
    RequestLifecycle.RUNNING: frozenset({RequestLifecycle.COMMITTED, RequestLifecycle.FAILED}),
    RequestLifecycle.COMMITTED: frozenset(),
    RequestLifecycle.FAILED: frozenset(),
}

#: The inverse: which states each target is reachable FROM. Derived, never written twice.
_LEGAL_FROM: Mapping[RequestLifecycle, frozenset[RequestLifecycle]] = {
    target: frozenset(source for source, targets in LEGAL_LIFECYCLE_TRANSITIONS.items()
                      if target in targets)
    for target in RequestLifecycle
}

#: The states in which work is claimed and a lease is held.
_LEASED_STATES: frozenset[RequestLifecycle] = frozenset(
    {RequestLifecycle.ACCEPTED, RequestLifecycle.RUNNING})

#: Column order, written once. The dataclass mirrors it field-for-field, so every read is
#: ``MaterializationRequestV1(*row)`` and a column added to one side without the other fails loudly.
_COLUMNS = (
    "request_id, logical_group_name, requested_by, authorized_roles, idempotency_key, "
    "activation_state, lifecycle_state, generation_id, run_id, resolved_input_digest, "
    "requested_at, accepted_at, lease_expires_at, updated_at")


#: The terminal states, as SQL literals rather than a bound parameter — deliberately, and measured.
#: 1053's reconciler index is PARTIAL (`WHERE lifecycle_state NOT IN ('committed', 'failed')`), and
#: PostgreSQL uses a partial index only when it can PROVE the query's predicate implies the index's.
#: With the states bound as a parameter that proof holds only while the planner is building CUSTOM
#: plans: psycopg prepares a statement server-side once it has been executed `prepare_threshold`
#: (5) times — which a long-lived reconciler reaches within seconds — and the resulting GENERIC plan
#: sees `$1` instead of the values. Measured on this repo's test server (PostgreSQL 18) under
#: `plan_cache_mode = force_generic_plan`: `lifecycle_state <> ALL($1)` plans a Seq Scan, while the
#: literal form plans a Bitmap Index Scan on the partial index. The literals are enum members
#: defined a few lines up, never caller input, so nothing here is interpolated from outside.
_NON_TERMINAL_PREDICATE = "lifecycle_state NOT IN ({})".format(
    ", ".join(f"'{state.value}'" for state in RequestLifecycle if state.is_terminal()))

#: The reconciler's statement, written once so the test suite can EXPLAIN the query that actually
#: runs rather than a look-alike.
_EXPIRED_REQUESTS_SQL = (
    f"SELECT {_COLUMNS} FROM materialization_request "
    f"WHERE {_NON_TERMINAL_PREDICATE} AND lease_expires_at IS NOT NULL AND lease_expires_at < %s "
    f"ORDER BY lease_expires_at, request_id")


def _text(value: object, *, field: str, why: str) -> str:
    """A required identifier: present, a string, and not blank."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"materialization request {field} is blank ({value!r}): {why}")
    return value


def _optional_text(value: object, *, field: str) -> str | None:
    """Absent, or real — never a blank string standing in for "unknown"."""
    if value is None:
        return None
    return _text(value, field=field,
                 why="a blank value records that the field was set when nothing was known, which "
                     "reads back as an answer rather than as an absence")


def _lease_seconds(value: object) -> float:
    """A lease must last. Zero is expired the instant it is granted."""
    if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
        raise ValueError(
            f"lease_seconds is {value!r}: a lease must have a positive duration, or the reconciler "
            f"adopts a request whose worker has not yet drawn breath")
    return float(value)


def _instant(value: object, *, field: str) -> _datetime.datetime:
    """An offset-aware instant. A naive one compared against ``timestamptz`` is read in the
    session's zone, so the same backlog would expire differently on two workers."""
    if not isinstance(value, _datetime.datetime):
        raise ValueError(f"materialization request {field} is {value!r}: an instant is required")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(
            f"materialization request {field} {value!r} carries no UTC offset: a naive timestamp "
            f"compared against a lease is read in whatever zone the session happens to hold")
    return value


@dataclass(frozen=True, slots=True)
class MaterializationRequestV1:
    """One request to materialize one logical group — the anchor a run is attributed to.

    ``authorized_roles`` is the roles snapshot taken when the request was recorded, not a live
    lookup: a run must be judged against the scope its requester *actually held*, not against
    whatever they hold by the time somebody reads the record. ``activation_state`` is the flag and
    interlock state observed at the same moment, opaque to this module — it is evidence about the
    world the decision was made in, and interpreting it here would make this store a policy.

    Frozen guards rebinding, not deep immutability: ``activation_state`` is a copied mapping, so the
    record is compared by value and never hashed.
    """

    request_id: str
    logical_group_name: str
    requested_by: str
    authorized_roles: tuple[str, ...]
    idempotency_key: str
    activation_state: Mapping[str, Any]
    lifecycle_state: RequestLifecycle
    generation_id: str | None
    run_id: str | None
    resolved_input_digest: str | None
    requested_at: _datetime.datetime
    accepted_at: _datetime.datetime | None
    lease_expires_at: _datetime.datetime | None
    updated_at: _datetime.datetime

    def __post_init__(self) -> None:
        _text(self.request_id, field="request_id",
              why="the request is what every later record is attributed to")
        _text(self.logical_group_name, field="logical_group_name",
              why="publication is atomic per group, so a request names the group it publishes")
        _text(self.requested_by, field="requested_by",
              why="a run that spends cluster resources is somebody's; an unattributed one is not "
                  "auditable")
        _text(self.idempotency_key, field="idempotency_key",
              why="the key is what stops one retried call from becoming two runs")
        object.__setattr__(self, "authorized_roles", tuple(self.authorized_roles))
        if not self.authorized_roles:
            raise ValueError(
                "materialization request authorized_roles is empty: the roles snapshot is what the "
                "run is judged against, and an empty snapshot records that none was taken rather "
                "than that the requester held nothing")
        for role in self.authorized_roles:
            _text(role, field="authorized_roles member",
                  why="a blank role name is not a role the requester could have held")
        if not isinstance(self.activation_state, Mapping):
            raise ValueError(
                f"materialization request activation_state is "
                f"{type(self.activation_state).__name__}: it is the flag/interlock state observed "
                f"at accept time, which is a mapping of names to values")
        object.__setattr__(self, "activation_state", dict(self.activation_state))
        object.__setattr__(self, "lifecycle_state", RequestLifecycle(self.lifecycle_state))
        for name in ("generation_id", "run_id", "resolved_input_digest"):
            _optional_text(getattr(self, name), field=name)
        for name in ("requested_at", "updated_at"):
            _instant(getattr(self, name), field=name)
        for name in ("accepted_at", "lease_expires_at"):
            if getattr(self, name) is not None:
                _instant(getattr(self, name), field=name)
        if self.lease_expires_at is not None and self.accepted_at is None:
            raise ValueError(
                "materialization request holds a lease with no acceptance: the lease is granted BY "
                "acceptance, and one without it would let the reconciler adopt a request nobody "
                "claimed")
        if self.lifecycle_state is RequestLifecycle.ACCEPTED and (
                self.accepted_at is None or self.lease_expires_at is None):
            raise ValueError(
                "materialization request is 'accepted' without an acceptance instant and a lease: "
                "'accepted' MEANS claimed-and-leased, and a lease-less accepted row is invisible to "
                "expired_requests while still non-terminal — the exact loss this table exists to "
                "prevent. Migration 1053 carries the same rule as a CHECK")


#: The fields that make a request THE request its idempotency key names. A retry that matches on all
#: of them is the same request and gets the stored row back; one that differs is a key reused for
#: different work, and answering it with the stored row would report the wrong request as queued.
IDEMPOTENT_IDENTITY_FIELDS: tuple[str, ...] = (
    "logical_group_name", "requested_by", "resolved_input_digest")

#: The complement, with the reason each field is NOT compared. Named rather than left implicit so
#: that a column added to the record forces a decision: the test suite asserts these two sets
#: partition the record's fields exactly, so a new identity-bearing column cannot be silently
#: omitted from the comparison above.
_NON_IDENTITY_FIELDS: frozenset[str] = frozenset({
    # The retry's own identifiers and mutable coordination state — never what makes it the same
    # request. `request_id` in particular is re-minted by a client that lost its first response,
    # which is the ordinary retry this table is built to absorb.
    "request_id", "idempotency_key", "lifecycle_state", "generation_id", "run_id",
    "requested_at", "accepted_at", "lease_expires_at", "updated_at",
    # Observed at the moment of asking, not part of what was asked: a flag flipped between the
    # first call and its retry must not turn one request into two runs.
    "authorized_roles", "activation_state",
})


def _row(row: Sequence[Any] | None) -> MaterializationRequestV1 | None:
    return None if row is None else MaterializationRequestV1(*row)


def _current(conn: DbConn, request_id: str) -> MaterializationRequestV1 | None:
    return _row(conn.execute(
        f"SELECT {_COLUMNS} FROM materialization_request WHERE request_id = %s",
        (request_id,)).fetchone())


def _must_exist(conn: DbConn, request_id: str) -> MaterializationRequestV1:
    existing = _current(conn, request_id)
    if existing is None:
        raise ValueError(
            f"no materialization request {request_id!r}: the request row is minted before any work "
            f"begins, so a missing one means the caller is advancing something nobody asked for")
    return existing


# ── the write surface ────────────────────────────────────────────────────────────────────────────


def record_request(
    conn: DbConn,
    *,
    request_id: str,
    logical_group_name: str,
    requested_by: str,
    authorized_roles: Sequence[str],
    idempotency_key: str,
    activation_state: Mapping[str, Any],
    resolved_input_digest: str | None = None,
) -> MaterializationRequestV1:
    """Record the request at ``requested`` — the run's identity, minted before any work begins.

    A duplicate ``idempotency_key`` returns the **existing** row rather than raising: a retried HTTP
    call must not become a second run, and answering the retry with the row it already has is the
    only reply that is true. The key naming a *different* request (another group, another actor,
    another resolved input) is refused instead — returning the stored row there would tell the
    caller their request was queued when a different one was, which is worse than the duplicate the
    key exists to prevent.
    """
    candidate = MaterializationRequestV1(
        request_id=request_id,
        logical_group_name=logical_group_name,
        requested_by=requested_by,
        authorized_roles=tuple(authorized_roles),
        idempotency_key=idempotency_key,
        activation_state=activation_state,
        lifecycle_state=RequestLifecycle.REQUESTED,
        generation_id=None,
        run_id=None,
        resolved_input_digest=resolved_input_digest,
        # Placeholders: the database stamps all three, so the record this function RETURNS is the
        # stored row rather than the caller's guess at it.
        requested_at=_datetime.datetime.now(tz=_datetime.UTC),
        accepted_at=None,
        lease_expires_at=None,
        updated_at=_datetime.datetime.now(tz=_datetime.UTC),
    )
    inserted = _row(conn.execute(
        "INSERT INTO materialization_request (request_id, logical_group_name, requested_by, "
        "authorized_roles, idempotency_key, activation_state, lifecycle_state, "
        "resolved_input_digest) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
        # The arbiter is named: a conflict on the PRIMARY KEY (one request_id reused for a second
        # request) is a caller bug and still raises, rather than being swallowed as a retry.
        f"ON CONFLICT (idempotency_key) DO NOTHING RETURNING {_COLUMNS}",
        (candidate.request_id, candidate.logical_group_name, candidate.requested_by,
         list(candidate.authorized_roles), candidate.idempotency_key,
         Jsonb(dict(candidate.activation_state)), candidate.lifecycle_state.value,
         candidate.resolved_input_digest)).fetchone())
    if inserted is not None:
        return inserted

    existing = _row(conn.execute(
        f"SELECT {_COLUMNS} FROM materialization_request WHERE idempotency_key = %s",
        (candidate.idempotency_key,)).fetchone())
    if existing is None:
        # Not an impossibility, and not an `assert` (which `python -O` strips, turning this into an
        # AttributeError on the next line): under an isolation level stricter than READ COMMITTED
        # the INSERT can see a concurrent inserter's conflict while this transaction's snapshot
        # cannot yet see the row. Nothing is wrong with the CALL, so this is not the module's
        # ValueError — it is a transient state the caller retries out of.
        raise RuntimeError(
            f"idempotency key {candidate.idempotency_key!r} conflicted with a row this "
            f"transaction's snapshot cannot see: another writer holds it uncommitted, so the "
            f"request is neither recorded nor readable here — retry in a fresh transaction")
    mismatched = [
        name for name in IDEMPOTENT_IDENTITY_FIELDS
        if getattr(existing, name) != getattr(candidate, name)]
    if mismatched:
        raise ValueError(
            f"idempotency key {candidate.idempotency_key!r} already names request "
            f"{existing.request_id!r}, which differs in {', '.join(mismatched)}: returning it would "
            f"tell the caller a request was queued when a different one was")
    return existing


def accept_request(conn: DbConn, *, request_id: str,
                   lease_seconds: float) -> MaterializationRequestV1:
    """Claim the request and grant its lease — ``requested`` → ``accepted``.

    Acceptance is the claim on the work, so it is a conditional UPDATE: two workers racing to accept
    one request cannot both win, because the loser matches no row. A request that is not
    ``requested`` is refused rather than re-claimed — handing one request to two workers would put
    two writers on one append-only event stream, which has no repair path.
    """
    seconds = _lease_seconds(lease_seconds)
    accepted = _row(conn.execute(
        # statement_timestamp(), not now(): now() is the TRANSACTION's start, so a worker that has
        # already spent minutes inside its transaction would be granted a lease that is already part
        # spent — and could be adopted by the reconciler while alive. statement_timestamp() is the
        # current statement's clock, and it is STABLE within the statement (clock_timestamp() is
        # not: two calls in one statement return different instants, which would make the granted
        # lease differ from the duration asked for by whatever the two readings drifted).
        "UPDATE materialization_request SET lifecycle_state = %s, "
        "accepted_at = statement_timestamp(), "
        "lease_expires_at = statement_timestamp() + make_interval(secs => %s) "
        f"WHERE request_id = %s AND lifecycle_state = %s RETURNING {_COLUMNS}",
        (RequestLifecycle.ACCEPTED.value, seconds, request_id,
         RequestLifecycle.REQUESTED.value)).fetchone())
    if accepted is None:
        existing = _must_exist(conn, request_id)
        raise ValueError(
            f"materialization request {request_id!r} is {existing.lifecycle_state.value!r}, not "
            f"'requested': acceptance is the claim on the work, and a second claim would hand one "
            f"request to two workers")
    return accepted


def renew_lease(conn: DbConn, *, request_id: str,
                lease_seconds: float) -> MaterializationRequestV1:
    """Extend the lease of a request that is being worked — legal from ``accepted``/``running``.

    Nothing is being worked in ``requested`` (nobody has claimed it) or in a terminal state (the
    work is over), and renewing there would keep a dead request out of the reconciler's reach for
    as long as the renewals kept coming.

    The new expiry runs from ``statement_timestamp()`` — this renewal's own clock. Measured from
    ``now()`` it would run from the transaction's start, so the renewal a long-running worker issues
    precisely because it is still alive would be the one that buys it the least time.
    """
    seconds = _lease_seconds(lease_seconds)
    renewed = _row(conn.execute(
        "UPDATE materialization_request "
        "SET lease_expires_at = statement_timestamp() + make_interval(secs => %s) "
        f"WHERE request_id = %s AND lifecycle_state = ANY(%s) RETURNING {_COLUMNS}",
        (seconds, request_id,
         sorted(state.value for state in _LEASED_STATES))).fetchone())
    if renewed is None:
        existing = _must_exist(conn, request_id)
        raise ValueError(
            f"materialization request {request_id!r} is {existing.lifecycle_state.value!r}: a lease "
            f"belongs to work in progress, so only "
            f"{sorted(state.value for state in _LEASED_STATES)} may renew one")
    return renewed


def advance_lifecycle(
    conn: DbConn,
    *,
    request_id: str,
    to_state: RequestLifecycle,
    generation_id: str | None = None,
    run_id: str | None = None,
) -> MaterializationRequestV1:
    """Move the request along a LEGAL edge of :data:`LEGAL_LIFECYCLE_TRANSITIONS`, and link it.

    ``accepted`` is NOT a target this function will move to, even though ``requested → accepted`` is
    a legal edge: acceptance *is* the granting of a lease, and this function takes no
    ``lease_seconds``, so it could only produce an ``accepted`` row with no ``accepted_at`` and no
    lease — non-terminal, and invisible to :func:`expired_requests`, which is precisely the losable
    run this table exists to prevent. :func:`accept_request` is the only door into ``accepted``, and
    migration ``1053`` carries the same rule as a CHECK so no other writer can open a second one.

    ``generation_id`` and ``run_id`` are stamped once and never overwritten: which compilation a
    request became, and which run carried it, are answers to questions that have one — and the plane
    those ids point into cannot be rewritten to match a second answer. Supplying a value that
    contradicts a stored one is refused; supplying the same value again is a no-op; supplying
    nothing leaves what is there.

    The refusals are ``ValueError`` (§14 has no member for a call assembled wrongly), and the UPDATE
    itself is conditional on the legal source states, so a concurrent advance is refused rather than
    silently applied twice.
    """
    target = RequestLifecycle(to_state)
    generation = _optional_text(generation_id, field="generation_id")
    run = _optional_text(run_id, field="run_id")
    if target is RequestLifecycle.ACCEPTED:
        raise ValueError(
            f"materialization request {request_id!r} cannot be advanced to 'accepted': acceptance "
            f"grants the lease, and this call carries no lease to grant — an accepted row without "
            f"one is non-terminal and invisible to the reconciler. Use accept_request()")
    existing = _must_exist(conn, request_id)

    if target not in LEGAL_LIFECYCLE_TRANSITIONS[existing.lifecycle_state]:
        legal = sorted(state.value for state in
                       LEGAL_LIFECYCLE_TRANSITIONS[existing.lifecycle_state])
        raise ValueError(
            f"materialization request {request_id!r} is {existing.lifecycle_state.value!r} and "
            f"cannot move to {target.value!r}: the legal moves from "
            f"{existing.lifecycle_state.value!r} are {legal or 'none — it is terminal'}")
    for name, supplied, stored in (("generation_id", generation, existing.generation_id),
                                   ("run_id", run, existing.run_id)):
        if supplied is not None and stored is not None and supplied != stored:
            raise ValueError(
                f"materialization request {request_id!r} already names {name} {stored!r}; "
                f"{supplied!r} would rewrite which compilation the run was, and the plane those "
                f"ids point into cannot be rewritten to agree")
    if target is RequestLifecycle.RUNNING and (run or existing.run_id) is None:
        raise ValueError(
            f"materialization request {request_id!r} cannot move to 'running' without a run_id: "
            f"'running' means a run was prepared, and the reconciler reads run evidence BY run id")

    moved = _row(conn.execute(
        "UPDATE materialization_request SET lifecycle_state = %s, "
        "generation_id = COALESCE(%s, generation_id), run_id = COALESCE(%s, run_id) "
        f"WHERE request_id = %s AND lifecycle_state = ANY(%s) RETURNING {_COLUMNS}",
        (target.value, generation, run, request_id,
         sorted(state.value for state in _LEGAL_FROM[target]))).fetchone())
    if moved is None:
        concurrent = _must_exist(conn, request_id)
        raise ValueError(
            f"materialization request {request_id!r} moved to "
            f"{concurrent.lifecycle_state.value!r} while this transition to {target.value!r} was "
            f"being applied: the lifecycle has a single writer per request, held by the lease")
    return moved


# ── the read surface ─────────────────────────────────────────────────────────────────────────────


def read_request(conn: DbConn, *, request_id: str) -> MaterializationRequestV1 | None:
    """The request as stored, or ``None`` when nobody asked for it."""
    return _current(conn, request_id)


def expired_requests(conn: DbConn, *,
                     now: _datetime.datetime) -> tuple[MaterializationRequestV1, ...]:
    """The reconciler's ONLY query: requests whose lease expired before ``now`` and which are not
    terminal — the ones whose worker may be gone.

    ``now`` is supplied rather than read from a clock here, matching the rest of the package: a
    store that minted its own instant would decide staleness by when it was *asked*, and no test
    could pin the boundary. Ordered by lease expiry with ``request_id`` as the tie-break, so two
    reconcilers draining one backlog see the same order rather than the table's physical one.

    A request in ``requested`` holds no lease and so is never returned: nobody has claimed it, and
    there is nothing to reconcile until somebody does.
    """
    boundary = _instant(now, field="expired_requests now")
    rows = conn.execute(_EXPIRED_REQUESTS_SQL, (boundary,)).fetchall()
    return tuple(MaterializationRequestV1(*row) for row in rows)
