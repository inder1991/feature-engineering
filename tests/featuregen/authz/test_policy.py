from tests.featuregen._helpers import mint_test_identity, mint_test_service_identity

from featuregen.authz.policy import AuthzDecision, authorize_command, seed_authz_policy
from featuregen.contracts.commands import Command
from featuregen.contracts.identity import IdentityEnvelope


def _cmd(action, actor, *, aggregate="feature", aggregate_id="feature_1", args=None):
    return Command(
        action=action,
        aggregate=aggregate,
        aggregate_id=aggregate_id,
        args=args or {},
        actor=actor,
        idempotency_key="idem_" + action,
    )


def test_authorized_human_action(db):
    seed_authz_policy(db)
    raj = mint_test_identity(subject="user:raj", role_claims=["release"])
    assert authorize_command(db, _cmd("activate", raj)) == AuthzDecision(True)


def test_wrong_role_denied(db):
    seed_authz_policy(db)
    raj = mint_test_identity(subject="user:raj", role_claims=["data_scientist"])
    decision = authorize_command(db, _cmd("activate", raj))
    assert decision.allowed is False
    assert decision.reason == "no matching authz policy"


def test_attested_service_authorized(db):
    seed_authz_policy(db)
    svc = mint_test_service_identity(
        subject="service:intake-agent",
        role_claims=["intake-agent"],
        attestation="signed-deploy-id:sp2-intake@1.4.0",
    )
    assert (
        authorize_command(
            db, _cmd("create_run", svc, aggregate="request", aggregate_id="request_1")
        ).allowed
        is True
    )


def test_self_asserted_service_denied(db):
    seed_authz_policy(db)
    rogue = IdentityEnvelope(
        subject="service:rogue",
        actor_kind="service",
        authenticated=True,
        auth_method="workload-identity",
        role_claims=("intake-agent",),
        attestation=None,
    )
    decision = authorize_command(
        db, _cmd("create_run", rogue, aggregate="request", aggregate_id="request_1")
    )
    assert decision.allowed is False


def test_gate_scoped_action_uses_gate_column(db):
    seed_authz_policy(db)
    owner = mint_test_identity(subject="user:do", role_claims=["data_owner"])
    ok = authorize_command(
        db,
        _cmd(
            "submit_human_signal",
            owner,
            aggregate="run",
            aggregate_id="run_1",
            args={"gate": "DATA_STEWARD", "task_id": "task_1"},
        ),
    )
    assert ok.allowed is True
    wrong_gate = authorize_command(
        db,
        _cmd(
            "submit_human_signal",
            owner,
            aggregate="run",
            aggregate_id="run_1",
            args={"gate": "COMPLIANCE", "task_id": "task_1"},
        ),
    )
    assert wrong_gate.allowed is False


def test_command_contract_fields_match():
    # The file is shared with Phase 06 (authoritative). Pin the transcribed dataclass
    # signatures to the overview contract so any divergence fails loudly here instead of
    # producing a silent clearLayers()-vs-clearFullLayers() mismatch across phases.
    import dataclasses

    from featuregen.contracts.commands import Command, CommandResult

    assert [f.name for f in dataclasses.fields(Command)] == [
        "action",
        "aggregate",
        "aggregate_id",
        "args",
        "actor",
        "idempotency_key",
        "expected_version",
    ]
    assert [f.name for f in dataclasses.fields(CommandResult)] == [
        "accepted",
        "aggregate_id",
        "produced_event_ids",
        "denied_reason",
    ]
