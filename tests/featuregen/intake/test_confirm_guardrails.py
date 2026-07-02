from psycopg.rows import dict_row
from tests.featuregen.intake.conftest import (
    INTAKE_SVC,
    OTHER_DS,
    REQUESTER,
    definition_draft,
    seed_validated_contract,
)

from featuregen.contracts import Command
from featuregen.events.store import load_stream
from featuregen.identity.build import build_service_identity
from featuregen.intake.commands import confirm_contract, open_gate1_task
from featuregen.intake.state import FeatureContractStatus, fold_feature_contract_state
from featuregen.security.audit import verify_chain


def _ready(db, run_id):
    seed_validated_contract(db, run_id=run_id, request_id="req_" + run_id, draft_body=definition_draft("req_" + run_id))
    open_gate1_task(db, Command("open_gate1_task", "feature_contract", run_id, {"run_id": run_id}, INTAKE_SVC, "o"))
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT task_id, task_version FROM human_tasks WHERE run_id=%s AND status='open'", (run_id,)
        )
        row = cur.fetchone()
    return row["task_id"], row["task_version"]


def _cmd(run_id, task_id, tv, actor):
    return Command(
        "confirm_contract", "feature_contract", run_id,
        {"run_id": run_id, "task_id": task_id, "expected_task_version": tv}, actor, "cc",
    )


def _security_rows(db):
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT attempted_action, decision, reason FROM security_audit "
            "WHERE attempted_action='confirm_contract' ORDER BY seq"
        )
        return cur.fetchall()


def test_different_data_scientist_is_denied_and_audited(db):
    task_id, tv = _ready(db, "run_dsx")
    res = confirm_contract(db, _cmd("run_dsx", task_id, tv, OTHER_DS))  # user:mia != owner user:raj
    assert res.accepted is False
    assert "requester" in res.denied_reason
    rows = _security_rows(db)
    assert rows and rows[-1]["decision"] == "denied"
    assert verify_chain(db) is True  # tamper-evident chain intact
    # the gate task was NOT consumed by the spoofed confirm
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT status FROM human_tasks WHERE task_id=%s", (task_id,))
        assert cur.fetchone()["status"] == "open"
    assert fold_feature_contract_state(load_stream(db, "feature_contract", "run_dsx")).status \
        is FeatureContractStatus.MINIMUM_CONTRACT_VALIDATED  # never advanced


def test_service_principal_cannot_confirm(db):
    task_id, tv = _ready(db, "run_svc")
    svc = build_service_identity(subject="service:intake-agent", role_claims=("intake-agent",), attestation="s")
    res = confirm_contract(db, _cmd("run_svc", task_id, tv, svc))  # actor_kind != human
    assert res.accepted is False
    assert "requester" in res.denied_reason
    assert _security_rows(db)[-1]["decision"] == "denied"


def test_no_regression_double_confirm_is_denied(db):
    task_id, tv = _ready(db, "run_dbl")
    assert confirm_contract(db, _cmd("run_dbl", task_id, tv, REQUESTER)).accepted is True
    again = confirm_contract(db, _cmd("run_dbl", task_id, tv, REQUESTER))
    assert again.accepted is False
    assert "no-regression" in again.denied_reason  # already CONFIRMED


def test_stale_task_version_is_rejected_by_occ(db):
    task_id, tv = _ready(db, "run_occ")
    res = confirm_contract(db, _cmd("run_occ", task_id, tv + 5, REQUESTER))  # stale task_version
    assert res.accepted is False
    assert "OCC" in res.denied_reason or "stale" in res.denied_reason
    assert fold_feature_contract_state(load_stream(db, "feature_contract", "run_occ")).status \
        is FeatureContractStatus.MINIMUM_CONTRACT_VALIDATED  # not confirmed
