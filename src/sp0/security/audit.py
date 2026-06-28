from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

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


def _canonical_ts(occurred_at: datetime) -> str:
    # Normalize to UTC so the hash basis is independent of the session/DB timezone the
    # value is later read back in (Postgres timestamptz round-trips an instant, not a
    # zone). Naive datetimes are treated as UTC.
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)
    return occurred_at.astimezone(timezone.utc).isoformat()


def _entry_hash(
    prev_hash: Optional[str],
    sec_id: str,
    event_type: str,
    actor_jsonb: Mapping[str, Any],
    attempted_action: str,
    aggregate: Optional[str],
    aggregate_id: Optional[str],
    decision: str,
    reason: Optional[str],
    retention_class: str,
    occurred_at: datetime,
) -> str:
    # Hash a CANONICAL (sorted-key, whitespace-free) JSON serialization of the full
    # logical row — including the ENTIRE actor envelope, retention_class and timestamp —
    # so editing any field (e.g. actor.role_claims) breaks verify_chain(). §6.2.
    canonical = json.dumps(
        {
            "prev_hash": prev_hash,
            "security_event_id": sec_id,
            "event_type": event_type,
            "actor": actor_jsonb,
            "attempted_action": attempted_action,
            "aggregate": aggregate,
            "aggregate_id": aggregate_id,
            "decision": decision,
            "reason": reason,
            "retention_class": retention_class,
            "occurred_at": _canonical_ts(occurred_at),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
    actor_jsonb = identity_to_jsonb(actor)
    # Set occurred_at explicitly (not via the column DEFAULT) so the exact instant is part
    # of the hash basis and matches what verify_chain() reads back.
    occurred_at = datetime.now(timezone.utc)
    entry_hash = _entry_hash(
        prev_hash, sec_id, event_type, actor_jsonb, attempted_action,
        aggregate, aggregate_id, decision, reason, retention_class, occurred_at,
    )
    conn.execute(
        """
        INSERT INTO security_audit
            (security_event_id, event_type, actor, attempted_action, aggregate,
             aggregate_id, decision, reason, prev_hash, entry_hash, retention_class,
             occurred_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            sec_id, event_type, Json(actor_jsonb), attempted_action,
            aggregate, aggregate_id, decision, reason, prev_hash, entry_hash,
            retention_class, occurred_at,
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
        SELECT security_event_id, event_type, actor, attempted_action,
               aggregate, aggregate_id, decision, reason, retention_class,
               occurred_at, prev_hash, entry_hash
          FROM security_audit
         ORDER BY seq ASC
        """
    ).fetchall()
    prev_hash: Optional[str] = None
    for (sec_id, event_type, actor_jsonb, attempted_action, aggregate, aggregate_id,
         decision, reason, retention_class, occurred_at, row_prev, entry_hash) in rows:
        if row_prev != prev_hash:
            return False
        if _entry_hash(prev_hash, sec_id, event_type, actor_jsonb, attempted_action,
                       aggregate, aggregate_id, decision, reason, retention_class,
                       occurred_at) != entry_hash:
            return False
        prev_hash = entry_hash
    return True
