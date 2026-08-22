"""The V2 build-set lane's RECONCILER: an abandoned generation gets a VERDICT, not a retry.

**Why this module exists at all.** :mod:`featuregen.materialize.reconcile` already does this job —
carefully, and wired into the worker — for ``materialization_request``, the LEGACY chain. It has
**zero** references to ``generation_request``. So the lane this platform is migrating *off* has crash
recovery and the lane it is migrating *onto* has none, and the cutover deletes the reconciled one.
Carried out naively that ends with strictly LESS crash recovery than before. This module is the port.

**THE WEDGE IT CLOSES.** ``generation_request_one_live_attempt`` is UNIQUE on
``(build_set_revision_id, environment_id)`` over the live statuses. A worker that dies between
``CLAIMED`` and terminal leaves a row nothing will ever move: the lane's redelivery path reads a
non-``REQUESTED``, non-terminal request, concludes *"another worker holds it"* and releases the
message for retry — correct while a worker is alive, and wrong forever once one is not. The queue
reclaimer returns the QUEUE row to ``ready`` and touches nothing else, so the request stays live and
**that build set can never be built again in that environment.**

**THE TRAP, AND IT IS WHY THE OBVIOUS PREDICATE IS WRONG.** *"Live request whose queue row is not
leased"* looks like the abandonment signal and is not: a request healthily awaiting redelivery after
``fail_generation(permanent=False)`` has a ``ready`` queue row and is byte-for-byte identical on that
test. Terminalizing it does QUIET damage — the redelivery then hits the lane's terminal
short-circuit and reports "already done" for a build that never happened.

So the predicate is the one :mod:`reconcile` derived: an abandoned request necessarily has an
**UNREACHABLE MESSAGE** — ``done``, ``dead``, or absent — never merely an unleased one. A ``ready``
or ``leased`` message means somebody still owns the work, and the only honest action is none.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from featuregen.materialize.reconcile import UNREACHABLE_MESSAGE_STATUSES
from featuregen.runtime.observability import counters, log
from featuregen.overlay.upload.build_set_store import (
    GenerationStatusV1,
    InvalidStatusMove,
    RequestMovedUnderneath,
    advance_request,
)

__all__ = [
    "DEFAULT_GENERATION_SWEEP_LIMIT",
    "ReconciledGeneration",
    "reconcile_abandoned_generations",
]

#: Bounded so one sweep cannot hold the worker tick open on a large backlog. The ordering below is
#: what makes a bounded sweep safe: the oldest abandoned requests are judged first, so a truncated
#: sweep leaves the NEWEST rows for the next tick rather than starving the oldest for ever.
DEFAULT_GENERATION_SWEEP_LIMIT = 50

#: The statuses a verdict may be written from. Deliberately INCLUDES ``REQUESTED``: a request
#: stranded behind a dead message was never claimed at all, and a sweep keyed on having-been-claimed
#: is structurally blind to that whole class.
_LIVE_STATUSES = ("REQUESTED", "CLAIMED", "RUNNING")

#: ▲ Spelled ``<> ALL`` over the UNREACHABLE set, not ``= ANY`` over a reachable one, for
#: :mod:`reconcile`'s reason: an unknown queue status must read as REACHABLE, so that a future fifth
#: status cannot quietly widen what this offers up for a verdict. Fail towards leaving work alone.
_ABANDONED_SQL = (
    "SELECT request_id, status FROM generation_request "
    "WHERE status = ANY(%s) "
    "AND NOT EXISTS (SELECT 1 FROM queue "
    "                WHERE queue.message_id = %s || generation_request.request_id "
    "                AND queue.status <> ALL(%s)) "
    "ORDER BY updated_at, request_id LIMIT %s"
)


@dataclass(frozen=True, slots=True)
class ReconciledGeneration:
    """One request this sweep judged, and what it was when the verdict was written."""

    request_id: str
    was: str


def reconcile_abandoned_generations(
    conn, *, limit: int = DEFAULT_GENERATION_SWEEP_LIMIT,
) -> tuple[ReconciledGeneration, ...]:
    """Terminalize generation requests whose message is unreachable. Returns what it judged.

    ``FAILED`` rather than ``REFUSED``: nothing was decided about the build set. The platform lost
    the worker, which is exactly the distinction migration 1094 draws for verifications and the one
    this lane must not blur — a refusal is a statement about the feature, and no such statement was
    ever made here.

    The verdict is written through :func:`advance_request`, so it inherits that function's
    compare-and-set: a request another writer moved between this sweep's read and its write is left
    to that writer rather than overwritten.
    """
    from featuregen.materialize.generation_lane import GENERATION_MESSAGE_PREFIX

    rows = conn.execute(
        _ABANDONED_SQL,
        (list(_LIVE_STATUSES), GENERATION_MESSAGE_PREFIX,
         sorted(UNREACHABLE_MESSAGE_STATUSES), limit)).fetchall()

    judged: list[ReconciledGeneration] = []
    for request_id, was in rows:
        try:
            advance_request(
                conn, request_id, GenerationStatusV1.FAILED,
                failure_reason=(
                    f"abandoned: the request was {was} and its queue message is no longer "
                    f"deliverable, so no worker will ever finish it. This is a platform fault, not "
                    f"a verdict about the build set — request the build again"))
        except RequestMovedUnderneath:
            # Somebody finished it between the read and the write. Their answer is the real one.
            continue
        except InvalidStatusMove:  # pragma: no cover — the query selects only live statuses
            continue
        judged.append(ReconciledGeneration(request_id=request_id, was=was))

    if judged:
        counters.incr("featuregen.generation.reconciled", len(judged))
        log("featuregen.generation.reconciled", level="warning",
            count=len(judged), request_ids=[r.request_id for r in judged])
    return tuple(judged)


def abandoned_generation_count(conn) -> int:
    """How many live requests currently have an unreachable message — the GAUGE, not the sweep.

    Separate from the sweep because a number an operator watches must not be a side effect of a
    bounded write: a persistently non-zero gauge with a zero sweep result means the limit is too low,
    and folding the two together hides exactly that.
    """
    from featuregen.materialize.generation_lane import GENERATION_MESSAGE_PREFIX

    row = conn.execute(
        "SELECT count(*) FROM generation_request "
        "WHERE status = ANY(%s) "
        "AND NOT EXISTS (SELECT 1 FROM queue "
        "                WHERE queue.message_id = %s || generation_request.request_id "
        "                AND queue.status <> ALL(%s))",
        (list(_LIVE_STATUSES), GENERATION_MESSAGE_PREFIX,
         sorted(UNREACHABLE_MESSAGE_STATUSES))).fetchone()
    return int(row[0])
