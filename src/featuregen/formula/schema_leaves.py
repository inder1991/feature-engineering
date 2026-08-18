"""The structural leaves every formula language version shares.

These carry **no versioned vocabulary**: refs, filters, literals, parameters, grains, source
relations, window units, decimal policy, and the validation that guards them. A ratio is a ratio in
v1 and in v2; a filter predicate means the same thing in both; `MAX_PREDICATES` bounds the same blast
radius either way. They are the same objects, not copies — `schema_v2` and `schema_v3` import them
verbatim, and duplicating them would fork validation the grammars must share.

**Why this module exists at all.** They used to live in `schema.py`, which is v1's module. Nothing
about them is v1, but the file name said otherwise — so every plan to remove v1 had to begin by
working out what would break, and the answer was "v2 and v3". Moving them here makes v1 removable by
deletion rather than by archaeology.

**What does NOT belong here:** anything with a version in its meaning — aggregate functions, final
operations, body shapes, proposals, output policies, the version pins. Those are the language, and
each language owns its own.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum

LogicalRef = str  # canonical "source::schema.table[.column]", normalized before hashing

# ---- hard limits (schema constants) ----
MAX_FILTER_DEPTH = 4
MAX_PREDICATES = 16
MAX_IN_LIST = 64


class SchemaError(Exception):
    """A TypedFormula proposal violates a normative §A schema/semantic rule."""


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
    children: tuple[FilterNode, ...]
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
class DecimalPolicy:
    precision: int
    scale: int
    rounding: RoundingMode
    overflow: OverflowBehavior


# ---- semantic validation ----

_NO_RIGHT_OPS = frozenset({FilterPredicateOp.IS_NULL, FilterPredicateOp.IS_NOT_NULL})
_SET_OPS = frozenset({FilterPredicateOp.IN, FilterPredicateOp.NOT_IN})
_ORDERED_OPS = frozenset(
    {
        FilterPredicateOp.GREATER_THAN,
        FilterPredicateOp.GREATER_OR_EQUAL,
        FilterPredicateOp.LESS_THAN,
        FilterPredicateOp.LESS_OR_EQUAL,
    }
)
_ORDERABLE_TYPES = frozenset({LiteralType.INTEGER, LiteralType.DECIMAL, LiteralType.DATE})
_PARAM_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _check_parameters(
    parameters: tuple[ParameterDecl, ...],
) -> dict[str, ParameterDecl]:
    declared: dict[str, ParameterDecl] = {}
    for i, decl in enumerate(parameters):
        path = f"parameters[{i}]"
        if not isinstance(decl, ParameterDecl):
            raise SchemaError(f"{path}: expected a ParameterDecl, got {type(decl).__name__}")
        if not isinstance(decl.name, str) or not _PARAM_NAME_RE.fullmatch(decl.name):
            raise SchemaError(
                f"{path}: name {decl.name!r} must match ^[a-z][a-z0-9_]{{0,63}}$"
            )
        if decl.name in declared:
            raise SchemaError(f"{path}: parameter names must be unique; {decl.name!r} repeats")
        if decl.allowed_set is not None:
            if len(decl.allowed_set) == 0:
                raise SchemaError(f"{path}: allowed_set must be non-empty when present")
            for j, entry in enumerate(decl.allowed_set):
                _parse_typed_value(decl.type, entry, f"{path}.allowed_set[{j}]")
        lo = (
            _parse_typed_value(decl.type, decl.allowed_min, f"{path}.allowed_min")
            if decl.allowed_min is not None
            else None
        )
        hi = (
            _parse_typed_value(decl.type, decl.allowed_max, f"{path}.allowed_max")
            if decl.allowed_max is not None
            else None
        )
        if lo is not None and hi is not None and lo > hi:
            raise SchemaError(
                f"{path}: allowed_min {decl.allowed_min!r} must be <= "
                f"allowed_max {decl.allowed_max!r}"
            )
        declared[decl.name] = decl
    return declared


def _check_decimal(decimal: DecimalPolicy) -> None:
    if decimal.scale < 0:
        raise SchemaError(f"decimal: scale {decimal.scale} must be >= 0")
    if decimal.precision < decimal.scale:
        raise SchemaError(
            f"decimal: precision {decimal.precision} must be >= scale {decimal.scale}"
        )


# ---- typed-literal parsing (canonical string forms only) ----

_INTEGER_RE = re.compile(r"^-?[0-9]+$")
_DECIMAL_RE = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")  # no exponent/NaN/Infinity
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")  # extended ISO only (no YYYYMMDD)


def _parse_typed_value(literal_type: LiteralType, value: str, path: str):
    """Parse a canonical value string to its LiteralType; raise SchemaError if it doesn't."""
    if not isinstance(value, str):
        raise SchemaError(f"{path}: literal value must be a canonical string, got {value!r}")
    if literal_type is LiteralType.STRING:
        return value
    if literal_type is LiteralType.INTEGER:
        if not _INTEGER_RE.fullmatch(value):
            raise SchemaError(f"{path}: {value!r} is not a canonical integer literal")
        return int(value)
    if literal_type is LiteralType.DECIMAL:
        if not _DECIMAL_RE.fullmatch(value):
            raise SchemaError(f"{path}: {value!r} is not a canonical decimal literal")
        try:
            return Decimal(value)
        except InvalidOperation as exc:  # pragma: no cover - regex already gates this
            raise SchemaError(f"{path}: {value!r} is not a canonical decimal literal") from exc
    if literal_type is LiteralType.BOOLEAN:
        if value not in ("true", "false"):
            raise SchemaError(
                f"{path}: {value!r} is not a canonical boolean literal ('true'/'false')"
            )
        return value == "true"
    if literal_type is LiteralType.DATE:
        if not _DATE_RE.fullmatch(value):
            raise SchemaError(f"{path}: {value!r} is not an ISO date literal (YYYY-MM-DD)")
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise SchemaError(f"{path}: {value!r} is not an ISO date literal") from exc
    raise SchemaError(f"{path}: unknown literal type {literal_type!r}")


