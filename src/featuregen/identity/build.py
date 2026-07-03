from __future__ import annotations

from collections.abc import Iterable

from featuregen.contracts.identity import IdentityEnvelope
from featuregen.identity._trust import _TRUST_CAPABILITY


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
    _capability: object | None = None,
) -> IdentityEnvelope:
    """Construct a human IdentityEnvelope.

    FAIL-CLOSED (SP-0.5 BLOCKER #1): ordinary callers get ``authenticated=False``. Claims on
    an envelope are only *asserted* here — they become *authenticated* solely when the caller
    hands over the private trust CAPABILITY (``identity._trust._TRUST_CAPABILITY``), which only a
    verifier holds after proving a token's signature/issuer/audience/expiry. The capability is
    compared by identity (``is``), never by value, and is absent from every ``__all__`` — so it
    replaces the old forgeable ``_verified: bool`` seam: ordinary code cannot name the object, so
    cannot mint a principal it has not proven. Passing the removed ``_verified`` kwarg now raises
    ``TypeError`` instead of forging anything.
    """
    if not subject.startswith("user:"):
        raise IdentityError("human subject must be prefixed 'user:'")
    authenticated = _capability is _TRUST_CAPABILITY
    env = IdentityEnvelope(
        subject=subject,
        actor_kind="human",
        authenticated=authenticated,
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
    if authenticated:
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
    _capability: object | None = None,
) -> IdentityEnvelope:
    """Construct a service (machine) IdentityEnvelope.

    FAIL-CLOSED like ``build_human_identity``: an ``attestation`` string supplied by a caller
    is a *claim*, not proof. The envelope is only ``authenticated=True`` when the caller hands
    over the private trust CAPABILITY, which the service verifier holds after proving a signed
    workload-identity token. The forgeable ``_verified: bool`` seam is gone.
    """
    if not subject.startswith("service:"):
        raise IdentityError("service subject must be prefixed 'service:'")
    authenticated = _capability is _TRUST_CAPABILITY
    env = IdentityEnvelope(
        subject=subject,
        actor_kind="service",
        authenticated=authenticated,
        auth_method="workload-identity",
        role_claims=tuple(role_claims),
        groups=tuple(groups),
        tenant=tenant,
        attestation=attestation,
        source_of_authority=source_of_authority,
    )
    if authenticated:
        validate_identity(env)
    return env
