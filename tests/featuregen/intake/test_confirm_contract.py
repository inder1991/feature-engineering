from psycopg.rows import dict_row
from tests.featuregen.intake.conftest import (
    INTAKE_SVC,
    REQUESTER,
    definition_draft,
    seed_validated_contract,
)

from featuregen.contracts import Command
from featuregen.events.store import load_stream
from featuregen.intake.commands import confirm_contract, open_gate1_task
from featuregen.intake.state import FeatureContractStatus, fold_feature_contract_state


def _gate_task(db, run_id):
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT task_id, task_version FROM human_tasks "
            "WHERE run_id=%s AND gate='CLARIFICATION' AND status='open'",
            (run_id,),
        )
        row = cur.fetchone()
    return row["task_id"], row["task_version"]


def _confirm_cmd(run_id, task_id, tv, *, actor=REQUESTER, **args):
    return Command(
        "confirm_contract", "feature_contract", run_id,
        {"run_id": run_id, "task_id": task_id, "expected_task_version": tv, **args},
        actor, "cc",
    )


def test_confirm_definition_folds_to_confirmed_and_emits_document(db):
    draft = definition_draft("req_ok")
    draft_doc_id, _ = seed_validated_contract(db, run_id="run_ok", request_id="req_ok", draft_body=draft)
    open_gate1_task(db, Command("open_gate1_task", "feature_contract", "run_ok", {"run_id": "run_ok"}, INTAKE_SVC, "o"))
    task_id, tv = _gate_task(db, "run_ok")

    res = confirm_contract(db, _confirm_cmd("run_ok", task_id, tv))
    assert res.accepted is True, res.denied_reason

    stream = load_stream(db, "feature_contract", "run_ok")
    assert fold_feature_contract_state(stream).status is FeatureContractStatus.CONFIRMED
    confirmed = next(e for e in stream if e.type == "CONTRACT_CONFIRMED")
    body = confirmed.payload["confirmed_body"]
    assert body["status"] == "CONFIRMED"
    assert body["feature_name"] == "declined_card_auth_count_90d"
    assert body["requires_independent_validation"] is False  # definition example (§Appendix)
    conf = body["confirmation"]
    assert conf["confirmed_by"] == "user:raj"  # the authenticated requester, never a service/LLM
    assert conf["source_of_authority"] == "oidc:raj"
    assert conf["selected_candidate"] is None and conf["rejected_candidates"] == []

    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT stage, branch_role, derived_from FROM documents "
            "WHERE run_id=%s AND stage='CONFIRMED_CONTRACT'",
            ("run_ok",),
        )
        doc = cur.fetchone()
        assert doc["branch_role"] == "primary"
        assert doc["derived_from"] == [draft_doc_id]  # CONFIRMED derived_from the final Draft (§8.5)
        cur.execute("SELECT status FROM human_tasks WHERE task_id=%s", (task_id,))
        assert cur.fetchone()["status"] == "answered"  # Gate #1 task → answered (§8.6)


def test_confirm_records_human_edits_and_feature_name_override(db):
    seed_validated_contract(db, run_id="run_ed", request_id="req_ed", draft_body=definition_draft("req_ed"))
    open_gate1_task(db, Command("open_gate1_task", "feature_contract", "run_ed", {"run_id": "run_ed"}, INTAKE_SVC, "o"))
    task_id, tv = _gate_task(db, "run_ed")
    edits = [{"field": "proposed_feature_name", "from": "declined_card_auth_count_90d", "to": "declined_auth_ct_90d"}]
    res = confirm_contract(
        db, _confirm_cmd("run_ed", task_id, tv, feature_name="declined_auth_ct_90d", human_edits=edits,
                         ambiguity_notes="declined encoding confirmed by requester")
    )
    assert res.accepted is True, res.denied_reason
    body = next(e for e in load_stream(db, "feature_contract", "run_ed") if e.type == "CONTRACT_CONFIRMED").payload["confirmed_body"]
    assert body["feature_name"] == "declined_auth_ct_90d"
    assert body["confirmation"]["human_edits"] == edits
    assert body["confirmation"]["ambiguity_notes"] == "declined encoding confirmed by requester"
