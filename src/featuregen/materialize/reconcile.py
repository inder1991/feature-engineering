"""Phase G §3.3 — the RECONCILER: an abandoned materialization run gets a VERDICT, not a retry.

**The hole this closes.** Task 7 made ``compile_feature_group`` reachable from shipped code, and in
doing so made three distinct classes of request able to strand with nothing left to drain them.
:func:`~featuregen.materialize.request_store.expired_requests` — the query migration 1053 built its
partial index for — has had **zero callers** since Task 1. This module is its caller, and the owner
of all three classes.

**THE THREE CLASSES, and where each comes from.**

* **P1 — the worker died and the message is dead.** The request is ``accepted`` with an expired
  lease and its queue row is ``dead``. Produced by ``_unclaimable`` (a delivery found a claim it did
  not make, refused to mint a second generation under one durable identity, and dead-lettered the
  message) followed by the dead worker's lease running out.
* **P2 — the message is dead and the claim is still LIVE.** The same state one step earlier.
  Another worker may be compiling it *right now*, so the only honest action is none: when that
  worker's lease runs out the request becomes P1 and this sweep judges it then. P2 is resolved by
  TIME, never by a verdict.
* **P3 — stranded at ``requested`` behind a dead message.** No lease was ever granted, so it is
  **structurally invisible** to ``expired_requests`` (whose predicate requires
  ``lease_expires_at IS NOT NULL``). A reconciler built on that one query silently ignores the whole
  class — which is why this module issues TWO.

**THE TRAP: A RELEASED LEASE IS INDISTINGUISHABLE FROM AN ABANDONED ONE.** Task 7's retry path
releases a claim by *expiring* the lease (``_RELEASED_LEASE_SECONDS``), because migration 1053
CHECKs that an ``accepted`` row HAS one — "nobody is working this" cannot be spelled as a NULL. So
for the whole backoff window a request that is healthily awaiting redelivery is, on the lease alone,
byte-for-byte P1. Terminalize it and the damage is QUIET: the redelivery arrives, hits the chain's
terminal short-circuit, and the lane reports ``replayed`` — "already done" — for a compile that
never happened.

So this module **consults the queue row, not the lease alone**: a message that is ``ready`` or
``leased`` has an owner and the request is left exactly as it is, however expired its lease looks.
That one rule carries three separate guarantees, and all three are load-bearing:

1. the release window above is never mistaken for abandonment;
2. a live compile is never judged — while ``compile_feature_group`` runs, its message is ``leased``
   for the whole duration;
3. **this sweep can never block on a row lock.** ``_commit`` holds a row lock on
   ``materialization_request`` for its entire transaction, L0 subprocess included. The only rows
   this sweep writes are rows whose message is unreachable, which by (2) is never a row a live
   compile holds — so the worker's single connection cannot be parked here for an L0 timeout.

**A VERDICT COMES FROM EVIDENCE, AND G-1'S EVIDENCE IS NARROW.** Plan §3.3 decides a prepared run
from its staging manifests; G-1 submits nothing and produces none. What G-1 does produce is:

* the **absence** proved by ``_commit``'s atomicity — a durably-visible ``accepted`` request means
  no generation, no compiled artifact, no validation report and no run event exist, because every
  one of those writes is inside the single transaction that also leaves the request terminal. That
  absence is the evidence behind the one verdict this module writes without reading the plane, and
  it is CHECKED (``generation_id``/``run_id`` are still unstamped) rather than merely argued;
* the plane's own **terminal event**, for a request that reached ``running``.

Everything else is refused. ``NO_LEGAL_TERMINAL`` and ``NO_RUN_EVIDENCE`` are verdicts *about this
release*, reported and counted rather than papered over with a state change nothing supports.

**NOTHING HERE RETRIES A SUBMISSION OR RE-DRIVES A COMPILE** (§3.3: a re-run is a NEW request). This
module imports no chain, enqueues nothing, and touches no queue row. Its only writes are
``advance_lifecycle`` along edges that were already legal.
"""
from __future__ import annotations

