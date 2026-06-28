from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sp0.contracts.commands import Command
from sp0.contracts.db import DbConn
from sp0.identity.build import IdentityError, validate_identity


@dataclass(frozen=True, slots=True)
class AuthzDecision:
    allowed: bool
    reason: Optional[str] = None


# §6.2 canonical action vocabulary. gate='' for non-gate actions (PK forbids NULL).
_POLICY_ROWS: tuple[tuple[str, str, str, str, Optional[str]], ...] = (
    ("create_request", "", "data_scientist", "human", None),
    ("create_request", "", "intake-agent", "service", None),
    ("create_run", "", "data_scientist", "human", None),
    ("create_run", "", "intake-agent", "service", None),
    ("select_candidate", "", "data_scientist", "human", None),
    ("submit_human_signal", "CLARIFICATION", "data_scientist", "human", None),
    ("submit_human_signal", "CLARIFICATION", "intake-agent", "service", None),
    ("submit_human_signal", "DATA_STEWARD", "data_owner", "human", None),
    ("submit_human_signal", "COMPLIANCE", "compliance", "human", None),
    ("submit_human_signal", "INDEPENDENT_VALIDATION", "validator", "human", None),
    ("submit_human_signal", "FINAL_APPROVAL", "approver", "human", None),
    ("open_task", "", "workflow", "service", None),
    ("activate", "", "release", "human", None),
    ("supersede", "", "release", "human", None),
    ("deprecate", "", "release", "human", None),
    ("retier", "", "release", "human", None),
    ("register_consumer", "", "owner", "human", None),
    ("deregister_consumer", "", "owner", "human", None),
    ("raise_monitoring_alert", "", "monitoring", "service", None),
    ("require_revalidation", "", "overlay", "service", None),
    ("record_revalidation_outcome", "", "overlay", "service", None),
    ("fact_confirmed_resume", "", "overlay", "service", None),
    ("cancel", "", "data_scientist", "human", None),
    ("withdraw", "", "data_scientist", "human", None),
    ("reject", "", "validator", "human", None),
    ("park", "", "data_scientist", "human", None),
    ("unpark", "", "data_scientist", "human", None),
    ("reopen_as_new_run", "", "data_scientist", "human", None),
    ("duplicate_of", "", "data_scientist", "human", None),
    ("manual_retry", "", "data_scientist", "human", None),
    ("resolve_degraded", "", "platform-admin", "human", None),
    ("migrate_workflow_version", "", "platform-admin", "human", None),
    ("migrate_feature_lifecycle_version", "", "platform-admin", "human", None),
    ("admin_correct", "", "platform-admin", "human", None),
    ("break_glass", "", "platform-admin", "human", None),
    ("read_audit", "", "auditor", "human", None),
    ("read_audit", "", "compliance", "human", None),
    ("read_audit", "", "owner", "human", None),
    # NOTE: security-stream reads are deliberately NOT authorized here. They are gated solely by
    # read_security_audit() (validate_identity + security/compliance role + self-audit). Adding
    # authz_policy rows would create a divergent second gate that the read path never consults.
)


def seed_authz_policy(conn: DbConn) -> None:
    for action, gate, role, kind, scope in _POLICY_ROWS:
        conn.execute(
            """
            INSERT INTO authz_policy (action, gate, permitted_role, actor_kind, scope)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (action, gate, permitted_role, actor_kind) DO NOTHING
            """,
            (action, gate, role, kind, scope),
        )


def _base_authorized(conn: DbConn, cmd: Command) -> AuthzDecision:
    try:
        validate_identity(cmd.actor)
    except IdentityError as exc:
        return AuthzDecision(False, str(exc))
    gate = cmd.args.get("gate", "") if cmd.action == "submit_human_signal" else ""
    rows = conn.execute(
        "SELECT permitted_role, actor_kind, scope FROM authz_policy WHERE action=%s AND gate=%s",
        (cmd.action, gate or ""),
    ).fetchall()
    for permitted_role, actor_kind, scope in rows:
        if actor_kind not in ("any", cmd.actor.actor_kind):
            continue
        if permitted_role not in cmd.actor.role_claims:
            continue
        if scope is not None and scope not in cmd.actor.groups:
            continue
        if cmd.actor.actor_kind == "service" and not cmd.actor.attestation:
            continue
        return AuthzDecision(True)
    return AuthzDecision(False, "no matching authz policy")


def authorize_command(conn: DbConn, cmd: Command) -> AuthzDecision:
    base = _base_authorized(conn, cmd)
    if not base.allowed:
        return base
    from sp0.authz.sod import enforce_sod  # local import avoids module cycle

    return enforce_sod(conn, cmd)
