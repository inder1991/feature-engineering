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
    _verified: bool = False,
) -> IdentityEnvelope:
    """Construct a human IdentityEnvelope.

    FAIL-CLOSED (SP-0.5 BLOCKER #1): ordinary callers get ``authenticated=False``. Claims on
    an envelope are only *asserted* here — they become *authenticated* solely when a verifier
    (``identity.verify.OidcVerifier``) has proven the token's signature/issuer/audience/expiry
    and passes the internal ``_verified=True`` flag. ``_verified`` is NOT part of the public
    contract: no application code may set it; only the identity-verification seam may. This is
    what makes it impossible for arbitrary code to mint a principal it has not proven.
    """
    if not subject.startswith("user:"):
        raise IdentityError("human subject must be prefixed 'user:'")
    env = IdentityEnvelope(
        subject=subject,
        actor_kind="human",
        authenticated=_verified,
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
    if _verified:
        # Only a verified envelope must satisfy the §6.1 authentication invariants; an
        # unauthenticated envelope is a legitimate value (e.g. anonymous / pre-authn).
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
    _verified: bool = False,
) -> IdentityEnvelope:
    """Construct a service (machine) IdentityEnvelope.

    FAIL-CLOSED like ``build_human_identity``: an ``attestation`` string supplied by a caller
    is a *claim*, not proof. The envelope is only ``authenticated=True`` when the service
    identity mechanism (mTLS / signed deploy token — stubbed for now, wired at deploy time)
    has verified it and passes ``_verified=True``. Service identity is a separate concern from
    human OIDC and stays on its own path.
    """
    if not subject.startswith("service:"):
        raise IdentityError("service subject must be prefixed 'service:'")
    env = IdentityEnvelope(
        subject=subject,
        actor_kind="service",
        authenticated=_verified,
        auth_method="workload-identity",
        role_claims=tuple(role_claims),
        groups=tuple(groups),
        tenant=tenant,
        attestation=attestation,
        source_of_authority=source_of_authority,
    )
    if _verified:
        validate_identity(env)
    return env
