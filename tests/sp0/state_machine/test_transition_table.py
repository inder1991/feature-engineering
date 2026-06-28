from __future__ import annotations

import psycopg
import pytest

from sp0.state_machine.guards import InMemoryPredicateRegistry
from sp0.state_machine.transition_table import (
    Transition,
    TransitionTableError,
    install_transition_table,
    load_transition_table,
)
from tests.sp0.state_machine._predicates import truthy


def _registry() -> InMemoryPredicateRegistry:
    reg = InMemoryPredicateRegistry()
    reg.register(truthy("confirmed_contract_exists", "confirmed_contract_ref"))
    reg.register(truthy("catalog_quality_passed", "catalog_quality_result_ref"))
    return reg


def _guarded(precedence: int = 100) -> Transition:
    return Transition(
        table_version=1,
        from_state="CONFIRMED_CONTRACT",
        to_state="SCHEMA_MAPPED",
        trigger="MAPPING_COMPLETED",
        guard_expr="confirmed_contract_exists AND catalog_quality_passed",
        guard_inputs={
            "confirmed_contract_exists": "confirmed_contract_ref",
            "catalog_quality_passed": "catalog_quality_result_ref",
        },
        precedence=precedence,
        on_success={"to": "SCHEMA_MAPPED", "emits": "FEATURE_MAPPED"},
        on_guard_fail={"to": "MAPPING_REVIEW_FAILED", "emits": "GUARD_FAILED"},
    )


def test_install_and_load_round_trip(conn) -> None:
    install_transition_table(conn, "run", 1, [_guarded()], _registry())
    table = load_transition_table(conn, "run", 1)
    assert table.kind == "run"
    assert table.table_version == 1
    assert len(table.transitions) == 1
    t = table.transitions[0]
    assert t.guard_expr == "confirmed_contract_exists AND catalog_quality_passed"
    assert t.on_success == {"to": "SCHEMA_MAPPED", "emits": "FEATURE_MAPPED"}
    assert t.on_guard_fail == {"to": "MAPPING_REVIEW_FAILED", "emits": "GUARD_FAILED"}
    assert "MAPPING_REVIEW_FAILED" in table.states


def test_install_feature_kind(conn) -> None:
    install_transition_table(conn, "feature", 1, [_guarded()], _registry())
    table = load_transition_table(conn, "feature", 1)
    assert table.kind == "feature"
    assert len(table.transitions) == 1


def test_matches_orders_by_precedence_desc(conn) -> None:
    install_transition_table(
        conn, "run", 1, [_guarded(precedence=50), _guarded(precedence=100)], _registry()
    )
    table = load_transition_table(conn, "run", 1)
    ms = table.matches("CONFIRMED_CONTRACT", "MAPPING_COMPLETED")
    assert [m.precedence for m in ms] == [100, 50]


def test_precedence_tie_rejected_at_install(conn) -> None:
    with pytest.raises(TransitionTableError):
        install_transition_table(
            conn, "run", 1, [_guarded(precedence=100), _guarded(precedence=100)], _registry()
        )


def test_unregistered_predicate_rejected(conn) -> None:
    reg = InMemoryPredicateRegistry()  # nothing registered
    with pytest.raises(TransitionTableError):
        install_transition_table(conn, "run", 1, [_guarded()], reg)


def test_guard_without_on_guard_fail_rejected(conn) -> None:
    bad = Transition(
        table_version=1, from_state="A", to_state="B", trigger="T",
        guard_expr="confirmed_contract_exists",
        guard_inputs={"confirmed_contract_exists": "confirmed_contract_ref"},
        precedence=100,
        on_success={"to": "B", "emits": "OK"},
        on_guard_fail=None,
    )
    with pytest.raises(TransitionTableError):
        install_transition_table(conn, "run", 1, [bad], _registry())


def test_binding_mismatch_rejected(conn) -> None:
    bad = Transition(
        table_version=1, from_state="A", to_state="B", trigger="T",
        guard_expr="confirmed_contract_exists",
        guard_inputs={"confirmed_contract_exists": "WRONG_REF"},  # != declared_inputs
        precedence=100,
        on_success={"to": "B", "emits": "OK"},
        on_guard_fail={"to": "F", "emits": "GUARD_FAILED"},
    )
    with pytest.raises(TransitionTableError):
        install_transition_table(conn, "run", 1, [bad], _registry())


def test_missing_binding_rejected(conn) -> None:
    bad = Transition(
        table_version=1, from_state="A", to_state="B", trigger="T",
        guard_expr="confirmed_contract_exists",
        guard_inputs={},  # no binding at all
        precedence=100,
        on_success={"to": "B", "emits": "OK"},
        on_guard_fail={"to": "F", "emits": "GUARD_FAILED"},
    )
    with pytest.raises(TransitionTableError):
        install_transition_table(conn, "run", 1, [bad], _registry())


def test_malformed_on_success_rejected(conn) -> None:
    bad = Transition(
        table_version=1, from_state="A", to_state="B", trigger="T",
        guard_expr=None, guard_inputs={}, precedence=100,
        on_success={"to": "B"},  # missing 'emits'
        on_guard_fail=None,
    )
    with pytest.raises(TransitionTableError):
        install_transition_table(conn, "run", 1, [bad], _registry())


def test_unguarded_transition_installs(conn) -> None:
    ok = Transition(
        table_version=1, from_state="A", to_state="B", trigger="T",
        guard_expr=None, guard_inputs={}, precedence=100,
        on_success={"to": "B", "emits": "MOVED"},
        on_guard_fail=None,
    )
    install_transition_table(conn, "run", 1, [ok], _registry())
    table = load_transition_table(conn, "run", 1)
    assert table.transitions[0].guard_expr is None


def test_unknown_kind_rejected(conn) -> None:
    with pytest.raises(TransitionTableError):
        install_transition_table(conn, "bogus", 1, [_guarded()], _registry())


def test_db_pk_blocks_identical_rows(conn) -> None:
    install_transition_table(conn, "run", 1, [_guarded()], _registry())
    with pytest.raises(psycopg.errors.UniqueViolation):
        install_transition_table(conn, "run", 1, [_guarded()], _registry())
