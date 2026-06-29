import pytest

from featuregen.contracts.gates import GateTaskSpec
from featuregen.gates.tasks import (
    IneligibleResponderError,
    ResponseNotAllowedError,
    SoDViolationError,
    bump_task_version,
    grant_task_delegation,
    open_task,
    submit_human_signal,
)
from featuregen.identity.build import build_human_identity, build_service_identity


def _svc():
    return build_service_identity(
        subject="service:intake-agent", role_claims=["workflow"],
        attestation="signed-deploy-id:sp2-intake@1.4.0",
    )


def _open(db, **kw):
    base = dict(
        gate="DATA_STEWARD",
        required_inputs=("confirmed_contract_ref",),
        eligible_assignees={"role": "data_owner", "scope": "core.transactions"},
        allowed_responses=("confirm", "edit", "reject"),
        run_id="run_1",
        sla="7d",
    )
    base.update(kw)
    return open_task(db, GateTaskSpec(**base), _svc())


def _owner(subject):
    return build_human_identity(
        subject=subject, role_claims=["data_owner"], groups=["core.transactions"]
    )


def test_single_quorum_answer_completes_and_cancels_timers(db):
    task_id = _open(db)
    res = submit_human_signal(
        db, task_id, response="confirm", actor=_owner("user:do1"),
        expected_task_version=1,
    )
    assert res.status == "answered"
    assert res.counted is True
    assert res.quorum_met is True
    sched = db.execute(
        "SELECT count(*) FROM timers WHERE task_id=%s AND status='scheduled'", (task_id,)
    ).fetchone()[0]
    assert sched == 0


def test_duplicate_subject_is_idempotent(db):
    task_id = _open(db, quorum_required=2)
    submit_human_signal(db, task_id, response="confirm", actor=_owner("user:do1"),
                        expected_task_version=1)
    again = submit_human_signal(db, task_id, response="confirm", actor=_owner("user:do1"),
                                expected_task_version=1)
    assert again.counted is False
    n = db.execute(
        "SELECT count(*) FROM human_task_responses WHERE task_id=%s", (task_id,)
    ).fetchone()[0]
    assert n == 1


def test_distinct_quorum_of_two_completes(db):
    task_id = _open(db, quorum_required=2)
    r1 = submit_human_signal(db, task_id, response="confirm", actor=_owner("user:do1"),
                             expected_task_version=1)
    assert r1.quorum_met is False
    r2 = submit_human_signal(db, task_id, response="confirm", actor=_owner("user:do2"),
                             expected_task_version=1)
    assert r2.quorum_met is True
    assert r2.status == "answered"


def test_conflicting_quorum_escalates(db):
    task_id = _open(db, quorum_required=2)
    submit_human_signal(db, task_id, response="confirm", actor=_owner("user:do1"),
                        expected_task_version=1)
    res = submit_human_signal(db, task_id, response="reject", actor=_owner("user:do2"),
                              expected_task_version=1)
    assert res.status == "conflict"
    assert res.quorum_met is False
    escal = db.execute(
        "SELECT count(*) FROM timers WHERE task_id=%s AND kind='escalation' "
        "AND idempotency_key LIKE '%%conflict-escalation'",
        (task_id,),
    ).fetchone()[0]
    assert escal == 1
    # the original SLA ladder was cancelled; only the conflict-escalation remains scheduled
    scheduled = db.execute(
        "SELECT count(*) FROM timers WHERE task_id=%s AND status='scheduled'", (task_id,)
    ).fetchone()[0]
    assert scheduled == 1


def test_stale_answer_rejected_on_version_change_not_run_advance(db):
    task_id = _open(db)
    bump_task_version(db, task_id)             # required_inputs changed -> task_version=2
    res = submit_human_signal(db, task_id, response="confirm", actor=_owner("user:do1"),
                              expected_task_version=1)
    assert res.counted is False
    n = db.execute(
        "SELECT count(*) FROM human_task_responses WHERE task_id=%s", (task_id,)
    ).fetchone()[0]
    assert n == 0


def test_ineligible_role_rejected(db):
    task_id = _open(db)
    wrong = build_human_identity(subject="user:x", role_claims=["data_scientist"],
                                 groups=["core.transactions"])
    with pytest.raises(IneligibleResponderError):
        submit_human_signal(db, task_id, response="confirm", actor=wrong,
                            expected_task_version=1)


def test_wrong_scope_rejected(db):
    task_id = _open(db)
    wrong_scope = build_human_identity(subject="user:y", role_claims=["data_owner"],
                                       groups=["other.table"])
    with pytest.raises(IneligibleResponderError):
        submit_human_signal(db, task_id, response="confirm", actor=wrong_scope,
                            expected_task_version=1)


