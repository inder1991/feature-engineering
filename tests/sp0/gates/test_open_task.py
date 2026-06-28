from datetime import timedelta

import pytest

from sp0.contracts.gates import GateTaskSpec
from sp0.gates.duration import parse_duration
from sp0.gates.tasks import (
    GateError,
    bump_task_version,
    cancel_task,
    cancel_tasks_on_run_advance,
    open_task,
)
from sp0.identity.build import build_service_identity


def _spec(**kw):
    base = dict(
        gate="DATA_STEWARD",
        required_inputs=("confirmed_contract_ref",),
        eligible_assignees={"role": "data_owner", "scope": "core.transactions"},
        allowed_responses=("confirm", "edit", "reject"),
        run_id="run_1",
        sla="7d",
    )
    base.update(kw)
    return GateTaskSpec(**base)


def _svc():
    return build_service_identity(
        subject="service:intake-agent", role_claims=["workflow"],
        attestation="signed-deploy-id:sp2-intake@1.4.0",
    )


def test_parse_duration():
    assert parse_duration("7d") == timedelta(days=7)
    assert parse_duration("3h") == timedelta(hours=3)
    assert parse_duration("30m") == timedelta(minutes=30)
    with pytest.raises(ValueError):
        parse_duration("7y")


def test_open_task_persists_and_schedules_ladder(db):
    task_id = open_task(db, _spec(), _svc())
    assert task_id.startswith("task_")
    row = db.execute(
        "SELECT task_version, gate, status, run_id FROM human_tasks WHERE task_id=%s",
        (task_id,),
    ).fetchone()
    assert row == (1, "DATA_STEWARD", "open", "run_1")
    kinds = db.execute(
        "SELECT kind FROM timers WHERE task_id=%s ORDER BY fire_at", (task_id,)
    ).fetchall()
    assert {k[0] for k in kinds} == {"reminder", "sla", "escalation", "auto_park"}
    cas = db.execute(
        "SELECT DISTINCT cas_task_version FROM timers WHERE task_id=%s", (task_id,)
    ).fetchall()
    assert cas == [(1,)]


def test_bump_task_version(db):
    task_id = open_task(db, _spec(), _svc())
    assert bump_task_version(db, task_id) == 2
    with pytest.raises(GateError):
        bump_task_version(db, "task_missing")


def test_cancel_task_marks_status_and_cancels_timers(db):
    task_id = open_task(db, _spec(), _svc())
    cancel_task(db, task_id, reason="run advanced past gate")
    assert db.execute(
        "SELECT status FROM human_tasks WHERE task_id=%s", (task_id,)
    ).fetchone()[0] == "cancelled"
    remaining = db.execute(
        "SELECT count(*) FROM timers WHERE task_id=%s AND status='scheduled'", (task_id,)
    ).fetchone()[0]
    assert remaining == 0


def test_cancel_tasks_on_run_advance_cancels_all_open_for_run(db):
    # Simulates the Phase-06/03 run-advance hook calling into this phase: every open gate task
    # for the run (and its timers) is cancelled in one shot.
    t1 = open_task(db, _spec(gate="DATA_STEWARD", required_inputs=("a_ref",)), _svc())
    t2 = open_task(
        db,
        _spec(gate="CLARIFICATION", required_inputs=("b_ref",),
              allowed_responses=("answer",)),
        _svc(),
    )
    assert t1 != t2
    n = cancel_tasks_on_run_advance(db, "run_1", reason="run advanced to next stage")
    assert n == 2
    statuses = {
        s[0]
        for s in db.execute(
            "SELECT status FROM human_tasks WHERE run_id='run_1'"
        ).fetchall()
    }
    assert statuses == {"cancelled"}
    sched = db.execute(
        "SELECT count(*) FROM timers WHERE status='scheduled'"
    ).fetchone()[0]
    assert sched == 0
