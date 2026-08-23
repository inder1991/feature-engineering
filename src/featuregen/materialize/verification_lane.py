"""§9.0 — the durable sandbox VERIFICATION worker. The lane the route's docstring promised.

**What was missing, exactly.** ``POST /feature-execution/verifications`` said *"a worker executes
it"* and recorded a `verification_attempt` at REQUEST time — an attempt that never ran. No worker
claimed anything: the durable worker's handler set held one handler, and it was compile. 1094's
lifecycle table had a store with **zero production callers**. This module is the worker, and the
route now creates the REQUEST and enqueues, writing the attempt only when execution actually
happened.

**The eight responsibilities of §9.0, in the order they run:** create (the route's half) · claim
under a lease and fence (`claim_verification`, the generation claim's exact shape over this lane's
own handler set and partition namespace — §9.0.2) · recheck the request-time decision · prepare
frozen inputs · execute OUTSIDE the database transaction · store the exact output revision and
findings · stop before any publication · mark PASSED / REFUSED / FAILED, idempotent on redelivery.

▲ **REFUSED is not FAILED**, and 1094 already draws the line this lane must not blur: REFUSED is a
product verdict (the artifact does not verify, findings name why) and FAILED is an outage (the
platform lost the run, `failure_reason` says how). Blurring them teaches an operator to read an
outage as a product answer.

▲ **THE EXECUTOR IS A SEAM, and its absence is a POSTURE, not a skip.** This deployment has no
execution substrate (step 0b: `FEATUREGEN_MATERIALIZE_ENABLED="0"`, the eight-variable execution
block commented out), so the shipped default executor is ``None`` and a claimed request FAILS with
the posture named — never a fake pass, never a silent drop. Tests inject an executor to drive the
PASSED and REFUSED paths for real.
"""
from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable

import psycopg

from featuregen.canonical import jcs_sha256
from featuregen.overlay.upload.verification_request_store import (
    InvalidVerificationMove,
    VerificationMovedUnderneath,
    VerificationStatusV1,
    advance_verification,
)
from featuregen.runtime.observability import counters, log
from featuregen.runtime.outbox import enqueue_checked
from featuregen.runtime.queue import (
    GenerationQueueClaim,
    claim_verification,
    complete_generation,
    fail_generation,
)

__all__ = [
    "VERIFICATION_FLAG",
    "VERIFICATION_HANDLER",
    "SandboxExecutionV1",
    "VerificationLaneOutcome",
    "enqueue_verification",
    "process_verification_once",
    "verification_enabled",
    "verification_message_id",
]

VERIFICATION_HANDLER = "verification.v2.run"
_NAMESPACE = "verification"
VERIFICATION_FLAG = "FEATUREGEN_VERIFICATION_V2_ENABLED"


def verification_enabled() -> bool:
    """Whether this deployment runs the V2 verification lane at all. Default **OFF** — the same
    kill-switch discipline as generation, read above the claim so an unconfigured deployment costs
    nothing."""
    return os.environ.get(VERIFICATION_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}


def verification_message_id(request_id: str) -> str:
    """The ONE message id for a request — what makes a redelivery the same message rather than a
    second cluster run."""
    if not request_id.strip():
        raise ValueError("a verification message id needs its request id")
    return f"{_NAMESPACE}:{request_id}"


def enqueue_verification(
    conn: psycopg.Connection, *, request_id: str, sealed_artifact_id: str, environment_id: str,
    priority: int = 100,
) -> int:
    """Freeze the job onto the queue, in the caller's transaction — request and work item commit
    together, so no request exists that nothing will pick up. Partitioned per (environment,
    artifact), which is exactly `verification_request_one_live`'s scope: one live run per artifact
    per environment."""
    return enqueue_checked(
        conn,
        message_id=verification_message_id(request_id),
        partition_key=f"{_NAMESPACE}:{environment_id}:{sealed_artifact_id}",
        handler=VERIFICATION_HANDLER,
        payload={"request_id": request_id},
        priority=priority,
    )


@dataclass(frozen=True, slots=True)
class SandboxExecutionV1:
    """What one sandbox run MEASURED — the executor seam's whole vocabulary.

    ``passed`` with non-empty ``findings`` is legal (informational findings); ``passed=False``
    REQUIRES findings, because a refusal that names nothing is not actionable — 1094's own CHECK
    (`verification_request_refused_names_findings`) enforces it at rest.
    """

    output_manifest_hash: str
    row_count: int
    findings: tuple[Mapping[str, str], ...]
    passed: bool


