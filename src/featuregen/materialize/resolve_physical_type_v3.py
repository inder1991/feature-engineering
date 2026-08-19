"""The published column type for a V3 formula — the driver `physical_types_v2` never had.

**What existed and what did not.** ``physical_types_v2`` ships the arithmetic: ``sum_type_v2``,
``multiply_type_v2``, ``product_sum_type_v2``, ``DecimalTypeV2`` and their overflow refusals. What it
does not ship is anything that *walks a formula and decides its column type* — the job
``resolve_physical_type`` does for V1. So ``PlannedFeature.physical_type``, which hard-requires a
resolved type, could never be filled for a V2 feature. The primitives were the hard half and the
driver was missing entirely.

**Nullability comes from the POLICIES, never from the SQL type.** A ``BIGINT`` count over a window
declared ``null`` when empty is a nullable column, and reading nullability off the type would
publish a column the pipeline can write NULLs into as ``NOT NULL``. V1 states this and V2 inherits
it unchanged — the policies are shared structural leaves, not versioned vocabulary.

**A refusal is RETURNED, not raised.** One refused feature is one governed verdict among the many a
compilation collects; raising would make the first bad feature hide every other verdict in the group.
Same shape ``compile_ir`` already uses.

**Only the four renderable aggregates resolve today, and the rest refuse BY NAME.** That is not a
limitation of this module — it is the renderer's, reported honestly here rather than resolved to a
plausible type for an operation nothing can emit. A `median` that typed cleanly and then failed at
render would waste the reader's time twice.
"""
from __future__ import annotations

from collections.abc import Mapping

from featuregen.formula.schema_leaves import (
    DecimalPolicy,
    EmptyWindowResult,
    NullInput,
    OverflowBehavior,
    RoundingMode,
    ZeroDenominator,
)
from featuregen.formula.schema_v2 import (
    AggregateFunctionV2,
    FinalOperationV2,
    TypedFormulaProposalV2,
)
from featuregen.formula.schema_v3 import TypedFormulaProposalV3
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.physical_types import PhysicalType
from featuregen.materialize.physical_types_v2 import (
    DecimalTypeV2,
    PhysicalTypeRefusalV2,
    sum_type_v2,
)

__all__ = ["COUNTING_AGGREGATES", "resolve_physical_type_v3"]

#: The aggregates whose published type is a count rather than a decimal. Named as a set because the
#: distinction is "does this produce a cardinality or a quantity", not "which four did V1 support".
COUNTING_AGGREGATES = frozenset({
    AggregateFunctionV2.COUNT_ROWS,
    AggregateFunctionV2.COUNT_NON_NULL,
    AggregateFunctionV2.COUNT_DISTINCT,
})

#: What this module can type TODAY. Everything else refuses by name rather than guessing.
#:
#: Deliberately not "every aggregate in the vocabulary": typing an operation the renderer cannot emit
#: produces a column definition for code that will never exist. The set widens in step 11 alongside
#: the renderer, and the two moving together is the point.
_TYPEABLE = COUNTING_AGGREGATES | {AggregateFunctionV2.SUM}


def resolve_physical_type_v3(
    proposal: TypedFormulaProposalV2 | TypedFormulaProposalV3,
    *,
    operand_types: Mapping[str, DecimalTypeV2],
) -> PhysicalType | MaterializationRefused:
    """The published column type for ``proposal``, or a typed refusal.

    Keyed on what the formula PRODUCES rather than on how it is spelled:

    ==============================  ==========================================================
    final operation / aggregate     published type
    ==============================  ==========================================================
    counting aggregates             ``BIGINT``
    ``SUM``                         ``DECIMAL(p,s)`` widened by :func:`sum_type_v2`
    ``RATIO`` / ``DIFFERENCE`` /    ``DECIMAL(p,s)`` from the formula's own ``DecimalPolicy``
    ``SIGNED_SUM``
    ==============================  ==========================================================

    Args:
        operand_types: the resolved decimal type of each operand ref. A ref with no entry is a
            refusal rather than an assumption — an operand whose type nobody established cannot be
            typed by guessing, and a column published on a guess is worse than one refused.
    """
    body = proposal.body
    final = _final_operation(body)
    expressions = _expressions(body)
    if not expressions:
        return MaterializationRefused(
            CompilationRefusalCode.FORMULA_SCHEMA_UNSUPPORTED,
            "a formula with no expressions publishes no value, so there is no column to type")

    unsupported = sorted({
        str(getattr(e, "aggregation", "?")) for e in expressions
        if _aggregation_of(e) not in _TYPEABLE})
    if unsupported:
        return MaterializationRefused(
            CompilationRefusalCode.FORMULA_SCHEMA_UNSUPPORTED,
            f"this build cannot type {unsupported}: the renderer has no branch for them, so a "
            f"column definition here would describe code that will never be emitted. Typing them "
            f"is the same piece of work as rendering them")

    nullable = _is_nullable(proposal, final, expressions)

    # COUNTS. A cardinality is a count whatever the final operation does with it — except that a
    # ratio or difference OF counts is an arithmetic result, not a count.
    if final is FinalOperationV2.IDENTITY:
        if _aggregation_of(expressions[0]) in COUNTING_AGGREGATES:
            return PhysicalType(sql_type="BIGINT", nullable=nullable,
                                rounding=None, overflow=None)
        return _decimal_from(proposal, _sum_type(expressions[0], operand_types), nullable)

    # EVERY OTHER FINAL OPERATION IS ARITHMETIC, so the published type is the formula's declared
    # decimal policy rather than anything derived from the operands. The policy is a GOVERNED
    # decision about how the answer is represented; deriving a wider type from the inputs would
    # quietly overrule it.
    return _decimal_from(proposal, None, nullable)


