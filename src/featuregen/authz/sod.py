from __future__ import annotations

from typing import Optional

from featuregen.authz.policy import AuthzDecision
from featuregen.contracts.commands import Command
from featuregen.contracts.db import DbConn


def two_party_ok(requester: str, approver: str) -> bool:
    return requester != approver


def three_party_disjoint(author: str, validators: set[str], approver: str) -> bool:
    return (
        author not in validators
        and approver not in validators
        and author != approver
    )


def resolve_run_author(conn: DbConn, run_id: str) -> Optional[str]:
    row = conn.execute(
        """
        SELECT actor->>'subject' FROM events
         WHERE aggregate='run' AND aggregate_id=%s
         ORDER BY stream_version ASC LIMIT 1
        """,
        (run_id,),
    ).fetchone()
    return row[0] if row else None


def gather_gate_responders(
    conn: DbConn,
    gate: str,
    *,
    run_id: Optional[str] = None,
    feature_id: Optional[str] = None,
) -> set[str]:
    rows = conn.execute(
        """
        SELECT coalesce(r.on_behalf_of, r.subject)
          FROM human_task_responses r
          JOIN human_tasks t ON t.task_id = r.task_id
         WHERE t.gate = %s
           AND ((%s::text IS NOT NULL AND t.run_id = %s)
                OR (%s::text IS NOT NULL AND t.feature_id = %s))
        """,
        (gate, run_id, run_id, feature_id, feature_id),
    ).fetchall()
    return {r[0] for r in rows}


def gate_sod_reason(
    conn: DbConn,
    *,
    gate: Optional[str],
    subject: str,
    run_id: Optional[str] = None,
    feature_id: Optional[str] = None,
) -> Optional[str]:
    """PURE-ish SoD predicate for a single gate answer, keyed to the EFFECTIVE authority
    (`subject`). Shared by BOTH the command-authz path (`enforce_sod`) and the direct
    `submit_human_signal` call path (§7) so the two can never diverge. Returns a denial reason
    string, or None when the answer satisfies SoD (§6.3)."""
    author = resolve_run_author(conn, run_id) if run_id else None
    if gate == "INDEPENDENT_VALIDATION":
        if author is not None and subject == author:
            return "independent validation requires validator != author"
    elif gate == "FINAL_APPROVAL":
        if author is not None and not two_party_ok(author, subject):
            return "four-eyes: approver != requester"
        validators = (
            gather_gate_responders(conn, "INDEPENDENT_VALIDATION", run_id=run_id)
            if run_id
            else set()
        )
        if validators and subject in validators:
            return "three-party: approver != validator"
    return None


def enforce_sod(conn: DbConn, cmd: Command) -> AuthzDecision:
    if cmd.action == "submit_human_signal":
        gate = cmd.args.get("gate")
        run_id = cmd.aggregate_id if cmd.aggregate == "run" else cmd.args.get("run_id")
        feature_id = (
            cmd.aggregate_id if cmd.aggregate == "feature" else cmd.args.get("feature_id")
        )
        reason = gate_sod_reason(
            conn, gate=gate, subject=cmd.actor.subject, run_id=run_id, feature_id=feature_id
        )
        return AuthzDecision(reason is None, reason)
    if cmd.action == "retier":
        # retier is ALWAYS dual-controlled (§4.4): the actor applying the risk-tier change must
        # be distinct from the requester. Require an explicit requested_by and enforce four-eyes.
        requested_by = cmd.args.get("requested_by")
        if requested_by is None:
            return AuthzDecision(False, "retier is dual-controlled: requested_by required")
        if not two_party_ok(requested_by, cmd.actor.subject):
            return AuthzDecision(False, "four-eyes: actor != requester for retier")
        return AuthzDecision(True)
    if cmd.action in ("activate", "supersede", "deprecate") and cmd.args.get(
        "compliance_sensitive"
    ):
        requested_by = cmd.args.get("requested_by")
        if requested_by is not None and not two_party_ok(requested_by, cmd.actor.subject):
            return AuthzDecision(
                False, "four-eyes: actor != requester for compliance-sensitive change"
            )
    return AuthzDecision(True)