import datetime as _datetime
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from featuregen.contracts.db import DbConn
from featuregen.materialize.control_plane import RunEventKind, read_run_events
from featuregen.materialize.queue_lane import (
    MATERIALIZATION_MESSAGE_PREFIX,
    materialization_message_id,
)
from featuregen.materialize.request_store import (
    LEGAL_LIFECYCLE_TRANSITIONS,
    NON_TERMINAL_PREDICATE,
    REQUEST_COLUMNS,
    MaterializationRequestV1,
    RequestLifecycle,
    advance_lifecycle,
    expired_requests,
)
from featuregen.runtime.observability import counters, log

__all__ = [
    "DEFAULT_SWEEP_LIMIT",
    "MIRRORED_TERMINALS",
    "REACHABLE_MESSAGE_STATUSES",
    "ReconciledRequest",
    "ReconciliationSweep",
    "ReconciliationVerdict",
    "reconcile_abandoned_requests",
]

#: How many candidates ONE sweep judges. Bounded because this runs on the worker's single connection
#: alongside the relay, the timers and every poller: a backlog drains over ticks a second apart,
#: which costs nothing next to how long it took to accumulate.
DEFAULT_SWEEP_LIMIT = 50

#: The queue statuses from which a message can still reach a worker. ``ready`` is deliverable (a
#: future ``available_at`` is a backoff, not an ending) and ``leased`` is held right now — and an
#: EXPIRED lease on a ``leased`` row is not an ending either, because ``reclaim_stuck_queue`` returns
#: it to ``ready``. ``done`` and ``dead`` are the two that mean no delivery is ever coming again,
#: and neither can be reversed by the runtime: ``claim_materialization`` only takes ``ready`` rows.
REACHABLE_MESSAGE_STATUSES: frozenset[str] = frozenset({"ready", "leased"})

#: The plane terminals a ``running`` request's verdict may be READ FROM, and the lifecycle each one
#: means. These are exactly the two ``chain._commit`` writes, with exactly the pairing it writes them
#: in: ``PUBLICATION_REFUSED`` beside ``committed`` (compiled, sealed, build PROVEN, publication
#: unproven) and ``RUN_FAILED`` beside ``failed``. No other terminal kind is mapped, because no code
#: path in G-1 can write one — ``PUBLISHED`` is forbidden outright (§3.5) and ``GATES_FAILED``
#: asserts a computation G-1 never performs — so a request carrying one is a state this release
#: cannot account for, and guessing its lifecycle would be inventing the evidence.
MIRRORED_TERMINALS: dict[RunEventKind, RequestLifecycle] = {
    RunEventKind.PUBLICATION_REFUSED: RequestLifecycle.COMMITTED,
    RunEventKind.RUN_FAILED: RequestLifecycle.FAILED,
}


class ReconciliationVerdict(StrEnum):
    """What one sweep decided about one request. Three of the seven change nothing, deliberately."""

    #: Its message can still be delivered. LEFT ALONE — this is the release-window guard.
    OWNED = "owned"
    #: No delivery is coming, but the request holds a LIVE lease: a worker may be compiling it.
    #: LEFT ALONE. P2 is resolved by the lease running out, never by a verdict.
    LEASED = "leased"
    #: Abandoned at ``accepted`` with nothing recorded anywhere. TERMINALIZED ``failed``.
    FAILED = "failed"
    #: Abandoned at ``running`` with a ``PUBLICATION_REFUSED`` terminal already on the plane.
    #: TERMINALIZED ``committed`` — a reading of the plane, not a decision about the run.
    COMMITTED = "committed"
    #: Abandoned at ``requested``. There is NO legal edge from ``requested`` to a terminal state
    #: (§3.2 ships ``requested → accepted`` only), and laundering the request through ``accepted``
    #: to reach one would record that a worker claimed work nobody ever claimed. REPORTED, not acted
    #: on: giving this class a terminal is a §3.2 state-machine decision, not a reconciler detail.
    NO_LEGAL_TERMINAL = "no_legal_terminal"
    #: Abandoned at ``running`` and the plane holds no terminal this release can read. §3.3 decides a
    #: prepared run from its staging manifests and G-1 produces none. REPORTED, not acted on.
    NO_RUN_EVIDENCE = "no_run_evidence"
    #: The request moved between being judged and being written — a concurrent adopter or a compile
    #: that finished. Nothing was written: ``advance_lifecycle``'s UPDATE is conditional on the
    #: states it is legal from, so the loser of the race matches no row.
    RACED = "raced"

    def is_terminalizing(self) -> bool:
        """Whether this verdict WROTE a lifecycle. Four of the seven never touch the row."""
        return self in _TERMINALIZING