#: ▲ THE EXECUTION SEAM. `None` — the shipped default — is this deployment's honest posture: no
#: Spark/Kedro substrate is configured (step 0b), so a claimed request FAILS with the posture
#: named. The seam takes (conn, request_id, sealed_artifact_id, environment_id) and returns a
#: SandboxExecutionV1; it must run its cluster work OUTSIDE any database transaction — the crash
#: window between external work and the commit is §9.1's whole subject, and holding a transaction
#: across a cluster run is how it gets wedged open.
_EXECUTOR: Callable[..., SandboxExecutionV1] | None = None


@dataclass(frozen=True, slots=True)
class VerificationLaneOutcome:
    status: str
    request_id: str | None = None
    detail: str = ""


def _read_request(conn, request_id: str):
    return conn.execute(
        "SELECT sealed_artifact_id, environment_id, status, action_decision_revision_id "
        "FROM verification_request WHERE request_id = %s", (request_id,)).fetchone()


def process_verification_once(
    conn: psycopg.Connection, *, owner: str,
) -> VerificationLaneOutcome | None:
    """Claim and drive ONE verification request to a terminal state. ``None`` = nothing to do."""
    if not verification_enabled():
        return None
    claim = claim_verification(conn, owner=owner)
    if claim is None:
        return None
    return _drive(conn, claim)


def _drive(conn: psycopg.Connection, claim: GenerationQueueClaim) -> VerificationLaneOutcome:
    request_id = str((claim.payload or {}).get("request_id") or "")
    row = _read_request(conn, request_id) if request_id else None
    if row is None:
        # A payload naming no readable request is deterministic: dead-letter, never retried.
        fail_generation(conn, claim, error=f"no verification request {request_id!r}",
                        permanent=True)
        return VerificationLaneOutcome("dead", request_id=request_id or None,
                                       detail="payload names no request")

    sealed_artifact_id, environment_id, status_text, decision_id = row
    status = VerificationStatusV1(status_text)
    if status.is_terminal:
        # A redelivery of finished work: nothing wrong, the message arrived twice. Completing
        # rather than failing — re-running would buy a second cluster run for a recorded verdict.
        complete_generation(conn, claim)
        return VerificationLaneOutcome("unclaimable", request_id=request_id,
                                       detail=f"already {status.value}")
    if status is not VerificationStatusV1.REQUESTED:
        # Another worker holds it (or held it — the reconciler owns the abandoned case, §9.0.1;
        # this release path is CORRECT while a worker is alive and is exactly what the reconciler's
        # unreachable-message predicate distinguishes when one is not).
        fail_generation(conn, claim, permanent=False,
                        error=f"request {request_id} is {status.value}: another worker holds it")
        return VerificationLaneOutcome("unclaimable", request_id=request_id,
                                       detail=f"another worker holds it ({status.value})")

    # ── 0. THE SECOND LOOK (§8.2): the request-time decision, before the act ────────────────────
    from featuregen.materialize.action_decision import DecisionDrift, DecisionMissing, recheck

    try:
        if decision_id is None:
            raise DecisionMissing(
                f"verification request {request_id} carries no action decision: work submitted "
                f"straight to the queue is refused at the worker, not run")
        recheck(conn, decision_id, current_pins=verification_evidence_pins(
            sealed_artifact_id=sealed_artifact_id, environment_id=environment_id))
    except (DecisionMissing, DecisionDrift) as exc:
        # ▲ REFUSED, not FAILED: a considered answer about this request, not an outage. Completed
        # on the queue — a redelivery reproduces it exactly.
        try:
            advance_verification(conn, request_id, VerificationStatusV1.REFUSED,
                                 findings=[{"code": "ACTION_DECISION_MISSING"
                                            if isinstance(exc, DecisionMissing)
                                            else "ACTION_DECISION_DRIFTED",
                                            "detail": str(exc)}])
        except (InvalidVerificationMove, VerificationMovedUnderneath):
            pass
        complete_generation(conn, claim)
        counters.incr("featuregen.verification.decision_refused")
        return VerificationLaneOutcome("refused", request_id=request_id, detail=str(exc))

    try:
        # ▲ NO EXPLICIT COMMIT, and the omission is load-bearing. The production worker's stage
        # connection is autocommit, so each advance persists as it lands; a `conn.commit()` here
        # would be a no-op there and a LEAK everywhere else — a transactional caller (every test)
        # would have its seed data committed past its rollback. The 1103 scope-lock leak, explicit.
        advance_verification(conn, request_id, VerificationStatusV1.CLAIMED)
    except (InvalidVerificationMove, VerificationMovedUnderneath) as exc:
        fail_generation(conn, claim, error=str(exc), permanent=False)
        return VerificationLaneOutcome("unclaimable", request_id=request_id, detail=str(exc))

    advance_verification(conn, request_id, VerificationStatusV1.RUNNING)

    # ── the EXECUTION, outside any database transaction ─────────────────────────────────────────
    if _EXECUTOR is None:
        # ▲ POSTURE, NOT A SKIP. FAILED (platform), never REFUSED (product) and never a quiet
        # pass: absence of a substrate says nothing about the artifact.
        advance_verification(
            conn, request_id, VerificationStatusV1.FAILED,
            failure_reason="no sandbox execution substrate is configured in this deployment "
                           "(step 0b): the artifact was not measured, which is a platform "
                           "posture and not a verdict about it")
        complete_generation(conn, claim)
        counters.incr("featuregen.verification.unexecutable")
        return VerificationLaneOutcome("failed", request_id=request_id,
                                       detail="no execution substrate")
    try:
        measured = _EXECUTOR(conn, request_id=request_id,
                             sealed_artifact_id=sealed_artifact_id,
                             environment_id=environment_id)
    except Exception as exc:  # noqa: BLE001 — the cluster is an outside system; bound the poison
        advance_verification(conn, request_id, VerificationStatusV1.FAILED,
                             failure_reason=f"{type(exc).__name__}: {exc}")
        complete_generation(conn, claim)
        counters.incr("featuregen.verification.failed")
        log("featuregen.verification.failed", level="error", request_id=request_id,
            error=f"{type(exc).__name__}: {exc}")
        return VerificationLaneOutcome("failed", request_id=request_id, detail=str(exc))

    # ── the OUTPUT REVISION: what publication will bind to — a thing, not a name ────────────────
    output_revision_id = jcs_sha256({
        "request_id": request_id, "sealed_artifact_id": sealed_artifact_id,
        "environment_id": environment_id,
        "output_manifest_hash": measured.output_manifest_hash,
        "row_count": measured.row_count})
    conn.execute(
        "INSERT INTO sandbox_output_revision (output_revision_id, request_id, "
        "sealed_artifact_id, environment_id, output_manifest_hash, row_count) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (request_id) DO NOTHING",
        (output_revision_id, request_id, sealed_artifact_id, environment_id,
         measured.output_manifest_hash, measured.row_count))

    if measured.passed:
        advance_verification(conn, request_id, VerificationStatusV1.PASSED,
                             execution_hash=output_revision_id,
                             findings=list(measured.findings))
    else:
        advance_verification(conn, request_id, VerificationStatusV1.REFUSED,
                             findings=list(measured.findings) or [
                                 {"code": "VERIFICATION_REFUSED",
                                  "detail": "the run refused without findings — executor defect"}])
    # ▲ AND IT STOPS HERE. Publication is a SEPARATE act with its own decision (§3); a worker that
    # published what it verified would be the conflation §2 exists to name.
    complete_generation(conn, claim)
    counters.incr("featuregen.verification.passed" if measured.passed
                  else "featuregen.verification.refused")
    return VerificationLaneOutcome("passed" if measured.passed else "refused",
                                   request_id=request_id)


