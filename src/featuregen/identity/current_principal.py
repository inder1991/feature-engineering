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


#: THE DEPLOYMENT'S AUTHORITY, named. `LocalWorkerIdentityResolver` can only answer for
#: `user:<username>` subjects held in this platform's own IAM tables, and answers UNVERIFIABLE for
#: everything else — a service principal, a tenanted subject, an external IdP. Every worker prong
#: that consults it is fail-closed, so a deployment whose principals live elsewhere must NAME the
#: adapter that can answer for them. `module.path:Attribute`, resolving to a
#: :class:`WorkerIdentityResolver` — a class (instantiated with no arguments) or a ready instance.
WORKER_IDENTITY_RESOLVER_ENV = "FEATUREGEN_WORKER_IDENTITY_RESOLVER"


class WorkerIdentityResolverUnavailable(RuntimeError):
    """The configured resolver cannot be loaded. Raised rather than falling back to local IAM: a
    deployment that named an authority and silently got a different one would report every one of
    its principals as unverifiable and look like an outage."""


def configured_worker_identity_resolver() -> WorkerIdentityResolver:
    """The resolver this deployment runs with — the one seam, read at the boundary.

    Unset means local IAM, which is correct for every deployment whose principals ARE local humans.
    Set-and-broken raises, naming the variable: an unimportable adapter is a configuration error,
    not a reason to answer with a different authority.
    """
    import os

    configured = os.environ.get(WORKER_IDENTITY_RESOLVER_ENV, "").strip()
    if not configured:
        return LocalWorkerIdentityResolver()
    if ":" not in configured:
        raise WorkerIdentityResolverUnavailable(
            f"{WORKER_IDENTITY_RESOLVER_ENV}={configured!r} is not a 'module.path:Attribute' "
            f"reference, so there is nothing to import")
    module_path, _, attribute = configured.partition(":")
    try:
        import importlib

        candidate = getattr(importlib.import_module(module_path), attribute)
    except (ImportError, AttributeError) as exc:
        raise WorkerIdentityResolverUnavailable(
            f"{WORKER_IDENTITY_RESOLVER_ENV}={configured!r} could not be imported: {exc}") from exc
    try:
        # A class is instantiated with no arguments; anything else is taken as a ready instance. A
        # class that needs arguments is a misconfiguration like any other and says so by name —
        # never a raw TypeError from three frames down.
        resolved = candidate() if isinstance(candidate, type) else candidate
    except Exception as exc:  # noqa: BLE001 — whatever a third-party adapter raises is the reason
        raise WorkerIdentityResolverUnavailable(
            f"{WORKER_IDENTITY_RESOLVER_ENV}={configured!r} could not be constructed: {exc}"
        ) from exc
    if not callable(getattr(resolved, "resolve_current_principal", None)):
        raise WorkerIdentityResolverUnavailable(
            f"{WORKER_IDENTITY_RESOLVER_ENV}={configured!r} resolved to {resolved!r}, which does "
            f"not implement WorkerIdentityResolver.resolve_current_principal")
    return resolved


def resolve_current_principal(
    conn: DbConn,
    frozen_subject: str,
    frozen_tenant: str | None,
    observed_at: datetime,
    *,
    resolver: WorkerIdentityResolver | None = None,
) -> CurrentPrincipalResolution:
    """Resolve through the deployment's configured authority.

    ``resolver`` is the explicit override (a test, or a caller that already holds one); absent it,
    :func:`configured_worker_identity_resolver` answers, so every worker prong — the draft author,
    the recipe author, and the generation lane's second prong — runs against the SAME authority a
    deployment configured once, rather than each hard-coding local IAM.
    """
    effective = resolver or configured_worker_identity_resolver()
    return effective.resolve_current_principal(
        conn, frozen_subject, frozen_tenant, observed_at)