def _decimal_from(
    proposal: TypedFormulaProposalV2 | TypedFormulaProposalV3,
    widened: DecimalTypeV2 | PhysicalTypeRefusalV2 | None,
    nullable: bool,
) -> PhysicalType | MaterializationRefused:
    """A decimal column from the formula's policy, or from a widened operand type."""
    policy: DecimalPolicy = proposal.decimal
    if isinstance(widened, PhysicalTypeRefusalV2):
        return MaterializationRefused(
            CompilationRefusalCode.FORMULA_SCHEMA_UNSUPPORTED,
            f"the summed type does not fit: {widened.detail}. A column that cannot hold its own "
            f"aggregate would truncate or overflow at run time, which is a wrong number rather "
            f"than a failure")
    precision = widened.precision if widened is not None else policy.precision
    scale = widened.scale if widened is not None else policy.scale
    return PhysicalType(
        sql_type=f"DECIMAL({precision},{scale})",
        nullable=nullable,
        rounding=RoundingMode(policy.rounding),
        overflow=OverflowBehavior(policy.overflow))


def _sum_type(expression, operand_types: Mapping[str, DecimalTypeV2]):
    """The widened type of one summed operand, or a refusal naming the ref.

    ``sum_type_v2`` widens because summing N rows of ``DECIMAL(p,s)`` needs headroom: publishing at
    the operand's own precision would overflow on exactly the data the feature exists to measure.
    """
    ref = getattr(expression, "operand", None) or getattr(expression, "operand_ref", None)
    if not ref:
        return PhysicalTypeRefusalV2(
            code=CompilationRefusalCode.FORMULA_SCHEMA_UNSUPPORTED.value,
            detail="a sum with no operand has nothing to widen from")
    operand = operand_types.get(str(ref))
    if operand is None:
        return PhysicalTypeRefusalV2(
            code=CompilationRefusalCode.FORMULA_SCHEMA_UNSUPPORTED.value,
            detail=f"no resolved type for operand {ref!r}: an operand whose type nobody "
                   f"established cannot be typed by guessing")
    return sum_type_v2(operand)


def _is_nullable(proposal, final, expressions) -> bool:
    """Whether the published column can hold NULL, from the POLICIES rather than the SQL type.

    Three independent sources, any one of which makes the column nullable:

    * an empty window declared ``null`` — a grain key with no rows in range produces NULL;
    * a null input declared ``propagate`` — one NULL row makes the aggregate NULL;
    * a ratio whose zero denominator is declared ``null``.

    A count is not exempt. ``BIGINT`` says nothing about nullability, and a count over a window
    declared NULL-when-empty is a nullable column — publishing it NOT NULL would reject rows the
    pipeline legitimately writes.
    """
    for expression in expressions:
        window = getattr(expression, "window", None)
        if window is not None:
            if str(getattr(window, "empty_window", "")) == EmptyWindowResult.NULL.value:
                return True
            # PROPAGATE, not "null": the vocabulary is IGNORE / PROPAGATE / ZERO, and PROPAGATE is
            # the one that lets a NULL row make the whole aggregate NULL. An earlier draft of this
            # named a member that does not exist, which would have raised on the first formula it
            # saw — caught by the test that asserts a non-nullable case.
            if str(getattr(window, "null_input", "")) == NullInput.PROPAGATE.value:
                return True
    if final is FinalOperationV2.RATIO:
        zero = getattr(proposal.body, "zero_denominator", None)
        if zero is not None and str(zero) == ZeroDenominator.NULL.value:
            return True
    return False


def _final_operation(body) -> FinalOperationV2:
    raw = getattr(body, "final_operation", FinalOperationV2.IDENTITY)
    return raw if isinstance(raw, FinalOperationV2) else FinalOperationV2(str(raw))


def _aggregation_of(expression) -> AggregateFunctionV2 | None:
    raw = getattr(expression, "aggregation", None)
    if raw is None:
        return None
    return raw if isinstance(raw, AggregateFunctionV2) else AggregateFunctionV2(str(raw))


def _expressions(body) -> tuple:
    """The body's expressions, through the v2 vocabulary's own walker.

    Not a hand-rolled traversal: ``body_expressions_v2`` already knows every body shape, and a second
    reading here would disagree with it the first time a shape was added.
    """
    from featuregen.formula.schema_v2 import body_expressions_v2

    return tuple(body_expressions_v2(body))
