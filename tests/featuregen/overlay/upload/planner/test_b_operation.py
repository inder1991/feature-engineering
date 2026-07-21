"""Phase 3C.2b-i-B · Task 8 — the closed operation-alias grammar (PURE, no DB).

Asserts ``normalize_operation`` is a TOTAL, case-insensitive fold of a raw LLM op string onto either
a ``SupportedOperation`` (a governable single-operand path aggregation + IDENTITY final op) or a
typed ``OperationRejection`` — deferred time ops, ordered ops (never inferred from operand order),
and everything else unrecognized. ``reason_to_b_disposition`` maps each reason to its same-named
``BDisposition`` member.
"""
from featuregen.overlay.upload.planner.b_dispositions import BDisposition
from featuregen.overlay.upload.planner.b_operation import (
    OperationReason,
    OperationRejection,
    SupportedOperation,
    normalize_operation,
    reason_to_b_disposition,
)
from featuregen.overlay.upload.planner.multisource_contracts import (
    FinalOperation,
    PathAggregation,
)


def test_sum_and_total_map_to_sum_identity_case_insensitive():
    for raw in ("sum", "total", "SUM", " Sum ", "TOTAL", "\tsum\n"):
        assert normalize_operation(raw) == SupportedOperation(
            PathAggregation.sum, FinalOperation.identity), raw


def test_min_max_count_count_distinct_with_one_alias_each():
    cases = {
        "min": PathAggregation.min,
        "minimum": PathAggregation.min,
        "max": PathAggregation.max,
        "maximum": PathAggregation.max,
        "count": PathAggregation.count,
        "count_distinct": PathAggregation.count_distinct,
        "distinct_count": PathAggregation.count_distinct,
        "n_distinct": PathAggregation.count_distinct,
    }
    for raw, expected_agg in cases.items():
        assert normalize_operation(raw) == SupportedOperation(
            expected_agg, FinalOperation.identity), raw


def test_time_ops_are_deferred():
    for raw in ("recency", "trend", "days_since", "rolling", "YTD", "over_time", "cumulative"):
        assert normalize_operation(raw) == OperationRejection(
            OperationReason.operation_deferred), raw


def test_ordered_ops_reject_missing_order_authority():
    for raw in ("ratio", "difference", "share", "percent", "minus", "subtract", "net", "diff"):
        assert normalize_operation(raw) == OperationRejection(
            OperationReason.operand_order_authority_missing), raw


def test_avg_stddev_unknown_compound_empty_and_none_are_unrecognized():
    for raw in ("avg", "stddev", "frobnicate", "", None, "sum_ratio", "take_latest"):
        assert normalize_operation(raw) == OperationRejection(
            OperationReason.operation_unrecognized), raw


def test_reason_to_b_disposition_maps_each_reason_to_same_named_member():
    assert (reason_to_b_disposition(OperationReason.operation_deferred)
            is BDisposition.operation_deferred)
    assert (reason_to_b_disposition(OperationReason.operand_order_authority_missing)
            is BDisposition.operand_order_authority_missing)
    assert (reason_to_b_disposition(OperationReason.operation_unrecognized)
            is BDisposition.operation_unrecognized)


def test_supported_operations_always_use_identity_final_op():
    for raw in ("sum", "min", "max", "count", "count_distinct"):
        op = normalize_operation(raw)
        assert isinstance(op, SupportedOperation)
        assert op.final_operation is FinalOperation.identity, raw
