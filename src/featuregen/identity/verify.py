"""Identity attestation seam (SP-0.5 BLOCKER #1).

A verifier is the ONLY token-facing code that may mint an ``authenticated=True`` IdentityEnvelope.
``build.py`` is fail-closed; a verifier proves a token and then hands ``build_*`` the private trust
CAPABILITY (``identity._trust._TRUST_CAPABILITY``) so the resulting principal is trustworthy for
authz / four-eyes / audit attribution. The capability replaces the old forgeable ``_verified: bool``
flag: ordinary code cannot name the object, so cannot mint a principal it has not proven.

OIDC-first, swappable issuer: ``OidcVerifier`` takes ``issuer`` / ``audience`` / ``jwks`` as
CONFIG. Moving from local users today to enterprise AD / Entra later is a configuration change
(different issuer + JWKS), never a code change. Both human (``verify_human``) and service
(``verify_service``) tokens are proven the same RS256/JWKS way. This is the IN-PROCESS verification
seam only; the deploy-time transport EDGE — where a bearer/mTLS token physically arrives and is
resolved into a call — is a separate deferred task (the SP-0.5 forgery-blocker follow-up).
"""

from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable

import jwt

from featuregen.contracts.identity import IdentityEnvelope

# IdentityError is defined in build.py (many modules already import it there). Re-export it so
# the verifier seam is self-contained: ``from featuregen.identity.verify import IdentityError``.
from featuregen.identity._trust import _TRUST_CAPABILITY
from featuregen.identity.build import IdentityError, build_human_identity, build_service_identity

__all__ = [
    "IdentityError",
    "IdentityVerifier",
    "OidcVerifier",
    "register_identity_verifier",
    "current_identity_verifier",
]


@runtime_checkable
class IdentityVerifier(Protocol):
    """Proves a bearer token and returns an authenticated IdentityEnvelope, or raises
    ``IdentityError`` on ANY failure (bad signature, wrong issuer/audience, expiry, malformed
    claims). Implementations MUST fail closed: never return an unauthenticated or partially
    trusted envelope."""

    def verify_human(self, token: str) -> IdentityEnvelope: ...

    def verify_service(self, token: str) -> IdentityEnvelope: ...


class OidcVerifier:
    """Verify an OIDC ID/access token (RS256 via JWKS) and map its verified claims to an
    authenticated human IdentityEnvelope.

    Trust is established by PyJWT: the signature is checked against the JWKS key selected by the
    token's ``kid`` header, and ``iss`` / ``aud`` / ``exp`` are enforced. Only after that do we call
    ``build_*`` with the private trust capability — so an ``authenticated=True`` envelope can exist
    ONLY downstream of a proven token.
    """

    def __init__(self, *, issuer: str, audience: str, jwks: dict[str, Any]) -> None:
        self._issuer = issuer
        self._audience = audience
        self._jwks = jwks

    def _signing_key(self, token: str) -> Any:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:  # malformed token header
            raise IdentityError(f"malformed token header: {exc}") from exc
        kid = header.get("kid")
        for jwk in self._jwks.get("keys", []):
            if jwk.get("kid") == kid:
                try:
                    return jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))
                except (jwt.PyJWTError, ValueError, TypeError) as exc:
                    raise IdentityError(f"unusable JWKS key for kid={kid!r}: {exc}") from exc
        raise IdentityError(f"no signing key in JWKS for kid={kid!r}")

    def _verify_claims(self, token: str) -> dict[str, Any]:
        key = self._signing_key(token)
        try:
            return jwt.decode(
                token,
                key=key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            # Collapse every PyJWT failure (bad signature, wrong iss/aud, expired, missing
            # required claim) into a single fail-closed IdentityError.
            raise IdentityError(f"token verification failed: {exc}") from exc

    def verify_human(self, token: str) -> IdentityEnvelope:
        claims = self._verify_claims(token)
        subject = claims["sub"]
        try:
            return build_human_identity(
                subject=subject,
                role_claims=tuple(claims.get("roles", ())),
                groups=tuple(claims.get("groups", ())),
                tenant=claims.get("tenant"),
                # source_of_authority is a provenance attribute the IdP may assert (e.g. the IAM
                # snapshot the session was authorized against); it is carried onto the envelope.
                # break_glass / impersonation are deliberately NOT mapped from claims — those are
                # privileged and must never be self-assertable by a token (SECURITY).
                source_of_authority=claims.get("source_of_authority"),
                _capability=_TRUST_CAPABILITY,
            )
        except IdentityError:
            raise
        except Exception as exc:  # pragma: no cover - defensive claim-shape guard
            raise IdentityError(f"could not map verified claims to identity: {exc}") from exc

    def verify_service(self, token: str) -> IdentityEnvelope:
        """Prove a signed workload-identity token (JWT-SVID style) and map it to an authenticated
        SERVICE principal. Same RS256/JWKS proof as ``verify_human`` — the signature, issuer,
        audience and expiry are enforced before the capability mints the envelope, so a service
        principal too can exist ONLY downstream of a proven token. The deploy-time transport edge
        (mTLS termination / token delivery) remains deferred."""
        claims = self._verify_claims(token)
        subject = claims["sub"]
        try:
            return build_service_identity(
                subject=subject,
                role_claims=tuple(claims.get("roles", ())),
                attestation=claims.get("attestation"),
                groups=tuple(claims.get("groups", ())),
                tenant=claims.get("tenant"),
                source_of_authority=claims.get("source_of_authority"),
                _capability=_TRUST_CAPABILITY,
            )
        except IdentityError:
            raise
        except Exception as exc:  # pragma: no cover - defensive claim-shape guard
            raise IdentityError(
                f"could not map verified claims to service identity: {exc}"
            ) from exc


# Process-wide identity verifier. Mirrors the `register_catalog_adapter` / `current_catalog_adapter`
# module-global idiom: last writer wins, fails CLOSED when unset so no code can resolve an
# authenticated principal without an explicitly configured verifier.
_IDENTITY_VERIFIER: IdentityVerifier | None = None


def register_identity_verifier(verifier: IdentityVerifier) -> None:
    """Register the process-wide identity verifier (production bootstrap / tests do this)."""
    global _IDENTITY_VERIFIER
    _IDENTITY_VERIFIER = verifier


def current_identity_verifier() -> IdentityVerifier:
    """Return the registered identity verifier.

    Fails closed: raises ``RuntimeError`` if none has been registered, so no caller can resolve
    an authenticated principal against a missing verifier.
    """
    if _IDENTITY_VERIFIER is None:
        raise RuntimeError(
            "no identity verifier registered; call register_identity_verifier(...) "
            "(the production identity bootstrap does this)"
        )
    return _IDENTITY_VERIFIER


def _clear_identity_verifier() -> None:
    """Test-only reset of the module-global verifier."""
    global _IDENTITY_VERIFIER
    _IDENTITY_VERIFIER = None
