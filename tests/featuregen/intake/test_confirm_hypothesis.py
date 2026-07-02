from psycopg.rows import dict_row
from tests.featuregen.intake.conftest import (
    INTAKE_SVC,
    REQUESTER,
    definition_draft,
    seed_validated_contract,
)

from featuregen.contracts import Command, run_projection
from featuregen.documents.primary import (
    StagePrimaryProjection,
    current_primary,
    register_primary_selected,
)
from featuregen.events.store import load_stream
from featuregen.intake.commands import confirm_contract, open_gate1_task
from featuregen.intake.state import FeatureContractStatus, fold_feature_contract_state


def _hypothesis_draft(request_id):
    d = definition_draft(request_id, intake_mode="hypothesis", risk_flags=["high_risk_use_case:credit_decisioning"])
    d["proposed_feature_name"] = "abrupt_category_shift"
    d["feature_semantics"]["target_definition"] = "higher credit risk (pinned label)"
    return d


def _ready(db, run_id):
    register_primary_selected(db)  # SP-2 bootstrap (seed_sp2) wires this durably in production
    draft_doc_id, cands = seed_validated_contract(
        db, run_id=run_id, request_id="req_" + run_id, draft_body=_hypothesis_draft("req_" + run_id), candidate_docs=3
    )
    open_gate1_task(db, Command("open_gate1_task", "feature_contract", run_id, {"run_id": run_id}, INTAKE_SVC, "o"))
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT task_id, task_version FROM human_tasks WHERE run_id=%s AND status='open'", (run_id,))
        row = cur.fetchone()
    return row["task_id"], row["task_version"], cands


def _cmd(run_id, task_id, tv, **args):
    return Command(
        "confirm_contract", "feature_contract", run_id,
        {"run_id": run_id, "task_id": task_id, "expected_task_version": tv, **args}, REQUESTER, "cc",
    )


def test_hypothesis_requires_candidate_selection(db):
    task_id, tv, _ = _ready(db, "run_hyp0")
    res = confirm_contract(db, _cmd("run_hyp0", task_id, tv))  # no candidate_doc_id
    assert res.accepted is False
    assert "candidate" in res.denied_reason.lower()  # calculation_method_chosen not satisfied
    assert fold_feature_contract_state(load_stream(db, "feature_contract", "run_hyp0")).status \
        is FeatureContractStatus.MINIMUM_CONTRACT_VALIDATED


def test_hypothesis_confirm_promotes_candidate_and_records_rejected(db):
    task_id, tv, cands = _ready(db, "run_hyp1")
    chosen = cands[1]
    res = confirm_contract(db, _cmd("run_hyp1", task_id, tv, candidate_doc_id=chosen))
    assert res.accepted is True, res.denied_reason
    # document PRIMARY_SELECTED promotion on the run — only the chosen doc is promoted (§7.1).
    # Drive the (async, checkpoint-driven) stage_primary projection synchronously to read it back.
    run_projection(db, StagePrimaryProjection())
    assert current_primary(db, "run_hyp1", "DRAFT_CONTRACT") == chosen
    stream = load_stream(db, "run", "run_hyp1")
    promos = [e for e in stream if e.type == "PRIMARY_SELECTED"]
    assert len(promos) == 1 and promos[0].payload == {"doc_id": chosen, "stage": "DRAFT_CONTRACT"}
    body = next(e for e in load_stream(db, "feature_contract", "run_hyp1") if e.type == "CONTRACT_CONFIRMED").payload["confirmed_body"]
    conf = body["confirmation"]
    assert conf["selected_candidate"] == chosen
    assert set(conf["rejected_candidates"]) == {cands[0], cands[2]}  # losers recorded HERE only (§8.3)
    assert body["requires_independent_validation"] is True  # risk-flagged use-case (§8.4 #1)
    assert body["intake_mode"] == "hypothesis"
    assert fold_feature_contract_state(load_stream(db, "feature_contract", "run_hyp1")).status \
        is FeatureContractStatus.CONFIRMED
