"""An abandoned generation gets a VERDICT, not a retry — and a live one gets left alone.

▲ The case worth reading first is `test_a_RELEASED_MESSAGE_IS_NOT_ABANDONED`. It pins the trap that
makes the obvious implementation wrong: a request awaiting redelivery looks exactly like an
abandoned one unless you ask the right question.
"""
from __future__ import annotations

import pytest

from featuregen.materialize.generation_lane import (
    GENERATION_MESSAGE_PREFIX,
    enqueue_generation,
)
from featuregen.materialize.reconcile_generation import (
    abandoned_generation_count,
    reconcile_abandoned_generations,
)
from featuregen.overlay.upload.build_set_store import (
    GenerationStatusV1,
    advance_request,
    read_request,
    request_generation,
)
from tests.featuregen.materialize.test_generation_lane import (
    ENV,
    GROUP,
    _approval,
    _job,
    _set,
)


def _request(db, *, request_id="req-rec", members=("sel-a",)):
    """A recorded build set, an approval, a REQUESTED request and its queue message."""
    build_set, _ = _set(db, revision_id=f"bs-{request_id}", members=members,
                        target=f"trr-{request_id}")
    approval = _approval(db, build_set, ENV)
    rid, created = request_generation(
        db, request_id=request_id, build_set_revision_id=build_set, environment_id=ENV,
        requested_by="user:ops", requested_at="2026-08-21T00:00:00Z",
        generation_authorization_revision_id=approval)
    assert created
    enqueue_generation(db, job=_job(rid), environment_id=ENV, logical_group_name=GROUP)
    return rid


def _set_message_status(db, request_id: str, status: str) -> None:
    db.execute("UPDATE queue SET status = %s WHERE message_id = %s",
               (status, f"{GENERATION_MESSAGE_PREFIX}{request_id}"))


def _drop_message(db, request_id: str) -> None:
    db.execute("DELETE FROM queue WHERE message_id = %s",
               (f"{GENERATION_MESSAGE_PREFIX}{request_id}",))


# ══ THE TRAP ═══════════════════════════════════════════════════════════════════════════════════
def test_a_RELEASED_MESSAGE_IS_NOT_ABANDONED(db):
    """▲ THE CASE THE OBVIOUS IMPLEMENTATION GETS WRONG, and it fails quietly rather than loudly.

    After `fail_generation(permanent=False)` — the lane's "another worker holds it" release — the
    request is still CLAIMED and its queue row is back to `ready`, awaiting redelivery. On the
    tempting predicate ("live request, queue row not leased") that is byte-for-byte an abandoned
    request. Terminalizing it does damage nobody sees: the redelivery arrives, hits the lane's
    terminal short-circuit, and reports "already done" for a build that never happened.
    """
    rid = _request(db, request_id="req-released")
    advance_request(db, rid, GenerationStatusV1.CLAIMED)
    _set_message_status(db, rid, "ready")

    assert reconcile_abandoned_generations(db) == ()
    assert read_request(db, rid).status is GenerationStatusV1.CLAIMED


def test_a_LEASED_MESSAGE_MEANS_SOMEBODY_IS_WORKING_IT(db):
    """A live claim is the ordinary case, and the sweep must be invisible to it."""
    rid = _request(db, request_id="req-leased")
    advance_request(db, rid, GenerationStatusV1.CLAIMED)
    advance_request(db, rid, GenerationStatusV1.RUNNING)
    _set_message_status(db, rid, "leased")

    assert reconcile_abandoned_generations(db) == ()
    assert read_request(db, rid).status is GenerationStatusV1.RUNNING


# ══ THE WEDGE ══════════════════════════════════════════════════════════════════════════════════
def test_a_DEAD_MESSAGE_ON_A_CLAIMED_REQUEST_IS_TERMINALIZED(db):
    """▲ The wedge this module exists for. Without a verdict the request stays CLAIMED for ever,
    and `generation_request_one_live_attempt` then makes that build set unbuildable in this
    environment PERMANENTLY — the queue reclaimer returns the message to `ready` and touches
    nothing else, so the lane releases it for retry on every delivery, for ever."""
    rid = _request(db, request_id="req-dead")
    advance_request(db, rid, GenerationStatusV1.CLAIMED)
    _set_message_status(db, rid, "dead")

    judged = reconcile_abandoned_generations(db)

    assert [j.request_id for j in judged] == [rid]
    assert judged[0].was == "CLAIMED"
    assert read_request(db, rid).status is GenerationStatusV1.FAILED


