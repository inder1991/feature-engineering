"""Spec §6 — the versioned physical type adapter: one governed formula → one published column type.

**The OPERATION decides the physical type; the logical word never does.**
``FormulaOutputPolicyV1.output_type`` is a LOGICAL type (``numeric`` / ``integer`` / ``decimal``),
and no mapping from it to a Hive/Spark type exists anywhere in this repository. Reading that word
and mapping it directly is the defect this module exists to prevent: a ``COUNT_DISTINCT`` is
logically ``integer`` and physically ``BIGINT``; a ``SUM`` publishes ``DECIMAL(p,s)`` taken from the
formula's OWN :class:`~featuregen.formula.schema.DecimalPolicy` whatever word Child-1 resolved.

The word is still read — but as **evidence about the OPERAND**, never as a type to map. Child-1
resolves a SUM's / DIFFERENCE's ``output_type`` to the operand's governed ``logical_representation``
verbatim (``output_authority._numeric_output_type``), so it is the only visible statement about what
is being summed. It is consulted for exactly one question: is the operand an EXACT numeric? An
operand whose type is unreadable (Child-1's ``"unknown"``) or inexact refuses with
``PHYSICAL_TYPE_UNSUPPORTED`` rather than publishing a fixed-point column nobody governed. §6's
"never silently map ambiguous numerics" is honoured by refusing, and no fallback type exists here.

**Nullability is part of the type decision**, not a downstream detail: §9's gate compares the staged
column's nullability against this answer, so a wrong answer there makes that gate unenforceable. It
is derived from the formula's own policies (§8 rule 4) and never from the SQL type:

* ``EmptyWindowResult.NULL`` on ANY expression — each ``AggregateExpression`` owns its own window
  (interfaces §6), so a ratio has two of them and either can put a NULL in the column;
* ``NullInput.PROPAGATE`` on ANY expression — a null operand VALUE makes the aggregate null, which
  is a NULL on a NON-empty window. §6 lists only the two sources below; this third one is added
  deliberately, because declaring a column non-null that a propagating null can fill is the
  direction of that decision that produces a broken write rather than a refusal;
* ``ZeroDenominator.NULL`` on a ratio.

Everything else is non-null — including the ``ERROR`` members of those two enums, which abort the
run instead of producing a value, so a column that exists at all was written without them firing.

**Two obligations this module RESOLVES but cannot itself enforce**, carried on :class:`PhysicalType`
so the renderer receives them rather than re-deriving them:

* ``RoundingMode`` — §6 requires it be implemented explicitly, never left to an engine default;
* ``OverflowBehavior.ERROR`` — Spark's default on decimal overflow is to return NULL, so honouring
  ERROR is deliberate configuration plus an explicit check in generated code (§9's
  ``OVERFLOW_VIOLATION`` gate is the last line, not the first). ``SATURATE`` is a deferred NFR and
  is refused wherever the policy governs: nothing in this slice clamps, and accepting the request
  would quietly substitute different overflow semantics.

**Where the decimal policy is validated.** Exactly where it governs the published type. A count
publishes ``BIGINT``, so its ``DecimalPolicy`` reaches no rendered expression and is neither
validated nor carried — validating it would refuse a correct feature over an inert field, and
carrying it would put a rounding mode on an integral column. The renderer must therefore take its
rounding/overflow obligations from :class:`PhysicalType`, never from ``formula.decimal``.

**A blind spot §6 does not resolve, recorded rather than papered over.** ``resolve_physical_type``
sees one formula and one resolved logical word, and that word is derived from ONE operand: the SUM's
own operand, or a DIFFERENCE's MINUEND (``output_authority._resolve_difference``). A RATIO's word is
the constant ``"decimal"`` and describes no operand at all. So a ratio's numerator/denominator and a
difference's subtrahend are INVISIBLE here: an inexact operand in one of those positions cannot be
refused by this adapter. Catching it needs the per-expression C1 facts (``ExprFacts.output_type``),
which this signature does not receive.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from featuregen.formula.schema import (
    AggregateExpression,
    AggregateFunction,
    DecimalPolicy,
    DiffBody,
    EmptyWindowResult,
    FormulaBody,
    NullInput,
    OverflowBehavior,
    RatioBody,
    RoundingMode,
    TypedFormulaV1,
    UnaryBody,
    ZeroDenominator,
    body_expressions,
)
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused

__all__ = [
    "PHYSICAL_TYPE_POLICY_VERSION",
    "PhysicalType",
    "resolve_physical_type",
]

#: The version of THIS mapping. It enters ``FeatureGroupPlanV1`` / ``group_plan_hash`` (§6) and the
#: materialization contract hash (§5.5), so a change to any rule below is a change of identity: a
#: column typed under a different policy is a different artifact even when the type string matches.
PHYSICAL_TYPE_POLICY_VERSION = 1

#: The maximum precision a Hive/Spark ``DECIMAL`` can hold. A policy above it is not representable.
_MAX_DECIMAL_PRECISION = 38

#: The base logical types a SUM / DIFFERENCE operand may have. An ALLOWLIST, deliberately: the
#: complement (an unreadable type, a string, an inexact binary fraction) is open-ended, and a
#: denylist would admit every future member of it. Base names only — a parameterised
#: ``numeric(18,2)`` is normalised to ``numeric`` before the test.
#:
#: Inexact binary floating-point types are absent on purpose. Their sum is order-dependent under
#: parallel execution, so the fixed-point value published from one is not reproducible — and a
#: money column that is not reproducible is worse than a refused one. ``money`` is likewise absent:
#: its scale is a locale setting rather than a declared column property, so the ``(p,s)`` it would
#: convert to is not knowable from governed metadata.
_EXACT_NUMERIC_LOGICAL_TYPES: frozenset[str] = frozenset(
    {"numeric", "decimal", "integer", "int", "int2", "int4", "int8", "bigint", "smallint"})

_COUNT_FUNCTIONS: frozenset[AggregateFunction] = frozenset({
    AggregateFunction.COUNT_ROWS,
    AggregateFunction.COUNT_NON_NULL,
    AggregateFunction.COUNT_DISTINCT,
})

#: Child-1's "the operand's type was absent or is not numeric" marker (``output_authority``). Kept
#: as a named constant so the refusal reads as a governed verdict rather than a string comparison.
_UNKNOWN_LOGICAL_TYPE = "unknown"


@dataclass(frozen=True, slots=True)
class PhysicalType:
    """The published column type for ONE feature: the SQL type, its nullability, its obligations.

    ``rounding`` and ``overflow`` are ``None`` when the published type is integral — there is no
    decimal arithmetic for them to govern, and a rounding mode recorded against a ``BIGINT`` would
    be an instruction the renderer could act on wrongly.
    """

    sql_type: str
    nullable: bool
    rounding: RoundingMode | None
    overflow: OverflowBehavior | None

    def identity_payload(self) -> dict[str, Any]:
        """The identity-bearing fields (§6: the resolved type enters ``group_plan_hash``).

        Nullability, rounding and overflow are all in it: two columns both typed ``DECIMAL(18,2)``
        that round differently, or that differ on whether a NULL may appear, hold different numbers
        and must not share one identity. ``PHYSICAL_TYPE_POLICY_VERSION`` is deliberately NOT here —
        it is a property of the whole plan, contributed once by the caller that builds the contract
        (§5.5), not once per feature column.
        """
        return {
            "sql_type": self.sql_type,
            "nullable": self.nullable,
            "rounding": self.rounding.value if self.rounding is not None else None,
            "overflow": self.overflow.value if self.overflow is not None else None,
        }


def _refuse(detail: str) -> MaterializationRefused:
    """Every refusal in this module is §6's single code — the type could not be resolved."""
    return MaterializationRefused(CompilationRefusalCode.PHYSICAL_TYPE_UNSUPPORTED, detail)


