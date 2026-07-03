from __future__ import annotations

import psycopg
import pytest

from featuregen.aggregates._append import append
from featuregen.contracts.gates import GateTaskSpec
from featuregen.db.migrations import apply_migrations
from featuregen.gates.tasks import open_task
from featuregen.identity.build import build_service_identity
from featuregen.intake.events import NEEDS_USE_CASE_ONBOARDING, USE_CASE_ONBOARDING_GATE

_RUN = "run_onb01"


def _svc(subject="service:workflow", role="workflow"):
    return build_service_identity(
        subject=subject, role_claims=[role],
        attestation="signed-deploy-id:workflow@1.0.0",
    )


def _onboarding_spec(**kw):
    base = dict(
        gate=USE_CASE_ONBOARDING_GATE,
        required_inputs=("draft_ref",),
        eligible_assignees={"role": "governance", "scope": "use-case-onboarding"},
        allowed_responses=("onboard", "reject"),
        run_id=_RUN,
        delegation_allowed=False,
    )
    base.update(kw)
    return GateTaskSpec(**base)


def test_use_case_onboarding_gate_task_opens(db):
    task_id = open_task(db, _onboarding_spec(), _svc())
    row = db.execute(
        "SELECT gate, run_id, delegation_allowed FROM human_tasks WHERE task_id=%s",
        (task_id,),
    ).fetchone()
    assert row == ("USE_CASE_ONBOARDING", _RUN, False)


def test_base_and_overlay_gates_still_accepted(db):
    for gate in ("CLARIFICATION", "FINAL_APPROVAL", "OVERLAY_DATA_OWNER", "OVERLAY_COMPLIANCE"):
        task_id = open_task(db, _onboarding_spec(gate=gate), _svc())
        got = db.execute(
            "SELECT gate FROM human_tasks WHERE task_id=%s", (task_id,)
        ).fetchone()[0]
        assert got == gate


def test_gate_check_rejects_unknown_gate(db):
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "INSERT INTO human_tasks (task_id, gate, eligible_assignees, allowed_responses) "
            "VALUES ('task_bad','NOT_A_GATE','{}'::jsonb, ARRAY['x'])"
        )


def test_onboarding_park_does_not_overload_waiting_on_fact(conn):
    # X6: the NEEDS_USE_CASE_ONBOARDING hold is NEVER stored in RUN_PARKED.waiting_on_fact — that
    # field is SP-1's fact-confirmed-resume key (run_lifecycle.py:112), so a later
    # fact_confirmed_resume(fact_key="NEEDS_USE_CASE_ONBOARDING") could WRONGLY unpark the hold. The
    # hold is instead the feature_contract folded status NEEDS_USE_CASE_ONBOARDING (carried by the
    # USE_CASE_ONBOARDING_REQUESTED event, emitted by P4) + the USE_CASE_ONBOARDING gate task opened
    # above. A run parked for onboarding therefore carries waiting_on_fact=None.
    env = append(
        conn,
        aggregate="run",
        aggregate_id=_RUN,
        run_id=_RUN,
        type="RUN_PARKED",
        payload={"run_id": _RUN, "owner": "governance", "waiting_on_fact": None},
        actor=_svc(),
    )
    got = conn.execute(
        "SELECT payload->>'waiting_on_fact' FROM events WHERE event_id=%s", (env.event_id,)
    ).fetchone()[0]
    assert got is None  # the onboarding hold is never overloaded onto waiting_on_fact
    # NEEDS_USE_CASE_ONBOARDING stays a domain constant (the FC folded status), not a park-reason key.
    assert NEEDS_USE_CASE_ONBOARDING == "NEEDS_USE_CASE_ONBOARDING"


def test_use_case_onboarding_migration_is_idempotent(conn):
    apply_migrations(conn)
    apply_migrations(conn)
    chk = conn.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='human_tasks_gate_check'"
    ).fetchone()[0]
    assert "USE_CASE_ONBOARDING" in chk
    assert "OVERLAY_DATA_OWNER" in chk  # regression: SP-1's overlay gates survive the rebuild
    idx = conn.execute(
        "SELECT 1 FROM pg_indexes WHERE indexname='human_tasks_use_case_onboarding_idx'"
    ).fetchone()
    assert idx is not None
