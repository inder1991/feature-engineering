from __future__ import annotations

import hashlib
from typing import Any, Optional

from psycopg.types.json import Json

from sp0.contracts.db import DbConn
from sp0.contracts.identity import IdentityEnvelope, identity_to_jsonb
from sp0.idgen import mint_id

# Transaction-scoped advisory-lock key that serializes ALL appends to the single
# tamper-evident security chain (§6.2). Without it the chain can FORK: on an empty table
# `... ORDER BY seq DESC LIMIT 1 FOR UPDATE` locks no rows, so two concurrent transactions
# both read prev_hash=None and both insert genesis rows; more generally two writers can chain
# off the same prev. `pg_advisory_xact_lock` is released automatically on COMMIT/ROLLBACK, so
# it never leaks. (Re-acquiring it within one transaction is a no-op — multiple appends in the
# same §5.1 step are fine.)
_SECURITY_CHAIN_LOCK_KEY = 7_000_007


def _entry_hash(
    prev_hash: Optional[str],
    sec_id: str,
    event_type: str,
    subject: str,
    attempted_action: str,
    aggregate: Optional[str],
    aggregate_id: Optional[str],
    decision: str,
    reason: Optional[str],
) -> str:
    payload = "|".join(
        [
            prev_hash or "",
            sec_id,
            event_type,
            subject,
            attempted_action,
            aggregate or "",
            aggregate_id or "",
            decision,
            reason or "",
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def record_security_event(
    conn: DbConn,
    *,
    event_type: str,
    actor: IdentityEnvelope,
    attempted_action: str,
    decision: str,
    reason: Optional[str] = None,
    aggregate: Optional[str] = None,
    aggregate_id: Optional[str] = None,
    retention_class: str = "regulator",
) -> str:
    # Serialize chain appends so the prev_hash read + insert is atomic for the single chain
    # (fixes the empty-table / same-prev fork race; FOR UPDATE alone cannot lock a row that
    # does not exist yet).
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_SECURITY_CHAIN_LOCK_KEY,))
    prev = conn.execute(
        "SELECT entry_hash FROM security_audit ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    prev_hash = prev[0] if prev else None
    sec_id = mint_id("sec")
    entry_hash = _entry_hash(
        prev_hash, sec_id, event_type, actor.subject, attempted_action,
        aggregate, aggregate_id, decision, reason,
    )
    conn.execute(
        """
        INSERT INTO security_audit
            (security_event_id, event_type, actor, attempted_action, aggregate,
             aggregate_id, decision, reason, prev_hash, entry_hash, retention_class)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            sec_id, event_type, Json(identity_to_jsonb(actor)), attempted_action,
            aggregate, aggregate_id, decision, reason, prev_hash, entry_hash,
            retention_class,
        ),
    )
    return sec_id


def record_denial(conn: DbConn, cmd: Any, reason: str) -> str:
    """Route an authorization denial to the security stream (§6.2), not the domain stream."""
    return record_security_event(
        conn,
        event_type="COMMAND_DENIED",
        actor=cmd.actor,
        attempted_action=cmd.action,
        decision="denied",
        reason=reason,
        aggregate=cmd.aggregate,
        aggregate_id=cmd.aggregate_id,
    )


class AuditReadDenied(Exception):
    """Raised when a non-security/compliance actor attempts to read the security stream (§6.2)."""


_AUDIT_READ_ROLES = ("security", "compliance")


def read_security_audit(
    conn: DbConn,
    actor: IdentityEnvelope,
    *,
    limit: int = 100,
) -> list[tuple[str, str, str, Optional[str]]]:
    # This function is the SINGLE enforcement path for security-stream reads: they are NOT
    # routed through execute_command/authz_policy, so there is no divergent second gate (the
    # authz_policy `read_security_audit` rows are intentionally absent — see Task 6). A role
    # claim alone is insufficient: the envelope must also be a VALID identity, else a spoofed
    # or unauthenticated envelope carrying a "security" claim could read the stream.
    from sp0.identity.build import IdentityError, validate_identity

    try:
        validate_identity(actor)
        identity_ok = True
    except IdentityError:
        identity_ok = False
    allowed = identity_ok and any(r in actor.role_claims for r in _AUDIT_READ_ROLES)
    if not allowed:
        record_security_event(
            conn,
            event_type="AUDIT_READ",
            actor=actor,
            attempted_action="read_security_audit",
            decision="denied",
            reason="security stream read restricted to security/compliance",
        )
        raise AuditReadDenied("security stream read restricted to security/compliance")
    record_security_event(
        conn,
        event_type="AUDIT_READ",
        actor=actor,
        attempted_action="read_security_audit",
        decision="flagged",
        reason="security stream read",
    )
    rows = conn.execute(
        """
        SELECT security_event_id, event_type, decision, reason
          FROM security_audit
         ORDER BY seq ASC
         LIMIT %s
        """,
        (limit,),
    ).fetchall()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


def verify_chain(conn: DbConn) -> bool:
    rows = conn.execute(
        """
        SELECT security_event_id, event_type, actor->>'subject', attempted_action,
               aggregate, aggregate_id, decision, reason, prev_hash, entry_hash
          FROM security_audit
         ORDER BY seq ASC
        """
    ).fetchall()
    prev_hash: Optional[str] = None
    for (sec_id, event_type, subject, attempted_action, aggregate, aggregate_id,
         decision, reason, row_prev, entry_hash) in rows:
        if row_prev != prev_hash:
            return False
        if _entry_hash(prev_hash, sec_id, event_type, subject, attempted_action,
                       aggregate, aggregate_id, decision, reason) != entry_hash:
            return False
        prev_hash = entry_hash
    return True