def _base_logical_type(word: str) -> str:
    """The comparable base of a Child-1 logical type word.

    Child-1 carries ``logical_representation`` VERBATIM, so the word may arrive parameterised
    (``numeric(18,2)``), upper-cased, or padded. Normalising the same way
    ``output_authority._is_numeric_logical_type`` does keeps one question with one answer.
    """
    return word.lower().split("(")[0].strip()


def _is_exact_numeric(word: str) -> bool:
    return _base_logical_type(word) in _EXACT_NUMERIC_LOGICAL_TYPES


def _decimal_type(policy: DecimalPolicy) -> str | MaterializationRefused:
    """``DECIMAL(p,s)`` for a policy this slice can honour, else the §6 refusal.

    ``schema._check_decimal`` only requires ``scale >= 0`` and ``precision >= scale``, so a
    zero-width decimal is an input a validated Child-1 formula can genuinely carry — the bounds
    here are not restating a check that already happened upstream.
    """
    if policy.overflow is OverflowBehavior.SATURATE:
        return _refuse(
            "the formula declares SATURATE overflow, which this slice does not implement: nothing "
            "clamps a decimal here, so publishing the column would substitute a different overflow "
            "semantics for the one the formula asked for")
    if not 1 <= policy.precision <= _MAX_DECIMAL_PRECISION:
        return _refuse(
            f"decimal precision {policy.precision} is outside the representable range "
            f"1..{_MAX_DECIMAL_PRECISION} for a Hive/Spark DECIMAL")
    if not 0 <= policy.scale <= policy.precision:
        return _refuse(
            f"decimal scale {policy.scale} is outside 0..{policy.precision} and so does not "
            f"describe a representable DECIMAL")
    return f"DECIMAL({policy.precision},{policy.scale})"


