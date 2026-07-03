from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Json

from featuregen.config import get_settings
from featuregen.contracts.db import DbConn
from featuregen.contracts.identity import IdentityEnvelope, identity_to_jsonb
from featuregen.idgen import mint_id


class AuditKeyNotConfigured(RuntimeError):
    """Raised when the security-audit HMAC key is not configured (§6.2, BLOCKER #4).

    The chain signature is KEYED so that a writer who can recompute an unkeyed hash cannot
    forge it. We fail CLOSED: rather than sign with a built-in default key (which would
    silently restore forgeability), signing/verification aborts until the operator sets
    ``FEATUREGEN_AUDIT_HMAC_KEY``. Tests inject a deterministic key via the environment.
    """


def _audit_hmac_key() -> str:
    """Resolve the audit-signing key from config (env ``FEATUREGEN_AUDIT_HMAC_KEY``).

    Resolved INSIDE the module so callers of ``record_security_event`` need not thread a key
    through. Fail-closed: a missing/empty key raises rather than defaulting.
    """
    key = get_settings().audit_hmac_key
    if not key:
        raise AuditKeyNotConfigured(
            "FEATUREGEN_AUDIT_HMAC_KEY is not configured; refusing to sign the "
            "security-audit chain with a default key (fail-closed)."
        )
    return key

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
        occurred_at = occurred_at.replace(tzinfo=UTC)
    return occurred_at.astimezone(UTC).isoformat()


def _entry_hash(
    key: str,
    prev_hash: str | None,
    sec_id: str,
    event_type: str,
    actor_jsonb: Mapping[str, Any],
    attempted_action: str,
    aggregate: str | None,
    aggregate_id: str | None,
    decision: str,
    reason: str | None,
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
    # KEYED signature (HMAC-SHA256), not a bare SHA-256 (BLOCKER #4): a bare digest is
    # forgeable by any writer who can recompute the chain. The MAC covers prev_hash ||
    # canonical so both the row content and its link to the previous entry are authenticated.
    prev_bytes = (prev_hash or "").encode("utf-8")
    return hmac.new(
        key.encode("utf-8"),
        prev_bytes + canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def record_security_event(
    conn: DbConn,
    *,
    event_type: str,
    actor: IdentityEnvelope,
    attempted_action: str,
    decision: str,
    reason: str | None = None,
    aggregate: str | None = None,
    aggregate_id: str | None = None,
    retention_class: str = "regulator",
    key: str | None = None,
) -> str:
    # Resolve the signing key from config when not injected (tests pass an explicit key).
    # Fail-closed: raises AuditKeyNotConfigured if unset — before any DB write.
    signing_key = key if key is not None else _audit_hmac_key()
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
    occurred_at = datetime.now(UTC)
    entry_hash = _entry_hash(
        signing_key,
        prev_hash,
        sec_id,
        event_type,
        actor_jsonb,
        attempted_action,
        aggregate,
        aggregate_id,
        decision,
        reason,
        retention_class,
        occurred_at,
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
            sec_id,
            event_type,
            Json(actor_jsonb),
            attempted_action,
            aggregate,
            aggregate_id,
            decision,
            reason,
            prev_hash,
            entry_hash,
            retention_class,
            occurred_at,
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
) -> list[tuple[str, str, str, str | None]]:
    # This function is the SINGLE enforcement path for security-stream reads: they are NOT
    # routed through execute_command/authz_policy, so there is no divergent second gate (the
    # authz_policy `read_security_audit` rows are intentionally absent — see Task 6). A role
    # claim alone is insufficient: the envelope must also be a VALID identity, else a spoofed
    # or unauthenticated envelope carrying a "security" claim could read the stream.
    from featuregen.identity.build import IdentityError, validate_identity

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


def verify_chain(
    conn: DbConn,
    *,
    key: str | None = None,
    expect_nonempty: bool = False,
) -> bool:
    # Recompute each row's KEYED signature with the configured (or injected) key. A wrong key
    # fails to verify — proof the chain is HMAC'd, not a bare hash (BLOCKER #4).
    signing_key = key if key is not None else _audit_hmac_key()
    rows = conn.execute(
        """
        SELECT security_event_id, event_type, actor, attempted_action,
               aggregate, aggregate_id, decision, reason, retention_class,
               occurred_at, prev_hash, entry_hash
          FROM security_audit
         ORDER BY seq ASC
        """
    ).fetchall()
    # An empty table verifying True let a TRUNCATE'd chain pass silently (BLOCKER #4). When a
    # non-empty chain is expected, treat empty as a verification FAILURE. Default preserves
    # the prior empty-is-ok contract for existing callers.
    if not rows:
        return not expect_nonempty
    prev_hash: str | None = None
    for (
        sec_id,
        event_type,
        actor_jsonb,
        attempted_action,
        aggregate,
        aggregate_id,
        decision,
        reason,
        retention_class,
        occurred_at,
        row_prev,
        entry_hash,
    ) in rows:
        if row_prev != prev_hash:
            return False
        computed = _entry_hash(
            signing_key,
            prev_hash,
            sec_id,
            event_type,
            actor_jsonb,
            attempted_action,
            aggregate,
            aggregate_id,
            decision,
            reason,
            retention_class,
            occurred_at,
        )
        # Constant-time compare: entry_hash is a secret-keyed MAC, so a short-circuiting `!=`
        # on hex strings would leak a MAC-forgery timing side-channel. The row_prev/prev_hash
        # check above is public stored hashes only, so it stays a plain `!=`. §6.2.
        if not hmac.compare_digest(computed, entry_hash):
            return False
        prev_hash = entry_hash
    return True
