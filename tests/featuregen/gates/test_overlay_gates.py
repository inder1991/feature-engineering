from __future__ import annotations

import psycopg
import pytest

from featuregen.contracts.gates import GateTaskSpec
from featuregen.db.migrations import apply_migrations
from featuregen.gates.tasks import _task_aggregate, open_task
from featuregen.identity.build import build_service_identity


def _svc():
    return build_service_identity(
        subject="service:overlay",
        role_claims=["overlay"],
        attestation="signed-deploy-id:overlay@1.0.0",
    )


def _overlay_spec(**kw):
    base = dict(
        gate="OVERLAY_DATA_OWNER",
        required_inputs=("proposed_value",),
        eligible_assignees={"role": "data_owner", "scope": "core.transactions"},
        allowed_responses=("confirm", "reject"),
        fact_key="a1b2c3",
        draft_event_id="evt_draft",
        target_event_id="evt_draft",
        evidence_ref="eviu_1",
    )
    base.update(kw)
    return GateTaskSpec(**base)


def test_task_aggregate_fact_key_arm():
    assert _task_aggregate(None, None, "fk1") == ("overlay_fact", "fk1")
    assert _task_aggregate("run_1", None) == ("run", "run_1")
    assert _task_aggregate(None, "feat_1") == ("feature", "feat_1")


def test_open_task_with_fact_key_inserts_overlay_columns(db):
    task_id = open_task(db, _overlay_spec(), _svc())
    row = db.execute(
        "SELECT gate, fact_key, draft_event_id, target_event_id, evidence_ref, run_id, feature_id "
        "FROM human_tasks WHERE task_id=%s",
        (task_id,),
    ).fetchone()
    assert row == ("OVERLAY_DATA_OWNER", "a1b2c3", "evt_draft", "evt_draft", "eviu_1", None, None)


def test_gate_check_accepts_overlay_compliance(db):
    task_id = open_task(db, _overlay_spec(gate="OVERLAY_COMPLIANCE"), _svc())
    gate = db.execute(
        "SELECT gate FROM human_tasks WHERE task_id=%s", (task_id,)
    ).fetchone()[0]
    assert gate == "OVERLAY_COMPLIANCE"


def test_gate_check_rejects_unknown_gate(db):
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "INSERT INTO human_tasks (task_id, gate, eligible_assignees, allowed_responses) "
            "VALUES ('task_bad','NOT_A_GATE','{}'::jsonb, ARRAY['x'])"
        )


def test_overlay_gates_migration_is_idempotent(conn):
    apply_migrations(conn)
    apply_migrations(conn)
    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='human_tasks'"
        ).fetchall()
    }
    assert {"fact_key", "draft_event_id", "target_event_id", "evidence_ref"} <= cols
