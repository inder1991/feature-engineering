import pytest

from featuregen.intake.banking_catalog import (
    BankingDomainCatalog,
    IntakeClassification,
    IntakeOutcome,
    classify_intent,
)

CAT = BankingDomainCatalog(
    version="banking-cat@1",
    banking_entities=frozenset({"customer", "account", "card"}),
    banking_terms=frozenset({"customer", "account", "card", "authorization", "authorizations",
                             "transaction", "credit", "balance", "mortgage"}),
    allowed_use_cases=frozenset({"behavioral_credit_scoring", "retail_churn"}),
    out_of_scope_use_cases=frozenset({"card_fraud_realtime"}),
    out_of_scope_terms=frozenset({"netflix", "cart abandonment"}),
    blocked_data_classes=frozenset({"protected_attribute"}),
    blocked_terms={"race": "protected_attribute", "gender": "protected_attribute"},
    sensitive_proxy_terms=frozenset({"zip code", "postal code"}),
    use_case_terms={"behavioral_credit_scoring": ("credit risk", "credit score"),
                    "retail_churn": ("churn",),
                    "card_fraud_realtime": ("real-time card fraud",)},
    predictive_markers=frozenset({"predict", "propensity", "more likely"}),
    scoped_use_cases=frozenset({"retail_churn"}),
)


def test_prohibited_data_class_is_the_most_restrictive_outcome():
    r = classify_intent("predict churn using race for netflix subscribers", catalog=CAT)
    assert r.outcome is IntakeOutcome.PROHIBITED_DATA_CLASS   # race dominates netflix + churn
    assert r.matched_class == "protected_attribute"
    assert r.catalog_version == "banking-cat@1"
    assert r.blocks


def test_out_of_scope_example_term():
    r = classify_intent("predict which netflix shows a customer will watch", catalog=CAT)
    assert r.outcome is IntakeOutcome.OUT_OF_SCOPE
    assert r.catalog_version == "banking-cat@1"


def test_out_of_scope_when_no_banking_concept_present():
    r = classify_intent("rank the best pizza toppings", catalog=CAT)
    assert r.outcome is IntakeOutcome.OUT_OF_SCOPE


def test_out_of_scope_use_case_is_rejected():
    r = classify_intent("build a real-time card fraud model", catalog=CAT)
    assert r.outcome is IntakeOutcome.OUT_OF_SCOPE
    assert r.matched_use_case == "card_fraud_realtime"


def test_sensitive_proxy_routes_to_clarification_not_a_block():
    r = classify_intent("credit risk score using the customer's zip code", catalog=CAT)
    assert r.outcome is IntakeOutcome.SENSITIVE_PROXY_CLARIFY
    assert r.needs_clarification
    assert not r.blocks


def test_missing_product_region_for_a_scoped_use_case_is_ambiguous():
    r = classify_intent("predict churn for these customers", catalog=CAT)
    assert r.outcome is IntakeOutcome.AMBIGUOUS_CLARIFY
    ok = classify_intent("predict churn for these customers", product="cards", region="UK",
                         catalog=CAT)
    assert ok.outcome is IntakeOutcome.CLEAR
    assert ok.matched_use_case == "retail_churn"


def test_in_scope_known_use_case_is_clear():
    r = classify_intent("build a credit risk score for customers", catalog=CAT)
    assert r.outcome is IntakeOutcome.CLEAR
    assert r.is_clear
    assert r.matched_use_case == "behavioral_credit_scoring"
    assert r.catalog_version == "banking-cat@1"


def test_plain_banking_feature_definition_is_clear():
    r = classify_intent(
        "90-day rolling count of declined card authorizations per customer", catalog=CAT)
    assert r.outcome is IntakeOutcome.CLEAR


def test_in_scope_unknown_use_case_routes_to_onboarding():
    r = classify_intent("predict which customers will prepay their mortgage early", catalog=CAT)
    assert r.outcome is IntakeOutcome.NEEDS_USE_CASE_ONBOARDING
    assert r.catalog_version == "banking-cat@1"


def test_catalog_unavailable_fails_closed_never_clear():
    for cat in (None, BankingDomainCatalog(version=None)):
        r = classify_intent("build a credit risk score for customers", catalog=cat)
        assert r.outcome is IntakeOutcome.AMBIGUOUS_CLARIFY
        assert r.catalog_version is None
        assert not r.is_clear


def test_every_outcome_stamps_the_catalog_version_when_catalog_is_available():
    intents = [
        "predict churn using race",                              # prohibited
        "predict which netflix shows to watch for a customer",   # out of scope
        "credit risk score using zip code",                      # proxy
        "predict churn for customers",                           # ambiguous (scoped, no product/region)
        "build a credit risk score for customers",               # clear
        "predict which customers will prepay their mortgage",    # onboarding
    ]
    for text in intents:
        r = classify_intent(text, catalog=CAT)
        assert isinstance(r, IntakeClassification)
        assert r.catalog_version == "banking-cat@1"              # §4.5(c): version on EVERY outcome


def test_as_mapping_is_the_persisted_provenance_shape():
    # R9 — submit_intent persists classification.as_mapping() on INTENT_SUBMITTED; MCV /
    # not_prohibited_intent / refine read it back. outcome is the string VALUE, not the enum.
    r = classify_intent("predict churn using race", catalog=CAT)
    assert r.as_mapping() == {
        "outcome": "PROHIBITED_DATA_CLASS",
        "catalog_version": "banking-cat@1",
        "matched_class": "protected_attribute",
    }
    clear = classify_intent("build a credit risk score for customers", catalog=CAT)
    assert clear.as_mapping() == {
        "outcome": "CLEAR", "catalog_version": "banking-cat@1", "matched_class": None,
    }
