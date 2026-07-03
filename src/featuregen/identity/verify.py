"""Identity attestation seam (SP-0.5 BLOCKER #1).

The ONLY place an ``authenticated=True`` IdentityEnvelope may be produced. ``build.py`` is
fail-closed; a verifier proves a token and passes the internal ``_verified=True`` flag so the
resulting principal is trustworthy for authz / four-eyes / audit attribution.

OIDC-first, swappable issuer: ``OidcVerifier`` takes ``issuer`` / ``audience`` / ``jwks`` as
CONFIG. Moving from local users today to enterprise AD / Entra later is a configuration change
(different issuer + JWKS), never a code change. Service (machine) identity is a separate
concern and stays on its own path (``verify_service``); its full mechanism (mTLS / signed
deploy token) is stubbed here and wired at deploy time.
"""

from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable

import jwt

from featuregen.contracts.identity import IdentityEnvelope

# IdentityError is defined in build.py (many modules already import it there). Re-export it so
# the verifier seam is self-contained: ``from featuregen.identity.verify import IdentityError``.
from featuregen.identity.build import IdentityError, build_human_identity

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
    token's ``kid`` header, and ``iss`` / ``aud`` / ``exp`` are enforced. Only after that do we
    call ``build_human_identity(..., _verified=True)`` — so the ``authenticated=True`` envelope
    can exist ONLY downstream of a proven token.
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
                _verified=True,
            )
        except IdentityError:
            raise
        except Exception as exc:  # pragma: no cover - defensive claim-shape guard
            raise IdentityError(f"could not map verified claims to identity: {exc}") from exc

    def verify_service(self, token: str) -> IdentityEnvelope:
        # Service (machine) identity is a SEPARATE concern from human OIDC. Its full mechanism
        # (mTLS / signed deploy token) is not modelled by this human-OIDC verifier. Keeping this
        # explicit — rather than silently reusing the human path — preserves the distinction and
        # fails closed until a real service verifier is wired at deploy time.
        raise IdentityError(
            "OidcVerifier proves human OIDC tokens only; service identity uses the "
            "workload-identity mechanism (wired at deploy time)"
        )


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