_TERMINALIZING: frozenset[ReconciliationVerdict] = frozenset(
    {ReconciliationVerdict.FAILED, ReconciliationVerdict.COMMITTED})


@dataclass(frozen=True, slots=True)
class ReconciledRequest:
    """One candidate, as FOUND and as judged.

    ``lifecycle_state`` and ``message_status`` are what the sweep SAW, never what it left behind —
    a report that showed the state it wrote would make a verdict unfalsifiable from its own record.
    """

    request_id: str
    lifecycle_state: RequestLifecycle
    #: The queue row's status, or ``None`` when the request has no message at all.
    message_status: str | None
    verdict: ReconciliationVerdict
    detail: str


@dataclass(frozen=True, slots=True)
class ReconciliationSweep:
    """What one bounded pass considered and what it decided — the whole of this module's output."""

    considered: tuple[ReconciledRequest, ...]

    @property
    def terminalized(self) -> int:
        """How many requests this sweep gave a terminal lifecycle. Every other candidate was left
        exactly as it was found, so a second sweep over an unchanged world terminalizes zero."""
        return sum(1 for row in self.considered if row.verdict.is_terminalizing())

    def verdict_for(self, request_id: str) -> ReconciliationVerdict | None:
        """This sweep's verdict on one request, or ``None`` when it was not a candidate."""
        for row in self.considered:
            if row.request_id == request_id:
                return row.verdict
        return None


# ── the second query, and why one is not enough ──────────────────────────────────────────────────

#: Non-terminal requests whose message can no longer be delivered — the signal ``expired_requests``
#: cannot produce, because a request that was never claimed holds no lease to expire.
#:
#: The two queries answer two different questions and NEITHER implies the other. This one asks "is
#: anything ever going to deliver this request's job again?", which is a fact about the QUEUE; the
#: other asks "is the worker that claimed it still alive?", which is a fact about the REQUEST. A
#: verdict needs both, and P3 is the class that has only the first.
#:
#: ``NON_TERMINAL_PREDICATE`` is imported rather than re-typed so this reaches 1053's partial index
#: (see its own comment for why the terminal states are literals). The correlated subquery is a
#: unique-index lookup on ``queue.message_id`` per candidate row, and the candidate set is already
#: narrowed to the non-terminal requests, which is the small set the partial index exists to keep
#: small.
_UNREACHABLE_MESSAGE_SQL = (
    f"SELECT {REQUEST_COLUMNS} FROM materialization_request "
    f"WHERE {NON_TERMINAL_PREDICATE} "
    f"AND NOT EXISTS (SELECT 1 FROM queue "
    f"WHERE queue.message_id = %s || materialization_request.request_id "
    f"AND queue.status = ANY(%s)) "
    f"ORDER BY requested_at, request_id LIMIT %s")


def _unreachable_message_requests(
    conn: DbConn, *, limit: int,
) -> tuple[MaterializationRequestV1, ...]:
    rows = conn.execute(
        _UNREACHABLE_MESSAGE_SQL,
        (MATERIALIZATION_MESSAGE_PREFIX, sorted(REACHABLE_MESSAGE_STATUSES), limit)).fetchall()
    return tuple(MaterializationRequestV1(*row) for row in rows)


@dataclass(frozen=True, slots=True)
class _Message:
    """The queue row a request's job is frozen on, as far as a verdict cares."""

    status: str
    last_error: str | None

    @property
    def reachable(self) -> bool:
        return self.status in REACHABLE_MESSAGE_STATUSES


