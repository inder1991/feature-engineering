"""Worker-time reauthorization against the current authentication authority."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from featuregen.contracts import DbConn, IdentityEnvelope
from featuregen.identity._trust import _TRUST_CAPABILITY
from featuregen.identity.build import build_human_identity


class PrincipalResolutionStatus(StrEnum):
    CURRENT = "current"
    REVOKED = "revoked"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True, slots=True)
class CurrentPrincipalResolution:
    status: PrincipalResolutionStatus
    principal: IdentityEnvelope | None = None
    reason: str | None = None


class WorkerIdentityResolver(Protocol):
    def resolve_current_principal(
        self,
        conn: DbConn,
        frozen_subject: str,
        frozen_tenant: str | None,
        observed_at: datetime,
    ) -> CurrentPrincipalResolution: ...


class LocalWorkerIdentityResolver:
    """Resolve ``user:<username>`` from the local IAM tables without replaying a session token."""

    def resolve_current_principal(
        self,
        conn: DbConn,
        frozen_subject: str,
        frozen_tenant: str | None,
        observed_at: datetime,
    ) -> CurrentPrincipalResolution:
        del observed_at
        if not frozen_subject.startswith("user:"):
            return CurrentPrincipalResolution(
                PrincipalResolutionStatus.UNVERIFIABLE,
                reason="subject is not backed by local human authentication",
            )
        if frozen_tenant is not None:
            return CurrentPrincipalResolution(
                PrincipalResolutionStatus.UNVERIFIABLE,
                reason="local IAM has no tenant authority",
            )
        username = frozen_subject.removeprefix("user:")
        if not username:
            return CurrentPrincipalResolution(
                PrincipalResolutionStatus.UNVERIFIABLE,
                reason="empty local username",
            )
        row = conn.execute(
            "SELECT user_id, disabled FROM app_user WHERE username = %s",
            (username,),
        ).fetchone()
        if row is None or row[1]:
            return CurrentPrincipalResolution(
                PrincipalResolutionStatus.REVOKED,
                reason="local user is missing or disabled",
            )
        memberships = conn.execute(
            "SELECT g.name, gr.role FROM app_user_group ug "
            "JOIN app_group g ON g.group_id = ug.group_id "
            "LEFT JOIN app_group_role gr ON gr.group_id = g.group_id "
            "WHERE ug.user_id = %s",
            (row[0],),
        ).fetchall()
        groups = sorted({membership[0] for membership in memberships})
        roles = sorted({
            membership[1] for membership in memberships if membership[1]})
        principal = build_human_identity(
            subject=frozen_subject,
            role_claims=roles,
            auth_method="password",
            groups=groups,
            _capability=_TRUST_CAPABILITY,
        )
        return CurrentPrincipalResolution(
            PrincipalResolutionStatus.CURRENT,
            principal=principal,
        )


def resolve_current_principal(
    conn: DbConn,
    frozen_subject: str,
    frozen_tenant: str | None,
    observed_at: datetime,
    *,
    resolver: WorkerIdentityResolver | None = None,
) -> CurrentPrincipalResolution:
    """Resolve with an injected deployment adapter; local IAM is the only built-in authority."""
    effective = resolver or LocalWorkerIdentityResolver()
    return effective.resolve_current_principal(
        conn, frozen_subject, frozen_tenant, observed_at)
