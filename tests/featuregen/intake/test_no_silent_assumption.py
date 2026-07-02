# tests/featuregen/intake/test_no_silent_assumption.py
import pytest

from featuregen.intake.commands import (
    NoSilentAssumptionError,
    assemble_draft_body,
    assemble_ledger_body,
    assert_no_silent_assumption,
)


def _draft(field_scores, open_fields, open_questions):
    return assemble_draft_body(
        request_id="req_1", intake_mode="definition", raw_input_ref="blob_1",
        raw_input_classification="clean", assumption_ledger_ref="doc_l",
        llm_output={
            "proposed_feature_name": "f",
            "feature_semantics": {"entity": "customer", "entity_grain": ["customer_id"],
                                  "observation_intent": {"kind": "point_in_time"},
                                  "calculation_method": "rolling_count", "windows": [], "filters": []},
            "field_scores": field_scores, "open_fields": open_fields, "open_questions": open_questions,
        },
        llm_call_ref="llmc_1",
    )


def test_inferred_field_with_ledger_entry_passes():
    draft = _draft(
        {"entity": {"ambiguity": 0.05, "confidence": 0.97, "source": "llm"},
         "entity_grain": {"ambiguity": 0.30, "confidence": 0.72, "source": "default"}},
        open_fields=[], open_questions=[],
    )
    ledger = assemble_ledger_body(
        request_id="req_1",
        assumptions=[{"field": "entity_grain", "value": ["customer_id", "as_of_date"],
                      "source": "default", "rationale": "platform convention"}],
    )
    assert_no_silent_assumption(draft, ledger)  # entity_grain accounted by the ledger


def test_open_field_with_question_passes_and_verbatim_needs_nothing():
    draft = _draft(
        {"filters": {"ambiguity": 0.80, "confidence": 0.40, "source": "llm"}},
        open_fields=["filters.declined_status_encoding"],
        open_questions=[{"field": "filters.declined_status_encoding", "question": "?",
                         "blocks_progress": True, "routed_to": "human"}],
    )
    assert_no_silent_assumption(draft, assemble_ledger_body(request_id="req_1", assumptions=[]))


def test_silent_inferred_field_is_rejected():
    draft = _draft(
        {"entity_grain": {"ambiguity": 0.30, "confidence": 0.72, "source": "default"}},
        open_fields=[], open_questions=[],
    )
    with pytest.raises(NoSilentAssumptionError):
        assert_no_silent_assumption(draft, assemble_ledger_body(request_id="req_1", assumptions=[]))


def test_open_field_without_question_is_rejected():
    draft = _draft(
        {"filters": {"ambiguity": 0.80, "confidence": 0.40, "source": "llm"}},
        open_fields=["filters.declined_status_encoding"], open_questions=[],
    )
    with pytest.raises(NoSilentAssumptionError):
        assert_no_silent_assumption(draft, assemble_ledger_body(request_id="req_1", assumptions=[]))