def _operand_evidence(body: FormulaBody) -> AggregateExpression | None:
    """The expression the formula's logical word actually describes, or ``None`` if it describes no
    operand at all.

    Child-1 derives the word from ONE place per body shape (``output_authority``): a unary body's
    own expression, a DIFFERENCE's MINUEND, and — for a RATIO — nowhere, because ``"decimal"`` is a
    constant. Treating the word as evidence about any other expression would be reading a fact
    about the numerator off a statement about the denominator.
    """
    if isinstance(body, UnaryBody):
        return body.expr
    if isinstance(body, DiffBody):
        return body.minuend
    return None


def _check_operand_type(body: FormulaBody, word: str) -> MaterializationRefused | None:
    """Refuse a published DECIMAL whose visible operand is not an exact numeric.

    A count's operands are integral by construction, so the word is not evidence about them (a
    ``COUNT_ROWS`` has no operand for Child-1 to read a type from at all, and its word is
    ``"unknown"`` for that reason rather than because anything is wrong). The check therefore
    applies only where the evidence exists: an aggregate that is not a count.
    """
    evidence = _operand_evidence(body)
    if evidence is None or evidence.aggregation in _COUNT_FUNCTIONS:
        return None
    if _base_logical_type(word) == _UNKNOWN_LOGICAL_TYPE:
        return _refuse(
            f"the operand of {evidence.aggregation.value} has no readable governed type "
            f"(Child-1 resolved {_UNKNOWN_LOGICAL_TYPE!r}), so publishing a fixed-point column "
            f"from it would be a numeric claim no governed fact supports")
    if not _is_exact_numeric(word):
        return _refuse(
            f"the operand of {evidence.aggregation.value} has logical type {word!r}, which is not "
            f"an exact numeric: the aggregate is not reproducible, so no fixed-point conversion of "
            f"it is unambiguous")
    return None


