"""Phase 3C.2b-i-B · Task 8 — the closed operation-alias grammar (PURE).

Maps a raw LLM op string onto a canonical governable operation or a typed deferral/reject. This is a
CLOSED grammar — a module-level enumerated alias map, NO free-text regex — and TOTAL: everything not
matched by an explicit branch is ``operation_unrecognized``. It never touches the DB or the data plane
(the governed output-field derivation is the sibling ``b_output_policy`` module).

The single-operand shape (spec 3C.2b-i-B): a SUPPORTED op resolves to a per-operand
``PathAggregation`` (the roll-up along the operand's own path to the landing) plus
``FinalOperation.identity`` — for ONE operand the final expression IS that sole path, so the
combination is the identity. Two whole buckets are deliberately DEFERRED, not fabricated:

* time / windowed ops (``recency``/``trend`` + their aliases) — A has no cross-catalog time combine in
  this slice; a windowed op without a window collapses into the same bucket (the only windowed ops are
  these deferred ones), so a missing window is never silently treated as "no window";
* ordered ops (``ratio``/``difference`` + their aliases) — operand ORDER (numerator/denominator,
  minuend/subtrahend) must NEVER be inferred from operand position, name, or description; these are
  deferred until a governed ordered-intent exists.

``avg``/``stddev`` ARE recognized aggregations in A's ``PathAggregation``, but ``PATH_AGG_TO_FUNCTION``
maps them to ``None`` (no additive/order-safe analog — ungovernable), so B's closed grammar does not
emit them: they fall through to ``operation_unrecognized``.

Reuses (does NOT redefine) A's ``PathAggregation``/``FinalOperation`` (``multisource_contracts.py``)
and B's ``OPERATION_ALIAS_VERSION``/``BDisposition`` (``b_dispositions.py``). Modelled on Task 6's
``b_role_policy.py`` (versioned, frozenset/dict-enumerated, total, fail-closed).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from featuregen.overlay.upload.planner.b_dispositions import OPERATION_ALIAS_VERSION, BDisposition
from featuregen.overlay.upload.planner.multisource_contracts import FinalOperation, PathAggregation

__all__ = [
    "OPERATION_ALIAS_VERSION",
    "OperationReason",
    "SupportedOperation",
    "OperationRejection",
    "normalize_operation",
    "reason_to_b_disposition",
]


class OperationReason(StrEnum):
    """The typed rejection reason. Named 1:1 with the same-named ``BDisposition`` members so the
    telemetry vocabulary is fixed; ``reason_to_b_disposition`` folds each onto its twin."""
    operation_deferred = "operation_deferred"
    operand_order_authority_missing = "operand_order_authority_missing"
    operation_unrecognized = "operation_unrecognized"


@dataclass(frozen=True, slots=True)
class SupportedOperation:
    """A governable single-operand op: the per-operand ``path_aggregation`` + the ``final_operation``
    (always ``identity`` — a lone operand's own path aggregation does the roll-up)."""
    path_aggregation: PathAggregation
    final_operation: FinalOperation


@dataclass(frozen=True, slots=True)
class OperationRejection:
    reason: OperationReason


# ---------------------------------------------------------------------------
# The closed alias vocabularies (enumerated, auditable — no regex). Keys are casefolded+stripped.
# ---------------------------------------------------------------------------

# Supported single-operand aggregations -> their canonical PathAggregation. Every value is a
# PATH_AGG_TO_FUNCTION-governable aggregation (sum/min/max/count/count_distinct); avg/stddev are
# deliberately ABSENT (ungovernable -> unrecognized).
_SUPPORTED_AGGREGATION_ALIASES: dict[str, PathAggregation] = {
    "sum": PathAggregation.sum,
    "total": PathAggregation.sum,
    "min": PathAggregation.min,
    "minimum": PathAggregation.min,
    "max": PathAggregation.max,
    "maximum": PathAggregation.max,
    "count": PathAggregation.count,
    "count_distinct": PathAggregation.count_distinct,
    "distinct_count": PathAggregation.count_distinct,
    "n_distinct": PathAggregation.count_distinct,
}

# Deferred time / windowed ops (recency/trend + aliases). Absorbs the dropped
# WINDOW_REQUIRED_UNSPECIFIED case: the only windowed ops are these deferred ones.
_DEFERRED_TIME_ALIASES: frozenset[str] = frozenset({
    "recency", "trend", "days_since", "time_since", "growth", "slope", "velocity",
    "rolling", "moving", "over_time", "cumulative", "ytd", "mtd", "qtd", "lifetime",
})

# Ordered ops (ratio/difference + aliases): ordering must never be inferred from operand order.
_ORDERED_OP_ALIASES: frozenset[str] = frozenset({
    "ratio", "rate", "proportion", "share", "percent",
    "difference", "diff", "minus", "subtract", "net",
})


def normalize_operation(raw: str | None) -> SupportedOperation | OperationRejection:
    """Fold a raw LLM op string onto a ``SupportedOperation`` or a typed ``OperationRejection``.

    TOTAL, ordered, fail-closed: casefold+strip the input, then classify by the CLOSED alias maps —
    supported aggregation, then explicit deferred time ops, then explicit ordered ops, and only then
    the ``operation_unrecognized`` fallback (so ``avg``/``stddev``/compound/empty/None all land there).
    ``None``/empty are unrecognized. A window on the raw proposal is NOT this module's concern — it is
    captured in ``RawFeatureProposalV1.window`` (T2) and never consumed for a supported op."""
    if raw is None:
        return OperationRejection(OperationReason.operation_unrecognized)
    token = raw.casefold().strip()
    if token in _SUPPORTED_AGGREGATION_ALIASES:
        return SupportedOperation(
            path_aggregation=_SUPPORTED_AGGREGATION_ALIASES[token],
            final_operation=FinalOperation.identity)
    if token in _DEFERRED_TIME_ALIASES:
        return OperationRejection(OperationReason.operation_deferred)
    if token in _ORDERED_OP_ALIASES:
        return OperationRejection(OperationReason.operand_order_authority_missing)
    return OperationRejection(OperationReason.operation_unrecognized)


_REASON_TO_B_DISPOSITION: dict[OperationReason, BDisposition] = {
    OperationReason.operation_deferred: BDisposition.operation_deferred,
    OperationReason.operand_order_authority_missing: BDisposition.operand_order_authority_missing,
    OperationReason.operation_unrecognized: BDisposition.operation_unrecognized,
}


def reason_to_b_disposition(reason: OperationReason) -> BDisposition:
    """Fold an ``OperationReason`` onto its same-named ``BDisposition`` member (1:1)."""
    return _REASON_TO_B_DISPOSITION[reason]
