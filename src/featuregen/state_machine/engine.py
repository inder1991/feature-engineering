from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from featuregen.contracts import GuardInputs, GuardOutcome, PredicateRegistry
from featuregen.state_machine.transition_table import TransitionTable

GUARD_FAILED = "GUARD_FAILED"
TRANSITION_REJECTED = "TRANSITION_REJECTED"


@dataclass(frozen=True, slots=True)
class TransitionResult:
    matched: bool
    passed: bool
    from_state: str
    to_state: str
    trigger: str
    emitted_event_type: str
    selected_precedence: int | None
    guard_outcome: GuardOutcome | None
    audit_payload: Mapping[str, Any]


def _guard_block(guard_expr: str, outcome: GuardOutcome) -> dict[str, Any]:
    return {
        "guard_expr": guard_expr,
        "passed": outcome.passed,
        "resolved_inputs": dict(outcome.resolved_inputs),
        "per_predicate": dict(outcome.per_predicate),
    }


def evaluate_transition(
    table: TransitionTable,
    registry: PredicateRegistry,
    from_state: str,
    trigger: str,
    inputs: GuardInputs,
) -> TransitionResult:
    candidates = table.matches(from_state, trigger)
    if not candidates:
        return TransitionResult(
            matched=False,
            passed=False,
            from_state=from_state,
            to_state=from_state,
            trigger=trigger,
            emitted_event_type=TRANSITION_REJECTED,
            selected_precedence=None,
            guard_outcome=None,
            audit_payload={
                "from_state": from_state,
                "trigger": trigger,
                "reason": "no_matching_transition",
            },
        )
    selected = candidates[0]  # highest precedence; ties impossible (validated at install)
    if selected.guard_expr is None:
        to_state = selected.on_success["to"]
        return TransitionResult(
            matched=True,
            passed=True,
            from_state=from_state,
            to_state=to_state,
            trigger=trigger,
            emitted_event_type=selected.on_success["emits"],
            selected_precedence=selected.precedence,
            guard_outcome=None,
            audit_payload={
                "from_state": from_state,
                "to_state": to_state,
                "trigger": trigger,
                "guard": None,
            },
        )
    outcome = registry.evaluate(selected.guard_expr, inputs)
    guard = _guard_block(selected.guard_expr, outcome)
    if outcome.passed:
        to_state = selected.on_success["to"]
        emitted = selected.on_success["emits"]
    else:
        # No fall-through to lower precedence (§4.1): route to on_guard_fail.
        assert selected.on_guard_fail is not None  # guaranteed by install validation
        to_state = selected.on_guard_fail["to"]
        emitted = selected.on_guard_fail["emits"]
    return TransitionResult(
        matched=True,
        passed=outcome.passed,
        from_state=from_state,
        to_state=to_state,
        trigger=trigger,
        emitted_event_type=emitted,
        selected_precedence=selected.precedence,
        guard_outcome=outcome,
        audit_payload={
            "from_state": from_state,
            "to_state": to_state,
            "trigger": trigger,
            "guard": guard,
        },
    )
