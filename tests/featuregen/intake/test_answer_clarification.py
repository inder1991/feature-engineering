from psycopg.rows import dict_row
from tests.featuregen._helpers import mint_test_identity

from featuregen.contracts import Command
from featuregen.intake.commands import (
    answer_clarification,
    open_clarification_task,
    register_intake_deps,
)
from featuregen.intake.redaction import DefaultIntentRedactor
from featuregen.intake.store import append_feature_contract_event as append_fc_event
from featuregen.intake.store import load_feature_contract
from featuregen.security.audit import verify_chain

OWNER = mint_test_identity(subject="user:raj", role_claims=("data_scientist",))
MALLORY = mint_test_identity(subject="user:mallory", role_claims=("data_scientist",))


class _View:
    def candidate_count(self, concept):
        return 1

    def metadata(self):
        return {}


class _LoopLLM:
    """LLMClient double: a schema-valid `renormalize` (resolves the answered open field) + an OK
    `contract_review`, so a real refinement round runs to CONTRACT_REFINED. (The brief's `_NoopLLM`
    returned only a CONTRACT_REVIEW body, which fails the renormalize output-schema — Task 5.5's
    refine_contract requires feature_semantics from the renormalize call.)"""

    def call(self, request):
        from featuregen.intake.llm import LLMResult

        if request.task == "renormalize":
            return LLMResult(
                output={
                    "feature_semantics": {
                        "entity": "customer",
                        "entity_grain": ["customer_id", "as_of_date"],
                        "observation_intent": {"kind": "point_in_time"},
                        "calculation_method": "rolling_count",
                        "windows": [],
                        "filters": [{"concept": "declined auth", "predicate": "auth_result='D'"}],
                    },
                    "open_fields": [],
                },
                self_reported_scores={"filters": {"ambiguity": 0.10, "confidence": 0.92, "source": "llm"}},
                call_ref="", status="ok",
            )
        return LLMResult(output={"review_type": "CONTRACT_REVIEW", "status": "OK", "findings": []},
                         self_reported_scores={}, call_ref="", status="ok")


def _seed_with_task(db, agent):
    from featuregen.intake.commands import freeze_draft
    run_id = "run_ans"
    # R4: INTENT_SUBMITTED is appended by the HUMAN requester (OWNER), so the P2 fold sets
    # state.requester == "user:raj" — the value the request-owner guard checks. (The service `agent`
    # still produces the downstream Draft/task events.)
    append_fc_event(db, run_id=run_id, type="INTENT_SUBMITTED",
                    payload={"request_id": "req_ans", "run_id": run_id, "intake_mode": "definition",
                             "raw_input_ref": "blob_x", "raw_input_classification": "clean",
                             "classification": {"outcome": "CLEAR", "catalog_version": "bdc-1"}},
                    actor=OWNER, expected_version=0)
    body = {"request_id": "req_ans", "intake_mode": "definition", "raw_input_ref": "blob_x",
            "raw_input_classification": "clean", "proposed_feature_name": "f",
            "feature_semantics": {"entity": "customer", "entity_grain": ["customer_id", "as_of_date"],
                                  "observation_intent": {"kind": "point_in_time"},
                                  "calculation_method": "rolling_count", "windows": [], "filters": []},
            "field_scores": {}, "open_fields": ["filters.declined_status_encoding"],
            "assumption_ledger_ref": "", "provenance": {"schema_version": 1}, "status": "NEEDS_CLARIFICATION"}
    ledger = {"request_id": "req_ans", "assumptions": []}
    draft_doc_id, ledger_doc_id = freeze_draft(db, run_id=run_id, request_id="req_ans", body=body,
                                               ledger_body=ledger, actor=agent)
    # refine_contract (Task 5.5) READS the current draft/ledger body from the INLINED event stream
    # (mcv._latest_body), exactly as the real producer (_produce_draft) inlines them — so the seed must
    # inline them on DRAFT_CONTRACT_PRODUCED too (never a by-doc-id body map).
    append_fc_event(db, run_id=run_id, type="DRAFT_CONTRACT_PRODUCED",
                    payload={"draft_doc_id": draft_doc_id, "assumption_ledger_ref": ledger_doc_id,
                             "open_fields": ["filters.declined_status_encoding"],
                             "draft_body": {**body, "assumption_ledger_ref": ledger_doc_id},
                             "assumption_ledger_body": ledger}, actor=agent)
    task_id = open_clarification_task(db, run_id=run_id, request_id="req_ans", draft_doc_id=draft_doc_id,
                                      field="filters", question="Which column?", owner_subject="user:raj",
                                      actor=agent)
    return run_id, task_id


def _answer_cmd(task_id, actor, *, response="confirm", version=1, answer="auth_result='D'"):
    return Command(action="answer_clarification", aggregate="feature_contract", aggregate_id=None,
                   args={"task_id": task_id, "response": response, "expected_task_version": version,
                         "answer": answer}, actor=actor, idempotency_key=f"ans:{task_id}:{actor.subject}")


