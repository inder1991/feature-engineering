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
    EmptyWindowResult,
    FilterNode,
    Grain,
    Inclusivity,
    LogicalRef,
    NullInput,
    ParameterDecl,
    SchemaError,
    SourceRelation,
    WindowUnit,
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
    # increment 3 — the at-cutoff group (event-time-ordered)
    LAST_KNOWN = "last_known"    # the value the world held at the cutoff; semi-additive
    FIRST_KNOWN = "first_known"  # the first value shown inside the window; semi-additive
    ZSCORE = "zscore"            # (last_known − mean) / stddev over one window; dimensionless
    # increment 4 — row-level date arithmetic, aggregated
    DATE_DIFF_AVG = "date_diff_avg"   # mean (operand − second_operand) in days across the window
    # increment 5 — trend and condition
    SLOPE = "slope"                  # OLS trend of operand over event time; units per day
    STREAK_PERIODS = "streak_periods"  # longest consecutive run of window-unit periods with a qualifying row
    ANY_MATCH = "any_match"          # boolean: any in-window row satisfies the filter
    # increment 6 — concentration: operand is the GROUPING dimension, second_operand the
    # optional weighting measure (absent = row-count shares)
    HHI = "hhi"                      # sum of squared group shares; 1/n .. 1
    TOP_SHARE = "top_share"          # the largest single group's share of the total; 0 .. 1


class FinalOperationV2(StrEnum):
    IDENTITY = "identity"
    RATIO = "ratio"
    DIFFERENCE = "difference"


# The offset cap: a year of monthly look-backs. An offset this large is a modelling smell worth a
# loud stop rather than a silent 10-year scan.
MAX_WINDOW_OFFSET_PERIODS = 12


class WindowBasisV2(StrEnum):
    """v1's two bases plus increment 7's FUTURE_HORIZON — the contractual-maturity direction:
    (cutoff, cutoff + L], read from contract terms knowable AT the cutoff (BR-4's
    contractual_future temporal anchor is the recipe-side twin). With an offset it becomes the
    LADDER BUCKET: offset k reads (cutoff + k·L, cutoff + (k+1)·L] — the maturity ladder is the
    lag composition pointed forward."""

    TRAILING = "trailing"
    CALENDAR_PERIOD = "calendar_period"
    FUTURE_HORIZON = "future_horizon"


@dataclass(frozen=True, slots=True)
class WindowPolicyV2:
    """v1's window plus ``offset_periods`` — the increment-4 fork that makes LAG and DELTA body
    COMPOSITIONS instead of new aggregates: offset 0 is the current period (byte-for-byte v1
    semantics), offset k is the window shifted back k×length — (cutoff − (k+1)·L, cutoff − k·L].
    A previous-period value is a unary body at offset 1; a delta is a difference body over
    offsets 0 and 1 of the SAME aggregate. Every offset is inside the identity."""

    event_time_ref: LogicalRef
    basis: WindowBasisV2
    length: int
    unit: WindowUnit
    start_inclusive: Inclusivity
    end_inclusive: Inclusivity
    timezone: str
    empty_window: EmptyWindowResult
    null_input: NullInput
    offset_periods: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.offset_periods, int) or isinstance(self.offset_periods, bool) \
                or not 0 <= self.offset_periods <= MAX_WINDOW_OFFSET_PERIODS:
            raise SchemaError(
                f"window.offset_periods must be an int in [0, {MAX_WINDOW_OFFSET_PERIODS}], "
                f"got {self.offset_periods!r}")


@dataclass(frozen=True, slots=True)
class AuthorityRefsV2:
    """The governed policy REFERENCES one expression computes under (increment 8): which
    eligible-status set filters rows, which sign/direction convention reads amounts, how
    reversals neutralize originals, and which rate policy converts currency. These are
    DECLARATIONS — carried in identity, so a formula with a reversal policy is a DIFFERENT
    formula from one without. Resolving each ref against its governed store (and refusing a
    monetary sum whose source needs conversion but declares none) is output authority's and
    BR-7's job — the schema's job is that the declaration exists, non-vacuously."""

    status_policy_ref: str = ""
    direction_policy_ref: str = ""
    reversal_policy_ref: str = ""
    currency_conversion_ref: str = ""

    def __post_init__(self) -> None:
        values = (self.status_policy_ref, self.direction_policy_ref,
                  self.reversal_policy_ref, self.currency_conversion_ref)
        if not any(v.strip() for v in values):
            raise SchemaError(
                "authority_refs with every ref blank is a lie — omit the block instead")
        if any(v != v.strip() for v in values):
            raise SchemaError("authority refs must not carry surrounding whitespace")


