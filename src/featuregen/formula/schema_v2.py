"""BR-6 increment 1 — Formula-v2 structural schema, BESIDE the immutable v1.

The versioning law this module exists to keep: **Formula-v1 stays frozen** — its schema,
canonicalization and every stored hash are untouched by v2's existence, which is why the v2 body
types are their OWN dataclasses even where they mirror v1 shape-for-shape (a shared body type
would let a v2 evolution move v1 identity). The structural LEAVES that carry no versioned
vocabulary — refs, filters, windows, grains, parameters, decimal policy — are imported from v1
verbatim: they are frozen with it, and duplicating them would fork validation the two grammars
must share.

Increment 1's operation vocabulary: the v1 four (sum / count_rows / count_non_null /
count_distinct) plus the first NEW group — **min, max, avg** — under the same body shapes
(identity / ratio / difference). Every further group (lag/delta, stddev/z-score,
percentile/median, slope, streak, concentration, effective-dated lookup, conversion, rollups)
lands as its own reviewed increment in ``operations_v2.py``, each with gold cases, per the plan's
"small increments" rule. An operation outside the vocabulary is UNSUPPORTED — classified, never
approximated.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from featuregen.formula.schema import (
    MAX_PREDICATES,
    AdditivityClass,
    DecimalPolicy,
    FilterNode,
    Grain,
    LogicalRef,
    ParameterDecl,
    SchemaError,
    SourceRelation,
    WindowPolicy,
    _check_filter_node,
    _require_column_ref,
    _require_contained_column,
    _require_table_ref,
)

FORMULA_SCHEMA_VERSION_V2 = 2
OPERATION_GRAMMAR_VERSION_V2 = 1
CANONICALIZATION_VERSION_V2 = 1


class AggregateFunctionV2(StrEnum):
    """The v2 per-expression aggregate vocabulary — v1's four plus increment 1's group."""

    SUM = "sum"
    COUNT_ROWS = "count_rows"
    COUNT_NON_NULL = "count_non_null"
    COUNT_DISTINCT = "count_distinct"
    MIN = "min"
    MAX = "max"
    AVG = "avg"
    # increment 2 — the distributional group
    RECENCY = "recency"          # duration since the latest in-window event, at the cutoff
    STDDEV = "stddev"
    PERCENTILE = "percentile"    # requires aggregation_argument p ∈ (0, 100)
    MEDIAN = "median"            # percentile 50 as its own authored name — no argument


class FinalOperationV2(StrEnum):
    IDENTITY = "identity"
    RATIO = "ratio"
    DIFFERENCE = "difference"


@dataclass(frozen=True, slots=True)
class AggregateExpressionV2:
    aggregation: AggregateFunctionV2
    operand: LogicalRef | None            # None IFF aggregation == COUNT_ROWS
    source_relation: SourceRelation
    filter: FilterNode | None
    window: WindowPolicy
    # increment 2: the aggregate's own argument — REQUIRED for percentile (p ∈ (0,100),
    # exclusive), FORBIDDEN for every other operation. A parameterized aggregate carries its
    # parameter in identity, never in a generic label.
    aggregation_argument: float | None = None


@dataclass(frozen=True, slots=True)
class UnaryBodyV2:
    expr: AggregateExpressionV2
    final_operation: FinalOperationV2 = field(default=FinalOperationV2.IDENTITY, init=False)


@dataclass(frozen=True, slots=True)
class RatioBodyV2:
    numerator: AggregateExpressionV2
    denominator: AggregateExpressionV2
    zero_denominator: str                 # v1 ZeroDenominator value, kept as its string
    final_operation: FinalOperationV2 = field(default=FinalOperationV2.RATIO, init=False)


@dataclass(frozen=True, slots=True)
class DiffBodyV2:
    minuend: AggregateExpressionV2
    subtrahend: AggregateExpressionV2
    final_operation: FinalOperationV2 = field(default=FinalOperationV2.DIFFERENCE, init=False)


FormulaBodyV2 = UnaryBodyV2 | RatioBodyV2 | DiffBodyV2


@dataclass(frozen=True, slots=True)
class TypedFormulaProposalV2:
    """The v2 proposal, mirroring v1's field set with the version triple pinned to v2. The
    version fields are DATA (validated == the v2 pins), never inferred — the dispatch rule."""

    formula_schema_version: int
    operation_grammar_version: int
    canonicalization_version: int
    grain: Grain
    body: FormulaBodyV2
    parameters: tuple[ParameterDecl, ...]
    decimal: DecimalPolicy
    expected_output: object | None


def _check_expression_v2(expr: AggregateExpressionV2, path: str,
                         params: dict[str, ParameterDecl]) -> None:
    if not isinstance(expr.aggregation, AggregateFunctionV2):
        raise SchemaError(f"{path}.aggregation: not a v2 aggregate: {expr.aggregation!r}")
    _require_table_ref(expr.source_relation.table_ref, f"{path}.source_relation.table_ref")
    if expr.aggregation is AggregateFunctionV2.PERCENTILE:
        if not isinstance(expr.aggregation_argument, (int, float)) or isinstance(
                expr.aggregation_argument, bool) or not 0 < expr.aggregation_argument < 100:
            raise SchemaError(
                f"{path}.aggregation_argument: percentile requires p strictly between 0 and "
                f"100, got {expr.aggregation_argument!r}")
    elif expr.aggregation_argument is not None:
        raise SchemaError(
            f"{path}.aggregation_argument: {expr.aggregation.value} takes no argument")
    if expr.aggregation is AggregateFunctionV2.COUNT_ROWS:
        if expr.operand is not None:
            raise SchemaError(f"{path}.operand: count_rows carries no operand")
    else:
        if expr.operand is None:
            raise SchemaError(f"{path}.operand: {expr.aggregation.value} requires an operand")
        _require_column_ref(expr.operand, f"{path}.operand")
        _require_contained_column(expr.operand, f"{path}.operand",
                                  expr.source_relation.table_ref)
    if expr.filter is not None:
        count = _check_filter_node(expr.filter, f"{path}.filter", 1,
                                   expr.source_relation.table_ref, params)
        if count > MAX_PREDICATES:
            raise SchemaError(f"{path}.filter: too many predicates ({count})")
    _require_column_ref(expr.window.event_time_ref, f"{path}.window.event_time_ref")
    _require_contained_column(expr.window.event_time_ref, f"{path}.window.event_time_ref",
                              expr.source_relation.table_ref)


def body_expressions_v2(body: FormulaBodyV2) -> tuple[AggregateExpressionV2, ...]:
    if isinstance(body, UnaryBodyV2):
        return (body.expr,)
    if isinstance(body, RatioBodyV2):
        return (body.numerator, body.denominator)
    if isinstance(body, DiffBodyV2):
        return (body.minuend, body.subtrahend)
    raise SchemaError(f"unknown v2 body shape: {type(body).__name__}")


def validate_semantics_v2(p: TypedFormulaProposalV2) -> None:
    """The v2 semantic gate — version pins are DATA and must equal the v2 constants exactly."""
    if p.formula_schema_version != FORMULA_SCHEMA_VERSION_V2:
        raise SchemaError(
            f"formula_schema_version: expected {FORMULA_SCHEMA_VERSION_V2}, "
            f"got {p.formula_schema_version}")
    if p.operation_grammar_version != OPERATION_GRAMMAR_VERSION_V2:
        raise SchemaError("operation_grammar_version: not the v2 grammar")
    if p.canonicalization_version != CANONICALIZATION_VERSION_V2:
        raise SchemaError("canonicalization_version: not the v2 canonicalization")
    for key in p.grain.keys:
        _require_column_ref(key, "grain.keys")
    params = {decl.name: decl for decl in p.parameters}
    for index, expr in enumerate(body_expressions_v2(p.body)):
        _check_expression_v2(expr, f"body.expr[{index}]", params)


# Additivity by v2 result shape — consumed by output validation (BR-2's RESULT_CLASS_ADDITIVITY
# is the recipe-side mirror; this is the formula-side truth for the increment-1 vocabulary).
AGGREGATE_ADDITIVITY_V2: dict[AggregateFunctionV2, AdditivityClass] = {
    AggregateFunctionV2.SUM: AdditivityClass.ADDITIVE,
    AggregateFunctionV2.COUNT_ROWS: AdditivityClass.ADDITIVE,
    AggregateFunctionV2.COUNT_NON_NULL: AdditivityClass.ADDITIVE,
    AggregateFunctionV2.COUNT_DISTINCT: AdditivityClass.NON_ADDITIVE,
    AggregateFunctionV2.MIN: AdditivityClass.NON_ADDITIVE,
    AggregateFunctionV2.MAX: AdditivityClass.NON_ADDITIVE,
    AggregateFunctionV2.AVG: AdditivityClass.NON_ADDITIVE,
    AggregateFunctionV2.RECENCY: AdditivityClass.NON_ADDITIVE,
    AggregateFunctionV2.STDDEV: AdditivityClass.NON_ADDITIVE,
    AggregateFunctionV2.PERCENTILE: AdditivityClass.NON_ADDITIVE,
    AggregateFunctionV2.MEDIAN: AdditivityClass.NON_ADDITIVE,
}
