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

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

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


# ===========================================================================
# Field-authority resolver (spec §4.2-4.4, §6.2): the WHICH-value-is-authoritative
# layer. The predicate tree above answers "does the active evidence satisfy this rule?";
# this layer answers, PER FIELD, "given the active evidence and a field policy, what is
# the display value, and is there a load-bearing (operational) value?".
#
# The load-bearing invariant: this resolver picks the authoritative value with a FIELD-SPECIFIC
# conflict strategy, NOT a single global max-strength. Two fields with the same evidence can
# resolve differently (prefer-confirmed vs most-restrictive vs union vs unresolved-on-conflict).
# ===========================================================================


@dataclass(frozen=True, slots=True)
class FieldEvidenceView:
    """A typed, lifecycle-filtered projection of one evidence record for a single field.

    Carries WHO asserted it (``producer``), HOW strongly (``strength``), the field ``value`` the
    record backs, and the source ``evidence_id`` for audit. This is NOT a raw ``(str, str)`` tuple:
    selection reasons about the producer/strength axis, so the enums are load-bearing (review
    must-fix). Views handed to the resolver are already filtered to ``lifecycle == ACTIVE``."""

    producer: EvidenceProducer
    strength: AssertionStrength
    value: str
    evidence_id: str


class ConflictStrategy(Enum):
    """HOW a field merges multiple active values into one (spec §4.3). Field-specific, never global:

    * ``PREFER_CONFIRMED`` — prefer the value backed by the highest strength
      (confirmed > attested > supported > proposed); distinct values tied at the top strength conflict.
    * ``MOST_RESTRICTIVE`` — for ordered fields (e.g. sensitivity), take the most severe value.
    * ``UNION_CLASSES`` — multi-valued fields: the sorted union of all values (never conflicts).
    * ``UNRESOLVED_ON_CONFLICT`` — one value only if all active values agree; otherwise a conflict."""

    PREFER_CONFIRMED = "prefer_confirmed"
    MOST_RESTRICTIVE = "most_restrictive"
    UNION_CLASSES = "union_classes"
    UNRESOLVED_ON_CONFLICT = "unresolved_on_conflict"


class InfluenceTier(Enum):
    """The MAXIMUM influence a field's value is permitted to have (spec §4.2), weakest → strongest.

    ``DISPLAY`` may only be shown; ``RECOMMENDATION`` may nudge; only ``OPERATIONAL`` may be
    load-bearing. A policy whose ``influence_max`` is below ``OPERATIONAL`` can never return a
    load-bearing value, however strong the evidence — the ceiling is enforced, not advisory."""

    DISPLAY = "display"
    RECOMMENDATION = "recommendation"
    OPERATIONAL = "operational"


class ResolutionMode(Enum):
    """How the field's operational truth is sourced (spec §4.4).

    ``GENERIC_FIELD`` — this resolver owns the operational value. ``SPECIALIZED_FACT`` — operational
    truth for grain/join/as-of comes from the specialized fact projection, NOT this resolver; here it
    is display-only (never load-bearing)."""

    GENERIC_FIELD = "generic_field"
    SPECIALIZED_FACT = "specialized_fact"


class Disqualifier(str, Enum):
    """A reason the selected evidence must NOT be treated as load-bearing even when the operational
    rule is satisfied (spec §6.2). Supplied by the caller as the ACTIVE set for the field; a policy
    lists which ones it honours. ``str`` mixin so ``sorted(...)`` over a set is deterministic.

    The lifecycle-derived disqualifiers cover evidence the caller kept in the active set but which a
    later lifecycle transition has compromised (drifted inputs, a gate rejection, a superseding record);
    ``OPEN_CONFLICT_REVIEW`` blocks while a conflict is under human review."""

    STALE_SELECTED_EVIDENCE = "stale_selected_evidence"
    REJECTED_SELECTED_EVIDENCE = "rejected_selected_evidence"
    SUPERSEDED_SELECTED_EVIDENCE = "superseded_selected_evidence"
    OPEN_CONFLICT_REVIEW = "open_conflict_review"


@dataclass(frozen=True, slots=True)
class FieldPolicy:
    """The per-field authority policy (spec §4.2-4.4). Immutable configuration, no evidence.

    * ``influence_max`` — the ceiling on how load-bearing this field may be (enforced).
    * ``display_rule`` — the (lenient) authority rule a value must satisfy to be shown at all.
    * ``operational_rule`` — the authority rule the active evidence must satisfy for a load-bearing
      value; ``None`` means the field is never operational.
    * ``disqualifiers`` — the ``Disqualifier`` reasons this field honours.
    * ``resolution_mode`` — ``GENERIC_FIELD`` (owned here) or ``SPECIALIZED_FACT`` (display-only).
    * ``conflict_strategy`` — the field-specific merge applied to the operational value.
    * ``severity_order`` — optional weakest→strongest order for ``MOST_RESTRICTIVE`` (Phase 0)."""

    influence_max: InfluenceTier
    display_rule: AuthorityPredicate
    operational_rule: AuthorityPredicate | None
    disqualifiers: tuple[Disqualifier, ...]
    resolution_mode: ResolutionMode
    conflict_strategy: ConflictStrategy
    severity_order: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FieldResolution:
    """The resolver's verdict for one field. ``display_value`` is what to show (or ``None``);
    ``load_bearing_value`` is the operational value or ``None`` when the field is not load-bearing;
    ``unresolved_reason`` names WHY there is no load-bearing value (``None`` when one was produced)."""

    display_value: str | None
    load_bearing_value: str | None
    unresolved_reason: str | None