def _message(conn: DbConn, request_id: str) -> _Message | None:
    """The request's message, or ``None`` when it has none.

    ``None`` is NOT an error state: it is read the same way ``dead`` is, because both mean no
    delivery is coming. The route mints the request row and the queue row in ONE transaction, so a
    request with no message is either a caller that bypassed the route or a transaction that rolled
    back the enqueue alone — and in every case nothing will drive it.
    """
    row = conn.execute("SELECT status, last_error FROM queue WHERE message_id = %s",
                       (materialization_message_id(request_id),)).fetchone()
    return None if row is None else _Message(status=row[0], last_error=row[1])


# ── the sweep ────────────────────────────────────────────────────────────────────────────────────


def reconcile_abandoned_requests(
    conn: DbConn,
    *,
    now: _datetime.datetime,
    limit: int = DEFAULT_SWEEP_LIMIT,
) -> ReconciliationSweep:
    """Judge up to ``limit`` non-terminal materialization requests, and write only what is proved.

    ``now`` is supplied rather than read here, matching ``expired_requests`` and the rest of the
    package: a sweep that minted its own instant would decide staleness by when it happened to be
    asked, and no test could pin the boundary.

    **NOT wrapped in a transaction, deliberately.** Each verdict is one conditional UPDATE, which is
    already atomic; an enclosing transaction would only make one candidate's lock wait hold every
    earlier candidate's correct verdict hostage, and would keep a lock open across the whole batch
    on a connection the rest of the worker's tick is waiting for.

    Args:
        conn: the worker's connection. Autocommit is expected (``runtime/worker.py``'s contract);
            inside a caller's transaction each UPDATE simply participates in it.
        now: an offset-aware instant. A lease is ``timestamptz``; a naive comparison would be read
            in whatever zone the session holds.
        limit: the bound on ONE sweep.

    Returns:
        A :class:`ReconciliationSweep` naming every candidate and what was decided about it.
    """
    candidates = _candidates(conn, now=now, limit=limit)
    judged = tuple(_judge(conn, request, now=now) for request in candidates)
    _report(judged)
    return ReconciliationSweep(judged)


#: The non-terminal states from which SOME terminal state is one legal edge away — derived from the
#: shipped transition set, never listed by hand. ``requested`` is the one that is not in it, which is
#: exactly what makes P3 unactionable in this release, and it is why the ordering below prefers the
#: others: a class this release cannot act on must not consume the budget a class it CAN act on
#: needs. Without that, a standing set of stranded ``requested`` rows would sit at the head of an
#: oldest-first sweep forever and hide every abandoned claim behind it.
_HAS_TERMINAL_EDGE: frozenset[RequestLifecycle] = frozenset(
    state for state, targets in LEGAL_LIFECYCLE_TRANSITIONS.items()
    if any(target.is_terminal() for target in targets))


def _candidates(
    conn: DbConn, *, now: _datetime.datetime, limit: int,
) -> tuple[MaterializationRequestV1, ...]:
    """The union of the two queries — actionable first, then oldest first, bounded.

    Both queries carry the bound in SQL: this runs on the worker's tick, and "the stuck set" is not
    a set with a natural ceiling. De-duplicated by ``request_id``, because P1 satisfies both
    predicates at once and is the whole point of running both.
    """
    found: dict[str, MaterializationRequestV1] = {}
    for request in expired_requests(conn, now=now, limit=limit):
        found.setdefault(request.request_id, request)
    for request in _unreachable_message_requests(conn, limit=limit):
        found.setdefault(request.request_id, request)
    ordered = sorted(found.values(),
                     key=lambda request: (request.lifecycle_state not in _HAS_TERMINAL_EDGE,
                                          request.requested_at, request.request_id))
    return tuple(ordered[:limit])


