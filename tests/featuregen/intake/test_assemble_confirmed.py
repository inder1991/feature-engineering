import pytest

from featuregen.intake.contract import (
    ContractSemanticError,
    assemble_confirmed,
    reshape_calculation_method,
    validate_semantics,
)


def _draft_semantics(calc="rolling_count", predicate="card_authorizations.auth_result = 'D'"):
    return {
        "entity": "customer",
        "entity_grain": ["customer_id", "as_of_date"],
        "observation_intent": {"kind": "point_in_time", "as_of_field": "as_of_date"},
        "calculation_method": calc,
        "windows": [{"name": "lookback", "value": "90d"}],
        "filters": [{"concept": "declined card authorization", "predicate": predicate}],
    }


def _draft_body():
    return {
        "request_id": "req_1", "intake_mode": "definition",
        "raw_input_ref": "blob_01H", "raw_input_classification": "clean",
        "proposed_feature_name": "declined_card_auth_count_90d",
        "assumption_ledger_ref": "doc_led1",
        "feature_semantics": _draft_semantics(),
        "field_scores": {}, "open_fields": [],
        "provenance": {"schema_version": 1, "llm_call_refs": ["llmc_1"]},
        "status": "NEEDS_CLARIFICATION",
    }


def test_reshape_rolling_count_matches_the_tagged_shape():
    cm = reshape_calculation_method(_draft_semantics())
    assert cm["method_version"] == 1
    assert cm["chosen"] == {
        "kind": "rolling_aggregate", "aggregation": "count", "window": "90d",
        "filter": {"concept": "declined card authorization",
                   "predicate": "card_authorizations.auth_result = 'D'"},
    }
    assert cm["considered"] == [cm["chosen"]]


def test_reshape_unknown_method_fails_closed():
    with pytest.raises(ContractSemanticError):
        reshape_calculation_method(_draft_semantics(calc="UNKNOWN"))


def test_reshape_non_rolling_label_requires_explicit_chosen_method():
    with pytest.raises(ContractSemanticError):
        reshape_calculation_method(_draft_semantics(calc="jensen_shannon"))
    # ...but an explicit tagged variant is accepted verbatim (hypothesis-mode candidate)
    chosen = {"kind": "distribution_divergence", "measure": "jensen_shannon",
              "window": "30d", "baseline_window": "180d"}
    cm = reshape_calculation_method(_draft_semantics(calc="jensen_shannon"), chosen_method=chosen)
    assert cm["chosen"] == chosen


def test_assemble_confirmed_renames_and_reshapes_deterministically_and_validates():
    confirmation = {"confirmed_by": "user:raj", "confirmed_at": "2026-07-01T10:22:41Z",
                    "selected_candidate": None, "rejected_candidates": [], "human_edits": []}
    confirmed = assemble_confirmed(
        _draft_body(), confirmation=confirmation, derived_from=["doc_draft1"],
    )
    # Draft→Confirmed renames (§4.2)
    assert confirmed["feature_name"] == "declined_card_auth_count_90d"
    assert confirmed["feature_grain"] == ["customer_id", "as_of_date"]
    assert confirmed["entity_key"] == "customer_id"       # derived: grain[0]
    assert confirmed["target"] is None
    assert confirmed["requires_independent_validation"] is False
    assert confirmed["status"] == "CONFIRMED"
    assert confirmed["provenance"]["derived_from"] == ["doc_draft1"]
    assert confirmed["provenance"]["llm_call_refs"] == ["llmc_1"]   # carried from the Draft
    # the assembled body is a valid CONFIRMED_CONTRACT
    validate_semantics(confirmed, stage="CONFIRMED_CONTRACT")


def test_assemble_confirmed_honours_a_gate1_feature_name_edit_and_risk_flag():
    confirmation = {"confirmed_by": "user:raj", "confirmed_at": "2026-07-01T10:22:41Z"}
    confirmed = assemble_confirmed(
        _draft_body(), confirmation=confirmation, derived_from=["doc_draft1"],
        feature_name="declined_auth_cnt_90d", requires_independent_validation=True,
    )
    assert confirmed["feature_name"] == "declined_auth_cnt_90d"
    assert confirmed["requires_independent_validation"] is True
