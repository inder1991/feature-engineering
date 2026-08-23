"""Step 13 — the ON-DEMAND verification lifecycle, and the question step 14 asks it.

**The gap this closes.** ``verification_attempt`` (1080) records what an attempt DID — its execution
hash, its staging path, the sealed artifact it verified. It has no status, so there is no state in
which a verification has been ASKED FOR and not yet run, and no way to tell "still running" from
"the worker died" from "nothing consumes this table". ``generation_request`` says exactly that in its
own comment and fixes it for generation; this is the same lifecycle over verification.

**PASSED / FAILED / REFUSED are three answers, not two.** REFUSED is a PRODUCT result: the artifact
does not verify, and the findings name which check and why. FAILED is an OUTAGE: the platform could
not finish, and somebody has to fix the platform. Collapsing them would tell an operator their
feature is broken when the cluster was down, and they would go and change a correct formula.

**Verification never implies permission.** Nothing here says "publishable", exactly as 1080 says
nothing: a field in this table would eventually be read as consent. :func:`current_verification` is
what step 14 asks — *is there a PASSING verification for this exact artifact, right now* — and the
answer is about evidence. Whether to publish on it is a decision somewhere else.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from enum import StrEnum

from featuregen.contracts.db import DbConn

__all__ = [
    "InvalidVerificationMove",
    "VerificationStatusV1",
    "advance_verification",
    "current_verification",
    "request_verification",
]


class VerificationStatusV1(StrEnum):
    """Where a verification has got to."""

    REQUESTED = "REQUESTED"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    #: The artifact verified. The ONLY status step 14 accepts as evidence.
    PASSED = "PASSED"
    #: The artifact does not verify. A PRODUCT result, naming which check.
    REFUSED = "REFUSED"
    #: The platform could not finish. An OPERATIONAL result, and somebody's to fix.
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in (VerificationStatusV1.PASSED, VerificationStatusV1.REFUSED,
                        VerificationStatusV1.FAILED, VerificationStatusV1.CANCELLED)


#: The path forward. One step at a time, so a PASSED request's history says it was actually worked
#: on rather than declared.
_NEXT: Mapping[VerificationStatusV1, frozenset[VerificationStatusV1]] = {
    VerificationStatusV1.REQUESTED: frozenset({VerificationStatusV1.CLAIMED}),
    VerificationStatusV1.CLAIMED: frozenset({VerificationStatusV1.RUNNING}),
    VerificationStatusV1.RUNNING: frozenset({VerificationStatusV1.PASSED}),
    VerificationStatusV1.PASSED: frozenset(),
    VerificationStatusV1.REFUSED: frozenset(),
    VerificationStatusV1.FAILED: frozenset(),
    VerificationStatusV1.CANCELLED: frozenset(),
}

#: Reachable from any NON-TERMINAL state. A run can be refused, break or be cancelled at any point,
#: and PASSED is deliberately absent: passing is something a run EARNS by reaching RUNNING first.
_FROM_ANYWHERE = frozenset({
    VerificationStatusV1.REFUSED, VerificationStatusV1.FAILED, VerificationStatusV1.CANCELLED})


class VerificationMovedUnderneath(RuntimeError):
    """The row changed between the read and the write — another worker advanced it.

    The same second line of defence `advance_request` grew for generation: the queue lease is what
    stops two workers holding one request, and this is what stops an expired lease's still-alive
    holder finishing the job as though nothing happened.
    """


class InvalidVerificationMove(ValueError):
    """A move off the lifecycle's path — a caller error, never a governed verdict."""


def request_verification(
    conn: DbConn,
    *,
    request_id: str,
    sealed_artifact_id: str,
    environment_id: str,
    requested_by: str,
    requested_at: str,
) -> tuple[str, bool]:
    """Ask for a verification, or return the LIVE one for this artifact and environment.

    Returns ``(request_id, created)``. ``created=False`` is the double-click answer.

    Idempotent on the WORK — the artifact and the environment — rather than on a caller-supplied
    key, because a client minting a fresh key per click would defeat a key-based guard and a
    verification costs a cluster run. The partial index scopes this to LIVE requests, so asking
    again after a refusal or an outage is allowed: the guard is against double-clicks, not against
    re-verifying something that changed.
    """
    inserted = conn.execute(
        "INSERT INTO verification_request (request_id, sealed_artifact_id, environment_id, "
        "status, requested_by, requested_at) VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT DO NOTHING RETURNING request_id",
        (request_id, sealed_artifact_id, environment_id,
         VerificationStatusV1.REQUESTED.value, requested_by, requested_at)).fetchone()
    if inserted is not None:
        return inserted[0], True

    live = conn.execute(
        "SELECT request_id FROM verification_request "
        "WHERE sealed_artifact_id = %s AND environment_id = %s "
        "AND status IN ('REQUESTED','CLAIMED','RUNNING')",
        (sealed_artifact_id, environment_id)).fetchone()
    if live is None:
        raise RuntimeError(
            f"verification request {request_id!r} was refused by the live-request guard and no "
            f"live request exists for {sealed_artifact_id!r} in {environment_id!r}: the two "
            f"readings disagree, and proceeding would start a cluster run nobody could find")
    return live[0], False


