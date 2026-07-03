"""Identity attestation boundary (SP-0.5 BLOCKER #1).

Proves the NON-NEGOTIABLE invariant: ordinary code cannot mint an ``authenticated=True``
principal. Only a verifier may attest an identity. The ``fake_oidc`` fixture stands up a
real RSA keypair + JWKS so ``OidcVerifier`` is exercised against genuine RS256 signatures
(no mocks): the trusted key mints valid tokens; a second, untrusted key forges.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "https://issuer.test/featuregen"
AUDIENCE = "featuregen"


def _new_rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _public_jwk(private_key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return jwk


@dataclass
class FakeOidc:
    """A throwaway OIDC issuer: a trusted signing key (in the JWKS) and an untrusted attacker
    key (NOT in the JWKS) so a forged signature can be exercised end-to-end."""

    issuer: str
    audience: str
    jwks: dict[str, Any]
    trusted_key: rsa.RSAPrivateKey
    attacker_key: rsa.RSAPrivateKey
    kid: str

    def mint(
        self,
        *,
        subject: str,
        roles: list[str],
        sign_with: str = "trusted-key",
        issuer: str | None = None,
        audience: str | None = None,
        expired: bool = False,
        groups: list[str] | None = None,
    ) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "sub": subject,
            "roles": list(roles),
            "groups": list(groups or []),
            "iss": issuer if issuer is not None else self.issuer,
            "aud": audience if audience is not None else self.audience,
            "iat": now,
            "exp": now - 60 if expired else now + 3600,
        }
        key = self.attacker_key if sign_with == "attacker-key" else self.trusted_key
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": self.kid})


@pytest.fixture
def fake_oidc() -> FakeOidc:
    trusted = _new_rsa_key()
    attacker = _new_rsa_key()
    kid = "test-key-1"
    jwks = {"keys": [_public_jwk(trusted, kid)]}
    return FakeOidc(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks=jwks,
        trusted_key=trusted,
        attacker_key=attacker,
        kid=kid,
    )


def test_forged_envelope_is_not_authenticated() -> None:
    """A directly-constructed envelope must NOT be authenticated=True (review BLOCKER #1).
    Only the verifier may mint an authenticated principal."""
    from featuregen.identity.build import build_human_identity

    env = build_human_identity(subject="user:mallory", role_claims=["platform-admin"])
    assert env.authenticated is False


def test_forged_service_envelope_is_not_authenticated() -> None:
    """Service identity is self-asserted too unless attested by a verifier: a direct build
    must be unauthenticated even when an attestation string is supplied."""
    from featuregen.identity.build import build_service_identity

    env = build_service_identity(
        subject="service:rogue",
        role_claims=["platform-admin"],
        attestation="i-say-so",
    )
    assert env.authenticated is False


def test_oidc_verifier_accepts_a_valid_token(fake_oidc: FakeOidc) -> None:
    """A token signed by the trusted key with the right issuer/audience yields an authenticated
    envelope carrying exactly the token's claims."""
    from featuregen.identity.verify import OidcVerifier

    v = OidcVerifier(issuer=fake_oidc.issuer, audience="featuregen", jwks=fake_oidc.jwks)
    env = v.verify_human(fake_oidc.mint(subject="user:raj", roles=["data_scientist"]))
    assert env.authenticated is True
    assert env.subject == "user:raj"
    assert env.role_claims == ("data_scientist",)
    assert env.actor_kind == "human"
    assert env.auth_method == "oidc"


def test_oidc_verifier_rejects_wrong_signature(fake_oidc: FakeOidc) -> None:
    from featuregen.identity.verify import IdentityError, OidcVerifier

    v = OidcVerifier(issuer=fake_oidc.issuer, audience="featuregen", jwks=fake_oidc.jwks)
    with pytest.raises(IdentityError):
        v.verify_human(
            fake_oidc.mint(subject="user:x", roles=["platform-admin"], sign_with="attacker-key")
        )


def test_oidc_verifier_rejects_wrong_issuer(fake_oidc: FakeOidc) -> None:
    from featuregen.identity.verify import IdentityError, OidcVerifier

    v = OidcVerifier(issuer=fake_oidc.issuer, audience="featuregen", jwks=fake_oidc.jwks)
    with pytest.raises(IdentityError):
        v.verify_human(
            fake_oidc.mint(subject="user:x", roles=["platform-admin"], issuer="https://evil.test/")
        )


def test_oidc_verifier_rejects_wrong_audience(fake_oidc: FakeOidc) -> None:
    from featuregen.identity.verify import IdentityError, OidcVerifier

    v = OidcVerifier(issuer=fake_oidc.issuer, audience="featuregen", jwks=fake_oidc.jwks)
    with pytest.raises(IdentityError):
        v.verify_human(
            fake_oidc.mint(subject="user:x", roles=["platform-admin"], audience="some-other-api")
        )


def test_oidc_verifier_rejects_expired_token(fake_oidc: FakeOidc) -> None:
    from featuregen.identity.verify import IdentityError, OidcVerifier

    v = OidcVerifier(issuer=fake_oidc.issuer, audience="featuregen", jwks=fake_oidc.jwks)
    with pytest.raises(IdentityError):
        v.verify_human(
            fake_oidc.mint(subject="user:x", roles=["platform-admin"], expired=True)
        )


def test_oidc_verifier_maps_groups(fake_oidc: FakeOidc) -> None:
    from featuregen.identity.verify import OidcVerifier

    v = OidcVerifier(issuer=fake_oidc.issuer, audience="featuregen", jwks=fake_oidc.jwks)
    env = v.verify_human(
        fake_oidc.mint(subject="user:raj", roles=["data_scientist"], groups=["team-risk"])
    )
    assert env.groups == ("team-risk",)


def test_current_identity_verifier_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no verifier registered the accessor must FAIL CLOSED (never silently allow)."""
    from featuregen.identity import verify

    monkeypatch.setattr(verify, "_IDENTITY_VERIFIER", None)
    with pytest.raises(RuntimeError):
        verify.current_identity_verifier()


def test_register_and_current_identity_verifier_round_trip(fake_oidc: FakeOidc) -> None:
    from featuregen.identity import verify
    from featuregen.identity.verify import OidcVerifier

    v = OidcVerifier(issuer=fake_oidc.issuer, audience="featuregen", jwks=fake_oidc.jwks)
    monkeypatch_value = verify._IDENTITY_VERIFIER
    try:
        verify.register_identity_verifier(v)
        assert verify.current_identity_verifier() is v
    finally:
        verify._IDENTITY_VERIFIER = monkeypatch_value
