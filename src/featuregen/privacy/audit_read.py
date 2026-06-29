from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

from featuregen.contracts import Command, EventEnvelope, IdentityEnvelope
from featuregen.governance.replay import ReplayMode, replay_run
from featuregen.security.audit import AuditReadDenied  # single shared exception class (Phase 07 authoritative)

if TYPE_CHECKING:
    from featuregen.contracts import DbConn

__all__ = ["AuditView", "AuditReadDenied", "read_audit"]

_ACTION = "read_audit"  # canonical §6.2 action vocabulary (matches Phase 07's authz_policy seed)

# authorize_command(conn, cmd) -> AuthzDecision (.allowed/.reason); record_security_event(...) -> str
AuthorizeCommand = Callable[..., object]
RecordSecurityEvent = Callable[..., str]


@dataclass(frozen=True, slots=True)
class AuditView:
    run_id: str
    events: tuple[EventEnvelope, ...]
    mode: ReplayMode
    degraded_artifacts: tuple[str, ...]


def read_audit(
    conn: "DbConn",
    *,
    run_id: str,
    actor: IdentityEnvelope,
    authorize_command: AuthorizeCommand,
    record_security_event: RecordSecurityEvent,
    upto_seq: Optional[int] = None,
) -> AuditView:
    """Authorized-and-logged audit read (§9/§6.2). Authorization is delegated to Phase 07's
    `authorize_command(conn, cmd) -> AuthzDecision` over a synthetic `read_audit` Command; every
    read (allow or deny) is recorded to the security stream via Phase 07's `record_security_event`.
    On deny: log AUDIT_READ/denied and raise `AuditReadDenied`. On allow: log AUDIT_READ/flagged
    and return the (privacy-degraded-labeled) reconstruction."""
    cmd = Command(
        action=_ACTION,
        aggregate="run",
        aggregate_id=run_id,
        args={},
        actor=actor,
        idempotency_key="audit_read:" + run_id + ":" + uuid.uuid4().hex,
    )
    decision = authorize_command(conn, cmd)
    if not decision.allowed:
        record_security_event(
            conn, event_type="AUDIT_READ", actor=actor, attempted_action=_ACTION,
            decision="denied", aggregate="run", aggregate_id=run_id,
            reason=getattr(decision, "reason", None) or "unauthorized audit read",
        )
        raise AuditReadDenied(f"actor {actor.subject!r} may not read audit for run {run_id!r}")

    record_security_event(
        conn, event_type="AUDIT_READ", actor=actor, attempted_action=_ACTION,
        decision="flagged", aggregate="run", aggregate_id=run_id, reason="audit read",
    )
    result = replay_run(conn, run_id, upto_seq=upto_seq)
    return AuditView(
        run_id=run_id,
        events=result.events,
        mode=result.mode,
        degraded_artifacts=result.degraded_artifacts,
    )
