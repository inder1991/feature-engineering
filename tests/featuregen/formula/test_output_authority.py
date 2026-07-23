"""Task 6 — operation map + corrected additivity + C1 output-authority resolver.

The authoritative output policy is resolved ONLY from C1 governed facts (the ``per_expr_facts``
``OperationalValue`` bundles seeded through the REAL governed path by the Task-5 fixtures) — NEVER
from the proposal's advisory ``expected_output``. See ``docs/superpowers/specs/...child1...`` §B
(operation map), §C (the operation-specific required-field matrix over C1) and §D (corrected
additivity).
"""
from __future__ import annotations

import pytest

from featuregen.formula.operations import to_path_aggregation
from featuregen.formula.output_authority import (
    ExprFacts,
    PartitionProof,
    formula_additivity,
)
from featuregen.formula.schema import (
    AdditivityClass,
    AggregateExpression,
    AggregateFunction,
    DiffBody,
    SourceRelation,
    UnaryBody,
)
from featuregen.overlay.field_authority import InfluenceTier
from featuregen.overlay.upload.operational_facts import (
    OperationalValue,
    read_operational_value,
)
from featuregen.overlay.upload.planner.multisource_contracts import PathAggregation

from tests.featuregen.formula.factories import (
    AMOUNT_REF,
    TABLE_REF,
    ratio_of_sums,
    sum_expression,
    trailing_90d_window,
)


# ── small builders ────────────────────────────────────────────────────────────────────────────────
def _ov(status: str, value: object | None) -> OperationalValue:
    """A hand-built ``OperationalValue`` for a pure-logic (no-DB) additivity test — the same shape
    C1 returns. The DB-backed resolver tests below use the REAL Task-5 fixtures instead."""
    return OperationalValue(
        value=value, influence=InfluenceTier.DISPLAY, producer=None, strength=None, status=status,
        conflict_status=None, selected_evidence_ids=(), decision_event_id="d1", fact_key=None,
        fact_event_id=None, policy_version="v", resolver_version="v")


def _unary(aggregation: AggregateFunction, *, operand: str | None = AMOUNT_REF) -> UnaryBody:
    return UnaryBody(expr=AggregateExpression(
        aggregation=aggregation, operand=operand,
        source_relation=SourceRelation(table_ref=TABLE_REF), filter=None,
        window=trailing_90d_window()))


def _facts(**fields: OperationalValue) -> ExprFacts:
    return ExprFacts(**fields)


# ── §D corrected additivity ───────────────────────────────────────────────────────────────────────
def test_count_distinct_is_non_additive():
    """§D — the BUG fix: a distinct count does NOT sum across partitions (``b_output_policy`` wrongly
    marks it ``additive``). Default (no disjointness proof) → NON_ADDITIVE."""
    body = _unary(AggregateFunction.COUNT_DISTINCT)
    result = formula_additivity(body, per_expr_facts={}, partition_proof=PartitionProof())
    assert result is AdditivityClass.NON_ADDITIVE


def test_count_distinct_additive_only_with_proven_disjoint_values():
    body = _unary(AggregateFunction.COUNT_DISTINCT)
    proof = PartitionProof(disjoint_distinct_values=True)
    assert formula_additivity(body, per_expr_facts={}, partition_proof=proof) \
        is AdditivityClass.ADDITIVE


def test_count_rows_additive_only_across_disjoint_partitions():
    body = _unary(AggregateFunction.COUNT_ROWS, operand=None)
    assert formula_additivity(body, per_expr_facts={}, partition_proof=PartitionProof()) \
        is AdditivityClass.NON_ADDITIVE
    proof = PartitionProof(disjoint_row_partitions=True)
    assert formula_additivity(body, per_expr_facts={}, partition_proof=proof) \
        is AdditivityClass.ADDITIVE


def test_ratio_is_non_additive():
    body = ratio_of_sums()
    assert formula_additivity(body, per_expr_facts={}, partition_proof=PartitionProof()) \
        is AdditivityClass.NON_ADDITIVE


def test_difference_is_non_additive():
    body = DiffBody(minuend=sum_expression(), subtrahend=sum_expression())
    assert formula_additivity(body, per_expr_facts={}, partition_proof=PartitionProof()) \
        is AdditivityClass.NON_ADDITIVE


def test_sum_carries_governed_additive_input_with_a_path_proof():
    body = _unary(AggregateFunction.SUM)
    facts = {"body.expr": _facts(additivity=_ov("resolved", "additive"))}
    proof = PartitionProof(path_additive=True)
    assert formula_additivity(body, per_expr_facts=facts, partition_proof=proof) \
        is AdditivityClass.ADDITIVE
    # …but without the path proof an additive input degrades conservatively.
    assert formula_additivity(body, per_expr_facts=facts, partition_proof=PartitionProof()) \
        is AdditivityClass.NON_ADDITIVE


# ── §B operation → path-aggregation compatibility map ─────────────────────────────────────────────
@pytest.mark.parametrize(
    ("fn", "expected"),
    [
        (AggregateFunction.SUM, PathAggregation.sum),
        (AggregateFunction.COUNT_ROWS, PathAggregation.count),
        (AggregateFunction.COUNT_NON_NULL, PathAggregation.count),
        (AggregateFunction.COUNT_DISTINCT, PathAggregation.count_distinct),
    ],
)
def test_to_path_aggregation_maps_the_supported_vocabulary(fn, expected):
    assert to_path_aggregation(fn) is expected
