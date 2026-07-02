from psycopg.rows import dict_row
from tests.featuregen.intake.conftest import (
    INTAKE_SVC,
    OTHER_DS,
    REQUESTER,
    definition_draft,
    seed_validated_contract,
)

from featuregen.contracts import Command
from featuregen.documents.draft import UNKNOWN
from featuregen.events.store import load_stream
from featuregen.intake.commands import open_gate1_task, request_edit
from featuregen.intake.state import FeatureContractStatus, fold_feature_contract_state


def _ready(db, run_id):
    draft_doc_id, _ = seed_validated_contract(
        db, run_id=run_id, request_id="req_" + run_id, draft_body=definition_draft("req_" + run_id)
    )
    open_gate1_task(db, Command("open_gate1_task", "feature_contract", run_id, {"run_id": run_id}, INTAKE_SVC, "o"))
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT task_id, task_version FROM human_tasks WHERE run_id=%s AND status='open'", (run_id,))
        row = cur.fetchone()
    return draft_doc_id, row["task_id"], row["task_version"]


def _edit_cmd(run_id, task_id, tv, field_edit, actor=REQUESTER):
    return Command(
        "request_edit", "feature_contract", run_id,
        {"run_id": run_id, "task_id": task_id, "expected_task_version": tv, "field_edit": field_edit},
        actor, "re",
    )


def test_edit_supersedes_draft_reruns_mcv_and_reopens_gate(db):
    draft_doc_id, task_id, tv = _ready(db, "run_ed1")
    edit = {"field": "proposed_feature_name", "from": "declined_card_auth_count_90d", "to": "declined_auth_ct_90d"}
    res = request_edit(db, _edit_cmd("run_ed1", task_id, tv, edit))
    assert res.accepted is True, res.denied_reason
    stream = load_stream(db, "feature_contract", "run_ed1")
    refined = next(e for e in stream if e.type == "CONTRACT_REFINED")
    assert refined.payload["draft_body"]["proposed_feature_name"] == "declined_auth_ct_90d"
    assert refined.payload["human_edits"] == [edit]
    # MCV re-ran → back to MINIMUM_CONTRACT_VALIDATED; a fresh gate task is open on the REVISED draft
    assert fold_feature_contract_state(stream).status is FeatureContractStatus.MINIMUM_CONTRACT_VALIDATED
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT status FROM human_tasks WHERE task_id=%s", (task_id,))
        assert cur.fetchone()["status"] == "answered"  # old gate task consumed by the edit (§8.6)
        cur.execute(
            "SELECT required_inputs FROM human_tasks WHERE run_id=%s AND status='open' AND gate='CLARIFICATION'",
            ("run_ed1",),
        )
        new_inputs = cur.fetchone()["required_inputs"]
        assert new_inputs != [draft_doc_id] and len(new_inputs) == 1  # keyed to the REVISED draft doc
        cur.execute(
            "SELECT count(*) AS n FROM documents WHERE run_id=%s AND stage='DRAFT_CONTRACT' AND %s = ANY(supersedes)",
            ("run_ed1", draft_doc_id),
        )
        assert cur.fetchone()["n"] == 1  # REVISED draft supersedes the prior on the DAG


def test_edit_reopening_a_field_drops_into_refinement_loop(db):
    _, task_id, tv = _ready(db, "run_ed2")
    edit = {"field": "proposed_feature_name", "from": "declined_card_auth_count_90d", "to": UNKNOWN}
    res = request_edit(db, _edit_cmd("run_ed2", task_id, tv, edit))
    assert res.accepted is True, res.denied_reason
    stream = load_stream(db, "feature_contract", "run_ed2")
    assert fold_feature_contract_state(stream).status is FeatureContractStatus.NEEDS_CLARIFICATION
    refined = next(e for e in stream if e.type == "CONTRACT_REFINED")
    assert "proposed_feature_name" in refined.payload["draft_body"]["open_fields"]
    # NO fresh gate task while a field is open (Refinement Loop owns re-clarification)
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT count(*) AS n FROM human_tasks WHERE run_id=%s AND status='open' AND gate='CLARIFICATION'",
            ("run_ed2",),
        )
        assert cur.fetchone()["n"] == 0


def test_edit_degrading_a_required_field_to_blank_does_not_reopen_gate(db):
    """MCV-floor bypass guard (§6.7): a non-UNKNOWN edit that DEGRADES a required field to a blank value
    (calculation_method -> "") must NOT re-open a confirmable Gate #1. Blanking a required field is
    invalidating (aligned with mcv._is_unknown — "", None, [], not only the exact sentinel), so the field
    re-opens and the run drops into the Refinement Loop (NEEDS_CLARIFICATION) instead of re-opening the
    gate on a contract that would FAIL the machine-checkable MCV floor. (Before the 7.6 fix this re-opened
    the gate: value == UNKNOWN was False, so the degraded field never entered open_fields and the stale
    MINIMUM_CONTRACT_VALIDATED short-circuit accepted an invalid contract.)"""
    _, task_id, tv = _ready(db, "run_ed5")
    edit = {"field": "feature_semantics.calculation_method", "from": "rolling_count", "to": ""}
    res = request_edit(db, _edit_cmd("run_ed5", task_id, tv, edit))
    assert res.accepted is True, res.denied_reason
    stream = load_stream(db, "feature_contract", "run_ed5")
    # NOT confirmable: the blanked required field re-opened → NEEDS_CLARIFICATION, never MCV-passed.
    assert fold_feature_contract_state(stream).status is FeatureContractStatus.NEEDS_CLARIFICATION
    refined = next(e for e in stream if e.type == "CONTRACT_REFINED")
    assert "feature_semantics.calculation_method" in refined.payload["draft_body"]["open_fields"]
    # NO fresh Gate #1 task re-opened on the invalid contract (Refinement Loop owns re-clarification).
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT count(*) AS n FROM human_tasks WHERE run_id=%s AND status='open' AND gate='CLARIFICATION'",
            ("run_ed5",),
        )
        assert cur.fetchone()["n"] == 0


def test_edit_by_non_owner_is_denied_and_audited(db):
    _, task_id, tv = _ready(db, "run_ed3")
    edit = {"field": "proposed_feature_name", "to": "x"}
    res = request_edit(db, _edit_cmd("run_ed3", task_id, tv, edit, actor=OTHER_DS))
    assert res.accepted is False
    assert "requester" in res.denied_reason
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT count(*) AS n FROM security_audit WHERE attempted_action='request_edit' AND decision='denied'"
        )
        assert cur.fetchone()["n"] == 1


def test_edit_with_stale_task_version_is_rejected(db):
    _, task_id, tv = _ready(db, "run_ed4")
    res = request_edit(db, _edit_cmd("run_ed4", task_id, tv + 9, {"field": "proposed_feature_name", "to": "x"}))
    assert res.accepted is False
    assert "stale" in res.denied_reason or "OCC" in res.denied_reason
