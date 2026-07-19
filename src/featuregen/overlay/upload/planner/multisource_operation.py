"""Phase 3C.2b-i-A · Task 3 — the closed operation→slot→path-strategy matrix + shape validation.

The single source of truth for spec §4: ``OPERATION_MATRIX`` maps every ``FinalOperation`` onto a
frozen ``OperationSpec`` (the ordered non-time roles, the optional TIME role, whether a ``window`` is
required, and the exact allowed per-slot ``PathAggregation`` set). ``validate_operation_shape`` runs
the §4 checks with EXACT role→slot validation (multiset of operand roles must equal the matrix's
required roles; each ``ordered_slot_id``/``time_slot_id`` in the final expression must reference a
real, correctly-roled, distinct operand; no duplicate operand ``slot_id``; ``window``/``time_slot_id``
present iff required; a ``stddev`` operand fails closed as ``unsupported_path_aggregation``;
``take_latest`` requires an ``ordering_anchor_concept`` else ``ordering_anchor_missing``).

Pure + deterministic — no DB, no I/O, no pydantic. It consumes ONLY the Task 2 contracts."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from featuregen.overlay.upload.planner.multisource_contracts import (
    FinalOperation,
    MultiSourcePlannerIntentV1,
    MultiSourceReason,
    PathAggregation,
    SemanticRole,
)

# ---------------------------------------------------------------------------
# Allowed per-slot PathAggregation sets (spec §4). Named once, reused across the matrix.
# ---------------------------------------------------------------------------

# IDENTITY measure — the additive/order-safe measures (avg is validated via its additive-decomposable
# components, spec §4). take_latest is deliberately EXCLUDED here (it is order-, not value-, sensitive).
_MEASURE_FULL: frozenset[PathAggregation] = frozenset(
    {PathAggregation.avg, PathAggregation.sum, PathAggregation.min, PathAggregation.max}
)
# RATIO/DIFFERENCE operands additionally allow take_latest (the canonical AVG(txn)/latest(balance)).
_OPERAND_FULL: frozenset[PathAggregation] = _MEASURE_FULL | {PathAggregation.take_latest}
# TREND measure is narrower — only the two additive roll-ups.
_TREND_MEASURE: frozenset[PathAggregation] = frozenset({PathAggregation.avg, PathAggregation.sum})
_TIME_LATEST: frozenset[PathAggregation] = frozenset({PathAggregation.take_latest})

# stddev is not resolvable initially (no additive/order-safe analog); every operand carrying it fails
# closed as UNSUPPORTED_PATH_AGGREGATION regardless of slot (spec §4). Closed set — deferred work
# would add members here, never widen an allowed set.
UNSUPPORTED_PATH_AGGREGATIONS: frozenset[PathAggregation] = frozenset({PathAggregation.stddev})


# ---------------------------------------------------------------------------
# Matrix entry (spec §4).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OperationSpec:
    """One row of the closed spec §4 matrix.

    ``ordered_roles`` are the non-TIME roles, in the exact sequence the final expression's
    ``ordered_slot_ids`` must present them (order-sensitive for RATIO/DIFFERENCE). ``time_role`` is the
    single TIME role a ``time_slot_id`` must reference, or ``None``. ``requires_window`` is True iff a
    ``window`` must be present. ``allowed_aggregations`` pins the allowed ``PathAggregation`` set per
    role (stored as pairs so the entry stays fully hashable/immutable)."""

    operation: FinalOperation
    ordered_roles: tuple[SemanticRole, ...]
    time_role: SemanticRole | None
    requires_window: bool
    allowed_aggregations: tuple[tuple[SemanticRole, frozenset[PathAggregation]], ...]

    @property
    def required_roles(self) -> tuple[SemanticRole, ...]:
        """The full multiset of operand roles this operation requires (ordered roles + TIME role)."""
        if self.time_role is None:
            return self.ordered_roles
        return (*self.ordered_roles, self.time_role)

    def allowed_for(self, role: SemanticRole) -> frozenset[PathAggregation]:
        """The allowed ``PathAggregation`` set for ``role`` (empty if the role is not part of this op)."""
        for candidate_role, allowed in self.allowed_aggregations:
            if candidate_role == role:
                return allowed
        return frozenset()


# The matrix — total over FinalOperation, closed (spec §4). Order-sensitivity is carried by the two
# distinct ``ordered_roles`` of RATIO/DIFFERENCE; there is no set-membership shortcut anywhere.
OPERATION_MATRIX: dict[FinalOperation, OperationSpec] = {
    FinalOperation.identity: OperationSpec(
        operation=FinalOperation.identity,
        ordered_roles=(SemanticRole.measure,),
        time_role=None,
        requires_window=False,
        allowed_aggregations=((SemanticRole.measure, _MEASURE_FULL),),
    ),
    FinalOperation.count: OperationSpec(
        operation=FinalOperation.count,
        ordered_roles=(SemanticRole.counted,),
        time_role=None,
        requires_window=False,
        allowed_aggregations=((SemanticRole.counted, frozenset({PathAggregation.count})),),
    ),
    FinalOperation.count_distinct: OperationSpec(
        operation=FinalOperation.count_distinct,
        ordered_roles=(SemanticRole.counted,),
        time_role=None,
        requires_window=False,
        allowed_aggregations=(
            (SemanticRole.counted, frozenset({PathAggregation.count_distinct})),
        ),
    ),
    FinalOperation.recency: OperationSpec(
        operation=FinalOperation.recency,
        ordered_roles=(),
        time_role=SemanticRole.time,
        requires_window=False,
        allowed_aggregations=((SemanticRole.time, _TIME_LATEST),),
    ),
    FinalOperation.trend: OperationSpec(
        operation=FinalOperation.trend,
        ordered_roles=(SemanticRole.measure,),
        time_role=SemanticRole.time,
        requires_window=True,
        allowed_aggregations=(
            (SemanticRole.measure, _TREND_MEASURE),
            (SemanticRole.time, _TIME_LATEST),
        ),
    ),
    FinalOperation.ratio: OperationSpec(
        operation=FinalOperation.ratio,
        ordered_roles=(SemanticRole.numerator, SemanticRole.denominator),
        time_role=None,
        requires_window=False,
        allowed_aggregations=(
            (SemanticRole.numerator, _OPERAND_FULL),
            (SemanticRole.denominator, _OPERAND_FULL),
        ),
    ),
    FinalOperation.difference: OperationSpec(
        operation=FinalOperation.difference,
        ordered_roles=(SemanticRole.minuend, SemanticRole.subtrahend),
        time_role=None,
        requires_window=False,
        allowed_aggregations=(
            (SemanticRole.minuend, _OPERAND_FULL),
            (SemanticRole.subtrahend, _OPERAND_FULL),
        ),
    ),
}


# ---------------------------------------------------------------------------
# validate_operation_shape (spec §4/§9).
# ---------------------------------------------------------------------------


def validate_operation_shape(
    intent: MultiSourcePlannerIntentV1,
) -> MultiSourceReason | None:
    """Validate an intent against the closed §4 matrix. Returns ``None`` when the shape is valid, or
    the single first-encountered reject reason: ``unsupported_path_aggregation`` (a stddev operand),
    ``ordering_anchor_missing`` (a take_latest operand without an ``ordering_anchor_concept``), or
    ``operand_shape_invalid`` (every structural violation — wrong operation, duplicate/absent/miswired
    slots, wrong role multiset, disallowed per-slot aggregation, mismatched window/time presence).

    Pure + deterministic: the same intent always yields the same reason."""
    final = intent.final_expression
    operands = intent.operands

    # The matrix is closed: an operation not present in it cannot be shaped.
    spec = OPERATION_MATRIX.get(final.operation)
    if spec is None:
        return MultiSourceReason.operand_shape_invalid

    # No duplicate operand slot_id (the slot_id is the operand's identity everywhere downstream).
    slot_ids = [operand.slot_id for operand in operands]
    if len(set(slot_ids)) != len(slot_ids):
        return MultiSourceReason.operand_shape_invalid
    by_id = {operand.slot_id: operand for operand in operands}

    # A globally-unsupported path aggregation (stddev) fails closed regardless of slot (spec §4).
    for operand in operands:
        if operand.path_strategy.aggregation in UNSUPPORTED_PATH_AGGREGATIONS:
            return MultiSourceReason.unsupported_path_aggregation

    # Exact role multiset — not set membership (spec §4).
    if Counter(operand.semantic_role for operand in operands) != Counter(spec.required_roles):
        return MultiSourceReason.operand_shape_invalid

    # ordered_slot_ids: exact arity, distinct, each a real operand of the correctly-ordered role.
    ordered = final.ordered_slot_ids
    if len(ordered) != len(spec.ordered_roles):
        return MultiSourceReason.operand_shape_invalid
    if len(set(ordered)) != len(ordered):
        return MultiSourceReason.operand_shape_invalid
    for slot_id, role in zip(ordered, spec.ordered_roles):
        ordered_operand = by_id.get(slot_id)
        if ordered_operand is None or ordered_operand.semantic_role != role:
            return MultiSourceReason.operand_shape_invalid

    # time_slot_id: present iff a TIME role is required; when present it must reference a real, distinct
    # TIME operand (never one already consumed as an ordered slot).
    if spec.time_role is None:
        if final.time_slot_id is not None:
            return MultiSourceReason.operand_shape_invalid
    else:
        time_slot_id = final.time_slot_id
        if time_slot_id is None or time_slot_id in ordered:
            return MultiSourceReason.operand_shape_invalid
        time_operand = by_id.get(time_slot_id)
        if time_operand is None or time_operand.semantic_role != spec.time_role:
            return MultiSourceReason.operand_shape_invalid

    # window present iff the operation requires it (TREND only).
    if spec.requires_window != (final.window is not None):
        return MultiSourceReason.operand_shape_invalid

    # Allowed per-slot aggregation (role multiset already verified, so every operand has a real role).
    for operand in operands:
        if operand.path_strategy.aggregation not in spec.allowed_for(operand.semantic_role):
            return MultiSourceReason.operand_shape_invalid

    # take_latest ⇒ its operand's path strategy must carry an ordering anchor concept.
    for operand in operands:
        if operand.path_strategy.aggregation is PathAggregation.take_latest:
            if not operand.path_strategy.ordering_anchor_concept:
                return MultiSourceReason.ordering_anchor_missing

    return None
