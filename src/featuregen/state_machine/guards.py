from __future__ import annotations

from typing import Any

from featuregen.contracts import GuardInputs, GuardOutcome, GuardPredicate
from featuregen.state_machine.guard_expr import (
    eval_guard_expr,
    parse_guard_expr,
    predicate_names,
)


class PredicateRegistrationError(Exception):
    """Raised on duplicate predicate registration (load-time error, §4.1)."""


class InMemoryPredicateRegistry:
    """Concrete `PredicateRegistry` (§4.1). Pure: predicates receive only their
    declared inputs, so they cannot read mutable projections or undeclared keys."""

    def __init__(self) -> None:
        self._predicates: dict[str, GuardPredicate] = {}

    def register(self, predicate: GuardPredicate) -> None:
        if predicate.name in self._predicates:
            raise PredicateRegistrationError(f"predicate {predicate.name!r} already registered")
        self._predicates[predicate.name] = predicate

    def get(self, name: str) -> GuardPredicate:
        try:
            return self._predicates[name]
        except KeyError:
            raise KeyError(f"predicate {name!r} not registered") from None

    def evaluate(self, guard_expr: str, inputs: GuardInputs) -> GuardOutcome:
        node = parse_guard_expr(guard_expr)
        per_predicate: dict[str, bool] = {}
        resolved: dict[str, Any] = {}
        for name in sorted(predicate_names(node)):
            predicate = self.get(name)
            view: dict[str, Any] = {}
            for key in predicate.declared_inputs:
                if key not in inputs:
                    raise KeyError(f"guard input {key!r} for predicate {name!r} not resolved")
                view[key] = inputs[key]
            per_predicate[name] = bool(predicate(view))
            resolved.update(view)
        passed = eval_guard_expr(node, per_predicate)
        return GuardOutcome(
            passed=passed,
            resolved_inputs=resolved,
            per_predicate=per_predicate,
        )
