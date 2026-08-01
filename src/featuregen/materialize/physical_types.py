"""Spec §6 — the versioned physical type adapter: one governed formula → one published column type.

**The OPERATION decides the physical type; the logical word never does.**
``FormulaOutputPolicyV1.output_type`` is a LOGICAL type (``numeric`` / ``integer`` / ``decimal``),
and no mapping from it to a Hive/Spark type exists anywhere in this repository. Reading that word
and mapping it directly is the defect this module exists to prevent: a ``COUNT_DISTINCT`` is
logically ``integer`` and physically ``BIGINT``; a ``SUM`` publishes ``DECIMAL(p,s)`` taken from the
formula's OWN :class:`~featuregen.formula.schema.DecimalPolicy` whatever word Child-1 resolved.

**The word is not read at all.** ``FormulaOutputPolicyV1.output_type`` describes at most ONE operand
per formula — a SUM's own, a DIFFERENCE's *minuend*, and for a RATIO the constant ``"decimal"``,
which describes no operand at all (``output_authority``) — so a ratio's numerator and denominator and
a difference's subtrahend are unrepresentable in it. §6 requires EVERY arithmetic operand to be an
exact numeric, so the evidence is taken per EXPRESSION from
:class:`~featuregen.materialize.expression_ir.OperandTypeEvidence`, which carries each operand's own
governed C1 ``logical_representation`` (Task 8.1). Consulting the word as well would give one
question two answers, and the weaker one would sometimes win.

**Only an EXACT numeric may back a `DECIMAL` (§6).** The catalog's ``_NUMERIC_LOGICAL_TYPES`` is
*arithmetic-capable*, not exact — it contains ``float``, ``double``, ``double precision``, ``real``
and ``money`` — so "is it numeric?" is the wrong question: publishing ``DECIMAL(p,s)`` from a binary
float asserts a reproducibility float arithmetic does not have. Three operand states refuse, each
with its own explanation and all with ``PHYSICAL_TYPE_UNSUPPORTED`` (§14's only member for a typing
verdict, and this module must not invent one):

* a GOVERNED type outside :data:`_EXACT_NUMERIC_LOGICAL_TYPES` — known, and unsupported here;
* UNGOVERNED — no governed decision backs the operand's type, so the fixed-point claim would rest on
  a file declaration nobody attested;
* UNAVAILABLE — the type authority itself is degraded (fork / hash-mismatch / unprojectable), which
  is a different problem with a different remedy and is kept a distinct branch so the allowlist test
  cannot swallow it and report "not an exact numeric" about a type nobody could read.

§6's "never silently map ambiguous numerics" is honoured by refusing; no fallback type exists here.

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

* ``RoundingMode`` — §6 requires it be implemented explicitly, never left to an engine default.
  For a RATIO, ``HALF_EVEN`` therefore REFUSES: Spark's decimal division rounds HALF_UP at the
  result scale before any explicit rounding call in generated code runs, so the emitted call
  re-rounds an already-rounded value and the declared mode is recorded but never applied
  (DEFERRED-WORK A.28). Non-ratio bodies keep both modes — post-aggregate rounding of a
  narrower-scale value has no engine pre-rounding to fight;
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

**Evidence is REQUIRED, never defaulted.** ``operand_types`` is a keyword argument with no default
and its key set must equal the formula's body paths exactly. A default of ``{}`` would let a caller
skip §6's gate by omission — the fail-open shape this task exists to close — and a mapping that does
not line up with the body is a call assembled wrongly, so it raises ``ValueError`` rather than
borrowing a governed code for a programming error (the line Task 9 drew).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from featuregen.formula.schema import (
    AggregateExpression,
    AggregateFunction,
    DecimalPolicy,
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
from featuregen.materialize.expression_ir import OperandTypeEvidence, OperandTypeStatus

__all__ = [
    "PHYSICAL_TYPE_POLICY_VERSION",
    "PhysicalType",
    "resolve_physical_type",
]

#: The version of THIS mapping. It enters ``FeatureGroupPlanV1`` / ``group_plan_hash`` (§6) and the
#: materialization contract hash (§5.5), so a change to any rule below is a change of identity: a
#: column typed under a different policy is a different artifact even when the type string matches.
#:
#: **2** (Task 8.1): the operand rule changed from "the formula's one logical word is exact" to
#: "EVERY arithmetic operand carries a GOVERNED exact-numeric type". Formulas that resolved to a
#: ``DECIMAL`` under version 1 can refuse under version 2 — a float denominator was invisible to the
#: word, and an ungoverned operand type was accepted — so the two versions are not interchangeable
#: and a contract keyed on the old one describes a column decided by a rule that no longer applies.
#:
#: **3** (codegen-review Task 8): a RATIO declaring ``HALF_EVEN`` rounding now REFUSES — Spark's
#: decimal division rounds HALF_UP at the result scale before any explicit rounding call runs, so
#: the declared mode was recorded but never applied. A ratio + half_even formula that resolved
#: under version 2 refuses under version 3, and the version-2 column it described carries a
#: rounding claim the engine did not honour — the same accept→refuse non-interchangeability that
#: separated 1 from 2 (DEFERRED-WORK A.28).
PHYSICAL_TYPE_POLICY_VERSION = 3

#: The maximum precision a Hive/Spark ``DECIMAL`` can hold. A policy above it is not representable.
_MAX_DECIMAL_PRECISION = 38

#: §6's ``EXACT_NUMERIC`` set — the base logical types an ARITHMETIC operand may have. An ALLOWLIST,
#: deliberately: the complement (an unreadable type, a string, an inexact binary fraction, whatever a
#: future catalog adds) is open-ended, and a denylist would admit every future member of it. Base
#: names only — a parameterised ``numeric(18,2)`` is normalised to ``numeric`` before the test.
#:
#: §6 spells the set ``{numeric, decimal, integer, int, int4, int8, bigint, smallint}``. ``int2`` is
#: the one addition: it is PostgreSQL's own alias for ``smallint`` (as ``int4``/``int8``, which §6
#: DOES list, are for ``integer``/``bigint``), so excluding it would refuse a genuinely exact column
#: over its spelling while accepting its two siblings.
#:
#: Inexact binary floating-point types are absent on purpose, and this is §6's whole point: they ARE
#: in the catalog's ``_NUMERIC_LOGICAL_TYPES`` (``b_output_policy.py:65``,
#: ``output_authority.py:59``), so a "is it numeric?" test passes a float and publishes fixed-point
#: anyway. A float sum is order-dependent under parallel execution, so the value published from one
#: is not reproducible — and a money column that is not reproducible is worse than a refused one.
#: ``money`` is absent for a different reason: its scale is a locale setting rather than a declared
#: column property, so the ``(p,s)`` it would convert to is not knowable from governed metadata.
_EXACT_NUMERIC_LOGICAL_TYPES: frozenset[str] = frozenset(
    {"numeric", "decimal", "integer", "int", "int2", "int4", "int8", "bigint", "smallint"})

#: The aggregates whose result is a COUNT. Their operand is not an arithmetic operand — you may
#: ``COUNT_DISTINCT`` a text column, and the count is integral whatever the column holds — so §6's
#: exact-numeric rule does not apply to it. ``COUNT_ROWS`` has no operand at all (schema [c9]).
_COUNT_FUNCTIONS: frozenset[AggregateFunction] = frozenset({
    AggregateFunction.COUNT_ROWS,
    AggregateFunction.COUNT_NON_NULL,
    AggregateFunction.COUNT_DISTINCT,
})


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


def _refuse_ungoverned(detail: str) -> MaterializationRefused:
    """The compiler could not ESTABLISH a governed type (§14, architect ruling 2026-07-28).

    Distinct from :func:`_refuse` because the two route to different people: a missing or
    unreadable type authority is a metadata/attestation problem, while a governed-but-unsupported
    type is a formula or physical-type-policy problem. One code for both would eventually drive
    automation to the wrong remediation path. The detail preserves WHICH cause applied.
    """
    return MaterializationRefused(CompilationRefusalCode.OUTPUT_TYPE_NOT_GOVERNED, detail)


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


def _require_evidence(
    expressions: tuple[tuple[str, AggregateExpression], ...],
    operand_types: Mapping[str, OperandTypeEvidence],
) -> None:
    """The supplied evidence must describe EXACTLY this formula's expressions.

    Two directions, both fatal to the gate if unchecked. A MISSING body path would let a caller skip
    §6's rule for one half of a ratio by omitting it. An EXTRA one, or an entry whose
    ``operand_ref`` is not the operand of the expression at that path, means the evidence was
    gathered for a different formula — and then the operand actually summed is typed by a statement
    about a column it is not.

    Raises:
        ValueError: the mapping does not line up with the body. A call assembled wrongly is not a
            governed verdict — §14 has no member for it, and Task 9 drew this line for exactly the
            same reason (a malformed declaration must not borrow a governed code).
    """
    paths = {path for path, _expr in expressions}
    supplied = set(operand_types)
    if paths != supplied:
        raise ValueError(
            f"operand_types must describe exactly the formula's expressions: "
            f"missing {sorted(paths - supplied)}, unexpected {sorted(supplied - paths)}. "
            f"Evidence omitted for a body path would silently exempt that operand from §6's "
            f"exact-numeric rule, which is the fail-open this argument exists to close")
    mismatched = sorted(
        path for path, expr in expressions
        if operand_types[path].operand_ref != expr.operand)
    if mismatched:
        raise ValueError(
            f"{len(mismatched)} evidence entr(y/ies) name a different operand than the expression "
            f"at the same body path ({', '.join(mismatched)}): the evidence was gathered for "
            f"another formula, so it types a column this one does not read")


def _check_operand_types(
    expressions: tuple[tuple[str, AggregateExpression], ...],
    operand_types: Mapping[str, OperandTypeEvidence],
) -> MaterializationRefused | None:
    """§6 — refuse a published ``DECIMAL`` unless EVERY arithmetic operand is a governed exact
    numeric.

    Every expression, not the one the formula's word happened to describe: a ratio's numerator AND
    denominator, a difference's minuend AND subtrahend. A count is skipped because its result is
    integral whatever its operand holds, so the operand is not arithmetic — and its evidence is
    still carried, just not consulted.

    Two CODES, three branches (§14, architect ruling 2026-07-28): UNGOVERNED and UNAVAILABLE both
    mean the compiler could not ESTABLISH a governed type, so both carry
    ``OUTPUT_TYPE_NOT_GOVERNED`` with the cause preserved in the detail; a governed type that
    materialization cannot safely represent carries ``PHYSICAL_TYPE_UNSUPPORTED``. They route to
    different people — attest/repair the metadata, versus change the formula or the type policy.

    The three refusing states are separate branches on purpose. An UNGOVERNED or UNAVAILABLE operand
    carries no type at all, so it is also absent from the exact-numeric allowlist: a single check
    would refuse it — with the wrong explanation, blaming a type nobody could read. The branch order
    is what keeps "we know the type and cannot use it" apart from "we could not read the type".
    """
    for path, expr in expressions:
        if expr.aggregation in _COUNT_FUNCTIONS:
            continue
        evidence = operand_types[path]
        where = f"the {expr.aggregation.value} operand at {path}"
        if evidence.status is OperandTypeStatus.UNAVAILABLE:
            return _refuse_ungoverned(
                f"{where} has no readable governed type: the C1 type-authority read failed closed "
                f"({evidence.read_status}), so the operand's type is UNKNOWN rather than known and "
                f"unsupported. Publishing a fixed-point column here would convert a value whose "
                f"representation could not be established; the remedy is to repair the type "
                f"authority, not to change the column")
        if evidence.status is not OperandTypeStatus.GOVERNED or evidence.logical_type is None:
            return _refuse_ungoverned(
                f"{where} carries no GOVERNED logical type (C1 status {evidence.read_status!r}), "
                f"so a published DECIMAL(p,s) would rest on a type declaration nobody attested. "
                f"§6 admits an exact numeric as a governed fact, never as a file's word for itself")
        if not _is_exact_numeric(evidence.logical_type):
            return _refuse(
                f"{where} has governed logical type {evidence.logical_type!r}, which is not an "
                f"exact numeric: its aggregate is order-dependent under parallel execution, so no "
                f"fixed-point conversion of it is reproducible. A numeric type is not sufficient — "
                f"only an exact one may back a DECIMAL column")
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


def resolve_physical_type(
    formula: TypedFormulaV1,
    *,
    operand_types: Mapping[str, OperandTypeEvidence],
) -> PhysicalType | MaterializationRefused:
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

    Args:
        formula: the governed formula whose column is being typed.
        operand_types: one :class:`~featuregen.materialize.expression_ir.OperandTypeEvidence` per
            body path — exactly the mapping ``{e.expr_path: e.operand_type for e in ir.expressions}``
            a compiled ``FormulaExecutionIRV1`` already holds. Required, because §6's rule is only a
            gate if it cannot be skipped by omission.

    Returns:
        The resolved :class:`PhysicalType`, or a :class:`MaterializationRefused` carrying
        ``PHYSICAL_TYPE_UNSUPPORTED`` — an arithmetic operand that is not a governed exact numeric,
        a decimal policy outside what a Hive/Spark ``DECIMAL`` can represent, an overflow
        behaviour this slice does not implement, or ``HALF_EVEN`` rounding on a ratio, which the
        engine's own division pre-rounds out of existence.

    Raises:
        ValueError: ``operand_types`` does not describe exactly this formula's expressions. A call
            assembled wrongly is not a governed verdict (§14 has no member for it).
        featuregen.formula.schema.SchemaError: ``formula.body`` is outside Child-1's closed union,
            raised by ``schema.body_expressions``. A forged object is not a governed verdict, and
            §14's vocabulary has no member for it.
    """
    body = formula.body
    # The body-shape gate, asked of the module that owns the union rather than re-asked here — and
    # structural rather than positional: EVERY return below needs the nullability this feeds, so a
    # body Child-1 does not define cannot reach one of them.
    expressions = body_expressions(body)
    # Checked BEFORE the count short-circuit: a count publishes BIGINT without consulting its
    # operand's type, so validating the pairing only on the decimal path would let a mismatched
    # mapping — evidence gathered for another formula — pass unnoticed for every count.
    _require_evidence(expressions, operand_types)
    nullable = _is_nullable(body, expressions)
    operation = _published_operation(body)

    if operation in _COUNT_FUNCTIONS:
        # Integral by construction, so the DECIMAL policy governs nothing here: it is neither
        # validated nor carried (module docstring). The count's own width is the engine's BIGINT.
        return PhysicalType(sql_type="BIGINT", nullable=nullable, rounding=None, overflow=None)

    refusal = _check_operand_types(expressions, operand_types)
    if refusal is not None:
        return refusal

    sql_type = _decimal_type(formula.decimal)
    if isinstance(sql_type, MaterializationRefused):
        return sql_type
    if isinstance(body, RatioBody) and formula.decimal.rounding is RoundingMode.HALF_EVEN:
        # Spark's decimal `Divide` wraps its result in `CheckOverflow`, which rounds HALF_UP at
        # the division result scale (clamped to MINIMUM_ADJUSTED_SCALE=6) BEFORE any explicit
        # rounding call in the generated code runs — at the published scale the emitted `bround`
        # re-rounds an already-rounded value and is a no-op (verified empirically: 1/2000000 →
        # 0.000001, not the HALF_EVEN 0.000000). §6 requires the mode to be implemented
        # explicitly, and generated code alone cannot: the ties are gone before it runs. A
        # declaration the engine silently ignores refuses instead of being recorded as applied.
        # Non-ratio bodies keep both modes — post-aggregate rounding of a narrower-scale value
        # has no engine pre-rounding to fight. DEFERRED-WORK A.28 holds the lifting conditions.
        return _refuse(
            "the formula declares half_even rounding on a ratio, which generated code cannot "
            "honour: Spark decimal division rounds HALF_UP at the result scale before any "
            "explicit rounding call; declare half_up, or wait for engine-side support")
    return PhysicalType(
        sql_type=sql_type,
        nullable=nullable,
        # Carried, not defaulted: §6 requires the rounding to be explicit in generated code, and
        # ERROR overflow to fail rather than take the engine's NULL.
        rounding=formula.decimal.rounding,
        overflow=formula.decimal.overflow,
    )
