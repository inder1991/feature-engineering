"""Shared test helpers.

Identity minting for tests goes THROUGH the identity-verification seam (SP-0.5 BLOCKER #1).
``build_human_identity`` / ``build_service_identity`` are fail-closed in production — they
produce ``authenticated=False``. Tests legitimately need authenticated principals, so this
module registers a permissive ``FakeVerifier`` and exposes ``mint_test_identity`` /
``mint_test_service_identity``, which mint authenticated envelopes ONLY via that verifier.

This keeps the whole suite exercising the same invariant as production — the verifier is the
sole authenticated-mint path — while confining the test-only plumbing to this one module. The
verifier is registered at import time (so module-level identity constants in child conftests
resolve) and re-registered per-test by an autouse fixture in ``tests/featuregen/conftest.py``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from featuregen.aggregates.ids import mint_id
from featuregen.contracts import Command, IdentityEnvelope
from featuregen.identity.build import build_human_identity, build_service_identity
from featuregen.identity.verify import current_identity_verifier, register_identity_verifier


class FakeVerifier:
    """Permissive test stand-in for a real OIDC / workload-identity verifier.

    It performs NO cryptographic check — that omission is exactly what makes it test-only;
    production registers an ``OidcVerifier``. Its job is to prove the boundary: it accepts a
    JSON claims "token" and mints an authenticated envelope through the internal ``_verified``
    seam, so tests obtain authenticated principals the same way production does — via a verifier.
    """

    def verify_human(self, token: str) -> IdentityEnvelope:
        claims = json.loads(token)
        return build_human_identity(_verified=True, **claims)

    def verify_service(self, token: str) -> IdentityEnvelope:
        claims = json.loads(token)
        return build_service_identity(_verified=True, **claims)


def install_fake_identity_verifier() -> None:
    """Register the permissive FakeVerifier as the process-wide identity verifier."""
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
    """Mint an AUTHENTICATED human identity for tests, THROUGH the registered verifier.

    Drop-in replacement for ``build_human_identity`` in tests: same keyword arguments, but the
    result is authenticated because it flows through the verifier seam (proving that path is the
    only way to obtain an authenticated principal)."""
    token = json.dumps(
        {
            "subject": subject,
            "role_claims": list(role_claims),
            "auth_method": auth_method,
            "groups": list(groups),
            "tenant": tenant,
            "source_of_authority": source_of_authority,
            "on_behalf_of": on_behalf_of,
            "impersonation": impersonation,
            "break_glass": break_glass,
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
    """Mint an AUTHENTICATED service identity for tests, THROUGH the registered verifier.
    Drop-in replacement for ``build_service_identity`` in tests."""
    token = json.dumps(
        {
            "subject": subject,
            "role_claims": list(role_claims),
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