@dataclass(frozen=True, slots=True)
class AggregateExpressionV2:
    aggregation: AggregateFunctionV2
    operand: LogicalRef | None            # None IFF aggregation == COUNT_ROWS
    source_relation: SourceRelation
    filter: FilterNode | None
    window: WindowPolicyV2
    # increment 2: the aggregate's own argument — REQUIRED for percentile (p ∈ (0,100),
    # exclusive), FORBIDDEN for every other operation. A parameterized aggregate carries its
    # parameter in identity, never in a generic label.
    aggregation_argument: float | None = None
    # increment 4: the second column of a row-level binary operation (date_diff_avg's
    # subtrahend) — REQUIRED where the rule table says so, FORBIDDEN elsewhere, same-table.
    second_operand: LogicalRef | None = None
    # increment 8: the governed policies this expression computes under. None = the recipe's
    # temporal/eligibility contract carries everything (many features need no row policies);
    # a present block is identity-bearing and never vacuous.
    authority_refs: AuthorityRefsV2 | None = None


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
    # increment 8: the allocation policy governing the SOURCE-grain → OUTPUT-grain rollup
    # (joint accounts, facility→obligor). "" = grains coincide or the rollup is a plain
    # per-entity aggregation needing no allocation. Identity-bearing.
    allocation_policy_ref: str = ""


def _check_expression_v2(expr: AggregateExpressionV2, path: str,
                         params: dict[str, ParameterDecl]) -> None:
    from featuregen.formula.operations_v2 import operation_rule

    if not isinstance(expr.aggregation, AggregateFunctionV2):
        raise SchemaError(f"{path}.aggregation: not a v2 aggregate: {expr.aggregation!r}")
    rule = operation_rule(expr.aggregation)
    _require_table_ref(expr.source_relation.table_ref, f"{path}.source_relation.table_ref")
    if rule.argument == "percentile":
        if not isinstance(expr.aggregation_argument, (int, float)) or isinstance(
                expr.aggregation_argument, bool) or not 0 < expr.aggregation_argument < 100:
            raise SchemaError(
                f"{path}.aggregation_argument: percentile requires p strictly between 0 and "
                f"100, got {expr.aggregation_argument!r}")
    elif expr.aggregation_argument is not None:
        raise SchemaError(
            f"{path}.aggregation_argument: {expr.aggregation.value} takes no argument")
    if expr.window.basis is WindowBasisV2.FUTURE_HORIZON and rule.order_sensitive:
        raise SchemaError(
            f"{path}: {expr.aggregation.value} is order-sensitive — it reads OBSERVED history, "
            "and a future horizon has none. Future windows carry sums, counts, shares and "
            "flags over contractual rows; they cannot carry last-known state or trends.")
    if not rule.operand_required:
        if expr.operand is not None:
            raise SchemaError(
                f"{path}.operand: {expr.aggregation.value} carries no operand")
    else:
        if expr.operand is None:
            raise SchemaError(f"{path}.operand: {expr.aggregation.value} requires an operand")
        _require_column_ref(expr.operand, f"{path}.operand")
        _require_contained_column(expr.operand, f"{path}.operand",
                                  expr.source_relation.table_ref)
    if rule.second_operand == "required" and expr.second_operand is None:
        raise SchemaError(
            f"{path}.second_operand: {expr.aggregation.value} requires its second column")
    if expr.second_operand is not None:
        if rule.second_operand == "forbidden":
            raise SchemaError(
                f"{path}.second_operand: {expr.aggregation.value} takes no second column")
        _require_column_ref(expr.second_operand, f"{path}.second_operand")
        _require_contained_column(expr.second_operand, f"{path}.second_operand",
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


def _aggregate_additivity_v2() -> dict[AggregateFunctionV2, AdditivityClass]:
    """A VIEW over the operation rule table (operations_v2 owns the semantics since increment 3;
    lazy so the two modules keep their one-way import)."""
    from featuregen.formula.operations_v2 import OPERATION_RULES

    return {agg: rule.additivity for agg, rule in OPERATION_RULES.items()}


def __getattr__(name: str):
    if name == "AGGREGATE_ADDITIVITY_V2":
        return _aggregate_additivity_v2()
    raise AttributeError(name)
