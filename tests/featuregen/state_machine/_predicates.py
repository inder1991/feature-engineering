from __future__ import annotations

from dataclasses import dataclass

from featuregen.contracts import GuardInputs


@dataclass(frozen=True, slots=True)
class TruthyPredicate:
    """Pure predicate: returns bool(inputs[key]) over its single declared input."""

    name: str
    declared_inputs: tuple[str, ...]
    key: str

    def __call__(self, inputs: GuardInputs) -> bool:
        return bool(inputs[self.key])


@dataclass(frozen=True, slots=True)
class PeekingPredicate:
    """Declares one input but illegally reads another — used to prove the registry
    mechanically blocks reads outside declared_inputs (the access raises KeyError)."""

    name: str
    declared_inputs: tuple[str, ...]
    peek_key: str

    def __call__(self, inputs: GuardInputs) -> bool:
        return bool(inputs[self.peek_key])


def truthy(name: str, key: str) -> TruthyPredicate:
    return TruthyPredicate(name=name, declared_inputs=(key,), key=key)