class _Conflict:
    """Sentinel type: ``_select`` returns the singleton ``_CONFLICT`` when active values disagree
    irreconcilably. A distinct type (not ``object``) so ``isinstance`` narrows the value back to
    ``str`` on the non-conflict path for the type checker."""

    __slots__ = ()


_CONFLICT = _Conflict()

# Strength precedence for PREFER_CONFIRMED: proposed < supported < attested < confirmed.
_STRENGTH_RANK: dict[AssertionStrength, int] = {
    AssertionStrength.PROPOSED: 0,
    AssertionStrength.SUPPORTED: 1,
    AssertionStrength.ATTESTED: 2,
    AssertionStrength.CONFIRMED: 3,
}


def _select(
    evidence: Sequence[FieldEvidenceView],
    strategy: ConflictStrategy,
    severity_order: tuple[str, ...] = (),
) -> str | _Conflict:
    """Merge the active field values into ONE value per ``strategy`` — the field-specific core.

    Returns the chosen ``str`` value, or the ``_CONFLICT`` sentinel when the strategy cannot pick a
    single value (distinct values tied at the top strength, or disagreement under
    ``UNRESOLVED_ON_CONFLICT``). Pure and total over ``(evidence, strategy)``."""
    values = [e.value for e in evidence]
    if not values:
        return _CONFLICT
    if strategy is ConflictStrategy.PREFER_CONFIRMED:
        top = max(_STRENGTH_RANK[e.strength] for e in evidence)
        top_values = {e.value for e in evidence if _STRENGTH_RANK[e.strength] == top}
        return next(iter(top_values)) if len(top_values) == 1 else _CONFLICT
    if strategy is ConflictStrategy.MOST_RESTRICTIVE:
        distinct = set(values)
        if severity_order:
            return max(distinct, key=lambda v: severity_order.index(v) if v in severity_order else -1)
        return max(distinct)  # Phase 0: a simple ordered (lexicographic) compare
    if strategy is ConflictStrategy.UNION_CLASSES:
        return ",".join(sorted(set(values)))  # multi-valued: sorted union, never conflicts
    # UNRESOLVED_ON_CONFLICT
    distinct = set(values)
    return next(iter(distinct)) if len(distinct) == 1 else _CONFLICT


def resolve_field_authority(
    evidence: Sequence[FieldEvidenceView],
    policy: FieldPolicy,
    active_disqualifiers: frozenset[Disqualifier],
) -> FieldResolution:
    """Resolve one field to a ``FieldResolution`` (spec §4.2-4.4, §6.2).

    The ``evidence`` passed here is ALREADY lifecycle-filtered to ``ACTIVE``: stale / rejected /
    superseded records are excluded upstream, or surfaced as ``active_disqualifiers``. This resolver
    does NOT re-check lifecycle — it reasons only over the active views and the policy.

    Resolution order (short-circuits, most-blocking first):

    1. Build the active ``(producer, strength)`` set.
    2. ``display`` = the PREFER_CONFIRMED pick (lenient) if the ``display_rule`` passes, else ``None``.
    3. ``SPECIALIZED_FACT`` → display-only; operational truth lives in the specialized fact projection.
    4. ``influence_max`` below ``OPERATIONAL`` → never load-bearing (the ceiling is enforced).
    5. Any honoured disqualifier fired → blocked even though the rule may be satisfied.
    6. No / unsatisfied ``operational_rule`` → authority insufficient.
    7. Otherwise select the load-bearing value with the field's ``conflict_strategy``; a conflict
       leaves it unresolved."""
    active_pairs = frozenset((e.producer, e.strength) for e in evidence)

    display_sel: str | _Conflict | None = (
        _select(evidence, ConflictStrategy.PREFER_CONFIRMED)
        if evaluate(policy.display_rule, active_pairs)
        else None
    )
    # Display is lenient: a conflicting lenient pick shows nothing rather than the sentinel.
    display = None if isinstance(display_sel, _Conflict) else display_sel

    if policy.resolution_mode is ResolutionMode.SPECIALIZED_FACT:
        return FieldResolution(display, None, "specialized_fact")

    if policy.influence_max is not InfluenceTier.OPERATIONAL:
        return FieldResolution(display, None, "influence_not_operational")

    fired = active_disqualifiers & set(policy.disqualifiers)
    if fired:
        return FieldResolution(display, None, f"disqualified:{sorted(fired)[0]}")

    if policy.operational_rule is None or not evaluate(policy.operational_rule, active_pairs):
        return FieldResolution(display, None, "authority_insufficient")

    lb = _select(evidence, policy.conflict_strategy, policy.severity_order)
    if isinstance(lb, _Conflict):
        return FieldResolution(display, None, "conflict")
    return FieldResolution(display, lb, None)