def _judge(conn: DbConn, request: MaterializationRequestV1, *,
           now: _datetime.datetime) -> ReconciledRequest:
    """One request's verdict. The two "leave it alone" tests come FIRST, and their order is the
    strength of the evidence: a deliverable message is a durable owner, while a live lease is only
    the belief that a process is still breathing."""
    message = _message(conn, request.request_id)
    if message is not None and message.reachable:
        return _left(request, message, ReconciliationVerdict.OWNED, detail=(
            f"the request's message is {message.status!r}, so a delivery is still coming: this is "
            f"either a scheduled retry (a released claim looks exactly like an abandoned one) or a "
            f"worker holding it right now"))
    if request.lease_expires_at is not None and request.lease_expires_at > now:
        return _left(request, message, ReconciliationVerdict.LEASED, detail=(
            f"no delivery is coming, but the claim is live until {request.lease_expires_at}: a "
            f"worker may be compiling this request now, and deciding it is over would race a live "
            f"compile onto an append-only plane"))
    return _abandoned(conn, request, message)


def _abandoned(conn: DbConn, request: MaterializationRequestV1,
               message: _Message | None) -> ReconciledRequest:
    """No owner, no claim — now what does the EVIDENCE support?"""
    state = request.lifecycle_state
    if state is RequestLifecycle.REQUESTED:
        return _left(request, message, ReconciliationVerdict.NO_LEGAL_TERMINAL, detail=(
            "the request was never claimed and its message is gone, and §3.2 ships no edge from "
            "'requested' to a terminal state. Reaching one through 'accepted' would record a claim "
            "no worker ever made; adding the edge is a state-machine decision, not this sweep's"))
    if state is RequestLifecycle.ACCEPTED:
        return _accepted(conn, request, message)
    if state is RequestLifecycle.RUNNING:
        return _running(conn, request, message)
    # Unreachable: both queries carry NON_TERMINAL_PREDICATE, and terminal states are exactly the
    # two it excludes. Stated rather than assumed, because a state added to the enum without a
    # branch here would otherwise fall through to a silent "nothing happened".
    return _left(request, message, ReconciliationVerdict.NO_RUN_EVIDENCE, detail=(
        f"lifecycle state {state.value!r} has no reconciliation rule in this release"))


def _accepted(conn: DbConn, request: MaterializationRequestV1,
              message: _Message | None) -> ReconciledRequest:
    """A claim whose worker is gone and whose message is gone — G-1's one provable abandonment.

    The evidence is an ABSENCE, and it is checked rather than argued. ``advance_lifecycle`` stamps
    ``generation_id`` and ``run_id`` in the SAME statement that moves the request to ``running``,
    inside ``_commit``'s single transaction — so an ``accepted`` request with neither stamped proves
    that transaction never committed, i.e. that no generation, no compiled artifact, no validation
    report and no run event exist for it. There is nothing on the plane to read, and ``failed`` is
    what ``RequestLifecycle.FAILED`` already documents: "the attempt is over without a committed run
    (including reconciled abandonment)".

    If either id IS stamped the absence is not proved, and this sweep refuses rather than guessing.
    """
    if request.generation_id is not None or request.run_id is not None:
        return _left(request, message, ReconciliationVerdict.NO_RUN_EVIDENCE, detail=(
            f"'accepted' names generation {request.generation_id!r} / run {request.run_id!r}, "
            f"which the chain stamps only when it moves a request to 'running': the absence that "
            f"would prove nothing was recorded does not hold, so the plane must be read by "
            f"something that can (G-2's staging-manifest reader)"))
    return _terminalize(conn, request, message, RequestLifecycle.FAILED,
                        verdict=ReconciliationVerdict.FAILED, detail=(
                            "the claim expired and no delivery is coming; the chain records a run "
                            "in ONE transaction, so an un-stamped 'accepted' request proves nothing "
                            "was written to the plane"))


