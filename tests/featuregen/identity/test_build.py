import pytest

from featuregen.contracts.identity import IdentityEnvelope
from featuregen.identity.build import (
    IdentityError,
    build_human_identity,
    build_service_identity,
    validate_identity,
)


def test_build_human_is_oidc_authenticated():
    env = build_human_identity(subject="user:raj", role_claims=["data_scientist"])
    assert env.actor_kind == "human"
    assert env.auth_method == "oidc"
    assert env.authenticated is True
    assert env.role_claims == ("data_scientist",)
    assert env.attestation is None


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