def test_response_not_allowed_rejected(db):
    task_id = _open(db)
    with pytest.raises(ResponseNotAllowedError):
        submit_human_signal(db, task_id, response="maybe", actor=_owner("user:do1"),
                            expected_task_version=1)


def test_late_answer_on_cancelled_task_refused(db):
    from featuregen.gates.tasks import cancel_task

    task_id = _open(db)
    cancel_task(db, task_id, reason="run advanced")
    res = submit_human_signal(db, task_id, response="confirm", actor=_owner("user:do1"),
                              expected_task_version=1)
    assert res.counted is False
    assert res.status == "cancelled"


def test_direct_submit_enforces_sod(db):
    # A DIRECT submit_human_signal call (not via execute_command) must still enforce SoD,
    # per the shared-contract docstring. Author of run_1 cannot validate their own run.
    from psycopg.types.json import Json

    db.execute(
        """
        INSERT INTO events (event_id, aggregate, aggregate_id, stream_version, run_id,
                            type, schema_version, table_version, actor, payload, provenance,
                            occurred_at)
        VALUES ('evt_run_1','run','run_1',1,'run_1','RUN_CREATED',1,1,%s,
                '{}'::jsonb,'{}'::jsonb, now())
        """,
        (Json({"subject": "user:author"}),),
    )
    task_id = _open(
        db, gate="INDEPENDENT_VALIDATION",
        eligible_assignees={"role": "validator"},
        allowed_responses=("validate", "reject"),
        required_inputs=("feature_plan_ref",),
    )
    author_as_validator = build_human_identity(
        subject="user:author", role_claims=["validator"]
    )
    with pytest.raises(SoDViolationError):
        submit_human_signal(db, task_id, response="validate",
                            actor=author_as_validator, expected_task_version=1)


def test_quorum_of_role_enforced_distinct_from_eligible_role(db):
    # eligible_assignees.role ("reviewer") is broader than quorum_of_role ("data_owner"):
    # an eligible reviewer who lacks the quorum role does NOT count and is refused; only
    # responders holding the quorum role advance the quorum.
    task_id = _open(
        db, gate="DATA_STEWARD",
        eligible_assignees={"role": "reviewer"},
        quorum_of_role="data_owner",
        quorum_required=2,
        allowed_responses=("confirm", "reject"),
    )
    only_reviewer = build_human_identity(subject="user:r0", role_claims=["reviewer"])
    with pytest.raises(IneligibleResponderError):
        submit_human_signal(db, task_id, response="confirm", actor=only_reviewer,
                            expected_task_version=1)
    a = build_human_identity(subject="user:a", role_claims=["reviewer", "data_owner"])
    b = build_human_identity(subject="user:b", role_claims=["reviewer", "data_owner"])
    r1 = submit_human_signal(db, task_id, response="confirm", actor=a,
                             expected_task_version=1)
    assert r1.quorum_met is False
    r2 = submit_human_signal(db, task_id, response="confirm", actor=b,
                             expected_task_version=1)
    assert r2.quorum_met is True
    assert r2.status == "answered"


def test_delegation_requires_grant_and_validates_principal(db):
    task_id = _open(db)        # DATA_STEWARD: eligible role=data_owner, scope=core.transactions
    delegate = build_human_identity(subject="user:assistant", role_claims=["intern"])

    # 1) no grant -> a delegated answer is refused
    with pytest.raises(IneligibleResponderError):
        submit_human_signal(db, task_id, response="confirm", actor=delegate,
                            expected_task_version=1, on_behalf_of="user:owner")

    # 2) granting for an INELIGIBLE principal is refused (principal eligibility verified here)
    ineligible = build_human_identity(
        subject="user:nobody", role_claims=["intern"], groups=["core.transactions"]
    )
    principal = build_human_identity(
        subject="user:owner", role_claims=["data_owner"], groups=["core.transactions"]
    )
    with pytest.raises(IneligibleResponderError):
        grant_task_delegation(db, task_id, principal=ineligible,
                              delegate_subject="user:assistant", granted_by=principal)

    # 3) valid grant -> the delegate may answer on the principal's behalf; the answer is
    #    attributed to the principal's authority (subject=delegate, on_behalf_of=principal)
    grant_task_delegation(db, task_id, principal=principal,
                          delegate_subject="user:assistant", granted_by=principal)
    res = submit_human_signal(db, task_id, response="confirm", actor=delegate,
                              expected_task_version=1, on_behalf_of="user:owner")
    assert res.counted is True
    assert res.status == "answered"
    assert res.quorum_met is True
    stored = db.execute(
        "SELECT subject, on_behalf_of FROM human_task_responses WHERE task_id=%s", (task_id,)
    ).fetchone()
    assert stored == ("user:assistant", "user:owner")
