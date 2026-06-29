from featuregen.contracts.identity import (
    IdentityEnvelope,
    identity_from_jsonb,
    identity_to_jsonb,
)


def _human() -> IdentityEnvelope:
    return IdentityEnvelope(
        subject="user:raj",
        actor_kind="human",
        authenticated=True,
        auth_method="oidc",
        role_claims=("data_scientist", "approver"),
        groups=("payments-ds",),
        tenant="retail-bank",
        source_of_authority="iam-snapshot@2026-06-27T10:14Z",
    )


def test_to_jsonb_emits_lists_not_tuples():
    d = identity_to_jsonb(_human())
    assert d["subject"] == "user:raj"
    assert d["role_claims"] == ["data_scientist", "approver"]
    assert isinstance(d["role_claims"], list)
    assert d["groups"] == ["payments-ds"]
    assert d["break_glass"] is False
    assert d["attestation"] is None


def test_round_trip_is_identity():
    env = _human()
    assert identity_from_jsonb(identity_to_jsonb(env)) == env


def test_service_attestation_round_trips():
    svc = IdentityEnvelope(
        subject="service:intake-agent",
        actor_kind="service",
        authenticated=True,
        auth_method="workload-identity",
        role_claims=("intake-agent",),
        attestation="signed-deploy-id:sp2-intake@1.4.0",
    )
    assert identity_from_jsonb(identity_to_jsonb(svc)) == svc


def test_gates_reexports_canonical_types():
    import featuregen.contracts.envelopes as envelopes
    import featuregen.contracts.gates as gates

    assert gates.GateTaskSpec is envelopes.GateTaskSpec
    assert gates.SignalResult is envelopes.SignalResult
