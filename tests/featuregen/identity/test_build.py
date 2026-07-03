import pytest

from featuregen.contracts.identity import IdentityEnvelope
from featuregen.identity.build import (
    IdentityError,
    build_human_identity,
    build_service_identity,
    validate_identity,
)


def test_build_human_is_unauthenticated_by_default():
    """FAIL-CLOSED (BLOCKER #1): a directly-built human identity carries its claimed shape but
    is NOT authenticated. Authentication is granted only by a verifier (see test_verify.py)."""
    env = build_human_identity(subject="user:raj", role_claims=["data_scientist"])
    assert env.actor_kind == "human"
    assert env.auth_method == "oidc"
    assert env.authenticated is False
    assert env.role_claims == ("data_scientist",)
    assert env.attestation is None


def test_verified_bool_kwarg_is_removed_and_cannot_forge():
    """The old forgeable ``_verified: bool`` seam is GONE (SP-0.5 BLOCKER #1 hardening). Passing
    it can no longer mint an authenticated principal — the kwarg does not exist, so an ordinary
    caller trying the historic bypass gets a TypeError, not a forged authenticated envelope."""
    with pytest.raises(TypeError):
        build_human_identity(
            subject="user:raj", role_claims=["data_scientist"], _verified=True
        )


def test_service_verified_bool_kwarg_is_removed_and_cannot_forge():
    with pytest.raises(TypeError):
        build_service_identity(
            subject="service:x", role_claims=["r"], attestation="a", _verified=True
        )


def test_mint_trusted_identity_yields_authenticated_principal():
    """The sanctioned internal factory (used only by the event-store serde and timer runtime) is
    the capability-holding path that mints an authenticated principal WITHOUT a token."""
    from featuregen.identity._trust import mint_trusted_identity

    env = mint_trusted_identity(
        subject="service:timer-runtime",
        actor_kind="service",
        auth_method="internal",
        role_claims=(),
    )
    assert env.authenticated is True
    assert env.actor_kind == "service"


def test_build_human_rejects_unprefixed_subject():
    with pytest.raises(IdentityError):
        build_human_identity(subject="raj", role_claims=["data_scientist"])


def test_build_service_requires_attestation():
    env = build_service_identity(
        subject="service:intake-agent",
        role_claims=["intake-agent"],
        attestation="signed-deploy-id:sp2-intake@1.4.0",
    )
    assert env.actor_kind == "service"
    assert env.auth_method == "workload-identity"
    assert env.attestation == "signed-deploy-id:sp2-intake@1.4.0"


def test_service_without_attestation_is_self_asserted_and_rejected():
    self_asserted = IdentityEnvelope(
        subject="service:rogue",
        actor_kind="service",
        authenticated=True,
        auth_method="workload-identity",
        role_claims=("approver",),
        attestation=None,
    )
    with pytest.raises(IdentityError):
        validate_identity(self_asserted)


def test_unauthenticated_is_rejected():
    anon = IdentityEnvelope(
        subject="user:ghost",
        actor_kind="human",
        authenticated=False,
        auth_method="oidc",
        role_claims=(),
    )
    with pytest.raises(IdentityError):
        validate_identity(anon)


def test_validate_identity_admits_attested_internal_trust_root():
    # The durable timer runtime is an internal trust root (no token); it authenticates via the
    # sanctioned capability with auth_method="internal" + an attestation. validate_identity must
    # admit it (SP-0.5 round-2). It currently rejects auth_method != "workload-identity".
    from featuregen.aggregates.activation import _TIMER_RUNTIME_ACTOR

    validate_identity(_TIMER_RUNTIME_ACTOR)  # must not raise


def test_validate_identity_still_rejects_unattested_internal():
    # No weakening: an internal service envelope WITHOUT attestation is still rejected.
    from featuregen.identity._trust import mint_trusted_identity

    env = mint_trusted_identity(
        subject="service:x", actor_kind="service", auth_method="internal",
        role_claims=(), attestation=None,
    )
    with pytest.raises(IdentityError):
        validate_identity(env)


def test_validate_identity_still_rejects_unattested_workload_service():
    from featuregen.identity._trust import mint_trusted_identity

    env = mint_trusted_identity(
        subject="service:y", actor_kind="service", auth_method="workload-identity",
        role_claims=(), attestation=None,
    )
    with pytest.raises(IdentityError):
        validate_identity(env)
