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
        security_audit". Phase 07 plugs in the real `authz_policy`-backed authorizer that does
        this; the Phase-06 default below allows all and writes nothing."""
        ...


class _AllowAllAuthorizer:
    def authorize(self, conn: DbConn, cmd: Command) -> AuthzDecision:
        return AuthzDecision(allowed=True)


_AUTHORIZER: CommandAuthorizer = _AllowAllAuthorizer()


def register_command_authorizer(authorizer: CommandAuthorizer) -> None:
    global _AUTHORIZER
    _AUTHORIZER = authorizer


def current_authorizer() -> CommandAuthorizer:
    return _AUTHORIZER
