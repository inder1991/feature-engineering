import pytest

from featuregen.intake import catalog
from featuregen.intake.banking_catalog import BankingDomainCatalog

_SEED = {
    "catalog_version": "banking-cat@1",
    "data_classes": ["transactions", "protected_attribute"],
    "entities": ["customer", "account"],
    "use_cases": [{"use_case": "retail_churn", "status": "active",
                   "blocked_data_classes": ["protected_attribute"]}],
}


def test_load_banking_catalog_from_seed_builds_a_reader():
    cat = catalog.load_banking_catalog_from_seed(_SEED)
    assert isinstance(cat, BankingDomainCatalog)
    assert cat.version == "banking-cat@1"
    assert cat.available


def test_register_then_current_round_trips_the_same_instance():
    cat = catalog.load_banking_catalog_from_seed(_SEED)
    catalog.register_intake_catalog(cat)
    assert catalog.current_intake_catalog() is cat


def test_register_intake_catalog_is_last_writer_wins():
    first = catalog.load_banking_catalog_from_seed(_SEED)
    second = catalog.load_banking_catalog_from_seed(_SEED)
    catalog.register_intake_catalog(first)
    catalog.register_intake_catalog(second)
    assert catalog.current_intake_catalog() is second


def test_current_intake_catalog_fails_closed_when_unset(monkeypatch):
    monkeypatch.setattr(catalog, "_INTAKE_CATALOG", None, raising=False)
    with pytest.raises(RuntimeError):
        catalog.current_intake_catalog()
