from featuregen.intake.banking_catalog import BankingDomainCatalog

_SEED = {
    "catalog_version": "0.1.0-draft",
    "data_classes": ["transactions", "balances", "protected_attribute", "geolocation"],
    "entities": ["customer", "account"],
    "use_cases": [
        {"use_case": "retail_churn", "status": "active",
         "blocked_data_classes": ["protected_attribute"],
         "allowed_data_classes": ["transactions", "balances"],
         "target": {"name": "churn", "definition": "no txn for 90d"}},
        {"use_case": "behavioral_credit_scoring", "status": "active",
         "blocked_data_classes": ["protected_attribute"],
         "target": {"name": "credit risk"}},
        {"use_case": "card_fraud_realtime", "status": "out_of_scope",
         "blocked_data_classes": ["protected_attribute"]},
    ],
}


def test_from_seed_maps_version_scope_and_use_cases():
    cat = BankingDomainCatalog.from_seed(_SEED)
    assert cat.version == "0.1.0-draft"
    assert cat.available
    assert "retail_churn" in cat.allowed_use_cases
    assert "behavioral_credit_scoring" in cat.allowed_use_cases
    assert "card_fraud_realtime" in cat.out_of_scope_use_cases
    assert "card_fraud_realtime" not in cat.allowed_use_cases


def test_from_seed_derives_blocked_classes_and_surface_terms():
    cat = BankingDomainCatalog.from_seed(_SEED)
    assert cat.blocked_data_classes == frozenset({"protected_attribute"})
    # protected_attribute expands to protected-attribute surface terms → each maps back to the class
    assert cat.blocked_terms["race"] == "protected_attribute"
    assert cat.blocked_terms["gender"] == "protected_attribute"


def test_from_seed_builds_banking_terms_and_use_case_terms():
    cat = BankingDomainCatalog.from_seed(_SEED)
    assert "customer" in cat.banking_entities
    assert "customer" in cat.banking_terms and "transactions" in cat.banking_terms
    assert "churn" in cat.use_case_terms["retail_churn"]
    assert "credit risk" in cat.use_case_terms["behavioral_credit_scoring"]


def test_geolocation_data_class_seeds_a_sensitive_proxy_term():
    cat = BankingDomainCatalog.from_seed(_SEED)
    assert "zip code" in cat.sensitive_proxy_terms


def test_missing_version_is_unavailable_fail_closed_gate():
    cat = BankingDomainCatalog.from_seed({"catalog_version": "", "use_cases": []})
    assert cat.available is False
    empty = BankingDomainCatalog(version=None)
    assert empty.available is False
