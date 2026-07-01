import pytest

from featuregen.intake import contract
from featuregen.intake.contract import ContractSemanticError, validate_semantics


def _draft(open_fields=None, calc="rolling_count", predicate="card_authorizations.auth_result = 'D'"):
    return {
        "request_id": "req_1",
        "intake_mode": "definition",
        "raw_input_ref": "blob_01H",
        "raw_input_classification": "clean",
        "proposed_feature_name": "declined_card_auth_count_90d",
        "assumption_ledger_ref": "doc_led1",
        "feature_semantics": {
            "entity": "customer",
            "entity_grain": ["customer_id", "as_of_date"],
            "observation_intent": {"kind": "point_in_time", "as_of_field": "as_of_date"},
            "calculation_method": calc,
            "windows": [{"name": "lookback", "value": "90d"}],
            "filters": [{"concept": "declined card authorization", "predicate": predicate}],
        },
        "field_scores": {"entity": {"ambiguity": 0.05, "confidence": 0.97, "source": "llm"}},
        "open_fields": open_fields if open_fields is not None else [],
        "provenance": {"schema_version": 1, "llm_call_refs": ["llmc_1"]},
        "status": "NEEDS_CLARIFICATION",
    }


def test_valid_draft_passes():
    validate_semantics(_draft(), stage="DRAFT_CONTRACT")


def test_draft_missing_semantic_block_field_is_rejected():
    body = _draft()
    del body["feature_semantics"]["calculation_method"]
    with pytest.raises(ContractSemanticError):
        validate_semantics(body, stage="DRAFT_CONTRACT")


def test_draft_closed_enum_violation_is_rejected():
    body = _draft()
    body["feature_semantics"]["observation_intent"]["kind"] = "made_up"
    with pytest.raises(ContractSemanticError):
        validate_semantics(body, stage="DRAFT_CONTRACT")


def test_draft_wrong_status_const_is_rejected():
    body = _draft()
    body["status"] = "CONFIRMED"
    with pytest.raises(ContractSemanticError):
        validate_semantics(body, stage="DRAFT_CONTRACT")


def test_draft_unknown_calc_method_must_be_listed_in_open_fields():
    # calculation_method == UNKNOWN but NOT in open_fields → rejected (§4.0)
    body = _draft(open_fields=[], calc=contract.UNKNOWN)
    with pytest.raises(ContractSemanticError):
        validate_semantics(body, stage="DRAFT_CONTRACT")
    # listing it makes the Draft valid
    body_ok = _draft(open_fields=["calculation_method"], calc=contract.UNKNOWN)
    validate_semantics(body_ok, stage="DRAFT_CONTRACT")


def test_draft_unknown_filter_predicate_requires_an_open_fields_entry():
    body = _draft(open_fields=[], predicate=contract.UNKNOWN)
    with pytest.raises(ContractSemanticError):
        validate_semantics(body, stage="DRAFT_CONTRACT")
    body_ok = _draft(open_fields=["filters.declined_status_encoding"], predicate=contract.UNKNOWN)
    validate_semantics(body_ok, stage="DRAFT_CONTRACT")


def _confirmed():
    return {
        "feature_name": "declined_card_auth_count_90d",
        "intake_mode": "definition",
        "raw_input_ref": "blob_01H",
        "raw_input_classification": "clean",
        "entity": "customer",
        "entity_key": "customer_id",
        "feature_grain": ["customer_id", "as_of_date"],
        "observation_intent": {"kind": "point_in_time"},
        "calculation_method": {"method_version": 1,
                               "chosen": {"kind": "rolling_aggregate", "aggregation": "count",
                                          "window": "90d"},
                               "considered": []},
        "target": None,
        "assumption_ledger_ref": "doc_led1",
        "requires_independent_validation": False,
        "confirmation": {"confirmed_by": "user:raj", "confirmed_at": "2026-07-01T10:22:41Z"},
        "provenance": {"derived_from": ["doc_draft1"], "schema_version": 1},
        "status": "CONFIRMED",
    }


def test_valid_confirmed_passes_and_bad_method_variant_is_rejected():
    validate_semantics(_confirmed(), stage="CONFIRMED_CONTRACT")
    body = _confirmed()
    body["calculation_method"]["chosen"] = {"kind": "rolling_aggregate"}  # missing aggregation/window
    with pytest.raises(ContractSemanticError):
        validate_semantics(body, stage="CONFIRMED_CONTRACT")


def test_ledger_source_enum_is_closed():
    ok = {"request_id": "req_1", "assumptions": [
        {"field": "entity_grain", "value": ["customer_id", "as_of_date"],
         "rationale": "platform default", "source": "default"}]}
    validate_semantics(ok, stage="ASSUMPTION_LEDGER")
    bad = {"request_id": "req_1", "assumptions": [
        {"field": "x", "value": 1, "rationale": "r", "source": "guess"}]}
    with pytest.raises(ContractSemanticError):
        validate_semantics(bad, stage="ASSUMPTION_LEDGER")


def test_unknown_stage_is_rejected():
    with pytest.raises(ContractSemanticError):
        validate_semantics({}, stage="MAPPED_CONTRACT")
