from psycopg.rows import dict_row

from featuregen.identity.build import build_human_identity
from featuregen.intake.commands import (
    RefineResult,
    freeze_draft,
    open_clarification_task,
    refine_contract,
)
from featuregen.intake.redaction import DefaultIntentRedactor
from featuregen.intake.store import append_feature_contract_event as append_fc_event
from featuregen.intake.store import load_feature_contract

# R4: the request owner is the INTENT_SUBMITTED event actor.subject (state.requester) — never a payload
# key. INTENT_SUBMITTED is issued by the HUMAN requester, so the P2 fold reads the owner from the event.
OWNER = build_human_identity(subject="user:raj", role_claims=("data_scientist",))


class ScriptedLLM:
    """LLMClient double: returns a canned structured output per task ("contract_review" / "renormalize")."""

    def __init__(self, by_task):
        self._by_task = by_task

    def call(self, request):
        from featuregen.intake.llm import LLMResult
        spec = self._by_task[request.task]
        return LLMResult(
            output=spec.get("output", {}),
            self_reported_scores=spec.get("self_reported_scores", {}),
            call_ref="", status="ok",
        )


class _View:
    def candidate_count(self, concept):
        return {"declined card authorization": 3}.get(concept, 1)

    def metadata(self):
        return {}


def _semantics(filter_predicate="UNKNOWN"):
    return {
        "entity": "customer",
        "entity_grain": ["customer_id", "as_of_date"],
        "observation_intent": {"kind": "point_in_time", "as_of_field": "as_of_date"},
        "calculation_method": "rolling_count",
        "windows": [{"name": "lookback", "value": "90d"}],
        "filters": [{"concept": "declined card authorization", "predicate": filter_predicate}],
    }


def _seed_draft(db, agent, *, run_id="run_ref", open_fields=("filters.declined_status_encoding",)):
    # R4: INTENT_SUBMITTED is appended by the HUMAN requester (OWNER) → the P2 fold sets state.requester
    # == "user:raj", the owner the Refinement Loop scopes clarification tasks to (never a payload key).
    append_fc_event(
        db, run_id=run_id, type="INTENT_SUBMITTED",
        payload={"request_id": "req_ref", "run_id": run_id, "intake_mode": "definition",
                 "raw_input_ref": "blob_x", "raw_input_classification": "clean",
                 "classification": {"outcome": "CLEAR", "catalog_version": "bdc-1"}},
        actor=OWNER, expected_version=0,
    )
    ledger = {"request_id": "req_ref", "assumptions": [
        {"field": "entity_grain", "value": ["customer_id", "as_of_date"], "rationale": "pit convention",
         "source": "default", "ambiguity": 0.30, "confidence": 0.72}]}
    body = {
        "request_id": "req_ref", "intake_mode": "definition", "raw_input_ref": "blob_x",
        "raw_input_classification": "clean", "proposed_feature_name": "declined_card_auth_count_90d",
        "feature_semantics": _semantics(),
        "field_scores": {
            "entity": {"ambiguity": 0.05, "confidence": 0.97, "source": "llm"},
            "entity_grain": {"ambiguity": 0.30, "confidence": 0.72, "source": "default"},
            "windows": {"ambiguity": 0.05, "confidence": 0.98, "source": "llm"},
            "filters": {"ambiguity": 0.80, "confidence": 0.40, "source": "llm"},
        },
        "open_fields": list(open_fields), "assumption_ledger_ref": "", "provenance": {"schema_version": 1},
        "status": "NEEDS_CLARIFICATION",
    }
    draft_doc_id, ledger_doc_id = freeze_draft(
        db, run_id=run_id, request_id="req_ref", body=body, ledger_body=ledger, actor=agent
    )
    append_fc_event(db, run_id=run_id, type="DRAFT_CONTRACT_PRODUCED",
                    payload={"draft_doc_id": draft_doc_id, "assumption_ledger_ref": ledger_doc_id,
                             "open_fields": list(open_fields)}, actor=agent)
    return run_id, draft_doc_id


def _no_review():
    return {"output": {"review_type": "CONTRACT_REVIEW", "status": "OK", "findings": []}}