def verification_evidence_pins(*, sealed_artifact_id: str, environment_id: str) -> dict[str, str]:
    """The EXECUTE_SANDBOX evidence pins — one definition for the route's decide and this worker's
    recheck, the same one-assembler rule as generation."""
    return {"sealed_artifact_id": sealed_artifact_id, "environment_id": environment_id}


def reconcile_abandoned_verifications(conn, *, limit: int = 50):
    """§9.0.1 — the reconciler this lane ships WITH, not after. The generation port's predicate:
    an abandoned request necessarily has an UNREACHABLE message — never merely an unleased one,
    because a request awaiting redelivery is byte-for-byte identical on that weaker test."""
    from featuregen.materialize.reconcile import UNREACHABLE_MESSAGE_STATUSES

    rows = conn.execute(
        "SELECT request_id, status FROM verification_request "
        "WHERE status = ANY(%s) "
        "AND NOT EXISTS (SELECT 1 FROM queue "
        "                WHERE queue.message_id = %s || verification_request.request_id "
        "                AND queue.status <> ALL(%s)) "
        "ORDER BY updated_at, request_id LIMIT %s",
        (["REQUESTED", "CLAIMED", "RUNNING"], f"{_NAMESPACE}:",
         sorted(UNREACHABLE_MESSAGE_STATUSES), limit)).fetchall()
    judged = []
    for request_id, was in rows:
        try:
            advance_verification(
                conn, request_id, VerificationStatusV1.FAILED,
                failure_reason=f"abandoned: the request was {was} and its queue message is no "
                               f"longer deliverable, so no worker will ever finish it. A platform "
                               f"fault, not a verdict about the artifact — request it again")
        except (InvalidVerificationMove, VerificationMovedUnderneath):
            continue
        judged.append(request_id)
    if judged:
        counters.incr("featuregen.verification.reconciled", len(judged))
        log("featuregen.verification.reconciled", level="warning", request_ids=judged)
    return tuple(judged)
