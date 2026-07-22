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

LogicalRef = str  # canonical "source::schema.table[.column]", normalized before hashing

# ---- identity version pins (known ints; see task brief "version pins") ----
FORMULA_SCHEMA_VERSION = 1
OPERATION_GRAMMAR_VERSION = 1
OUTPUT_POLICY_VERSION = 1
CANONICALIZATION_VERSION = 1

# ---- hard limits (schema constants) ----
MAX_FILTER_DEPTH = 4
MAX_PREDICATES = 16
MAX_IN_LIST = 64


class SchemaError(Exception):
    """A TypedFormula proposal violates a normative §A schema/semantic rule."""


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


class WindowUnit(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


class Inclusivity(StrEnum):
    INCLUSIVE = "inclusive"
    EXCLUSIVE = "exclusive"


class EmptyWindowResult(StrEnum):
    NULL = "null"
    ZERO = "zero"
    ERROR = "error"


class NullInput(StrEnum):
    IGNORE = "ignore"
    PROPAGATE = "propagate"
    ZERO = "zero"


class ZeroDenominator(StrEnum):
    NULL = "null"
    ZERO = "zero"
    ERROR = "error"


class RoundingMode(StrEnum):
    HALF_UP = "half_up"
    HALF_EVEN = "half_even"
    DOWN = "down"
    UP = "up"
    FLOOR = "floor"
    CEILING = "ceiling"


class OverflowBehavior(StrEnum):
    ERROR = "error"
    SATURATE = "saturate"


class LiteralType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    DATE = "date"


class ParamClass(StrEnum):
    SEMANTIC = "semantic"
    OPERATIONAL = "operational"


class FilterKind(StrEnum):
    """JSON discriminator for the filter AST union. [c9]"""

    BOOL = "bool"
    PREDICATE = "predicate"


class FilterBoolOp(StrEnum):
    AND = "and"
    OR = "or"
    NOT = "not"


class FilterPredicateOp(StrEnum):
    EQUAL = "equal"
    NOT_EQUAL = "not_equal"
    GREATER_THAN = "greater_than"
    GREATER_OR_EQUAL = "greater_or_equal"
    LESS_THAN = "less_than"
    LESS_OR_EQUAL = "less_or_equal"
    IN = "in"
    NOT_IN = "not_in"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"


class AdditivityClass(StrEnum):
    ADDITIVE = "additive"
    NON_ADDITIVE = "non_additive"
    SEMI_ADDITIVE = "semi_additive"


# ---- leaves ----

@dataclass(frozen=True, slots=True)
class TypedLiteral:
    type: LiteralType
    value: str  # value ALWAYS a canonical string


@dataclass(frozen=True, slots=True)
class ParameterDecl:
    # name matches /^[a-z][a-z0-9_]{0,63}$/ and is UNIQUE across the proposal [c14]
    name: str
    type: LiteralType
    param_class: ParamClass
    classification: str
    nullable: bool
    allowed_set: tuple[str, ...] | None
    allowed_min: str | None
    allowed_max: str | None


@dataclass(frozen=True, slots=True)
class ParameterRef:
    name: str


# ---- filter AST (discriminated union on `kind`) [c9] ----

@dataclass(frozen=True, slots=True)
class FilterPredicate:
    op: FilterPredicateOp
    left: LogicalRef
    right_literal: TypedLiteral | None = None
    right_param: ParameterRef | None = None
    right_set: tuple[TypedLiteral, ...] | None = None
    kind: FilterKind = field(default=FilterKind.PREDICATE, init=False)


@dataclass(frozen=True, slots=True)
class FilterBool:
    op: FilterBoolOp
    children: tuple["FilterNode", ...]
    kind: FilterKind = field(default=FilterKind.BOOL, init=False)


FilterNode = FilterPredicate | FilterBool  # serialized with an explicit "kind" field

# PREDICATE INVARIANTS (enforced by validate_semantics) [c9]:
#   IS_NULL/IS_NOT_NULL -> right_literal=right_param=right_set=None
#   IN/NOT_IN           -> exactly right_set (non-empty, <= MAX_IN_LIST)
#                          (sorted+deduped is a canonicalization rule, §E)
#   all other ops       -> exactly ONE of right_literal | right_param
#   NOT (bool)          -> exactly 1 child;  AND/OR -> >=2 children
#   right_param.name must resolve to a declared ParameterDecl


# ---- source, grain, window ----

@dataclass(frozen=True, slots=True)
class SourceRelation:
    table_ref: LogicalRef  # a TABLE logical_ref (no .column); source implicit in it [c8]


@dataclass(frozen=True, slots=True)
class Grain:
    entity: str
    keys: tuple[LogicalRef, ...]  # ORDER IS SEMANTIC (§D); excludes business_dt


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


@dataclass(frozen=True, slots=True)
class DecimalPolicy:
    precision: int
    scale: int
    rounding: RoundingMode
    overflow: OverflowBehavior


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


# ---- semantic validation ----

_NO_RIGHT_OPS = frozenset({FilterPredicateOp.IS_NULL, FilterPredicateOp.IS_NOT_NULL})
_SET_OPS = frozenset({FilterPredicateOp.IN, FilterPredicateOp.NOT_IN})


def validate_semantics(p: TypedFormulaProposalV1) -> None:
    """Raise SchemaError on any §A semantic-rule violation; return None if valid."""
    for i, key in enumerate(p.grain.keys):
        _require_column_ref(key, f"grain.keys[{i}]")
    for path, expr in _body_expressions(p.body):
        _check_expression(path, expr)


# ---- logical_ref parsing ("source::schema.table[.column]") ----

def _split_logical_ref(ref: LogicalRef, path: str) -> tuple[str, list[str]]:
    """Split a canonical logical_ref into (source, dotted object parts)."""
    if not isinstance(ref, str) or "::" not in ref:
        raise SchemaError(f"{path}: logical_ref {ref!r} must be 'source::schema.table[.column]'")
    source, _, rest = ref.partition("::")
    if not source or not rest or "::" in rest:
        raise SchemaError(f"{path}: logical_ref {ref!r} must be 'source::schema.table[.column]'")
    parts = rest.split(".")
    if len(parts) not in (2, 3) or any(not part for part in parts):
        raise SchemaError(
            f"{path}: logical_ref {ref!r} must have non-empty "
            "'schema.table' or 'schema.table.column' after '::'"
        )
    return source, parts


def _require_table_ref(ref: LogicalRef, path: str) -> None:
    _, parts = _split_logical_ref(ref, path)
    if len(parts) != 2:
        raise SchemaError(f"{path}: {ref!r} must be a table logical_ref (no .column)")


def _require_column_ref(ref: LogicalRef, path: str) -> str:
    """Validate a column logical_ref; return its table logical_ref prefix."""
    source, parts = _split_logical_ref(ref, path)
    if len(parts) != 3:
        raise SchemaError(f"{path}: {ref!r} must be a column logical_ref ('schema.table.column')")
    return f"{source}::{parts[0]}.{parts[1]}"


def _require_contained_column(ref: LogicalRef, path: str, table_ref: LogicalRef) -> None:
    """Same-table containment: the column must live in the expression's table.

    Pure same-table only — cross-table reachability (joins over VERIFIED
    bridges) is explicitly DEFERRED to governed planning (Child 3).
    """
    column_table = _require_column_ref(ref, path)
    if column_table != table_ref:
        raise SchemaError(
            f"{path}: {ref!r} is not contained in the expression's "
            f"source_relation.table_ref {table_ref!r}"
        )


def _body_expressions(
    body: FormulaBody,
) -> tuple[tuple[str, AggregateExpression], ...]:
    """The body's expressions keyed by canonical internal path. [c4]"""
    if isinstance(body, UnaryBody):
        return (("body.expr", body.expr),)
    if isinstance(body, RatioBody):
        return (("body.numerator", body.numerator), ("body.denominator", body.denominator))
    if isinstance(body, DiffBody):
        return (("body.minuend", body.minuend), ("body.subtrahend", body.subtrahend))
    raise SchemaError(f"body must be UnaryBody | RatioBody | DiffBody, got {type(body).__name__}")


def _check_expression(path: str, expr: AggregateExpression) -> None:
    _require_table_ref(expr.source_relation.table_ref, f"{path}.source_relation.table_ref")
    table_ref = expr.source_relation.table_ref
    if expr.operand is not None:
        _require_contained_column(expr.operand, f"{path}.operand", table_ref)
    _require_contained_column(
        expr.window.event_time_ref, f"{path}.window.event_time_ref", table_ref
    )
    if expr.filter is not None:
        predicate_count = _check_filter_node(
            expr.filter, f"{path}.filter", depth=1, table_ref=table_ref
        )
        if predicate_count > MAX_PREDICATES:
            raise SchemaError(
                f"{path}.filter: {predicate_count} predicates exceeds "
                f"MAX_PREDICATES={MAX_PREDICATES}"
            )


def _check_filter_node(node: FilterNode, path: str, depth: int, table_ref: LogicalRef) -> int:
    """Enforce the [c9] predicate/bool invariants; return the predicate count."""
    if depth > MAX_FILTER_DEPTH:
        raise SchemaError(
            f"{path}: filter tree depth {depth} exceeds MAX_FILTER_DEPTH={MAX_FILTER_DEPTH}"
        )
    if isinstance(node, FilterPredicate):
        _check_predicate(node, path)
        _require_contained_column(node.left, f"{path}.left", table_ref)
        return 1
    if isinstance(node, FilterBool):
        if node.op is FilterBoolOp.NOT:
            if len(node.children) != 1:
                raise SchemaError(
                    f"{path}: 'not' requires exactly 1 child, got {len(node.children)}"
                )
        elif len(node.children) < 2:
            raise SchemaError(
                f"{path}: '{node.op.value}' requires at least 2 children, "
                f"got {len(node.children)}"
            )
        return sum(
            _check_filter_node(child, f"{path}.children[{i}]", depth + 1, table_ref)
            for i, child in enumerate(node.children)
        )
    raise SchemaError(
        f"{path}: filter node must be FilterPredicate | FilterBool, got {type(node).__name__}"
    )


def _check_predicate(node: FilterPredicate, path: str) -> None:
    if node.op in _NO_RIGHT_OPS:
        if not (node.right_literal is None and node.right_param is None and node.right_set is None):
            raise SchemaError(f"{path}: '{node.op.value}' takes no right-hand side")
        return
    if node.op in _SET_OPS:
        if node.right_literal is not None or node.right_param is not None:
            raise SchemaError(
                f"{path}: '{node.op.value}' takes exactly right_set "
                "(no right_literal/right_param)"
            )
        if node.right_set is None:
            raise SchemaError(f"{path}: '{node.op.value}' requires right_set")
        if len(node.right_set) == 0:
            raise SchemaError(f"{path}: '{node.op.value}' requires a non-empty right_set")
        if len(node.right_set) > MAX_IN_LIST:
            raise SchemaError(
                f"{path}: right_set size {len(node.right_set)} exceeds "
                f"MAX_IN_LIST={MAX_IN_LIST}"
            )
        return
    # all remaining ops: exactly ONE of right_literal | right_param
    if node.right_set is not None:
        raise SchemaError(f"{path}: '{node.op.value}' does not take right_set")
    if (node.right_literal is None) == (node.right_param is None):
        raise SchemaError(
            f"{path}: '{node.op.value}' requires exactly one of right_literal | right_param"
        )
