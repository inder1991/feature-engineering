from __future__ import annotations

import pytest
from tests.featuregen.state_machine._predicates import PeekingPredicate, truthy

from featuregen.state_machine.guard_expr import GuardExprError
from featuregen.state_machine.guards import (
    InMemoryPredicateRegistry,
    PredicateRegistrationError,
)


def _registry() -> InMemoryPredicateRegistry:
    reg = InMemoryPredicateRegistry()
    reg.register(truthy("confirmed_contract_exists", "confirmed_contract_ref"))
    reg.register(truthy("catalog_quality_passed", "catalog_quality_result_ref"))
    return reg


def test_register_and_get() -> None:
    reg = _registry()
    assert reg.get("confirmed_contract_exists").name == "confirmed_contract_exists"


def test_duplicate_registration_is_load_error() -> None:
    reg = _registry()
    with pytest.raises(PredicateRegistrationError):
        reg.register(truthy("confirmed_contract_exists", "confirmed_contract_ref"))


def test_get_unknown_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        InMemoryPredicateRegistry().get("nope")


def test_evaluate_true_records_outcome() -> None:
    reg = _registry()
    outcome = reg.evaluate(
        "confirmed_contract_exists AND catalog_quality_passed",
        {"confirmed_contract_ref": "doc_1", "catalog_quality_result_ref": "doc_2"},
    )
    assert outcome.passed is True
    assert outcome.per_predicate == {
        "confirmed_contract_exists": True,
        "catalog_quality_passed": True,
    }
    assert outcome.resolved_inputs == {
        "confirmed_contract_ref": "doc_1",
        "catalog_quality_result_ref": "doc_2",
    }


def test_evaluate_false_when_one_predicate_false() -> None:
    reg = _registry()
    outcome = reg.evaluate(
        "confirmed_contract_exists AND catalog_quality_passed",
        {"confirmed_contract_ref": "doc_1", "catalog_quality_result_ref": ""},
    )
    assert outcome.passed is False
    assert outcome.per_predicate["catalog_quality_passed"] is False


def test_resolved_inputs_excludes_undeclared_keys() -> None:
    reg = _registry()
    outcome = reg.evaluate(
        "confirmed_contract_exists",
        {"confirmed_contract_ref": "doc_1", "some_mutable_projection": "LEAK"},
    )
    assert "some_mutable_projection" not in outcome.resolved_inputs
    assert outcome.resolved_inputs == {"confirmed_contract_ref": "doc_1"}


def test_predicate_cannot_read_undeclared_input() -> None:
    reg = InMemoryPredicateRegistry()
    reg.register(PeekingPredicate(name="peeker", declared_inputs=("a",), peek_key="b"))
    with pytest.raises(KeyError):
        reg.evaluate("peeker", {"a": "x", "b": "y"})


def test_missing_declared_input_raises() -> None:
    reg = _registry()
    with pytest.raises(KeyError):
        reg.evaluate("confirmed_contract_exists", {})


def test_unregistered_predicate_in_expr_raises() -> None:
    reg = _registry()
    with pytest.raises(KeyError):
        reg.evaluate(
            "confirmed_contract_exists AND not_registered", {"confirmed_contract_ref": "x"}
        )


def test_malformed_expr_propagates() -> None:
    reg = _registry()
    with pytest.raises(GuardExprError):
        reg.evaluate("confirmed_contract_exists AND", {"confirmed_contract_ref": "x"})
