import pytest
from psycopg.rows import dict_row

from featuregen.contracts import Command
from featuregen.identity.build import build_human_identity
from featuregen.intake.commands import (
    RefineResult,
    _classify_raw_input,
    freeze_draft,
    open_clarification_task,
    refine_contract,
    submit_intent,
)
from featuregen.intake.redaction import DefaultIntentRedactor, EgressViolation
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


def _seed_draft(db, agent, *, run_id="run_ref", open_fields=("filters.declined_status_encoding",),
                semantics=None, raw_input_classification="clean"):
    # R4: INTENT_SUBMITTED is appended by the HUMAN requester (OWNER) → the P2 fold sets state.requester
    # == "user:raj", the owner the Refinement Loop scopes clarification tasks to (never a payload key).
    # `raw_input_classification` is the ORIGINAL intent label refine_contract reads as `raw_class`; a
    # `contains_pii`-origin run must still renormalize cleanly (its draft fields are already redacted).
    append_fc_event(
        db, run_id=run_id, type="INTENT_SUBMITTED",
        payload={"request_id": "req_ref", "run_id": run_id, "intake_mode": "definition",
                 "raw_input_ref": "blob_x", "raw_input_classification": raw_input_classification,
                 "classification": {"outcome": "CLEAR", "catalog_version": "bdc-1"}},
        actor=OWNER, expected_version=0,
    )
    ledger = {"request_id": "req_ref", "assumptions": [
        {"field": "entity_grain", "value": ["customer_id", "as_of_date"], "rationale": "pit convention",
         "source": "default", "ambiguity": 0.30, "confidence": 0.72}]}
    body = {
        "request_id": "req_ref", "intake_mode": "definition", "raw_input_ref": "blob_x",
        "raw_input_classification": "clean", "proposed_feature_name": "declined_card_auth_count_90d",
        "feature_semantics": semantics or _semantics(),
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
    # The refinement Loop READS the current draft/ledger body from the INLINED event stream
    # (mcv._latest_body), exactly as the real producer (_produce_draft) inlines them — so the seed
    # must inline them on DRAFT_CONTRACT_PRODUCED too (never rely on a by-doc-id body map).
    append_fc_event(db, run_id=run_id, type="DRAFT_CONTRACT_PRODUCED",
                    payload={"draft_doc_id": draft_doc_id, "assumption_ledger_ref": ledger_doc_id,
                             "open_fields": list(open_fields),
                             "draft_body": {**body, "assumption_ledger_ref": ledger_doc_id},
                             "assumption_ledger_body": ledger}, actor=agent)
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
        # Idempotent auto-park: the two exhausted re-drives append exactly ONE RUN_PARKED (the second is a
        # no-op on the still-parked run), never a duplicate.
        assert cur.fetchone()["n"] == 1


# A full, schema-valid DRAFT_CONTRACT body the FakeLLM returns for structure_intent (mirrors
# test_submit_intent_definition._DEFINITION_OUTPUT): the echoed envelope is discarded, only the
# semantic subset + assumptions are read by the assemblers (Task 4.2).
_STRUCTURE_INTENT_OUTPUT = {
    "request_id": "ECHO", "intake_mode": "definition", "raw_input_ref": "blob_echo",
    "raw_input_classification": "clean", "assumption_ledger_ref": "doc_echo",
    "status": "NEEDS_CLARIFICATION", "provenance": {"schema_version": 1},
    "proposed_feature_name": "declined_card_auth_count_90d",
    "feature_semantics": {
        "entity": "customer",
        "entity_grain": ["customer_id", "as_of_date"],
        "observation_intent": {"kind": "point_in_time", "as_of_field": "as_of_date",
                               "rule": "use only data available strictly before as_of_date"},
        "calculation_method": "rolling_count",
        "windows": [{"name": "lookback", "value": "90d"}],
        "filters": [{"concept": "declined card authorization", "predicate": "UNKNOWN"}],
        "target_definition": "N/A (definition-mode feature, no target)",
    },
    "field_scores": {
        "entity": {"ambiguity": 0.05, "confidence": 0.97, "source": "llm"},
        "entity_grain": {"ambiguity": 0.30, "confidence": 0.72, "source": "default"},
        "calculation_method": {"ambiguity": 0.10, "confidence": 0.90, "source": "llm"},
        "windows": {"ambiguity": 0.05, "confidence": 0.98, "source": "llm"},
        "filters": {"ambiguity": 0.80, "confidence": 0.40, "source": "llm"},
    },
    "open_fields": ["filters.declined_status_encoding"],
    "open_questions": [{"field": "filters.declined_status_encoding",
                        "question": "Which column/value marks a declined authorization?",
                        "ambiguity": 0.80, "confidence": 0.40, "blocks_progress": True, "routed_to": "human"}],
    "assumptions": [
        {"field": "entity_grain", "value": ["customer_id", "as_of_date"], "source": "default",
         "rationale": "point-in-time features are grained by entity × as_of_date by convention",
         "ambiguity": 0.30, "confidence": 0.72},
        {"field": "calculation_method.window", "value": "90d", "source": "llm",
         "rationale": "window stated verbatim in the intent ('90-day rolling')",
         "ambiguity": 0.05, "confidence": 0.98},
    ],
}


def test_submit_intent_then_refine_reads_draft_body_end_to_end(db, sp2_schemas, intake_env, agent):
    # END-TO-END: real intake (submit_intent → _produce_draft) INLINES the draft/ledger body on
    # DRAFT_CONTRACT_PRODUCED (it never calls freeze_draft), then a REAL refinement reads that body
    # from the event stream. Fails before the fix (refine_contract read the in-process body map, which
    # _produce_draft never populated → IntakeError "no stored body").
    intake_env.script_llm(_STRUCTURE_INTENT_OUTPUT)
    res = submit_intent(db, Command(
        "submit_intent", "feature_contract", None,
        {"intent_text": "90-day rolling count of declined card authorizations per customer",
         "intake_mode": "definition", "raw_input_classification": "clean"},
        OWNER, "e2e-submit-refine",
    ))
    assert res.accepted is True, res.denied_reason
    run_id = res.aggregate_id

    # a human answer that resolves the one open field is pinned on the stream (Task 5.6 would emit it)
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
    r = refine_contract(db, run_id, client=client, redactor=DefaultIntentRedactor(),
                        catalog=_View(), actor=agent)
    assert r.status == "validated", r
    types = [e.type for e in load_feature_contract(db, run_id)]
    assert "CONTRACT_REFINED" in types
    assert "MINIMUM_CONTRACT_VALIDATED" in types
    # Fix 3: the CONTRACT_REFINED payload carries the FRESH open_questions (empty after resolution).
    refined = next(e for e in load_feature_contract(db, run_id) if e.type == "CONTRACT_REFINED")
    assert "open_questions" in refined.payload


class _ExplodingLLM:
    def call(self, request):  # pragma: no cover - reached only if egress fails to fail-closed
        raise AssertionError("LLM must not be dispatched when the egress backstop fails closed")


def test_renormalize_fails_closed_on_pii_in_prior_semantics(db, sp2_schemas, agent):
    # Fix 2: prior_semantics rides a NON-reserved model-facing key that assert_llm_safe does not scan;
    # un-redacted PII there must FAIL CLOSED (EgressViolation) BEFORE any LLM dispatch (mirrors
    # critique.py). An answer targets the open field so the renormalize branch is taken.
    pii_semantics = _semantics()
    pii_semantics["filters"] = [{"concept": "declined card authorization",
                                 "predicate": "escalate to ops.alerts@example.com"}]  # EMAIL leak
    run_id, _ = _seed_draft(db, agent, semantics=pii_semantics)
    append_fc_event(db, run_id=run_id, type="CLARIFICATION_ANSWERED",
                    payload={"task_id": "t1", "field": "filters", "answer": "still unclear",
                             "response": "confirm", "answered_by": "user:raj"}, actor=agent)
    with pytest.raises(EgressViolation):
        refine_contract(db, run_id, client=_ExplodingLLM(), redactor=DefaultIntentRedactor(),
                        catalog=_View(), actor=agent)
    # no LLM was dispatched → no audit call recorded on the stream (fail-closed, nothing committed)
    assert "LLM_CALL_RECORDED" not in [e.type for e in load_feature_contract(db, run_id)]


def test_renormalize_survives_contains_pii_origin_intent(db, sp2_schemas, agent):
    # SP-2 merge-blocker: `renormalize` composes its request from the ALREADY-REDACTED structured draft
    # fields, so it must classify THAT payload "clean" (+ stamp a redaction_version) — NOT forward the
    # raw intent's original `contains_pii` label. Before the fix it forwarded raw_class="contains_pii"
    # with no redaction_version, so assert_llm_safe HARD-RAISED EgressViolation on EVERY clarification
    # round of a PII-origin run: call_llm records LLM_EGRESS_BLOCKED and re-raises, and there is NO
    # `except EgressViolation` in refine_contract/advance_intake → an UNHANDLED CRASH on exactly the
    # PII-bearing runs the redaction machinery exists to serve. Fails before the fix; passes after.
    #
    # The intent is AUTO-classified `contains_pii` by SP-0's inline-secret scan (an SSN in the raw text):
    pii_intent = "90-day declined-auth count per customer; contact me re: ssn 123-45-6789"
    assert _classify_raw_input(pii_intent, None) == "contains_pii"

    # ...but the STRUCTURED draft (renormalize's ACTUAL payload source) is clean-by-construction, and an
    # answer targets the one open field → the renormalize branch is taken with raw_class="contains_pii".
    run_id, _ = _seed_draft(db, agent, raw_input_classification="contains_pii")
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
    # No unhandled EgressViolation: the renormalize round completes as a `clean`-origin run would, the
    # renormalize LLM call actually DISPATCHED (assert_llm_safe passed → LLM_CALL_RECORDED), reaching MCV.
    res = refine_contract(db, run_id, client=client, redactor=DefaultIntentRedactor(),
                          catalog=_View(), actor=agent)
    assert res.status == "validated", res
    types = [e.type for e in load_feature_contract(db, run_id)]
    assert "LLM_CALL_RECORDED" in types      # renormalize dispatched (was egress-blocked before the fix)
    assert "CONTRACT_REFINED" in types
    assert "MINIMUM_CONTRACT_VALIDATED" in types


def test_renormalize_still_fails_closed_on_genuine_pii_for_contains_pii_origin(db, sp2_schemas, agent):
    # Companion to the above: classifying the renormalize payload "clean" must NOT weaken the no-PII
    # boundary. GENUINE PII in the composed model-facing content still FAILS CLOSED via the `_first_pii`
    # pre-scan BEFORE any dispatch — even for a `contains_pii`-origin run.
    pii_semantics = _semantics()
    pii_semantics["filters"] = [{"concept": "declined card authorization",
                                 "predicate": "escalate to ops.alerts@example.com"}]  # EMAIL leak
    run_id, _ = _seed_draft(db, agent, semantics=pii_semantics, raw_input_classification="contains_pii")
    append_fc_event(db, run_id=run_id, type="CLARIFICATION_ANSWERED",
                    payload={"task_id": "t1", "field": "filters", "answer": "still unclear",
                             "response": "confirm", "answered_by": "user:raj"}, actor=agent)
    with pytest.raises(EgressViolation):
        refine_contract(db, run_id, client=_ExplodingLLM(), redactor=DefaultIntentRedactor(),
                        catalog=_View(), actor=agent)
    # fail-closed at the pre-scan → the LLM was never dispatched, nothing committed to the stream.
    assert "LLM_CALL_RECORDED" not in [e.type for e in load_feature_contract(db, run_id)]
