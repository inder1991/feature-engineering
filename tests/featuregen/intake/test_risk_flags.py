"""F4 / P1-d: risk_flags is computed platform-side from the intake classification (a catalog-DECLARED
high-risk matched use-case) and lands on the Draft body → requires_independent_validation at Gate #1.
Before the fix, assemble_draft_body never populated risk_flags, so RIV was ALWAYS False."""

from featuregen.intake.banking_catalog import BankingDomainCatalog, classify_intent
from featuregen.intake.commands import (
    _requires_independent_validation,
    _risk_flags_for,
    assemble_draft_body,
)

_SEED = {
    "catalog_version": "bdc-test",
    "entities": ["customer", "card"],
    "data_classes": ["transactions", "card_authorizations"],
    "use_cases": [
        {"use_case": "credit_decisioning", "status": "active", "high_risk": True},
        {"use_case": "card_authorization", "status": "active", "target": {"name": "declined_auth"}},
    ],
}


def _catalog():
    return BankingDomainCatalog.from_seed(_SEED)


def _llm_out():
    return {"proposed_feature_name": "f", "feature_semantics": {"entity": "customer"}}


def test_catalog_declares_high_risk_use_cases_from_seed():
    cat = _catalog()
    assert "credit_decisioning" in cat.high_risk_use_cases
    assert "card_authorization" not in cat.high_risk_use_cases  # not flagged high_risk


def test_risk_flags_for_high_risk_matched_use_case():
    cat = _catalog()
    cls = classify_intent("credit decisioning for the customer", catalog=cat)
    assert cls.matched_use_case == "credit_decisioning"
    assert _risk_flags_for(cls, cat) == ["high_risk_use_case:credit_decisioning"]


def test_risk_flags_empty_for_benign_use_case():
    cat = _catalog()
    cls = classify_intent("count declined card authorization for the customer", catalog=cat)
    assert cls.matched_use_case == "card_authorization"
    assert _risk_flags_for(cls, cat) == []


def test_risk_flags_empty_when_catalog_or_use_case_missing():
    assert _risk_flags_for(classify_intent("x", catalog=_catalog()), None) == []


def test_assemble_draft_body_sets_risk_flags_and_riv_true():
    body = assemble_draft_body(
        request_id="r", intake_mode="definition", raw_input_ref="b",
        raw_input_classification="clean", assumption_ledger_ref="l", llm_output=_llm_out(),
        llm_call_ref="c", risk_flags=["high_risk_use_case:credit_decisioning"],
    )
    assert body["risk_flags"] == ["high_risk_use_case:credit_decisioning"]
    assert _requires_independent_validation(body) is True


def test_assemble_draft_body_defaults_no_risk_flags_riv_false():
    body = assemble_draft_body(
        request_id="r", intake_mode="definition", raw_input_ref="b",
        raw_input_classification="clean", assumption_ledger_ref="l", llm_output=_llm_out(),
        llm_call_ref="c",
    )
    assert body["risk_flags"] == []  # before F4 the key was absent entirely
    assert _requires_independent_validation(body) is False
