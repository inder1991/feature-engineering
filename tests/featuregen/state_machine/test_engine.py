from __future__ import annotations

from featuregen.state_machine.engine import (
    GUARD_FAILED,
    TRANSITION_REJECTED,
    evaluate_transition,
)
from featuregen.state_machine.guards import InMemoryPredicateRegistry
from featuregen.state_machine.transition_table import Transition, TransitionTable
from tests.featuregen.state_machine._predicates import truthy

INPUTS = {"confirmed_contract_ref": "doc_1"}


def _registry() -> InMemoryPredicateRegistry:
    reg = InMemoryPredicateRegistry()
    reg.register(truthy("confirmed_contract_exists", "confirmed_contract_ref"))
    # A SECOND, INDEPENDENT predicate over a DIFFERENT input — used by the
    # no-fall-through test's lower-precedence transition so it genuinely WOULD
    # pass on the same inputs that make the higher-precedence guard fail.
    reg.register(truthy("alt_ready", "alt_ready_ref"))
    return reg


def _guarded(table_version: int, to_state: str, emits: str, precedence: int = 100) -> Transition:
    return Transition(
        table_version=table_version,
        from_state="CONFIRMED_CONTRACT",
        to_state=to_state,
        trigger="MAPPING_COMPLETED",
        guard_expr="confirmed_contract_exists",
        guard_inputs={"confirmed_contract_exists": "confirmed_contract_ref"},
        precedence=precedence,
        on_success={"to": to_state, "emits": emits},
        on_guard_fail={"to": "MAPPING_REVIEW_FAILED", "emits": GUARD_FAILED},
    )


def _alt_guarded(to_state: str, emits: str, precedence: int) -> Transition:
    """A lower-precedence transition on the SAME (from_state, trigger) whose guard
    reads a DIFFERENT input. With the no-fall-through inputs below it evaluates True,
    so any regression that fell through to it would change the emitted event/state."""
    return Transition(
        table_version=1,
        from_state="CONFIRMED_CONTRACT",
        to_state=to_state,
        trigger="MAPPING_COMPLETED",
        guard_expr="alt_ready",
        guard_inputs={"alt_ready": "alt_ready_ref"},
        precedence=precedence,
        on_success={"to": to_state, "emits": emits},
        on_guard_fail={"to": "ALT_FAILED", "emits": GUARD_FAILED},
    )


def test_guard_pass_emits_on_success() -> None:
    table = TransitionTable("run", 1, (_guarded(1, "SCHEMA_MAPPED", "FEATURE_MAPPED"),))
    res = evaluate_transition(table, _registry(), "CONFIRMED_CONTRACT", "MAPPING_COMPLETED", INPUTS)
    assert res.matched and res.passed
    assert res.to_state == "SCHEMA_MAPPED"
    assert res.emitted_event_type == "FEATURE_MAPPED"
    assert res.selected_precedence == 100
    assert res.audit_payload["guard"]["passed"] is True
    assert res.audit_payload["guard"]["resolved_inputs"] == {"confirmed_contract_ref": "doc_1"}


def test_guard_fail_emits_guard_failed_no_fallthrough() -> None:
    # The high (prec 100) transition's guard `confirmed_contract_exists` FAILS
    # (confirmed_contract_ref == ""). The low (prec 50) transition's guard
    # `alt_ready` reads a DIFFERENT input (alt_ready_ref == "ready") and WOULD
    # PASS. "No fall-through" (§4.1) means the engine commits to the single
    # highest-precedence match and emits its GUARD_FAILED — it must NOT drop to
    # the passing prec-50 transition. A regression that re-introduced fall-through
    # would instead emit "ALT_EVENT" / "ALT_STATE", so this test now discriminates.
    high = _guarded(1, "SCHEMA_MAPPED", "FEATURE_MAPPED", precedence=100)
    low = _alt_guarded("ALT_STATE", "ALT_EVENT", precedence=50)
    table = TransitionTable("run", 1, (high, low))
    res = evaluate_transition(
        table, _registry(), "CONFIRMED_CONTRACT", "MAPPING_COMPLETED",
        {"confirmed_contract_ref": "", "alt_ready_ref": "ready"},
    )
    assert res.matched and not res.passed
    assert res.selected_precedence == 100
    assert res.emitted_event_type == GUARD_FAILED
    assert res.to_state == "MAPPING_REVIEW_FAILED"
    assert res.emitted_event_type != "ALT_EVENT"   # would-pass prec-50 was NOT chosen
    assert res.to_state != "ALT_STATE"
    assert res.audit_payload["guard"]["passed"] is False


def test_no_matching_transition_is_rejected() -> None:
    table = TransitionTable("run", 1, (_guarded(1, "SCHEMA_MAPPED", "FEATURE_MAPPED"),))
    res = evaluate_transition(table, _registry(), "SOME_OTHER_STATE", "MAPPING_COMPLETED", INPUTS)
    assert not res.matched and not res.passed
    assert res.emitted_event_type == TRANSITION_REJECTED
    assert res.to_state == "SOME_OTHER_STATE"
    assert res.audit_payload["reason"] == "no_matching_transition"


def test_unguarded_transition_passes() -> None:
    t = Transition(
        table_version=1, from_state="A", to_state="B", trigger="T",
        guard_expr=None, guard_inputs={}, precedence=100,
        on_success={"to": "B", "emits": "MOVED"}, on_guard_fail=None,
    )
    res = evaluate_transition(TransitionTable("run", 1, (t,)), _registry(), "A", "T", {})
    assert res.passed and res.to_state == "B" and res.emitted_event_type == "MOVED"
    assert res.audit_payload["guard"] is None


def test_table_version_pinning_resolves_differently() -> None:
    reg = _registry()
    v1 = TransitionTable("run", 1, (_guarded(1, "SCHEMA_MAPPED", "FEATURE_MAPPED"),))
    v2 = TransitionTable("run", 2, (_guarded(2, "OTHER_STATE", "OTHER_EVENT"),))
    r1 = evaluate_transition(v1, reg, "CONFIRMED_CONTRACT", "MAPPING_COMPLETED", INPUTS)
    r2 = evaluate_transition(v2, reg, "CONFIRMED_CONTRACT", "MAPPING_COMPLETED", INPUTS)
    assert r1.to_state == "SCHEMA_MAPPED" and r1.emitted_event_type == "FEATURE_MAPPED"
    assert r2.to_state == "OTHER_STATE" and r2.emitted_event_type == "OTHER_EVENT"
