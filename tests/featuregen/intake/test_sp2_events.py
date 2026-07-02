from __future__ import annotations

import pytest

from featuregen.contracts import SchemaValidationError
from featuregen.events.registry import event_registry
from featuregen.intake.events import (
    INTENT_REJECTED,
    INTENT_SUBMITTED,
    LLM_CALL_RECORDED,
    NEEDS_USE_CASE_ONBOARDING,
    SP2_EVENT_SCHEMA_VERSION,
    SP2_EVENT_SCHEMAS,
    USE_CASE_ONBOARDING_GATE,
    register_sp2_event_types,
)

_ALL_TWELVE = {
    "INTENT_SUBMITTED",
    "DRAFT_CONTRACT_PRODUCED",
    "CONTRACT_CRITIQUED",
    "FIELD_AUTO_RESOLVED",
    "CLARIFICATION_REQUESTED",
    "CLARIFICATION_ANSWERED",
    "CONTRACT_REFINED",
    "MINIMUM_CONTRACT_VALIDATED",
    "CONTRACT_CONFIRMED",
    "USE_CASE_ONBOARDING_REQUESTED",
    "INTENT_REJECTED",
    "LLM_CALL_RECORDED",
}
# Task 9.5a — the hypothesis-mode candidate-generation shadow advance_intake records so the P2 fold
# surfaces state.candidate_doc_ids (its candidate_doc_ids drive MCV #2, §6.7 #2 / gap D).
_ALL_FC_EVENT_TYPES = _ALL_TWELVE | {"CANDIDATES_GENERATED"}


def test_all_twelve_fc_event_types_present():
    assert set(SP2_EVENT_SCHEMAS) == _ALL_FC_EVENT_TYPES
    assert INTENT_SUBMITTED == "INTENT_SUBMITTED"
    assert LLM_CALL_RECORDED == "LLM_CALL_RECORDED"


def test_gate_and_park_constants():
    assert USE_CASE_ONBOARDING_GATE == "USE_CASE_ONBOARDING"
    assert NEEDS_USE_CASE_ONBOARDING == "NEEDS_USE_CASE_ONBOARDING"


def test_register_makes_every_type_writable():
    reg = event_registry()
    register_sp2_event_types(reg)
    for type_name in _ALL_FC_EVENT_TYPES:
        reg.assert_writable(type_name, SP2_EVENT_SCHEMA_VERSION)  # active → no raise
        assert reg.max_active_versions()[type_name] == SP2_EVENT_SCHEMA_VERSION


def test_intent_submitted_schema_validates_required_and_enums():
    reg = event_registry()
    register_sp2_event_types(reg)
    # R2: the payload carries only SEMANTIC fields — id fields (feature_contract_id/run_id/request_id)
    # ride typed columns and are NOT in required[].
    good = {
        "intake_mode": "definition",
        "raw_input_ref": "blob_01H",
        "raw_input_classification": "clean",
    }
    reg.validate(INTENT_SUBMITTED, 1, good)  # no raise (no id fields needed)
    with pytest.raises(SchemaValidationError):
        reg.validate(INTENT_SUBMITTED, 1, {**good, "intake_mode": "guesswork"})  # closed enum
    with pytest.raises(SchemaValidationError):
        bad = dict(good)
        del bad["raw_input_ref"]
        reg.validate(INTENT_SUBMITTED, 1, bad)  # missing a SEMANTIC required field
    # R2: an id field is never required — a payload carrying one still validates (additive).
    reg.validate(INTENT_SUBMITTED, 1, {**good, "run_id": "run_1"})


def test_intent_rejected_classification_is_a_closed_enum():
    reg = event_registry()
    register_sp2_event_types(reg)
    base = {
        "feature_contract_id": "fc_1",
        "run_id": "run_1",
        "classification": "OUT_OF_SCOPE",
        "catalog_version": "bdc@2026-06-01",
    }
    reg.validate(INTENT_REJECTED, 1, base)  # no raise
    reg.validate(INTENT_REJECTED, 1, {**base, "classification": "PROHIBITED_DATA_CLASS"})
    with pytest.raises(SchemaValidationError):
        reg.validate(INTENT_REJECTED, 1, {**base, "classification": "MEH"})


def test_event_schemas_require_only_semantic_fields():
    # R2: no id field (feature_contract_id / run_id / request_id) appears in ANY required[].
    id_fields = {"feature_contract_id", "run_id", "request_id"}
    for type_name, schema in SP2_EVENT_SCHEMAS.items():
        assert id_fields.isdisjoint(schema["required"]), type_name
    assert SP2_EVENT_SCHEMAS[LLM_CALL_RECORDED]["required"] == ["llm_call_ref"]
    assert SP2_EVENT_SCHEMAS[INTENT_SUBMITTED]["required"] == [
        "intake_mode",
        "raw_input_ref",
        "raw_input_classification",
    ]
