from psycopg.rows import dict_row
from tests.featuregen.intake.conftest import (
    INTAKE_SVC,
    REQUESTER,
    definition_draft,
    seed_needs_clarification,
    seed_validated_contract,
)

from featuregen.contracts import Command
from featuregen.contracts.gates import GateTaskSpec
from featuregen.gates.tasks import open_task
from featuregen.intake.commands import open_gate1_task


def _open_cmd(run_id):
    return Command("open_gate1_task", "feature_contract", run_id, {"run_id": run_id}, INTAKE_SVC, "og1")


def test_open_gate1_opens_dedicated_confirm_task_for_owner(db):
    draft = definition_draft("req_a")
    draft_doc_id, _ = seed_validated_contract(db, run_id="run_a", request_id="req_a", draft_body=draft)
    res = open_gate1_task(db, _open_cmd("run_a"))
    assert res.accepted is True, res.denied_reason
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT gate, allowed_responses, delegation_allowed, required_inputs, "
            "eligible_assignees, quorum_required, status "
            "FROM human_tasks WHERE run_id=%s AND status='open'",
            ("run_a",),
        )
        row = cur.fetchone()
    assert row["gate"] == "CLARIFICATION"
    assert set(row["allowed_responses"]) == {"confirm", "edit", "reject"}
    assert row["delegation_allowed"] is False  # author-owned intent lock (§8.2)
    assert row["required_inputs"] == [draft_doc_id]  # a re-normalization stales the task (§12)
    assert row["eligible_assignees"] == {"role": "data_scientist", "subject": "user:raj"}
    assert row["quorum_required"] == 1


def test_open_gate1_denied_before_mcv(db):
    """Gate #1 can NEVER open on an under-specified contract (§6.7): folded status must be
    MINIMUM_CONTRACT_VALIDATED."""
    seed_needs_clarification(db, run_id="run_b", request_id="req_b", draft_body=definition_draft("req_b"))
    res = open_gate1_task(db, _open_cmd("run_b"))
    assert res.accepted is False
    assert "MINIMUM_CONTRACT_VALIDATED" in res.denied_reason
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT count(*) AS n FROM human_tasks WHERE run_id=%s", ("run_b",))
        assert cur.fetchone()["n"] == 0


def test_open_gate1_cancels_pending_clarification_tasks(db):
    """Opening the gate is the defensive close of any still-pending per-field clarification tasks
    (§8.6) — none can be answered behind an open gate."""
    seed_validated_contract(db, run_id="run_c", request_id="req_c", draft_body=definition_draft("req_c"))
    stray = open_task(
        db,
        GateTaskSpec(
            gate="CLARIFICATION",
            required_inputs=("filters.declined_status_encoding",),
            eligible_assignees={"role": "data_scientist", "subject": "user:raj"},
            allowed_responses=("answer",),
            run_id="run_c",
            delegation_allowed=False,
        ),
        REQUESTER,
    )
    res = open_gate1_task(db, _open_cmd("run_c"))
    assert res.accepted is True, res.denied_reason
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT status FROM human_tasks WHERE task_id=%s", (stray,))
        assert cur.fetchone()["status"] == "cancelled"
        cur.execute(
            "SELECT count(*) AS n FROM human_tasks WHERE run_id=%s AND status='open'", ("run_c",)
        )
        assert cur.fetchone()["n"] == 1  # only the fresh Gate #1 task remains open
