from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from psycopg.types.json import Json

from sp0.contracts.db import DbConn
from sp0.contracts.identity import IdentityEnvelope
from sp0.gates.duration import parse_duration
from sp0.idgen import mint_id
from sp0.security.audit import record_security_event


class BreakGlassError(Exception):
    """Raised when break-glass dual-control or independent-review rules are violated (§6.3)."""


def open_break_glass_review(
    conn: DbConn,
    *,
    actor: IdentityEnvelope,
    co_signer: IdentityEnvelope,
    attempted_action: str,
    aggregate: Optional[str],
    aggregate_id: Optional[str],
    sla: str,
) -> str:
    review_id = mint_id("bgr")
    record_security_event(
        conn,
        event_type="BREAK_GLASS_REVIEW_REQUIRED",
        actor=actor,
        attempted_action=attempted_action,
        decision="flagged",
        reason=f"review_id={review_id}",
        aggregate=aggregate,
        aggregate_id=aggregate_id,
    )
    agg = aggregate or "request"
    agg_id = aggregate_id or review_id
    fire_at = datetime.now(timezone.utc) + parse_duration(sla)
    conn.execute(
        """
        INSERT INTO timers
            (timer_id, idempotency_key, aggregate, aggregate_id, task_id, kind, fire_at,
             status, payload)
        VALUES (%s,%s,%s,%s,%s,'escalation',%s,'scheduled',%s)
        """,
        (
            mint_id("tmr"), f"{review_id}:sla", agg, agg_id, review_id, fire_at,
            Json({"break_glass_review": review_id, "invoker": actor.subject,
                  "co_signer": co_signer.subject}),
        ),
    )
    return review_id


def invoke_break_glass(
    conn: DbConn,
    *,
    actor: IdentityEnvelope,
    co_signer: IdentityEnvelope,
    attempted_action: str,
    aggregate: Optional[str] = None,
    aggregate_id: Optional[str] = None,
    sla: str = "1d",
) -> str:
    if "platform-admin" not in actor.role_claims:
        raise BreakGlassError("break-glass invoker must be platform-admin")
    if "platform-admin" not in co_signer.role_claims:
        raise BreakGlassError("break-glass co-signer must be platform-admin")
    if co_signer.subject == actor.subject:
        raise BreakGlassError("dual control requires two distinct platform-admins")
    record_security_event(
        conn,
        event_type="BREAK_GLASS",
        actor=actor,
        attempted_action=attempted_action,
        decision="allowed_break_glass",
        reason=f"co_signer={co_signer.subject}",
        aggregate=aggregate,
        aggregate_id=aggregate_id,
    )
    return open_break_glass_review(
        conn,
        actor=actor,
        co_signer=co_signer,
        attempted_action=attempted_action,
        aggregate=aggregate,
        aggregate_id=aggregate_id,
        sla=sla,
    )


def sign_off_break_glass_review(
    conn: DbConn,
    review_id: str,
    *,
    reviewer: IdentityEnvelope,
    invoker_subject: str,
    co_signer_subject: str,
) -> None:
    if not any(r in reviewer.role_claims for r in ("compliance", "platform-admin")):
        raise BreakGlassError("break-glass reviewer must be compliance or platform-admin")
    if reviewer.subject in (invoker_subject, co_signer_subject):
        raise BreakGlassError(
            "break-glass review must be independent of invoker and co-signer"
        )
    record_security_event(
        conn,
        event_type="BREAK_GLASS_REVIEW",
        actor=reviewer,
        attempted_action=f"sign_off:{review_id}",
        decision="flagged",
        reason="signed_off",
    )
    conn.execute(
        "UPDATE timers SET status='cancelled' WHERE task_id=%s AND status='scheduled'",
        (review_id,),
    )
