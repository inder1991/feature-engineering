from __future__ import annotations

from featuregen.commands.authz_seam import AuthzDecision
from featuregen.contracts import Command, DbConn


class PolicyAuthorizer:
    """Production `CommandAuthorizer` (§6.2). Delegates the permit/deny decision to
    `authz.policy.authorize_command` (canonical action vocabulary + SoD engine) and — per the
    `CommandAuthorizer` contract — routes every DENIAL to the tamper-evident security stream via
    `security.audit.record_denial` (NOT the domain stream). Returns the seam `AuthzDecision`
    that `execute_command` consumes. Registered by `bootstrap_phase07` so state-mutating commands
    no longer run under the allow-all Phase-06 default."""

    def authorize(self, conn: DbConn, cmd: Command) -> AuthzDecision:
        from featuregen.authz.policy import authorize_command
        from featuregen.security.audit import record_denial

        decision = authorize_command(conn, cmd)
        if not decision.allowed:
            reason = decision.reason or "command denied"
            record_denial(conn, cmd, reason)
            return AuthzDecision(allowed=False, reason=reason)
        return AuthzDecision(allowed=True)