def _check_typed_literal(lit: TypedLiteral, path: str) -> None:
    if not isinstance(lit, TypedLiteral) or not isinstance(lit.type, LiteralType):
        raise SchemaError(f"{path}: expected a TypedLiteral with a LiteralType")
    _parse_typed_value(lit.type, lit.value, path)


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


def _check_filter_node(
    node: FilterNode,
    path: str,
    depth: int,
    table_ref: LogicalRef,
    params: dict[str, ParameterDecl],
) -> int:
    """Enforce the [c9] predicate/bool invariants; return the predicate count."""
    if depth > MAX_FILTER_DEPTH:
        raise SchemaError(
            f"{path}: filter tree depth {depth} exceeds MAX_FILTER_DEPTH={MAX_FILTER_DEPTH}"
        )
    if isinstance(node, FilterPredicate):
        _check_predicate(node, path, params)
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
            _check_filter_node(child, f"{path}.children[{i}]", depth + 1, table_ref, params)
            for i, child in enumerate(node.children)
        )
    raise SchemaError(
        f"{path}: filter node must be FilterPredicate | FilterBool, got {type(node).__name__}"
    )


def _check_predicate(
    node: FilterPredicate, path: str, params: dict[str, ParameterDecl]
) -> None:
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
        for i, entry in enumerate(node.right_set):
            _check_typed_literal(entry, f"{path}.right_set[{i}]")
        return
    # all remaining ops: exactly ONE of right_literal | right_param
    if node.right_set is not None:
        raise SchemaError(f"{path}: '{node.op.value}' does not take right_set")
    if (node.right_literal is None) == (node.right_param is None):
        raise SchemaError(
            f"{path}: '{node.op.value}' requires exactly one of right_literal | right_param"
        )
    right_type: LiteralType
    if node.right_literal is not None:
        _check_typed_literal(node.right_literal, f"{path}.right_literal")
        right_type = node.right_literal.type
    else:
        # right_param.name must resolve to a declared ParameterDecl [c9]
        if not isinstance(node.right_param, ParameterRef):
            raise SchemaError(
                f"{path}.right_param: expected a ParameterRef, "
                f"got {type(node.right_param).__name__}"
            )
        name = node.right_param.name
        if name not in params:
            raise SchemaError(
                f"{path}.right_param: {name!r} does not resolve to a declared ParameterDecl"
            )
        right_type = params[name].type
    # predicate-operator type compatibility: ordered comparisons only against
    # orderable (integer/decimal/date) literals or params
    if node.op in _ORDERED_OPS and right_type not in _ORDERABLE_TYPES:
        raise SchemaError(
            f"{path}: '{node.op.value}' requires an integer/decimal/date "
            f"right-hand side, got {right_type.value!r}"
        )