def test_a_different_data_scientist_is_denied_and_security_audited(db, sp2_schemas, agent):
    run_id, task_id = _seed_with_task(db, agent)
    res = answer_clarification(db, _answer_cmd(task_id, MALLORY))
    assert res.accepted is False
    assert "request owner" in res.denied_reason
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT count(*) AS n FROM security_audit WHERE decision='denied' "
            "AND attempted_action='answer_clarification'"
        )
        assert cur.fetchone()["n"] == 1
    assert verify_chain(db) is True  # the tamper-evident chain stays intact
    # the task was NOT answered
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT status FROM human_tasks WHERE task_id=%s", (task_id,))
        assert cur.fetchone()["status"] == "open"


def test_owner_answer_is_counted_and_shadowed(db, sp2_schemas, agent):
    run_id, task_id = _seed_with_task(db, agent)
    res = answer_clarification(db, _answer_cmd(task_id, OWNER))
    assert res.accepted is True, res.denied_reason
    types = [e.type for e in load_feature_contract(db, run_id)]
    assert "CLARIFICATION_ANSWERED" in types
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT status FROM human_tasks WHERE task_id=%s", (task_id,))
        assert cur.fetchone()["status"] == "answered"


def test_non_human_actor_is_denied(db, sp2_schemas, agent):
    # R4 the request-owner guard is BOTH `actor_kind=="human"` AND subject==owner: the service agent
    # (a non-human actor) may never answer a human clarification, even were it somehow the requester.
    run_id, task_id = _seed_with_task(db, agent)
    res = answer_clarification(db, _answer_cmd(task_id, agent))
    assert res.accepted is False
    assert "request owner" in res.denied_reason
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT status FROM human_tasks WHERE task_id=%s", (task_id,))
        assert cur.fetchone()["status"] == "open"


def test_stale_task_version_is_not_counted(db, sp2_schemas, agent):
    run_id, task_id = _seed_with_task(db, agent)
    res = answer_clarification(db, _answer_cmd(task_id, OWNER, version=99))  # wrong task_version
    assert res.accepted is False
    assert "not counted" in res.denied_reason


def test_owner_answer_drives_the_refinement_loop_when_deps_registered(db, sp2_schemas, agent):
    run_id, task_id = _seed_with_task(db, agent)
    register_intake_deps(client=_LoopLLM(), redactor=DefaultIntentRedactor(), catalog=_View())
    try:
        answer_clarification(db, _answer_cmd(task_id, OWNER))
    finally:
        register_intake_deps(client=None, redactor=None, catalog=None)
    types = [e.type for e in load_feature_contract(db, run_id)]
    assert "CONTRACT_REFINED" in types  # the loop ran a round on the answer


class _ValidatingLoopLLM:
    """Like _LoopLLM but the renormalize output is fully MCV-complete (a window is present), so the
    answer's refinement round re-passes the MCV floor → RefineResult 'validated'."""

    def call(self, request):
        from featuregen.intake.llm import LLMResult

        if request.task == "renormalize":
            return LLMResult(
                output={"feature_semantics": {
                    "entity": "customer", "entity_grain": ["customer_id", "as_of_date"],
                    "observation_intent": {"kind": "point_in_time"},
                    "calculation_method": "rolling_count",
                    "windows": [{"name": "lookback", "value": "90d"}],
                    "filters": [{"concept": "declined auth", "predicate": "auth_result='D'"}]},
                    "open_fields": []},
                self_reported_scores={"filters": {"ambiguity": 0.10, "confidence": 0.92, "source": "llm"}},
                call_ref="", status="ok")
        return LLMResult(output={"review_type": "CONTRACT_REVIEW", "status": "OK", "findings": []},
                         self_reported_scores={}, call_ref="", status="ok")


def test_owner_answer_self_advances_to_gate1_when_validated(db, sp2_schemas, agent):
    """F7/P3: when the answer completes the contract (refine re-passes the MCV floor), answer_clarification
    opens Gate #1 ITSELF — the run reaches the gate with NO separate advance_intake dispatch."""
    run_id, task_id = _seed_with_task(db, agent)
    register_intake_deps(client=_ValidatingLoopLLM(), redactor=DefaultIntentRedactor(), catalog=_View())
    try:
        res = answer_clarification(db, _answer_cmd(task_id, OWNER))
    finally:
        register_intake_deps(client=None, redactor=None, catalog=None)
    assert res.accepted is True
    assert "MINIMUM_CONTRACT_VALIDATED" in [e.type for e in load_feature_contract(db, run_id)]
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT allowed_responses FROM human_tasks WHERE run_id=%s AND status='open'", (run_id,))
        rows = cur.fetchall()
    assert any(set(r["allowed_responses"]) == {"confirm", "edit", "reject"} for r in rows), \
        "Gate #1 must self-open on a validating answer (F7)"
