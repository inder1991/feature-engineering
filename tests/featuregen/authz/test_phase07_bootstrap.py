import pytest
from tests.featuregen._helpers import make_cmd

from featuregen.aggregates.bootstrap import bootstrap_phase07
from featuregen.aggregates.commands import register_phase06_commands
from featuregen.commands.api import execute_command
from featuregen.commands.authz_seam import current_authorizer, register_command_authorizer
from featuregen.commands.registry import clear_registry
from featuregen.contracts.gates import GateTaskSpec
from featuregen.contracts.identity import IdentityEnvelope
from featuregen.gates.tasks import open_task, submit_human_signal
from featuregen.identity.build import IdentityError, build_human_identity, build_service_identity


@pytest.fixture(autouse=True)
def _wired(db):
    """Wire the FULL production path: §4.4 catalog + §6.2 PolicyAuthorizer (seeded)."""
    saved = current_authorizer()
    clear_registry()
    register_phase06_commands()
    bootstrap_phase07(db)
    yield
    clear_registry()
    register_command_authorizer(saved)


def _svc():
    return build_service_identity(
        subject="service:intake-agent",
        role_claims=["workflow"],
        attestation="signed-deploy-id:wf@1.0.0",
    )


def _data_steward_task(db, run_id):
    return open_task(
        db,
        GateTaskSpec(
            gate="DATA_STEWARD",
            required_inputs=("confirmed_contract_ref",),
            eligible_assignees={"role": "data_owner", "scope": "core.transactions"},
            allowed_responses=("confirm", "reject"),
            run_id=run_id,
            sla="7d",
        ),
        _svc(),
    )


def test_unauthorized_command_is_denied_logged_and_emits_no_event(db):
    # A data_scientist may NOT `activate` (§6.2 requires the `release` role). With the real
    # PolicyAuthorizer registered, execute_command must deny, route the denial to the
    # tamper-evident security stream, and run NO handler (no domain event).
    actor = build_human_identity(subject="user:ds", role_claims=["data_scientist"])
    before = db.execute("SELECT count(*) FROM events").fetchone()[0]
    res = execute_command(
        db,
        make_cmd(
            "activate",
            "feature",
            "feat_unauth",
            {"feature_version_id": "fv1", "use_case": "fraud"},
            actor=actor,
        ),
    )
    assert res.accepted is False
    denied = db.execute(
        "SELECT count(*) FROM security_audit "
        "WHERE event_type='COMMAND_DENIED' AND attempted_action='activate'"
    ).fetchone()[0]
    assert denied == 1
    after = db.execute("SELECT count(*) FROM events").fetchone()[0]
    assert after == before  # handler never ran -> no domain event


def test_submit_human_signal_routes_through_execute_command(db):
    task_id = _data_steward_task(db, "run_ok")
    owner = build_human_identity(
        subject="user:do", role_claims=["data_owner"], groups=["core.transactions"]
    )
    res = execute_command(
        db,
        make_cmd(
            "submit_human_signal",
            "run",
            "run_ok",
            {
                "gate": "DATA_STEWARD",
                "task_id": task_id,
                "response": "confirm",
                "expected_task_version": 1,
            },
            actor=owner,
        ),
    )
    assert res.accepted is True
    n = db.execute(
        "SELECT count(*) FROM human_task_responses WHERE task_id=%s", (task_id,)
    ).fetchone()[0]
    assert n == 1


def test_submit_human_signal_denied_for_wrong_role_logs_and_skips_handler(db):
    task_id = _data_steward_task(db, "run_bad")
    intruder = build_human_identity(
        subject="user:intruder", role_claims=["compliance"], groups=["core.transactions"]
    )
    res = execute_command(
        db,
        make_cmd(
            "submit_human_signal",
            "run",
            "run_bad",
            {
                "gate": "DATA_STEWARD",
                "task_id": task_id,
                "response": "confirm",
                "expected_task_version": 1,
            },
            actor=intruder,
        ),
    )
    assert res.accepted is False
    n = db.execute(
        "SELECT count(*) FROM human_task_responses WHERE task_id=%s", (task_id,)
    ).fetchone()[0]
    assert n == 0  # handler skipped -> no answer recorded
    denied = db.execute(
        "SELECT count(*) FROM security_audit "
        "WHERE event_type='COMMAND_DENIED' AND attempted_action='submit_human_signal'"
    ).fetchone()[0]
    assert denied == 1


def test_submit_human_signal_direct_call_rejects_unauthenticated_actor(db):
    task_id = _data_steward_task(db, "run_unauth")
    unauth = IdentityEnvelope(
        subject="user:ghost",
        actor_kind="human",
        authenticated=False,
        auth_method="oidc",
        role_claims=("data_owner",),
        groups=("core.transactions",),
    )
    with pytest.raises(IdentityError):
        submit_human_signal(db, task_id, response="confirm", actor=unauth, expected_task_version=1)
