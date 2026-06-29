from __future__ import annotations

from collections.abc import Iterable

from featuregen.contracts.identity import IdentityEnvelope


class IdentityError(Exception):
    """Raised when an IdentityEnvelope is malformed or not validly attested (§6.1)."""


def validate_identity(env: IdentityEnvelope) -> None:
    if not env.authenticated:
        raise IdentityError("actor not authenticated")
    if env.actor_kind == "service":
        if env.auth_method != "workload-identity":
            raise IdentityError("service actor must authenticate via workload-identity")
        if not env.attestation:
            raise IdentityError(
                "service role_claims must be attested by a signed deploy identity, "
                "not self-asserted"
            )
    elif env.actor_kind == "human":
        if env.auth_method != "oidc":
            raise IdentityError("human actor must authenticate via oidc")
    else:
        raise IdentityError(f"unknown actor_kind: {env.actor_kind}")


def build_human_identity(
    *,
    subject: str,
    role_claims: Iterable[str],
    auth_method: str = "oidc",
    groups: Iterable[str] = (),
    tenant: str | None = None,
    source_of_authority: str | None = None,
    on_behalf_of: str | None = None,
    impersonation: str | None = None,
    break_glass: bool = False,
) -> IdentityEnvelope:
    if not subject.startswith("user:"):
        raise IdentityError("human subject must be prefixed 'user:'")
    env = IdentityEnvelope(
        subject=subject,
        actor_kind="human",
        authenticated=True,
        auth_method=auth_method,
        role_claims=tuple(role_claims),
        groups=tuple(groups),
        tenant=tenant,
        on_behalf_of=on_behalf_of,
        impersonation=impersonation,
        break_glass=break_glass,
        source_of_authority=source_of_authority,
        attestation=None,
    )
    validate_identity(env)
    return env


def build_service_identity(
    *,
    subject: str,
    role_claims: Iterable[str],
    attestation: str,
    groups: Iterable[str] = (),
    tenant: str | None = None,
    source_of_authority: str | None = None,
) -> IdentityEnvelope:
    if not subject.startswith("service:"):
        raise IdentityError("service subject must be prefixed 'service:'")
    env = IdentityEnvelope(
        subject=subject,
        actor_kind="service",
        authenticated=True,
        auth_method="workload-identity",
        role_claims=tuple(role_claims),
        groups=tuple(groups),
        tenant=tenant,
        attestation=attestation,
        source_of_authority=source_of_authority,
    )
    validate_identity(env)
    return env