def _is_nullable(
    body: FormulaBody, expressions: tuple[tuple[str, AggregateExpression], ...]
) -> bool:
    """Whether the published column can hold a NULL, from the formula's own policies (§8 rule 4)."""
    for _path, expr in expressions:
        if expr.window.empty_window is EmptyWindowResult.NULL:
            return True
        if expr.window.null_input is NullInput.PROPAGATE:
            return True
    return isinstance(body, RatioBody) and body.zero_denominator is ZeroDenominator.NULL


def _published_operation(body: FormulaBody) -> AggregateFunction | None:
    """The aggregate whose output IS the feature, or ``None`` when a final operation produces it.

    A RATIO and a DIFFERENCE publish the result of the final operation, not of either half, so §6's
    table keys them on the body shape; only a unary body publishes an aggregate directly. A body
    outside the union is not rejected here — ``body_expressions`` already answers that question,
    and asking it twice is two places for the answer to differ.
    """
    return body.expr.aggregation if isinstance(body, UnaryBody) else None


def resolve_physical_type(formula: TypedFormulaV1) -> PhysicalType | MaterializationRefused:
    """The published column type for ``formula`` (§6), or a typed refusal.

    The mapping, keyed on the OPERATION:

    ==========================================  ====================================
    operation                                   published type
    ==========================================  ====================================
    ``COUNT_ROWS`` / ``COUNT_NON_NULL`` /       ``BIGINT``
    ``COUNT_DISTINCT``
    ``SUM``                                     ``DECIMAL(p,s)`` from ``DecimalPolicy``
    ``RATIO``                                   ``DECIMAL(p,s)`` from ``DecimalPolicy``
    ``DIFFERENCE``                              ``DECIMAL(p,s)`` from ``DecimalPolicy``
    ==========================================  ====================================

    Nullability comes from the formula's ``EmptyWindowResult`` / ``NullInput`` / ``ZeroDenominator``
    policies, never from the SQL type — a ``BIGINT`` count over a window declared ``NULL`` when
    empty is a nullable column.

    A refusal is RETURNED rather than raised: one refused feature is one governed verdict among the
    many a compilation collects, the shape ``ir.compile_ir`` already uses.

    Returns:
        The resolved :class:`PhysicalType`, or a :class:`MaterializationRefused` carrying
        ``PHYSICAL_TYPE_UNSUPPORTED`` — an operand this slice cannot convert unambiguously, a
        decimal policy outside what a Hive/Spark ``DECIMAL`` can represent, or an overflow behaviour
        it does not implement.

    Raises:
        featuregen.formula.schema.SchemaError: ``formula.body`` is outside Child-1's closed union,
            raised by ``schema.body_expressions``. A forged object is not a governed verdict, and
            §14's vocabulary has no member for it.
    """
    body = formula.body
    # The body-shape gate, asked of the module that owns the union rather than re-asked here — and
    # structural rather than positional: EVERY return below needs the nullability this feeds, so a
    # body Child-1 does not define cannot reach one of them.
    expressions = body_expressions(body)
    nullable = _is_nullable(body, expressions)
    operation = _published_operation(body)

    if operation in _COUNT_FUNCTIONS:
        # Integral by construction, so the DECIMAL policy governs nothing here: it is neither
        # validated nor carried (module docstring). The count's own width is the engine's BIGINT.
        return PhysicalType(sql_type="BIGINT", nullable=nullable, rounding=None, overflow=None)

    refusal = _check_operand_type(body, formula.output.output_type)
    if refusal is not None:
        return refusal

    sql_type = _decimal_type(formula.decimal)
    if isinstance(sql_type, MaterializationRefused):
        return sql_type
    return PhysicalType(
        sql_type=sql_type,
        nullable=nullable,
        # Carried, not defaulted: §6 requires the rounding to be explicit in generated code, and
        # ERROR overflow to fail rather than take the engine's NULL.
        rounding=formula.decimal.rounding,
        overflow=formula.decimal.overflow,
    )