def _running(conn: DbConn, request: MaterializationRequestV1,
             message: _Message | None) -> ReconciledRequest:
    """A request that reached ``running`` — the one case where the plane may already hold a verdict.

    This is not durably reachable in G-1 (``_commit`` moves ``accepted → running →
    committed|failed`` inside one transaction, so no reader ever observes the middle state), which is
    exactly why it is handled by READING rather than by deciding: if the state is ever observed, the
    code that observes it does not understand how it got there. The plane's own terminal event is
    the only thing that does.

    No terminal event, or one this release cannot account for, is ``NO_RUN_EVIDENCE``. §3.3 decides a
    prepared run from its staging manifests; G-1 submits nothing and writes none, and marking the
    request ``failed`` while an open run stream says otherwise would put the two records in
    disagreement on no evidence at all.
    """
    events = read_run_events(conn, request.run_id) if request.run_id else ()
    terminal = events[-1] if events and events[-1].is_terminal() else None
    target = None if terminal is None else MIRRORED_TERMINALS.get(terminal.event_kind)
    if terminal is None or target is None:
        return _left(request, message, ReconciliationVerdict.NO_RUN_EVIDENCE, detail=(
            f"the run stream for {request.run_id!r} holds "
            f"{'no terminal event' if terminal is None else terminal.event_kind.value}, and this "
            f"release produces no staging manifests to decide it from (§3.3): a verdict here would "
            f"be invented, not read"))
    return _terminalize(
        conn, request, message, target,
        verdict=(ReconciliationVerdict.COMMITTED if target is RequestLifecycle.COMMITTED
                 else ReconciliationVerdict.FAILED),
        detail=f"the plane already holds {terminal.event_kind.value} for run {request.run_id!r}")


def _terminalize(
    conn: DbConn,
    request: MaterializationRequestV1,
    message: _Message | None,
    target: RequestLifecycle,
    *,
    verdict: ReconciliationVerdict,
    detail: str,
) -> ReconciledRequest:
    """Write the verdict along an edge that was ALREADY legal, or report the race that beat it to it.

    The legality check is not defensive duplication of ``advance_lifecycle``'s: it is what makes the
    ``except`` below narrow. Once the edge is known legal from the state that was judged, the only
    way that call can refuse is that the row moved underneath this sweep — a concurrent adopter, or a
    compile that committed — and reporting THAT as ``raced`` is honest, where swallowing an illegal
    transition would hide a bug in this module behind the same word.
    """
    if target not in LEGAL_LIFECYCLE_TRANSITIONS[request.lifecycle_state]:
        raise ValueError(
            f"reconciling {request.request_id!r} would move {request.lifecycle_state.value!r} to "
            f"{target.value!r}, which is not a legal edge: a verdict is written along the shipped "
            f"state machine or it is not written")
    try:
        advance_lifecycle(conn, request_id=request.request_id, to_state=target)
    except ValueError as exc:
        return _left(request, message, ReconciliationVerdict.RACED, detail=(
            f"the request moved while this verdict was being applied, so nothing was written: "
            f"{exc}"))
    log("materialize.reconcile.verdict", level="warning", request_id=request.request_id,
        found=request.lifecycle_state.value, verdict=verdict.value,
        message_status=None if message is None else message.status,
        message_error=None if message is None else message.last_error, detail=detail)
    return _left(request, message, verdict, detail=detail)


def _left(request: MaterializationRequestV1, message: _Message | None,
          verdict: ReconciliationVerdict, *, detail: str) -> ReconciledRequest:
    """One judged candidate, recorded as it was FOUND."""
    return ReconciledRequest(
        request_id=request.request_id, lifecycle_state=request.lifecycle_state,
        message_status=None if message is None else message.status, verdict=verdict, detail=detail)


def _report(judged: Sequence[ReconciledRequest]) -> None:
    """Counters for what was DONE, gauges for what is STANDING — and no per-tick log line.

    The distinction is the difference between a usable signal and 86 000 lines a day. A verdict is
    an event and is counted (and logged, in :func:`_terminalize`); a request stranded with no legal
    terminal is a CONDITION that persists across every sweep until somebody acts on it, so it is a
    gauge. Logging one per tick would bury the next real signal inside it.

    Every member is gauged on every sweep, including the zeroes: a gauge that only appears when it
    is non-zero cannot be alerted on, because "no value" and "nothing wrong" look the same.
    """
    counts: dict[ReconciliationVerdict, int] = {verdict: 0 for verdict in ReconciliationVerdict}
    for row in judged:
        counts[row.verdict] += 1
    for verdict, count in counts.items():
        counters.gauge(f"materialize.reconcile.{verdict.value}", count)
        if count and verdict.is_terminalizing():
            counters.incr(f"materialize.reconcile.{verdict.value}", count)
