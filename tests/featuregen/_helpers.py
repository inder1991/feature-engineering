"""Shared test helpers.

Identity minting for tests goes THROUGH the genuine verifier path (SP-0.5 BLOCKER #1). This module
stands up ONE session-scoped fake OIDC issuer — a real RSA signing key + JWKS — and registers a
``FakeVerifier`` that delegates to a REAL ``OidcVerifier`` configured with that JWKS/issuer/audience.
``mint_test_identity`` / ``mint_test_service_identity`` therefore SIGN a real JWT with the fake
issuer's key and run it through genuine RS256/JWKS verification: every authenticated identity in the
whole suite is produced by the same verifier path as production, never by a forgeable flag. The
private trust capability is NEVER touched by test code — it can only be reached, indirectly, by
minting a token and proving it.

The only thing that makes this "fake" is that the issuer's keys are generated in-process rather than
belonging to a real IdP; the verification is real. The verifier is registered at import time (so
module-level identity constants in child conftests resolve) and re-registered per-test by an autouse
fixture in ``tests/featuregen/conftest.py``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from featuregen.aggregates.ids import mint_id
from featuregen.contracts import Command, IdentityEnvelope
from featuregen.identity.verify import (
    OidcVerifier,
    current_identity_verifier,
    register_identity_verifier,
)

# ── Session-scoped fake OIDC issuer: ONE RSA keypair + JWKS for the whole suite ──────────────────
_ISSUER = "https://issuer.test/featuregen"
_AUDIENCE = "featuregen"
_KID = "test-signing-key-1"
_SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks() -> dict[str, Any]:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(_SIGNING_KEY.public_key()))
    jwk.update({"kid": _KID, "use": "sig", "alg": "RS256"})
    return {"keys": [jwk]}


# The REAL production verifier, configured against the fake issuer's JWKS. All test authenticated
# identities are minted by THIS object proving a genuinely-signed token.
_REAL_VERIFIER = OidcVerifier(issuer=_ISSUER, audience=_AUDIENCE, jwks=_jwks())


def _sign(claims: dict[str, Any]) -> str:
    now = int(time.time())
    payload = {"iss": _ISSUER, "aud": _AUDIENCE, "iat": now, "exp": now + 3600, **claims}
    return jwt.encode(payload, _SIGNING_KEY, algorithm="RS256", headers={"kid": _KID})


class FakeVerifier:
    """Test verifier that proves a genuinely-signed fake JWT via a REAL ``OidcVerifier``.

    There is no forgeable flag anywhere: the only way this yields an authenticated envelope is by
    the real verifier checking the token's RS256 signature/issuer/audience/expiry — exactly the
    production path. "Fake" refers only to the in-process issuer keys, not the verification.
    """

    def __init__(self) -> None:
        self._verifier = _REAL_VERIFIER

    def verify_human(self, token: str) -> IdentityEnvelope:
        return self._verifier.verify_human(token)

    def verify_service(self, token: str) -> IdentityEnvelope:
        return self._verifier.verify_service(token)


def install_fake_identity_verifier() -> None:
    """Register the fake-issuer-backed real verifier as the process-wide identity verifier."""
    register_identity_verifier(FakeVerifier())


# Register at import time so module-level identity constants (e.g. ``REQUESTER = mint_test_identity(...)``
# in child conftests) resolve a verifier the moment this helper is imported.
install_fake_identity_verifier()


def mint_test_identity(
    *,
    subject: str = "user:raj",
    role_claims: Iterable[str] = ("data_scientist",),
    auth_method: str = "oidc",
    groups: Iterable[str] = (),
    tenant: str | None = None,
    source_of_authority: str | None = None,
    on_behalf_of: str | None = None,
    impersonation: str | None = None,
    break_glass: bool = False,
) -> IdentityEnvelope:
    """Mint an AUTHENTICATED human identity for tests by SIGNING a JWT and proving it.

    Drop-in replacement for ``build_human_identity`` in tests: same keyword arguments, but the
    result is authenticated because a genuinely-signed token flows through a real ``OidcVerifier``
    (proving that verifier path is the only way to obtain an authenticated principal). ``auth_method``
    (always ``oidc`` for a human) plus the privileged ``on_behalf_of`` / ``impersonation`` /
    ``break_glass`` are accepted for signature compatibility but NOT trusted from a token — the
    verifier never maps them (a token must never self-assert break-glass). No test mints those via
    this helper; they are set on the envelope only by the code paths that legitimately own them."""
    token = _sign(
        {
            "sub": subject,
            "roles": list(role_claims),
            "groups": list(groups),
            "tenant": tenant,
            "source_of_authority": source_of_authority,
        }
    )
    return current_identity_verifier().verify_human(token)


def mint_test_service_identity(
    *,
    subject: str,
    role_claims: Iterable[str],
    attestation: str,
    groups: Iterable[str] = (),
    tenant: str | None = None,
    source_of_authority: str | None = None,
) -> IdentityEnvelope:
    """Mint an AUTHENTICATED service identity for tests by SIGNING a workload-identity JWT and
    proving it. Drop-in replacement for ``build_service_identity`` in tests."""
    token = _sign(
        {
            "sub": subject,
            "roles": list(role_claims),
            "attestation": attestation,
            "groups": list(groups),
            "tenant": tenant,
            "source_of_authority": source_of_authority,
        }
    )
    return current_identity_verifier().verify_service(token)


def make_actor(subject="user:raj", actor_kind="human", roles=("data_scientist",)):
    if actor_kind == "service":
        return mint_test_service_identity(
            subject=subject, role_claims=roles, attestation="test-attestation"
        )
    return mint_test_identity(subject=subject, role_claims=roles)


def make_cmd(
    action, aggregate, aggregate_id, args, *, actor=None, idem=None, expected_version=None
):
    return Command(
        action=action,
        aggregate=aggregate,
        aggregate_id=aggregate_id,
        args=args,
        actor=actor or make_actor(),
        idempotency_key=idem or mint_id("idem"),
        expected_version=expected_version,
    )