def test_open_clarification_task_is_owner_scoped_and_delegation_off(db, sp2_schemas, agent):
    run_id, draft_doc_id = _seed_draft(db, agent)
    task_id = open_clarification_task(
        db, run_id=run_id, request_id="req_ref", draft_doc_id=draft_doc_id,
        field="filters", question="Which column marks a declined auth?", owner_subject="user:raj", actor=agent,
    )
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT gate, eligible_assignees, allowed_responses, delegation_allowed, required_inputs, run_id "
            "FROM human_tasks WHERE task_id=%s", (task_id,)
        )
        row = cur.fetchone()
    assert row["gate"] == "CLARIFICATION"
    assert row["eligible_assignees"] == {"role": "data_scientist", "subject": "user:raj"}
    assert sorted(row["allowed_responses"]) == ["confirm", "edit", "reject"]
    assert row["delegation_allowed"] is False        # author-owned intent lock (§6.5, §8.2)
    assert row["required_inputs"] == [draft_doc_id]   # a re-normalized draft stales the pending answer
    types = [e.type for e in load_feature_contract(db, run_id)]
    assert "CLARIFICATION_REQUESTED" in types


def test_initial_refine_opens_a_must_ask_task_for_the_open_field(db, sp2_schemas, agent):
    run_id, _ = _seed_draft(db, agent)
    client = ScriptedLLM({"contract_review": _no_review()["output"] and {"review_type": "CONTRACT_REVIEW", "status": "OK", "findings": []}})
    res = refine_contract(db, run_id, client=client, redactor=DefaultIntentRedactor(),
                          catalog=_View(), actor=agent)
    assert isinstance(res, RefineResult)
    assert res.status == "clarifying"
    assert "filters.declined_status_encoding" in res.open_fields
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT count(*) AS n FROM human_tasks WHERE run_id=%s AND status='open'", (run_id,))
        assert cur.fetchone()["n"] == 1


def test_answered_field_renormalizes_to_mcv_validated(db, sp2_schemas, agent):
    run_id, _ = _seed_draft(db, agent)
    # a prior human answer is pinned on the stream (as answer_clarification would emit, Task 5.6)
    append_fc_event(db, run_id=run_id, type="CLARIFICATION_ANSWERED",
                    payload={"task_id": "task_x", "field": "filters",
                             "answer": "card_authorizations.auth_result = 'D'", "response": "confirm",
                             "answered_by": "user:raj"}, actor=agent)
    client = ScriptedLLM({
        "renormalize": {
            "output": {"feature_semantics": _semantics("card_authorizations.auth_result = 'D'"),
                       "open_fields": []},
            "self_reported_scores": {
                "entity": {"ambiguity": 0.05, "confidence": 0.97, "source": "llm"},
                "entity_grain": {"ambiguity": 0.30, "confidence": 0.72, "source": "default"},
                "windows": {"ambiguity": 0.05, "confidence": 0.98, "source": "llm"},
                "filters": {"ambiguity": 0.10, "confidence": 0.92, "source": "llm"},
            },
        },
        "contract_review": {"review_type": "CONTRACT_REVIEW", "status": "OK", "findings": []},
    })
    res = refine_contract(db, run_id, client=client, redactor=DefaultIntentRedactor(),
                          catalog=_View(), actor=agent)
    assert res.status == "validated", res
    types = [e.type for e in load_feature_contract(db, run_id)]
    assert "CONTRACT_REFINED" in types
    assert "MINIMUM_CONTRACT_VALIDATED" in types


def test_refinement_loop_is_bounded_and_auto_parks(db, sp2_schemas, agent, monkeypatch):
    import featuregen.intake.commands as cmds
    monkeypatch.setattr(cmds, "MAX_REFINEMENT_ROUNDS", 1)
    run_id, _ = _seed_draft(db, agent)
    # An answer that does NOT resolve the open field (renormalize keeps it UNKNOWN) → the loop cannot
    # converge; with the round budget = 1 the SECOND refine auto-parks instead of looping forever.
    append_fc_event(db, run_id=run_id, type="CLARIFICATION_ANSWERED",
                    payload={"task_id": "t1", "field": "filters", "answer": "still unclear",
                             "response": "confirm", "answered_by": "user:raj"}, actor=agent)
    client = ScriptedLLM({
        "renormalize": {"output": {"feature_semantics": _semantics("UNKNOWN"),
                                   "open_fields": ["filters.declined_status_encoding"]},
                        "self_reported_scores": {"filters": {"ambiguity": 0.80, "confidence": 0.40, "source": "llm"}}},
        "contract_review": {"review_type": "CONTRACT_REVIEW", "status": "OK", "findings": []},
    })
    refine_contract(db, run_id, client=client, redactor=DefaultIntentRedactor(), catalog=_View(), actor=agent)
    res = refine_contract(db, run_id, client=client, redactor=DefaultIntentRedactor(), catalog=_View(), actor=agent)
    assert res.status == "parked"
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT count(*) AS n FROM events WHERE aggregate='run' AND run_id=%s AND type='RUN_PARKED'",
                    (run_id,))
        assert cur.fetchone()["n"] >= 1
