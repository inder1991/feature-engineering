"""Authority predicate tree + pure evaluator (spec §4.1).

This is the WHAT-authority-a-value-has layer: a small, closed predicate language over
`(producer, strength)` evidence pairs. It answers "does the active evidence satisfy this authority
rule?" — it does NOT resolve WHO may confirm a fact (that stays in `authority.py`).

Two flat `any_of`/`all_of` fields are ambiguous (AND-of-OR vs OR?), so authority is expressed as a
closed, composable tree of three node types:

* ``HasEvidence(producer, strength)`` — a leaf, satisfied iff the active evidence set contains that
  exact ``(producer, strength)`` pair.
* ``AnyOf(conditions)`` — satisfied iff ANY child is satisfied (disjunction).
* ``AllOf(conditions)`` — satisfied iff ALL children are satisfied (conjunction).

``AnyOf``/``AllOf`` REJECT an empty ``conditions`` tuple at construction time: ``all([]) == True``
would silently authorize everything (and ``any([]) == False`` silently deny everything) — both are
footguns, so an empty tree is a ``ValueError`` (spec §4.1, review item 15).

Pure: no DB, no LLM, no I/O. ``evaluate`` is a total function of ``(predicate, active-set)``. The
caller builds the active set from ``lifecycle == ACTIVE`` evidence only (spec §4.1)."""

from __future__ import annotations

from dataclasses import dataclass

from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer


class AuthorityPredicate:
    """Base of the closed predicate tree; subclassed only by the three node types below."""

    __slots__ = ()


@dataclass(frozen=True, slots=True)
class HasEvidence(AuthorityPredicate):
    """Leaf: satisfied iff the active set contains this exact ``(producer, strength)`` pair."""

    producer: EvidenceProducer
    strength: AssertionStrength


@dataclass(frozen=True, slots=True)
class AnyOf(AuthorityPredicate):
    """Disjunction: satisfied iff ANY child is satisfied. Empty ``conditions`` is rejected."""

    conditions: tuple[AuthorityPredicate, ...]

    def __post_init__(self) -> None:
        if not self.conditions:
            raise ValueError("AnyOf requires at least one condition (empty AnyOf denies everything)")


@dataclass(frozen=True, slots=True)
class AllOf(AuthorityPredicate):
    """Conjunction: satisfied iff ALL children are satisfied. Empty ``conditions`` is rejected."""

    conditions: tuple[AuthorityPredicate, ...]

    def __post_init__(self) -> None:
        if not self.conditions:
            raise ValueError("AllOf requires at least one condition (empty AllOf authorizes everything)")


def evaluate(
    pred: AuthorityPredicate,
    active: frozenset[tuple[EvidenceProducer, AssertionStrength]],
) -> bool:
    """Evaluate ``pred`` against the ``active`` ``(producer, strength)`` evidence set (spec §4.1).

    Total and pure: ``HasEvidence`` → membership, ``AnyOf`` → any child, ``AllOf`` → all children.
    The ``active`` set must be built from ``lifecycle == ACTIVE`` evidence only."""
    if isinstance(pred, HasEvidence):
        return (pred.producer, pred.strength) in active
    if isinstance(pred, AnyOf):
        return any(evaluate(child, active) for child in pred.conditions)
    if isinstance(pred, AllOf):
        return all(evaluate(child, active) for child in pred.conditions)
    raise TypeError(f"unknown AuthorityPredicate node: {type(pred).__name__!r}")
