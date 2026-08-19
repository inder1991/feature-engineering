"""The published column type for a V3 formula — the driver `physical_types_v2` never had.

**What existed and what did not.** ``physical_types_v2`` ships the arithmetic: ``sum_type_v2``,
``multiply_type_v2``, ``product_sum_type_v2``, ``DecimalTypeV2`` and their overflow refusals. What it
does not ship is anything that *walks a formula and decides its column type* — the job
``resolve_physical_type`` does for V1. So ``PlannedFeature.physical_type``, which hard-requires a
resolved type, could never be filled for a V2 feature. The primitives were the hard half and the
driver was missing entirely.

**The DECLARED decimal policy governs, and ``sum_type_v2``'s widening is NOT applied.** Widening
needs the operand's real precision and scale; ``OperandTypeEvidence`` carries a governed *word*
(``"numeric"``), not a width, and nothing else in the compiled IR establishes one. Publishing a
wider type than the author declared, derived from a precision nobody read, is a worse answer than
publishing what they asked for. An earlier draft took ``DecimalTypeV2`` values, which no caller in
this codebase can produce — a signature satisfiable only by a test that invented them.

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
from featuregen.materialize.expression_ir import OperandTypeEvidence, OperandTypeStatus
from featuregen.materialize.physical_types import (
    _MAX_DECIMAL_PRECISION,
    PhysicalType,
    _is_exact_numeric,
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
_TYPEABLE = COUNTING_AGGREGATES | {
    AggregateFunctionV2.SUM,
    # Step 11 — the ordinary aggregates. They need no new typing RULE: each publishes the formula's
    # declared decimal policy, exactly as a sum does, and each is refused unless its operand is a
    # governed exact numeric. A `min` over a DATE is therefore refused rather than published as a
    # DECIMAL, which is the honest answer while the published type is a decimal.
    AggregateFunctionV2.AVG,
    AggregateFunctionV2.MIN,
    AggregateFunctionV2.MAX,
}


def resolve_physical_type_v3(
    proposal: TypedFormulaProposalV2 | TypedFormulaProposalV3,
    *,
    operand_types: Mapping[str, OperandTypeEvidence],
) -> PhysicalType | MaterializationRefused:
    """The published column type for ``proposal``, or a typed refusal.

    ==============================  ==========================================================
    aggregate / final operation     published type
    ==============================  ==========================================================
    counting aggregates             ``BIGINT``
    everything else                 ``DECIMAL(p,s)`` from the formula's declared ``DecimalPolicy``
    ==============================  ==========================================================

    **The DECLARED policy governs, including for SUM.** ``physical_types_v2`` ships ``sum_type_v2``,
    which widens ``DECIMAL(18,2)`` to ``DECIMAL(28,2)`` because summing N rows needs headroom, and
    it is deliberately NOT applied here. Widening needs the operand's real precision and scale, and
    nothing in the compiled IR establishes them — ``OperandTypeEvidence`` carries a governed
    *word* (``"numeric"``), not a width. Widening on anything less would publish a type the author
    did not declare, derived from a precision nobody read. An earlier draft of this module took
    ``DecimalTypeV2`` values instead, which no caller in this codebase can produce; that signature
    was satisfiable only by a test that invented them.

    Nullability comes from the POLICIES, never from the SQL type: a ``BIGINT`` count over a window
    declared ``null`` when empty is a nullable column, and reading nullability off the type would
    publish a column the pipeline can write NULLs into as ``NOT NULL``.

    Args:
        operand_types: one :class:`OperandTypeEvidence` per BODY PATH — exactly the mapping
            ``{e.expr_path: e.operand_type for e in ir.expressions}`` a compiled IR already holds.
            Required, because §6's exact-numeric rule is only a gate if it cannot be skipped by
            omission.

    Returns:
        The resolved :class:`PhysicalType`, or a :class:`MaterializationRefused`.

    Raises:
        ValueError: the evidence does not describe exactly this formula's expressions. A call
            assembled wrongly is not a governed verdict.
    """
    body = proposal.body
    final = _final_operation(body)
    expressions = _expressions(body)
    if not expressions:
        return MaterializationRefused(
            CompilationRefusalCode.FORMULA_SCHEMA_UNSUPPORTED,
            "a formula with no expressions publishes no value, so there is no column to type")

    paths = _paths_for(final, expressions)
    _require_evidence(paths, expressions, operand_types)

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
    # ratio or difference OF counts is an arithmetic result, not a count. Checked before the operand
    # rule because a count publishes BIGINT without consulting its operand's type.
    if final is FinalOperationV2.IDENTITY and _aggregation_of(
            expressions[0]) in COUNTING_AGGREGATES:
        return PhysicalType(sql_type="BIGINT", nullable=nullable, rounding=None, overflow=None)

    refusal = _check_operand_types(paths, expressions, operand_types)
    if refusal is not None:
        return refusal

    policy: DecimalPolicy = proposal.decimal
    sql_type = _decimal_type(policy)
    if isinstance(sql_type, MaterializationRefused):
        return sql_type
    if final is FinalOperationV2.RATIO and RoundingMode(policy.rounding) is RoundingMode.HALF_EVEN:
        # V1's finding, and it is about the ENGINE rather than about a language: Spark's decimal
        # divide wraps its result in CheckOverflow, which rounds HALF_UP at the division result
        # scale BEFORE any explicit rounding the generated code performs — so the ties are gone
        # before the emitted `bround` runs, and a declaration the engine silently ignores is
        # refused rather than recorded as applied.
        return MaterializationRefused(
            CompilationRefusalCode.PHYSICAL_TYPE_UNSUPPORTED,
            "the formula declares half_even rounding on a ratio, which generated code cannot "
            "honour: Spark decimal division rounds HALF_UP at the result scale before any explicit "
            "rounding call; declare half_up, or wait for engine-side support")

    return PhysicalType(
        sql_type=sql_type,
        nullable=nullable,
        # Carried, not defaulted: §6 requires the rounding to be explicit in generated code, and
        # ERROR overflow to fail rather than take the engine's NULL.
        rounding=RoundingMode(policy.rounding),
        overflow=OverflowBehavior(policy.overflow))


def _paths_for(final: FinalOperationV2, expressions: tuple) -> tuple[str, ...]:
    """The body paths this formula's expressions sit at, in the final operation's own order.

    Taken from the compiler's map rather than restated, so the evidence is keyed by exactly the
    paths a compiled IR keys its expressions by. A second spelling here would pair evidence with a
    different expression than the one it was gathered for, which is the failure `_require_evidence`
    exists to catch.
    """
    from featuregen.materialize.compile_ir_v2 import _BODY_PATHS_BY_OPERATION

    paths = _BODY_PATHS_BY_OPERATION.get(final)
    if paths is None or len(paths) != len(expressions):
        # Typing an operation with no body-path spelling is the compiler's refusal to make, not
        # this module's; fall back to whatever the expressions themselves declare so the evidence
        # check below still says something true.
        return tuple(getattr(e, "expr_path", f"body.expr[{i}]")
                     for i, e in enumerate(expressions))
    return paths


def _require_evidence(
    paths: tuple[str, ...], expressions: tuple,
    operand_types: Mapping[str, OperandTypeEvidence],
) -> None:
    """The evidence must describe EXACTLY this formula's expressions, both directions.

    A MISSING body path would let a caller skip the exact-numeric rule for one half of a ratio by
    omitting it. An EXTRA one — or an entry naming a different operand than the expression at that
    path — means the evidence was gathered for another formula, and the operand actually summed is
    then typed by a statement about a column it is not.
    """
    supplied = set(operand_types)
    if set(paths) != supplied:
        raise ValueError(
            f"operand_types must describe exactly the formula's expressions: "
            f"missing {sorted(set(paths) - supplied)}, unexpected {sorted(supplied - set(paths))}. "
            f"Evidence omitted for a body path would silently exempt that operand from the "
            f"exact-numeric rule, which is the fail-open this argument exists to close")
    mismatched = sorted(
        path for path, expression in zip(paths, expressions, strict=True)
        if operand_types[path].operand_ref != _operand_of(expression))
    if mismatched:
        raise ValueError(
            f"{len(mismatched)} evidence entr(y/ies) name a different operand than the expression "
            f"at the same body path ({', '.join(mismatched)}): the evidence was gathered for "
            f"another formula, so it types a column this one does not read")


def _check_operand_types(
    paths: tuple[str, ...], expressions: tuple,
    operand_types: Mapping[str, OperandTypeEvidence],
) -> MaterializationRefused | None:
    """Refuse a published DECIMAL unless EVERY arithmetic operand is a governed exact numeric.

    Every expression, not the one the formula's word happened to describe: a ratio's numerator AND
    denominator, a difference's minuend AND subtrahend. Counts are skipped — their result is
    integral whatever the operand holds — and their evidence is still carried, just not consulted.

    Three branches, deliberately, because they route to different people: an UNAVAILABLE read means
    the type could not be ESTABLISHED (repair the type authority), an ungoverned one means nobody
    attested it (attest it), and a governed non-exact type means the formula or the policy has to
    change. One check would refuse all three with the last one's explanation.
    """
    for path, expression in zip(paths, expressions, strict=True):
        if _aggregation_of(expression) in COUNTING_AGGREGATES:
            continue
        evidence = operand_types[path]
        where = f"the {_aggregation_of(expression)} operand at {path}"
        if evidence.status is OperandTypeStatus.UNAVAILABLE:
            return MaterializationRefused(
                CompilationRefusalCode.OUTPUT_TYPE_NOT_GOVERNED,
                f"{where} has no readable governed type: the type-authority read failed closed "
                f"({evidence.read_status}), so the operand's type is UNKNOWN rather than known and "
                f"unsupported. The remedy is to repair the type authority, not to change the column")
        if evidence.status is not OperandTypeStatus.GOVERNED or evidence.logical_type is None:
            return MaterializationRefused(
                CompilationRefusalCode.OUTPUT_TYPE_NOT_GOVERNED,
                f"{where} carries no GOVERNED logical type (status {evidence.read_status!r}), so a "
                f"published DECIMAL(p,s) would rest on a type declaration nobody attested")
        if not _is_exact_numeric(evidence.logical_type):
            return MaterializationRefused(
                CompilationRefusalCode.PHYSICAL_TYPE_UNSUPPORTED,
                f"{where} has governed logical type {evidence.logical_type!r}, which is not an "
                f"exact numeric: its aggregate is order-dependent under parallel execution, so no "
                f"fixed-point conversion of it is reproducible")
    return None


def _decimal_type(policy: DecimalPolicy) -> str | MaterializationRefused:
    """``DECIMAL(p,s)`` for a policy this slice can honour, else the refusal."""
    if OverflowBehavior(policy.overflow) is OverflowBehavior.SATURATE:
        return MaterializationRefused(
            CompilationRefusalCode.PHYSICAL_TYPE_UNSUPPORTED,
            "the formula declares SATURATE overflow, which this slice does not implement: nothing "
            "clamps a decimal here, so publishing the column would substitute a different overflow "
            "semantics for the one the formula asked for")
    if not 1 <= policy.precision <= _MAX_DECIMAL_PRECISION:
        return MaterializationRefused(
            CompilationRefusalCode.PHYSICAL_TYPE_UNSUPPORTED,
            f"decimal precision {policy.precision} is outside the representable range "
            f"1..{_MAX_DECIMAL_PRECISION} for a Hive/Spark DECIMAL")
    if not 0 <= policy.scale <= policy.precision:
        return MaterializationRefused(
            CompilationRefusalCode.PHYSICAL_TYPE_UNSUPPORTED,
            f"decimal scale {policy.scale} is outside 0..{policy.precision} and so does not "
            f"describe a representable DECIMAL")
    return f"DECIMAL({policy.precision},{policy.scale})"


def _operand_of(expression) -> str | None:
    return getattr(expression, "operand", None) or getattr(expression, "operand_ref", None)


def _is_nullable(proposal, final, expressions) -> bool:
    """Whether the published column can hold NULL, from the POLICIES rather than the SQL type.

    FOUR independent sources, any one of which makes the column nullable:

    * an empty window declared ``null`` — a grain key with no rows in range produces NULL;
    * a null input declared ``propagate`` — one NULL row makes the aggregate NULL;
    * a null input declared ``ignore`` on a NON-COUNT aggregate — a non-empty window in which every
      row's operand is NULL aggregates to NULL, and the renderer deliberately does not coalesce it;
    * a ratio whose zero denominator is declared ``null``.

    **The third was missing here and is V1's.** It matters most for the aggregates step 11 adds:
    ``avg``/``min``/``max`` over an all-NULL group are NULL in Spark exactly as ``sum`` is, and
    publishing the column NOT NULL would reject rows the pipeline legitimately writes. Counts are
    exempt from this source ALONE — every COUNT answers an all-null group with 0 — and from no
    other: ``BIGINT`` says nothing about nullability, so a count over a window declared
    NULL-when-empty is still a nullable column.
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
            if (str(getattr(window, "null_input", "")) == NullInput.IGNORE.value
                    and _aggregation_of(expression) not in COUNTING_AGGREGATES):
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
