"""TypedFormula authoring schema — Child-1 spec §A (normative, verbatim).

Frozen slotted dataclasses + StrEnum. OFFLINE authoring only: no execution,
no Spark. JSON canonicalization/hashing (§E) is a separate concern; this
module owns the structural schema and `validate_semantics`.

A ``LogicalRef`` is the canonical string ``source::schema.table[.column]``
(``::`` separates the source from the object path). ``SourceRelation.table_ref``
carries NO ``.column``; operand/filter/event-time refs carry exactly one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from featuregen.formula.schema_leaves import (
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
    ZeroDenominator,
    _check_decimal,
    _check_filter_node,
    _check_parameters,
    _require_column_ref,
    _require_contained_column,
    _require_table_ref,
)

# ---- identity version pins (known ints; see task brief "version pins") ----
FORMULA_SCHEMA_VERSION = 1
OPERATION_GRAMMAR_VERSION = 1
OUTPUT_POLICY_VERSION = 1
CANONICALIZATION_VERSION = 1


# ---- enums (exact string values are the serialized form) ----

class AggregateFunction(StrEnum):
    """The per-expression aggregate ONLY (never a FinalOperation). [c3]"""

    SUM = "sum"
    COUNT_ROWS = "count_rows"
    COUNT_NON_NULL = "count_non_null"
    COUNT_DISTINCT = "count_distinct"


class FinalOperation(StrEnum):
    """The formula body shape ONLY. [c3]"""

    IDENTITY = "identity"
    RATIO = "ratio"
    DIFFERENCE = "difference"


class WindowBasis(StrEnum):
    TRAILING = "trailing"
    CALENDAR_PERIOD = "calendar_period"


@dataclass(frozen=True, slots=True)
class WindowPolicy:
    event_time_ref: LogicalRef  # [c1] the column ordering the window (identity-bearing)
    basis: WindowBasis
    length: int
    unit: WindowUnit
    start_inclusive: Inclusivity
    end_inclusive: Inclusivity
    timezone: str
    empty_window: EmptyWindowResult
    null_input: NullInput


# ---- aggregate expression (an operand slot) ----
# NO expression_id — an expression's internal id is its canonical PATH [c4]

@dataclass(frozen=True, slots=True)
class AggregateExpression:
    aggregation: AggregateFunction  # [c3] cannot be a final op
    operand: LogicalRef | None  # None IFF aggregation == COUNT_ROWS [c9]
    source_relation: SourceRelation  # required (incl. COUNT_ROWS) [c6]
    filter: FilterNode | None
    window: WindowPolicy


# ---- body: discriminated union on final_operation [c3] ----

@dataclass(frozen=True, slots=True)
class UnaryBody:
    expr: AggregateExpression
    final_operation: FinalOperation = field(default=FinalOperation.IDENTITY, init=False)


@dataclass(frozen=True, slots=True)
class RatioBody:
    numerator: AggregateExpression
    denominator: AggregateExpression
    zero_denominator: ZeroDenominator
    final_operation: FinalOperation = field(default=FinalOperation.RATIO, init=False)


@dataclass(frozen=True, slots=True)
class DiffBody:
    minuend: AggregateExpression
    subtrahend: AggregateExpression
    final_operation: FinalOperation = field(default=FinalOperation.DIFFERENCE, init=False)


FormulaBody = UnaryBody | RatioBody | DiffBody  # serialized with "final_operation"


# ---- top level ----

@dataclass(frozen=True, slots=True)
class ExpectedOutput:
    """Advisory only — never identity-bearing."""

    output_type: str | None
    unit: str | None
    currency: str | None


@dataclass(frozen=True, slots=True)
class TypedFormulaProposalV1:
    formula_schema_version: int
    operation_grammar_version: int
    canonicalization_version: int
    grain: Grain
    body: FormulaBody
    parameters: tuple[ParameterDecl, ...]
    decimal: DecimalPolicy
    expected_output: ExpectedOutput | None


@dataclass(frozen=True, slots=True)
class FormulaOutputPolicyV1:
    output_type: str
    unit: str | None
    currency: str | None
    output_additivity: AdditivityClass
    external_type_required: bool


@dataclass(frozen=True, slots=True)
class TypedFormulaV1:
    """AUTHORITATIVE identity object.

    NO capability_policy_version, NO ids/timestamps/critic/provenance. [c7]
    """

    formula_schema_version: int
    operation_grammar_version: int
    output_policy_version: int
    canonicalization_version: int
    grain: Grain
    body: FormulaBody
    parameters: tuple[ParameterDecl, ...]
    decimal: DecimalPolicy
    output: FormulaOutputPolicyV1


def validate_semantics(p: TypedFormulaProposalV1) -> None:
    """Raise SchemaError on any §A semantic-rule violation; return None if valid."""
    _check_version_pins(p)
    params = _check_parameters(p.parameters)
    _check_decimal(p.decimal)
    for i, key in enumerate(p.grain.keys):
        _require_column_ref(key, f"grain.keys[{i}]")
    for path, expr in body_expressions(p.body):
        _check_expression(path, expr, params)


def _check_version_pins(p: TypedFormulaProposalV1) -> None:
    """The identity versions must be known ints (v1 pins exactly one value each)."""
    pins = (
        ("formula_schema_version", p.formula_schema_version, FORMULA_SCHEMA_VERSION),
        ("operation_grammar_version", p.operation_grammar_version, OPERATION_GRAMMAR_VERSION),
        ("canonicalization_version", p.canonicalization_version, CANONICALIZATION_VERSION),
    )
    for name, value, known in pins:
        if not isinstance(value, int) or isinstance(value, bool) or value != known:
            raise SchemaError(f"{name}: {value!r} is not a known version (expected {known})")


def _check_window(window: WindowPolicy, path: str) -> None:
    if not isinstance(window.length, int) or isinstance(window.length, bool) or window.length < 1:
        raise SchemaError(f"{path}.length: {window.length!r} must be an int >= 1")
    if not isinstance(window.timezone, str) or not window.timezone:
        raise SchemaError(f"{path}.timezone: a non-empty IANA timezone is required")
    try:
        ZoneInfo(window.timezone)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise SchemaError(
            f"{path}.timezone: {window.timezone!r} is not a known IANA timezone"
        ) from exc
    if not isinstance(window.start_inclusive, Inclusivity):
        raise SchemaError(f"{path}.start_inclusive: an Inclusivity value is required")
    if not isinstance(window.end_inclusive, Inclusivity):
        raise SchemaError(f"{path}.end_inclusive: an Inclusivity value is required")


def body_expressions(
    body: FormulaBody,
) -> tuple[tuple[str, AggregateExpression], ...]:
    """The body's expressions keyed by canonical internal path. [c4]

    Public since Spec-A Task 7 (it was the private ``_body_expressions``), for the same reason
    ``canonical.filter_plain`` and ``join_path.table_of_ref`` were made public before it: an adapter
    must ask the question the owning module already answers. ``materialize.ir`` compiles ONE
    ``ExpressionExecutionIR`` per body path, and a second enumeration there could disagree with this
    one about how many expressions a body has, or what each is called — and the path names the
    staging output every later stage reads.
    """
    if isinstance(body, UnaryBody):
        return (("body.expr", body.expr),)
    if isinstance(body, RatioBody):
        return (("body.numerator", body.numerator), ("body.denominator", body.denominator))
    if isinstance(body, DiffBody):
        return (("body.minuend", body.minuend), ("body.subtrahend", body.subtrahend))
    raise SchemaError(f"body must be UnaryBody | RatioBody | DiffBody, got {type(body).__name__}")


def _check_expression(
    path: str, expr: AggregateExpression, params: dict[str, ParameterDecl]
) -> None:
    # Body discriminator [c3]: `aggregation` is ALWAYS an AggregateFunction,
    # never a FinalOperation (nor a raw string).
    if not isinstance(expr.aggregation, AggregateFunction):
        raise SchemaError(
            f"{path}.aggregation: {expr.aggregation!r} must be an AggregateFunction "
            "(FinalOperation is the body shape only)"
        )
    # COUNT_ROWS <-> operand is None [c9]
    if expr.aggregation is AggregateFunction.COUNT_ROWS:
        if expr.operand is not None:
            raise SchemaError(f"{path}.operand: 'count_rows' takes no operand")
    elif expr.operand is None:
        raise SchemaError(
            f"{path}.operand: '{expr.aggregation.value}' requires an operand"
        )
    _require_table_ref(expr.source_relation.table_ref, f"{path}.source_relation.table_ref")
    table_ref = expr.source_relation.table_ref
    if expr.operand is not None:
        _require_contained_column(expr.operand, f"{path}.operand", table_ref)
    _require_contained_column(
        expr.window.event_time_ref, f"{path}.window.event_time_ref", table_ref
    )
    _check_window(expr.window, f"{path}.window")
    if expr.filter is not None:
        predicate_count = _check_filter_node(
            expr.filter, f"{path}.filter", depth=1, table_ref=table_ref, params=params
        )
        if predicate_count > MAX_PREDICATES:
            raise SchemaError(
                f"{path}.filter: {predicate_count} predicates exceeds "
                f"MAX_PREDICATES={MAX_PREDICATES}"
            )
