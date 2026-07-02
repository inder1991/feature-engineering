import json

from psycopg.rows import dict_row

from featuregen.contracts import Command
from featuregen.events.store import load_stream
from featuregen.identity.build import build_human_identity
from featuregen.intake.commands import submit_intent
from featuregen.intake.events import DRAFT_CONTRACT_PRODUCED, INTENT_SUBMITTED

ALICE = build_human_identity(subject="user:alice", role_claims=("data_scientist",))

_INTENT = "90-day rolling count of declined card authorizations per customer"

# A full, schema-valid DRAFT_CONTRACT body the FakeLLM returns for structure_intent, plus the
# `assumptions` array the platform folds into the ledger. The assembler reads only the semantic
# subset + assumptions; the echoed envelope is discarded (Task 4.2).
_DEFINITION_OUTPUT = {
    "request_id": "ECHO", "intake_mode": "definition", "raw_input_ref": "blob_echo",
    "raw_input_classification": "clean", "assumption_ledger_ref": "doc_echo", "status": "NEEDS_CLARIFICATION",
    "provenance": {"schema_version": 1},
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


def _cmd(**args):
    base = {"intent_text": _INTENT, "intake_mode": "definition", "raw_input_classification": "clean"}
    base.update(args)
    return Command("submit_intent", "feature_contract", None, base, ALICE, "k1")


def test_definition_intent_produces_draft_and_ledger(db, intake_env):
    intake_env.script_llm(_DEFINITION_OUTPUT)
    res = submit_intent(db, _cmd())
    assert res.accepted is True, res.denied_reason
    run_id = res.aggregate_id

    # feature_contract stream: INTENT_SUBMITTED then DRAFT_CONTRACT_PRODUCED
    fc = load_stream(db, "feature_contract", run_id)
    types = [e.type for e in fc]
    assert types[0] == INTENT_SUBMITTED
    assert DRAFT_CONTRACT_PRODUCED in types

    submitted = fc[0]
    assert submitted.payload["intake_mode"] == "definition"
    assert submitted.payload["raw_input_ref"].startswith("blob_")
    # R9: the classification mapping rides INTENT_SUBMITTED; R4: requester = the event actor.subject
    assert submitted.payload["classification"]["outcome"] == "CLEAR"
    assert submitted.payload["classification"]["catalog_version"] == "bdc-2026.1"
    assert submitted.payload["requester"] == "user:alice"
    # R2: no id fields ride the payload (they ride typed columns / seam kwargs)
    assert "run_id" not in submitted.payload
    assert "request_id" not in submitted.payload
    # the RAW intent text is never inlined into the domain event
    assert "declined card authorizations" not in json.dumps(submitted.payload)

    produced = next(e for e in fc if e.type == DRAFT_CONTRACT_PRODUCED)
    draft_body = produced.payload["draft_body"]
    assert draft_body["status"] == "NEEDS_CLARIFICATION"
    assert draft_body["proposed_feature_name"] == "declined_card_auth_count_90d"
    assert draft_body["open_fields"] == ["filters.declined_status_encoding"]
    # envelope is platform-authoritative, not the model's echo
    assert draft_body["request_id"] != "ECHO"
    assert draft_body["raw_input_ref"] != "blob_echo"
    assert produced.payload["catalog_version"] == "bdc-2026.1"
    # R12: standardized doc-ref keys (NOT `assumption_ledger_doc_id` / `document_id`)
    assert produced.payload["draft_doc_id"].startswith("doc_")
    assert produced.payload["assumption_ledger_ref"].startswith("doc_")
    assert "run_id" not in produced.payload and "request_id" not in produced.payload  # R2
    assert produced.payload["assumption_ledger_body"]["assumptions"][0]["field"] == "entity_grain"

    # two frozen documents on the run's DAG
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT stage, body_classification FROM documents WHERE run_id=%s ORDER BY stage", (run_id,)
        )
        rows = cur.fetchall()
    stages = sorted(r["stage"] for r in rows)
    assert stages == ["ASSUMPTION_LEDGER", "DRAFT_CONTRACT"]
    assert all(r["body_classification"] == "governance-retained" for r in rows)


def test_invalid_intake_mode_is_denied(db, intake_env):
    res = submit_intent(db, _cmd(intake_mode="nonsense"))
    assert res.accepted is False
    assert "intake_mode" in res.denied_reason
