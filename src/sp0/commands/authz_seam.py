from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

from sp0.contracts import Command, DbConn


@dataclass(frozen=True, slots=True)
class AuthzDecision:
    allowed: bool
    reason: Optional[str] = None


@runtime_checkable
class CommandAuthorizer(Protocol):
    def authorize(self, conn: DbConn, cmd: Command) -> AuthzDecision:
        """Decide whether `cmd` is permitted. CONTRACT: an authorizer that DENIES is responsible
        for writing the denial to the `security_audit` stream (tamper-evident, NOT the domain
        stream) — this is how `execute_command` fulfils the contract's "on deny, writes to
        security_audit". `bootstrap_phase07` plugs in the real `authz_policy`-backed authorizer
        that does this. Until then the fail-safe default below DENIES every command (it writes
        nothing to the audit stream because an unconfigured system is not yet operational)."""
        ...


class _DenyAllAuthorizer:
    """Fail-safe default: until `bootstrap_phase07` registers the real `PolicyAuthorizer`, the
    system is unconfigured and MUST reject every state-mutating command (fail-closed, never
    fail-open). It writes no `security_audit` row because no real authz policy is in effect yet —
    these are misconfiguration rejections, not policy denials."""

    def authorize(self, conn: DbConn, cmd: Command) -> AuthzDecision:
        return AuthzDecision(
            allowed=False,
            reason="no command authorizer configured (bootstrap_phase07 not run); denied fail-safe",
        )


_DEFAULT_AUTHORIZER: CommandAuthorizer = _DenyAllAuthorizer()
_AUTHORIZER: CommandAuthorizer = _DEFAULT_AUTHORIZER


def register_command_authorizer(authorizer: CommandAuthorizer) -> None:
    global _AUTHORIZER
    _AUTHORIZER = authorizer


def current_authorizer() -> CommandAuthorizer:
    return _AUTHORIZER
