from __future__ import annotations

import pytest

from sp0.contracts import SchemaValidationError
from sp0.documents.draft import (
    UNKNOWN,
    DraftValidationError,
    draft_has_open_fields,
    register_draft_schemas,
    validate_draft,
)
from sp0.documents.registry import DocumentSchemaRegistry


def _valid_draft(**over):
    body = {
        "request_id": "req_1",
        "intake_mode": "hypothesis",
        "raw_input_ref": "blob_raw_1",
        "raw_input_classification": "contains_pii",
        "target": "churn",
        "entity": "customer",
        "feature_concept": "salary irregularity",
        "open_fields": ["lookback_window"],
        "assumption_ledger_ref": "doc_led_1",
        "status": "NEEDS_CLARIFICATION",
    }
    body.update(over)
    return body


def test_valid_draft_passes():
    validate_draft(_valid_draft())


def test_inline_raw_input_is_rejected():
    bad = _valid_draft()
    bad["raw_input"] = "Customer SSN 123-45-6789 churns when..."
    with pytest.raises(DraftValidationError):
        validate_draft(bad)


def test_missing_assumption_ledger_ref_rejected():
    bad = _valid_draft()
    del bad["assumption_ledger_ref"]
    with pytest.raises(DraftValidationError):
        validate_draft(bad)


def test_invalid_classification_rejected():
    with pytest.raises(DraftValidationError):
        validate_draft(_valid_draft(raw_input_classification="maybe"))


def test_invalid_intake_mode_rejected():
    with pytest.raises(DraftValidationError):
        validate_draft(_valid_draft(intake_mode="freeform"))


def test_open_fields_signal_for_gate1():
    assert draft_has_open_fields(_valid_draft(open_fields=["lookback_window"])) is True
    assert draft_has_open_fields(_valid_draft(open_fields=[])) is False


def test_unknown_value_must_be_listed_in_open_fields():
    # §3.5: a field set to the UNKNOWN sentinel MUST appear in open_fields.
    bad = _valid_draft(target=UNKNOWN, open_fields=["lookback_window"])  # 'target' unlisted
    with pytest.raises(DraftValidationError):
        validate_draft(bad)


def test_unknown_value_listed_in_open_fields_passes():
    validate_draft(_valid_draft(target=UNKNOWN, open_fields=["target", "lookback_window"]))


def test_draft_validation_error_is_a_schema_validation_error():
    assert issubclass(DraftValidationError, SchemaValidationError)


def test_registered_draft_schema_blocks_inline_raw_input(db):
    reg = DocumentSchemaRegistry(db)
    register_draft_schemas(reg)
    reg.validate("DRAFT_CONTRACT", 1, _valid_draft())  # ok
    bad = _valid_draft()
    bad["raw_input"] = "secret"
    with pytest.raises(SchemaValidationError):
        reg.validate("DRAFT_CONTRACT", 1, bad)


def test_assumption_ledger_schema_registered(db):
    reg = DocumentSchemaRegistry(db)
    register_draft_schemas(reg)
    reg.validate(
        "ASSUMPTION_LEDGER", 1,
        {"request_id": "req_1",
         "assumptions": [{"field": "lookback_window", "value": 90,
                          "rationale": "default"}]},
    )