def test_an_ABSENT_MESSAGE_READS_THE_SAME_AS_A_DEAD_ONE(db):
    """Both mean no delivery is coming. A sweep that only understood `dead` would strand every
    request whose queue row was reaped."""
    rid = _request(db, request_id="req-gone")
    advance_request(db, rid, GenerationStatusV1.CLAIMED)
    _drop_message(db, rid)

    assert [j.request_id for j in reconcile_abandoned_generations(db)] == [rid]
    assert read_request(db, rid).status is GenerationStatusV1.FAILED


def test_a_REQUEST_STRANDED_AT_REQUESTED_IS_ALSO_JUDGED(db):
    """▲ The class a claim-based sweep is structurally blind to. This one was never claimed at all
    — its message died before any worker reached it — so a query keyed on having-been-claimed would
    ignore it, and it would hold the one-live index for ever."""
    rid = _request(db, request_id="req-stranded")
    _set_message_status(db, rid, "dead")

    assert [j.request_id for j in reconcile_abandoned_generations(db)] == [rid]
    assert read_request(db, rid).status is GenerationStatusV1.FAILED


def test_the_VERDICT_IS_FAILED_NOT_REFUSED_and_says_why(db):
    """▲ FAILED is the platform's fault; REFUSED is a statement about the build set. Nothing was
    decided about this build — the worker was lost — and blurring the two teaches an operator to
    read an outage as a product answer. Migration 1094 draws exactly this line for verifications."""
    rid = _request(db, request_id="req-reason")
    advance_request(db, rid, GenerationStatusV1.CLAIMED)
    _set_message_status(db, rid, "dead")

    reconcile_abandoned_generations(db)

    request = read_request(db, rid)
    assert request.status is GenerationStatusV1.FAILED
    assert request.refusals == ()
    assert "platform fault" in request.failure_reason
    assert "request the build again" in request.failure_reason


def test_a_TERMINAL_REQUEST_IS_NOT_RE_JUDGED(db):
    """Its message is `done`, which is unreachable — so only the live-status filter keeps a finished
    build out of the sweep. Without it every finished build would be re-terminalized.

    ▲ REFUSED rather than SUCCEEDED, and the reason is 1095's chain working: a SUCCEEDED request
    must name a real `sealed_artifact_v2` row produced under the same authorization, so faking one
    here would mean fabricating an artifact to test a sweep that never looks at artifacts."""
    rid = _request(db, request_id="req-finished")
    advance_request(db, rid, GenerationStatusV1.CLAIMED)
    advance_request(db, rid, GenerationStatusV1.REFUSED,
                    refusals=[{"code": "NOT_RESOLVED", "detail": "no formula"}])
    _set_message_status(db, rid, "done")

    assert reconcile_abandoned_generations(db) == ()
    assert read_request(db, rid).status is GenerationStatusV1.REFUSED


# ══ THE GAUGE ══════════════════════════════════════════════════════════════════════════════════
def test_the_GAUGE_COUNTS_WITHOUT_WRITING(db):
    """An operator watching this number must not be the reason rows get terminalized — and a
    persistently non-zero gauge beside a zero sweep is how "the limit is too low" becomes visible."""
    rid = _request(db, request_id="req-gauge")
    advance_request(db, rid, GenerationStatusV1.CLAIMED)
    _set_message_status(db, rid, "dead")

    assert abandoned_generation_count(db) == 1
    assert read_request(db, rid).status is GenerationStatusV1.CLAIMED


def test_the_SWEEP_IS_BOUNDED_and_takes_the_OLDEST_first(db):
    """A bounded sweep that took the newest rows would starve the oldest for ever. Ordering by
    `updated_at` is what makes truncation safe rather than merely small."""
    first = _request(db, request_id="req-old")
    second = _request(db, request_id="req-new")
    for rid in (first, second):
        advance_request(db, rid, GenerationStatusV1.CLAIMED)
        _set_message_status(db, rid, "dead")
    db.execute("UPDATE generation_request SET updated_at = now() - interval '1 hour' "
               "WHERE request_id = %s", (first,))

    judged = reconcile_abandoned_generations(db, limit=1)

    assert [j.request_id for j in judged] == [first]
    assert read_request(db, second).status is GenerationStatusV1.CLAIMED
