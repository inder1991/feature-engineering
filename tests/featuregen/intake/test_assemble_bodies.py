from featuregen.documents.draft import validate_draft
from featuregen.intake.commands import (
    DRAFT_STATUS,
    assemble_draft_body,
    assemble_ledger_body,
)
from featuregen.intake.contract import validate_semantics

_LLM_OUTPUT = {
    # envelope the model echoed — MUST be ignored by the assembler
    "request_id": "MODEL_ECHO", "raw_input_ref": "blob_echo", "status": "CONFIRMED",
    "proposed_feature_name": "declined_card_auth_count_90d",
    "feature_semantics": {
        "entity": "customer",
        "entity_grain": ["customer_id", "as_of_date"],
        "observation_intent": {"kind": "point_in_time", "as_of_field": "as_of_date",
                               "rule": "use only data available strictly before as_of_date"},
        "calculation_method": "rolling_count",
        "windows": [{"name": "lookback", "value": "90d"}],
        "filters": [{"concept": "declined card authorization", "predicate": "UNKNOWN"}],
    },
    "field_scores": {
        "entity": {"ambiguity": 0.05, "confidence": 0.97, "source": "llm"},
        "entity_grain": {"ambiguity": 0.30, "confidence": 0.72, "source": "default"},
    },
    "open_fields": ["filters.declined_status_encoding"],
    "open_questions": [{"field": "filters.declined_status_encoding",
                        "question": "Which column/value marks a declined authorization?",
                        "blocks_progress": True, "routed_to": "human"}],
    "assumptions": [
        {"field": "entity_grain", "value": ["customer_id", "as_of_date"], "source": "default",
         "rationale": "point-in-time features are grained by entity × as_of_date by convention",
         "ambiguity": 0.30, "confidence": 0.72},
    ],
}


def test_assemble_ledger_body_shape():
    body = assemble_ledger_body(request_id="req_1", assumptions=_LLM_OUTPUT["assumptions"])
    assert body["request_id"] == "req_1"
    item = body["assumptions"][0]
    assert item["field"] == "entity_grain"
    assert item["value"] == ["customer_id", "as_of_date"]
    assert item["source"] == "default"
    assert item["rationale"]
    assert item["auto_resolved_at"]  # stamped by the platform
    # the assembled body is a valid ASSUMPTION_LEDGER (SP-2 §4.3)
    validate_semantics(body, stage="ASSUMPTION_LEDGER")


def test_assemble_ledger_body_defaults_source_and_stamps_missing_extras():
    # a bare SP-0 assumption (field/value/rationale only) is completed by the platform
    body = assemble_ledger_body(
        request_id="req_2",
        assumptions=[{"field": "entity", "value": "customer", "rationale": "sole entity in scope"}],
    )
    item = body["assumptions"][0]
    assert item["source"] == "llm"          # default when the model omits it
    assert item["auto_resolved_at"]         # stamped by the platform
    # optional numeric extras are OMITTED (not stamped as null) so the body stays schema-valid
    assert "ambiguity" not in item
    assert "confidence" not in item
    validate_semantics(body, stage="ASSUMPTION_LEDGER")


def test_assemble_draft_body_is_envelope_authoritative_and_valid():
    body = assemble_draft_body(
        request_id="req_1",
        intake_mode="definition",
        raw_input_ref="blob_authoritative",
        raw_input_classification="clean",
        assumption_ledger_ref="doc_ledger",
        llm_output=_LLM_OUTPUT,
        llm_call_ref="llmc_1",
    )
    # SP-0 envelope, set by the platform — the model's echoed values are discarded
    assert body["request_id"] == "req_1"
    assert body["raw_input_ref"] == "blob_authoritative"
    assert body["status"] == DRAFT_STATUS == "NEEDS_CLARIFICATION"
    assert body["assumption_ledger_ref"] == "doc_ledger"
    assert body["provenance"]["llm_call_refs"] == ["llmc_1"]
    # semantic subset carried through
    assert body["proposed_feature_name"] == "declined_card_auth_count_90d"
    assert body["feature_semantics"]["entity"] == "customer"
    assert body["open_fields"] == ["filters.declined_status_encoding"]
    # SP-0 envelope validation passes (raw text is a ref, open_fields honoured)
    validate_draft(body)
    # SP-2 semantic validation passes (content-schema + UNKNOWN-listed-in-open_fields, §4.0)
    validate_semantics(body, stage="DRAFT_CONTRACT")
    assert "raw_input" not in body