def advance_verification(
    conn: DbConn,
    request_id: str,
    to: VerificationStatusV1,
    *,
    execution_hash: str | None = None,
    findings: Sequence[Mapping[str, str]] = (),
    failure_reason: str | None = None,
) -> VerificationStatusV1:
    """Move ONE step and return the new status. The caller commits.

    Raises:
        InvalidVerificationMove: the move is not on the lifecycle's path. A worker that jumped
            straight to PASSED would produce a green verification whose history says nothing ever
            ran — which is precisely the evidence step 14 must not be able to be handed.
    """
    row = conn.execute(
        "SELECT status FROM verification_request WHERE request_id = %s", (request_id,)).fetchone()
    if row is None:
        raise InvalidVerificationMove(f"verification request {request_id} does not exist")

    current = VerificationStatusV1(row[0])
    permitted = _NEXT[current] | (frozenset() if current.is_terminal else _FROM_ANYWHERE)
    if to not in permitted:
        raise InvalidVerificationMove(
            f"verification request {request_id} cannot move {current.value} → {to.value}. "
            f"Permitted: {sorted(s.value for s in permitted) or 'nothing — it is terminal'}. "
            f"A re-verification is a NEW request against the same artifact, never a step backwards")

    payload = json.dumps([dict(finding) for finding in findings]) if findings else None
    # ▲ COMPARE-AND-SET, not read-then-write — hardened when the worker arrived (§9.0). The read
    # above validates the move; the UPDATE re-states the status in its own WHERE clause, so the row
    # is only written if it is still what was validated. Without this, two workers both read
    # REQUESTED, both find CLAIMED legal, and both write it — the second silently overwriting the
    # first with a lifecycle table that permitted every individual step.
    moved = conn.execute(
        "UPDATE verification_request SET status = %s, "
        "execution_hash = COALESCE(%s, execution_hash), "
        "findings = CASE WHEN %s::jsonb IS NULL THEN findings ELSE %s::jsonb END, "
        "failure_reason = %s, updated_at = now() "
        "WHERE request_id = %s AND status = %s RETURNING status",
        (to.value, execution_hash, payload, payload, failure_reason, request_id,
         current.value)).fetchone()
    if moved is None:
        raise VerificationMovedUnderneath(
            f"verification request {request_id} was {current.value} when this move was validated "
            f"and is not any more: another worker advanced it. This move is ABANDONED rather than "
            f"applied over theirs")
    return to


def current_verification(
    conn: DbConn, *, sealed_artifact_id: str, environment_id: str,
) -> tuple[str, str] | None:
    """The PASSING verification for this exact artifact here, or ``None``.

    **Keyed on the artifact, never on the feature or the group.** A verification is evidence about
    the bytes that were rendered; a passing verdict for a previous artifact of the same feature says
    nothing about this one, and letting the question be asked by feature name is how a re-rendered
    artifact inherits the old one's green tick.

    Returns:
        ``(request_id, execution_hash)`` for the most recent PASSED request, or ``None``. ``None``
        covers "never asked", "asked and running" and "asked and refused" alike — deliberately, at
        THIS boundary: the caller is step 14, and every one of those is equally not-evidence.
        Anything that wants to tell them apart reads the row.
    """
    row = conn.execute(
        "SELECT request_id, execution_hash FROM verification_request "
        "WHERE sealed_artifact_id = %s AND environment_id = %s AND status = 'PASSED' "
        "ORDER BY updated_at DESC LIMIT 1",
        (sealed_artifact_id, environment_id)).fetchone()
    return (row[0], row[1]) if row is not None else None
