"""Task 8 + 8.1 — the versioned physical type adapter (spec §6).

Two properties under test throughout.

**1. The OPERATION decides the physical type, not the logical word.**
``FormulaOutputPolicyV1.output_type`` is a logical type (``numeric`` / ``integer`` / ``decimal``);
mapping that word straight onto a Hive type is the defect this module exists to prevent. A
``COUNT_DISTINCT`` is logically ``integer`` and physically ``BIGINT``; a ``SUM`` is ``DECIMAL(p,s)``
from the formula's OWN ``DecimalPolicy`` whatever word Child-1 resolved.

**2. Only a GOVERNED EXACT numeric operand may back a ``DECIMAL`` — and EVERY arithmetic operand is
checked (Task 8.1).** "Numeric" and "exact-numeric" are different questions: the catalog's
``_NUMERIC_LOGICAL_TYPES`` contains ``float``, ``double``, ``double precision``, ``real`` and
``money``, so a float ratio passes a numericness test and still publishes fixed-point — asserting a
reproducibility float arithmetic does not have. And the check cannot be asked of Child-1's formula
word, which describes a SUM's own operand, a DIFFERENCE's MINUEND, and for a RATIO nothing at all;
so the evidence is per-EXPRESSION ``OperandTypeEvidence``, carried by the IR.

The formulas here are built by hand rather than taken from ``fixtures.py`` because the fixtures are
the three worked features — they cover one nullability combination between them, and this module's
whole surface is the combinations. The worked features are asserted too (at the end of the file),
so the synthetic builders cannot drift away from a real Child-1 formula; and one end-to-end test
compiles a real expression against the real governed catalog, so the hand-built evidence cannot
drift away from what ``compile_expression`` actually produces.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from tests.featuregen.materialize import fixtures

from featuregen.formula.schema import (
    CANONICALIZATION_VERSION,
    FORMULA_SCHEMA_VERSION,
    OPERATION_GRAMMAR_VERSION,
    OUTPUT_POLICY_VERSION,
    AdditivityClass,
    AggregateExpression,
    AggregateFunction,
    DecimalPolicy,
    DiffBody,
    EmptyWindowResult,
    FormulaOutputPolicyV1,
    Grain,
    Inclusivity,
    NullInput,
    OverflowBehavior,
    RatioBody,
    RoundingMode,
    SchemaError,
    SourceRelation,
    TypedFormulaV1,
    UnaryBody,
    WindowBasis,
    WindowPolicy,
    WindowUnit,
    ZeroDenominator,
    body_expressions,
)
from featuregen.materialize import physical_types
from featuregen.materialize.canonical import materialize_hash
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.expression_ir import OperandTypeEvidence, OperandTypeStatus
from featuregen.materialize.physical_types import (
    PHYSICAL_TYPE_POLICY_VERSION,
    PhysicalType,
    resolve_physical_type,
)

TABLE_REF = "hdfc::public.transactions"
REF_AMT = f"{TABLE_REF}.txn_amt"
REF_DT = f"{TABLE_REF}.txn_dt"
REF_CIF = f"{TABLE_REF}.cif_id"

_POLICY = DecimalPolicy(precision=18, scale=2, rounding=RoundingMode.HALF_UP,
                        overflow=OverflowBehavior.ERROR)


# ── builders ─────────────────────────────────────────────────────────────────────────────────────

def _window(
    *,
    empty_window: EmptyWindowResult = EmptyWindowResult.ZERO,
    null_input: NullInput = NullInput.IGNORE,
) -> WindowPolicy:
    return WindowPolicy(
        event_time_ref=REF_DT, basis=WindowBasis.TRAILING, length=30, unit=WindowUnit.DAY,
        start_inclusive=Inclusivity.INCLUSIVE, end_inclusive=Inclusivity.EXCLUSIVE,
        timezone="Asia/Kolkata", empty_window=empty_window, null_input=null_input)


def _expr(
    aggregation: AggregateFunction,
    *,
    operand: str | None = REF_AMT,
    empty_window: EmptyWindowResult = EmptyWindowResult.ZERO,
    null_input: NullInput = NullInput.IGNORE,
) -> AggregateExpression:
    return AggregateExpression(
        aggregation=aggregation,
        operand=None if aggregation is AggregateFunction.COUNT_ROWS else operand,
        source_relation=SourceRelation(table_ref=TABLE_REF), filter=None,
        window=_window(empty_window=empty_window, null_input=null_input))


def _formula(body, *, output_type: str, decimal: DecimalPolicy = _POLICY) -> TypedFormulaV1:
    """A ``TypedFormulaV1`` with a chosen body, logical word and decimal policy."""
    return TypedFormulaV1(
        formula_schema_version=FORMULA_SCHEMA_VERSION,
        operation_grammar_version=OPERATION_GRAMMAR_VERSION,
        output_policy_version=OUTPUT_POLICY_VERSION,
        canonicalization_version=CANONICALIZATION_VERSION,
        grain=Grain(entity="customer", keys=(REF_CIF,)),
        body=body, parameters=(), decimal=decimal,
        output=FormulaOutputPolicyV1(
            output_type=output_type, unit=None, currency=None,
            output_additivity=AdditivityClass.NON_ADDITIVE, external_type_required=False))


def _sum(*, output_type: str = "numeric", decimal: DecimalPolicy = _POLICY,
         empty_window: EmptyWindowResult = EmptyWindowResult.ZERO,
         null_input: NullInput = NullInput.IGNORE) -> TypedFormulaV1:
    return _formula(
        UnaryBody(expr=_expr(AggregateFunction.SUM, empty_window=empty_window,
                             null_input=null_input)),
        output_type=output_type, decimal=decimal)


def _count(aggregation: AggregateFunction = AggregateFunction.COUNT_DISTINCT, *,
           output_type: str = "integer", decimal: DecimalPolicy = _POLICY,
           empty_window: EmptyWindowResult = EmptyWindowResult.ZERO) -> TypedFormulaV1:
    return _formula(UnaryBody(expr=_expr(aggregation, empty_window=empty_window)),
                    output_type=output_type, decimal=decimal)


def _ratio(*, zero_denominator: ZeroDenominator = ZeroDenominator.ZERO,
           numerator_empty: EmptyWindowResult = EmptyWindowResult.ZERO,
           denominator_empty: EmptyWindowResult = EmptyWindowResult.ZERO,
           decimal: DecimalPolicy = _POLICY) -> TypedFormulaV1:
    return _formula(
        RatioBody(numerator=_expr(AggregateFunction.SUM, empty_window=numerator_empty),
                  denominator=_expr(AggregateFunction.SUM, empty_window=denominator_empty),
                  zero_denominator=zero_denominator),
        output_type="decimal", decimal=decimal)


def _difference(*, minuend: AggregateFunction = AggregateFunction.SUM,
                subtrahend: AggregateFunction = AggregateFunction.SUM,
                output_type: str = "numeric",
                decimal: DecimalPolicy = _POLICY) -> TypedFormulaV1:
    return _formula(DiffBody(minuend=_expr(minuend), subtrahend=_expr(subtrahend)),
                    output_type=output_type, decimal=decimal)


# ── operand type evidence (Task 8.1) ─────────────────────────────────────────────────────────────

def _governed(logical_type: str, *, ref: str = REF_AMT) -> OperandTypeEvidence:
    return OperandTypeEvidence(operand_ref=ref, status=OperandTypeStatus.GOVERNED,
                               logical_type=logical_type, read_status="resolved")


def _ungoverned(*, ref: str = REF_AMT, read_status: str = "no_decision") -> OperandTypeEvidence:
    """A read that SUCCEEDED and found no governed decision. The type slot is empty by
    construction — the evidence type has no way to carry an unattested word."""
    return OperandTypeEvidence(operand_ref=ref, status=OperandTypeStatus.UNGOVERNED,
                               logical_type=None, read_status=read_status)


def _unavailable(*, ref: str = REF_AMT, read_status: str = "hash_mismatch") -> OperandTypeEvidence:
    """A read that FAILED CLOSED — fork / hash_mismatch / projection_unavailable."""
    return OperandTypeEvidence(operand_ref=ref, status=OperandTypeStatus.UNAVAILABLE,
                               logical_type=None, read_status=read_status)


def _evidence(formula: TypedFormulaV1, **overrides: OperandTypeEvidence) -> dict:
    """One evidence entry per body path: GOVERNED ``numeric`` unless a test says otherwise.

    Keyed by body path with the dots dropped so a test can write ``denominator=_governed("real")``.
    The default is the ordinary case — a governed exact operand — so every test that is NOT about
    operand types reads as it did before Task 8.1.
    """
    keyed = {f"body.{name}": value for name, value in overrides.items()}
    built = {}
    for path, expr in body_expressions(formula.body):
        if path in keyed:
            built[path] = keyed[path]
        elif expr.operand is None:
            built[path] = OperandTypeEvidence(
                operand_ref=None, status=OperandTypeStatus.NO_OPERAND, logical_type=None,
                read_status=None)
        else:
            built[path] = _governed("numeric", ref=expr.operand)
    assert set(keyed) <= set(built), f"override names a path this body does not have: {keyed}"
    return built


def _resolved(formula: TypedFormulaV1, **overrides: OperandTypeEvidence) -> PhysicalType:
    """Resolve, asserting success — a refusal here is a test failure, not a value."""
    result = resolve_physical_type(formula, operand_types=_evidence(formula, **overrides))
    assert isinstance(result, PhysicalType), result
    return result


def _refusal(formula: TypedFormulaV1, **overrides: OperandTypeEvidence) -> MaterializationRefused:
    result = resolve_physical_type(formula, operand_types=_evidence(formula, **overrides))
    assert isinstance(result, MaterializationRefused), result
    return result


# ── the policy version ───────────────────────────────────────────────────────────────────────────

def test_the_policy_version_is_a_declared_constant():
    """§6: it enters ``FeatureGroupPlanV1``/``group_plan_hash`` and the contract hash (§5.5).

    **2**, not 1: Task 8.1 changed the operand rule from "the formula's one logical word is exact"
    to "EVERY arithmetic operand carries a GOVERNED exact-numeric type". A float denominator that
    resolved to ``DECIMAL`` under version 1 refuses under version 2, so a contract keyed on the old
    version describes a column decided by a rule that no longer applies.
    """
    assert PHYSICAL_TYPE_POLICY_VERSION == 2


# ── counts → BIGINT ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("aggregation", [
    AggregateFunction.COUNT_ROWS,
    AggregateFunction.COUNT_NON_NULL,
    AggregateFunction.COUNT_DISTINCT,
])
def test_every_count_publishes_BIGINT(aggregation):
    assert _resolved(_count(aggregation)).sql_type == "BIGINT"


def test_the_operation_beats_the_logical_word_for_a_count():
    """A ``COUNT_DISTINCT`` is logically ``integer``; a word saying otherwise cannot move it.

    Mapping the logical word is the defect §6 exists to prevent, so the discriminating case is a
    formula whose word DISAGREES with its operation: an implementation reading the word would
    publish a decimal here.
    """
    assert _resolved(_count(output_type="decimal")).sql_type == "BIGINT"


def test_the_operation_beats_the_logical_word_for_a_sum():
    """The mirror: a SUM over an INTEGER operand still publishes the formula's DECIMAL(p,s)."""
    assert _resolved(_sum(output_type="integer")).sql_type == "DECIMAL(18,2)"


# ── SUM / RATIO / DIFFERENCE → DECIMAL(p,s) from the formula's OWN policy ────────────────────────

def test_sum_takes_precision_and_scale_from_the_formulas_decimal_policy():
    policy = DecimalPolicy(precision=38, scale=6, rounding=RoundingMode.HALF_EVEN,
                           overflow=OverflowBehavior.ERROR)
    assert _resolved(_sum(decimal=policy)).sql_type == "DECIMAL(38,6)"


def test_ratio_takes_precision_and_scale_from_the_formulas_decimal_policy():
    assert _resolved(_ratio()).sql_type == "DECIMAL(18,2)"


def test_difference_takes_precision_and_scale_from_the_formulas_decimal_policy():
    assert _resolved(_difference()).sql_type == "DECIMAL(18,2)"


def test_a_difference_of_two_counts_is_decimal_though_no_operand_type_exists():
    """§6 keys the table on the FINAL operation, and a count's operands are integral by
    construction — so two ``COUNT_ROWS`` halves, which have no operand at all, cannot refuse it."""
    assert _resolved(
        _difference(minuend=AggregateFunction.COUNT_ROWS,
                    subtrahend=AggregateFunction.COUNT_ROWS,
                    output_type="unknown")).sql_type == "DECIMAL(18,2)"


# ══ Task 8.1 — EVERY arithmetic operand must be a GOVERNED EXACT numeric (§6) ════════════════════
# The seven acceptance cases fixed by the architect's 2026-07-27 ruling, plus the boundaries that
# make each of them a discriminator rather than a coincidence.


def test_exact_decimal_ratio_survives():
    """The positive case, and it must not be carried by the DEFAULT evidence: both halves are given
    EXPLICIT exact types that differ from each other and from the builder's default."""
    resolved = _resolved(_ratio(), numerator=_governed("decimal"), denominator=_governed("integer"))
    assert resolved.sql_type == "DECIMAL(18,2)"


@pytest.mark.parametrize("exact", sorted(physical_types._EXACT_NUMERIC_LOGICAL_TYPES))
def test_every_allowlisted_exact_type_publishes_a_decimal(exact):
    assert _resolved(_sum(), expr=_governed(exact)).sql_type == "DECIMAL(18,2)"


def test_string_operand_dies():
    """A SUM over a text column is not arithmetic at all — and the governed type says so."""
    for text_type in ("varchar", "text", "character varying", "date", "boolean"):
        assert _refusal(_sum(), expr=_governed(text_type)).code is (
            CompilationRefusalCode.PHYSICAL_TYPE_UNSUPPORTED)


def test_money_operand_dies():
    """``money`` is IN the catalog's numeric set and must still be refused: its scale is a locale
    setting rather than a declared column property, so the ``(p,s)`` it would convert to is not
    knowable from governed metadata."""
    assert _refusal(_sum(), expr=_governed("money")).code is (
        CompilationRefusalCode.PHYSICAL_TYPE_UNSUPPORTED)


def test_float_numerator_dies():
    """Invisible to Child-1's word — a RATIO's ``output_type`` is the CONSTANT ``"decimal"`` and
    describes no operand — which is the whole reason the evidence is per-expression."""
    assert _refusal(_ratio(), numerator=_governed("double precision")).code is (
        CompilationRefusalCode.PHYSICAL_TYPE_UNSUPPORTED)


def test_float_denominator_dies():
    assert _refusal(_ratio(), denominator=_governed("float")).code is (
        CompilationRefusalCode.PHYSICAL_TYPE_UNSUPPORTED)


def test_float_difference_subtrahend_dies():
    """Child-1 derives a DIFFERENCE's word from the MINUEND only (``_resolve_difference``), so the
    subtrahend is the position an implementation reading the word cannot see."""
    assert _refusal(_difference(), subtrahend=_governed("real")).code is (
        CompilationRefusalCode.PHYSICAL_TYPE_UNSUPPORTED)


def test_a_float_in_ANY_arithmetic_position_dies():
    """The complete matrix, so no position is checked only by accident."""
    assert _refusal(_sum(), expr=_governed("real"))
    assert _refusal(_ratio(), numerator=_governed("real"))
    assert _refusal(_ratio(), denominator=_governed("real"))
    assert _refusal(_difference(), minuend=_governed("real"))
    assert _refusal(_difference(), subtrahend=_governed("real"))


@pytest.mark.parametrize("inexact", ["float", "double", "double precision", "real", "money"])
def test_the_refused_set_is_exactly_the_types_the_catalog_calls_numeric(inexact):
    """§6's ``REFUSED`` set, one by one. Every member is in ``_NUMERIC_LOGICAL_TYPES``
    (``output_authority.py:59``), which is what makes "is it numeric?" the WRONG test: the obvious
    fix passes all five and publishes fixed-point anyway."""
    from featuregen.formula.output_authority import _NUMERIC_LOGICAL_TYPES

    assert inexact in _NUMERIC_LOGICAL_TYPES
    assert inexact not in physical_types._EXACT_NUMERIC_LOGICAL_TYPES
    assert _refusal(_sum(), expr=_governed(inexact)).code is (
        CompilationRefusalCode.PHYSICAL_TYPE_UNSUPPORTED)


def test_the_allowlist_covers_every_type_SPEC_6_names_and_nothing_inexact():
    """The allowlist pinned in both directions against §6's own two sets. ``int2`` is the single
    addition — PostgreSQL's alias for ``smallint``, as ``int4``/``int8`` (which §6 DOES list) are
    for ``integer``/``bigint`` — so refusing it would reject an exact column over its spelling."""
    spec_exact = {"numeric", "decimal", "integer", "int", "int4", "int8", "bigint", "smallint"}
    assert spec_exact <= physical_types._EXACT_NUMERIC_LOGICAL_TYPES
    assert physical_types._EXACT_NUMERIC_LOGICAL_TYPES - spec_exact == {"int2"}


def test_unknown_or_unreadable_type_dies_or_requires_authority():
    """BOTH die, and they die as SEPARATE branches carrying different explanations.

    WHICH code, and why. §14 now carries TWO typing verdicts (architect ruling 2026-07-28), because
    the code is what drives remediation routing:

    * ``OUTPUT_TYPE_NOT_GOVERNED`` — the compiler could not ESTABLISH a governed type. Both causes
      below take it, with the cause preserved in the detail.
    * ``PHYSICAL_TYPE_UNSUPPORTED`` — the type IS governed and known, and materialization cannot
      safely represent it (a float operand under an exact-decimal rule, an out-of-range precision).

    An earlier revision routed all three through one code; that would eventually send automation
    down the wrong remediation path. The DIAGNOSIS was already separate, and still is:

    * UNGOVERNED — nobody has attested this column's type. Remedy: attest it.
    * UNAVAILABLE — the type authority read failed closed (fork / hash-mismatch / unprojectable).
      Remedy: repair the decision log or the projection; attesting again would not help.

    Both states also carry NO type, so both are absent from the exact-numeric allowlist. A single
    check would therefore refuse them anyway — with the wrong explanation, blaming a type nobody
    could read. That is the collapse this test exists to prevent.
    """
    ungoverned = _refusal(_sum(), expr=_ungoverned())
    unavailable = _refusal(_sum(), expr=_unavailable())
    inexact = _refusal(_sum(), expr=_governed("real"))

    assert ungoverned.code is CompilationRefusalCode.OUTPUT_TYPE_NOT_GOVERNED
    assert unavailable.code is CompilationRefusalCode.OUTPUT_TYPE_NOT_GOVERNED
    # …and a GOVERNED-but-unusable type keeps the other code, so the two remedies stay routable.
    assert inexact.code is CompilationRefusalCode.PHYSICAL_TYPE_UNSUPPORTED

    assert "no GOVERNED logical type" in ungoverned.detail
    assert "failed closed" in unavailable.detail and "hash_mismatch" in unavailable.detail
    assert "not an exact numeric" in inexact.detail

    # No branch may be reachable through another's message.
    assert "not an exact numeric" not in ungoverned.detail
    assert "not an exact numeric" not in unavailable.detail
    assert "failed closed" not in ungoverned.detail
    assert len({ungoverned.detail, unavailable.detail, inexact.detail}) == 3


@pytest.mark.parametrize("read_status", ["fork", "hash_mismatch", "projection_unavailable"])
def test_every_C1_fail_closed_status_names_itself_in_the_refusal(read_status):
    """An operator told only "unavailable" cannot tell a forked decision log from a lagged
    projection, and the two are fixed by different people."""
    refusal = _refusal(_sum(), expr=_unavailable(read_status=read_status))
    assert read_status in refusal.detail


def test_an_UNGOVERNED_operand_is_refused_even_when_the_catalog_word_would_be_exact():
    """Closes DEFERRED-WORK A.9's third row. The evidence type cannot carry an unattested word at
    all, so there is no "ungoverned but numeric" path that publishes fixed-point: a DECIMAL(p,s)
    column is a governed claim about the operand, and an unattested `data_type` is not one."""
    assert _refusal(_sum(), expr=_ungoverned()).code is (
        CompilationRefusalCode.OUTPUT_TYPE_NOT_GOVERNED)


def test_evidence_claiming_GOVERNED_with_NO_type_is_REFUSED_not_a_crash():
    """A boundary belt, and it is load-bearing precisely because `resolve_physical_type` takes the
    mapping from a CALLER. `_operand_type_evidence` never builds this pair, but a hand-assembled or
    degenerate evidence object can — and without the guard the exactness test would call
    ``.lower()`` on ``None`` and raise, turning a governed refusal into a stack trace (§14: a
    governed failure never surfaces as a bare ``AttributeError``).
    """
    degenerate = OperandTypeEvidence(operand_ref=REF_AMT, status=OperandTypeStatus.GOVERNED,
                                     logical_type=None, read_status="resolved")
    result = resolve_physical_type(_sum(), operand_types={"body.expr": degenerate})
    assert isinstance(result, MaterializationRefused), result
    assert result.code is CompilationRefusalCode.OUTPUT_TYPE_NOT_GOVERNED


def test_a_parameterised_or_upper_case_governed_type_is_normalised_not_refused():
    """``logical_representation`` is carried VERBATIM, so the word may arrive parameterised
    (``numeric(18,2)``), upper-cased or padded — none of those is a different type."""
    assert _resolved(_sum(), expr=_governed("NUMERIC(18,2)")).sql_type == "DECIMAL(18,2)"
    assert _resolved(_sum(), expr=_governed(" decimal ")).sql_type == "DECIMAL(18,2)"
    assert _resolved(_sum(), expr=_governed("BIGINT")).sql_type == "DECIMAL(18,2)"


# ── the count exemption, and its limit ───────────────────────────────────────────────────────────

def test_a_counts_operand_type_is_carried_but_NOT_consulted():
    """A ``COUNT_DISTINCT`` over a text column is a legal, correct feature: the count is integral
    whatever the column holds, so its operand is not an arithmetic operand. Refusing it would
    refuse a correct feature; the evidence is still carried, just not consulted."""
    assert _resolved(_count(), expr=_governed("varchar")).sql_type == "BIGINT"
    assert _resolved(_count(), expr=_unavailable()).sql_type == "BIGINT"
    assert _resolved(_count(AggregateFunction.COUNT_NON_NULL),
                     expr=_governed("real")).sql_type == "BIGINT"


def test_a_difference_whose_minuend_is_a_count_checks_only_the_SUM_half():
    """The count half is exempt; the arithmetic half is not, and it is checked at its own path."""
    assert _resolved(
        _difference(minuend=AggregateFunction.COUNT_NON_NULL, subtrahend=AggregateFunction.SUM),
        minuend=_governed("varchar")).sql_type == "DECIMAL(18,2)"
    assert _refusal(
        _difference(minuend=AggregateFunction.COUNT_NON_NULL, subtrahend=AggregateFunction.SUM),
        minuend=_governed("varchar"), subtrahend=_governed("double")).code is (
        CompilationRefusalCode.PHYSICAL_TYPE_UNSUPPORTED)


def test_a_difference_whose_minuend_is_a_sum_over_an_unreadable_operand_is_refused():
    assert _refusal(
        _difference(minuend=AggregateFunction.SUM, subtrahend=AggregateFunction.COUNT_ROWS),
        minuend=_unavailable()).code is CompilationRefusalCode.OUTPUT_TYPE_NOT_GOVERNED


# ── the formula's logical WORD is no longer operand evidence ─────────────────────────────────────

def test_the_formulas_logical_word_can_no_longer_ACCEPT_an_inexact_operand():
    """The regression direction that matters. Under Task 8, a word of ``"numeric"`` was the only
    statement about the operand — so a float operand behind a numeric word published fixed-point."""
    assert _refusal(_sum(output_type="numeric"), expr=_governed("double precision")).code is (
        CompilationRefusalCode.PHYSICAL_TYPE_UNSUPPORTED)


def test_the_formulas_logical_word_can_no_longer_REFUSE_a_governed_exact_operand():
    """The mirror: Child-1's ``"unknown"`` is not evidence about an operand the catalog governs."""
    assert _resolved(_sum(output_type="unknown"), expr=_governed("numeric")).sql_type == (
        "DECIMAL(18,2)")


def test_the_module_never_reads_the_formulas_output_policy_for_a_type():
    """A source assertion: two answers to one question is the defect, and the weaker answer
    (a word describing at most one operand per formula) must not be consultable at all."""
    source = Path(physical_types.__file__).read_text(encoding="utf-8")
    executable = "\n".join(
        line for line in source.splitlines()
        if not line.lstrip().startswith(("#", "*", '"""', "'''")))
    assert "output.output_type" not in executable
    assert "formula.output" not in executable


# ── the evidence must line up with the body: a mismatched CALL is a ValueError ───────────────────

def test_evidence_missing_for_a_body_path_is_a_ValueError_not_a_refusal():
    """The fail-open this argument exists to close: an omitted half of a ratio would exempt that
    operand from §6 entirely. A call assembled wrongly is not a governed verdict (§14 has no member
    for it), so it raises rather than borrowing ``PHYSICAL_TYPE_UNSUPPORTED``."""
    formula = _ratio()
    evidence = _evidence(formula)
    del evidence["body.denominator"]
    with pytest.raises(ValueError, match="body.denominator"):
        resolve_physical_type(formula, operand_types=evidence)


def test_evidence_for_a_body_path_the_formula_does_not_have_is_a_ValueError():
    formula = _sum()
    evidence = _evidence(formula) | {"body.denominator": _governed("numeric")}
    with pytest.raises(ValueError, match="body.denominator"):
        resolve_physical_type(formula, operand_types=evidence)


def test_evidence_naming_a_DIFFERENT_operand_than_the_expression_is_a_ValueError():
    """Evidence gathered for another formula types a column this one does not read — and it would
    do so silently, since the paths still line up."""
    formula = _sum()
    evidence = {"body.expr": _governed("numeric", ref=f"{TABLE_REF}.some_other_column")}
    with pytest.raises(ValueError, match="different operand"):
        resolve_physical_type(formula, operand_types=evidence)


def test_the_pairing_is_checked_for_a_COUNT_too():
    """A count returns BIGINT without consulting its operand's type, so validating the pairing only
    on the decimal path would let mismatched evidence through for every count."""
    formula = _count()
    evidence = {"body.expr": _governed("numeric", ref=f"{TABLE_REF}.some_other_column")}
    with pytest.raises(ValueError, match="different operand"):
        resolve_physical_type(formula, operand_types=evidence)


def test_operand_types_has_no_default():
    """A default of ``{}`` would let a caller skip §6's gate by omission."""
    import inspect

    parameter = inspect.signature(resolve_physical_type).parameters["operand_types"]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


# ── nullability is part of the type decision ─────────────────────────────────────────────────────

def test_a_zero_empty_window_yields_a_non_null_column():
    assert _resolved(_sum(empty_window=EmptyWindowResult.ZERO)).nullable is False


def test_a_null_empty_window_yields_a_nullable_column():
    assert _resolved(_sum(empty_window=EmptyWindowResult.NULL)).nullable is True


def test_a_null_empty_window_on_EITHER_half_makes_the_column_nullable():
    """Each ``AggregateExpression`` owns its own window (interfaces §6), so a ratio has two
    empty-window policies and either one can put a NULL in the published column."""
    assert _resolved(_ratio(denominator_empty=EmptyWindowResult.NULL)).nullable is True
    assert _resolved(_ratio(numerator_empty=EmptyWindowResult.NULL)).nullable is True


def test_zero_denominator_NULL_yields_a_nullable_column():
    assert _resolved(_ratio(zero_denominator=ZeroDenominator.NULL)).nullable is True


def test_zero_denominator_ZERO_leaves_the_column_non_null():
    assert _resolved(_ratio(zero_denominator=ZeroDenominator.ZERO)).nullable is False


def test_a_count_is_nullable_when_its_empty_window_says_NULL():
    """Nullability is decided by the policies, not by the SQL type: an empty window is empty
    whether the aggregate is a SUM or a COUNT."""
    assert _resolved(_count(empty_window=EmptyWindowResult.NULL)).nullable is True
    assert _resolved(_count(empty_window=EmptyWindowResult.ZERO)).nullable is False


def test_a_propagating_null_input_makes_the_column_nullable():
    """Beyond §6's two listed sources: ``NullInput.PROPAGATE`` says a null operand VALUE makes the
    aggregate null, which is a null in the published column on a NON-empty window."""
    assert _resolved(_sum(null_input=NullInput.PROPAGATE)).nullable is True
    assert _resolved(_sum(null_input=NullInput.IGNORE)).nullable is False
    assert _resolved(_sum(null_input=NullInput.ZERO)).nullable is False


# ── the decimal policy is validated exactly where it governs ────────────────────────────────────

def test_a_precision_above_38_is_refused():
    """Hive/Spark DECIMAL maxes at precision 38 — one above it is not representable."""
    policy = DecimalPolicy(precision=39, scale=2, rounding=RoundingMode.HALF_UP,
                           overflow=OverflowBehavior.ERROR)
    assert _refusal(_sum(decimal=policy)).code is (
        CompilationRefusalCode.PHYSICAL_TYPE_UNSUPPORTED)


def test_precision_38_is_accepted():
    """The boundary is inclusive; refusing it would reject the widest legal money column."""
    policy = DecimalPolicy(precision=38, scale=0, rounding=RoundingMode.HALF_UP,
                           overflow=OverflowBehavior.ERROR)
    assert _resolved(_sum(decimal=policy)).sql_type == "DECIMAL(38,0)"


def test_a_precision_below_1_is_refused():
    """``schema._check_decimal`` permits ``precision=0`` (it only checks ``precision >= scale``),
    so a formula that reaches here with a zero-width decimal is a real input, not a hypothetical."""
    policy = DecimalPolicy(precision=0, scale=0, rounding=RoundingMode.HALF_UP,
                           overflow=OverflowBehavior.ERROR)
    assert _refusal(_sum(decimal=policy)).code is (
        CompilationRefusalCode.PHYSICAL_TYPE_UNSUPPORTED)


def test_a_scale_outside_the_precision_is_refused():
    policy = DecimalPolicy(precision=4, scale=6, rounding=RoundingMode.HALF_UP,
                           overflow=OverflowBehavior.ERROR)
    assert _refusal(_sum(decimal=policy)).code is (
        CompilationRefusalCode.PHYSICAL_TYPE_UNSUPPORTED)
    negative = DecimalPolicy(precision=4, scale=-1, rounding=RoundingMode.HALF_UP,
                             overflow=OverflowBehavior.ERROR)
    assert _refusal(_sum(decimal=negative)).code is (
        CompilationRefusalCode.PHYSICAL_TYPE_UNSUPPORTED)


def test_SATURATE_is_refused():
    """A deferred NFR (spec "Deferred NFRs"): nothing in this slice clamps, so accepting the
    request would silently substitute a different overflow semantics."""
    policy = DecimalPolicy(precision=18, scale=2, rounding=RoundingMode.HALF_UP,
                           overflow=OverflowBehavior.SATURATE)
    assert _refusal(_sum(decimal=policy)).code is (
        CompilationRefusalCode.PHYSICAL_TYPE_UNSUPPORTED)
    assert _refusal(_ratio(decimal=policy)).code is (
        CompilationRefusalCode.PHYSICAL_TYPE_UNSUPPORTED)


def test_half_even_on_a_ratio_is_refused_not_silently_half_up():
    """Spark's decimal ``Divide`` applies ``CheckOverflow`` with hard-coded HALF_UP at the division
    result scale (clamped to MINIMUM_ADJUSTED_SCALE=6) BEFORE any explicit rounding call in the
    generated code runs — at published scale 6 the emitted ``bround`` is a no-op (verified
    empirically: 1/2000000 → 0.000001, not the HALF_EVEN 0.000000). A governed declaration the
    engine silently ignores must refuse instead of publishing a column whose recorded mode is
    false."""
    policy = DecimalPolicy(precision=18, scale=2, rounding=RoundingMode.HALF_EVEN,
                           overflow=OverflowBehavior.ERROR)
    refusal = _refusal(_ratio(decimal=policy))
    assert refusal.code is CompilationRefusalCode.PHYSICAL_TYPE_UNSUPPORTED
    assert "half_up" in refusal.detail


def test_half_even_on_a_plain_aggregate_is_still_allowed():
    """Post-aggregate rounding of a narrower-scale value has no engine pre-rounding to fight: a SUM
    (and a DIFFERENCE — the refusal is about DIVISION, not about the two-expression shape) keeps
    both modes."""
    policy = DecimalPolicy(precision=18, scale=2, rounding=RoundingMode.HALF_EVEN,
                           overflow=OverflowBehavior.ERROR)
    assert _resolved(_sum(decimal=policy)).rounding is RoundingMode.HALF_EVEN
    assert _resolved(_difference(decimal=policy)).rounding is RoundingMode.HALF_EVEN


def test_a_counts_decimal_policy_governs_nothing_and_is_therefore_not_validated():
    """PINS A DECISION §6 does not make. A count publishes BIGINT, so its ``DecimalPolicy``
    reaches no rendered expression: neither an unrepresentable precision nor an unimplemented
    ``SATURATE`` can change a single value. Validating it would refuse a correct feature for an
    inert field; carrying it would put a rounding mode on an integral column. Both are therefore
    skipped — and the renderer must take its obligations from :class:`PhysicalType`, never from
    ``formula.decimal``, or this choice becomes a fail-open.
    """
    policy = DecimalPolicy(precision=40, scale=39, rounding=RoundingMode.HALF_UP,
                           overflow=OverflowBehavior.SATURATE)
    resolved = _resolved(_count(decimal=policy))
    assert resolved.sql_type == "BIGINT"
    assert resolved.rounding is None and resolved.overflow is None


# ── overflow and rounding are RESOLVED into the decision, not dropped ────────────────────────────

def test_overflow_ERROR_is_carried_into_the_type_decision():
    """§6: Spark's default on decimal overflow is a NULL, so ERROR is real work in the generated
    code. It cannot be honoured by a renderer that never receives it."""
    assert _resolved(_sum()).overflow is OverflowBehavior.ERROR


def test_the_rounding_mode_is_carried_into_the_type_decision():
    """§6: rounding is implemented explicitly, never left to an engine default."""
    policy = DecimalPolicy(precision=18, scale=2, rounding=RoundingMode.FLOOR,
                           overflow=OverflowBehavior.ERROR)
    assert _resolved(_sum(decimal=policy)).rounding is RoundingMode.FLOOR


# ── never DOUBLE ─────────────────────────────────────────────────────────────────────────────────

def _executable_source(module) -> str:
    """The module's executable text, lower-cased, with comments and docstrings stripped.

    Borrowed from ``test_inputs.py`` / ``test_expression_ir.py``. Necessary since Task 8.1: §6's
    REFUSED set is now documented by name in this module's prose, so a whole-file match would fail
    for an EXPLANATION of what is forbidden — and would be satisfied by deleting that explanation,
    which is the wrong incentive in both directions.
    """
    import inspect
    import io
    import tokenize

    kept: list[str] = []
    tokens = tokenize.generate_tokens(io.StringIO(inspect.getsource(module)).readline)
    previous = tokenize.INDENT
    for token in tokens:
        if token.type == tokenize.COMMENT:
            continue
        if token.type == tokenize.STRING and previous in (
                tokenize.INDENT, tokenize.DEDENT, tokenize.NEWLINE, tokenize.NL):
            continue
        if token.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT):
            kept.append(token.string)
        previous = token.type
    return " ".join(kept).lower()


def test_the_module_never_names_a_binary_float_type_IN_EXECUTABLE_CODE():
    """§6: "Never silently map ambiguous numerics to ``DOUBLE``." Ambiguity refuses.

    Asserted against the source because a mapping can only be proven absent by its absence — a
    table with no DOUBLE row today can grow one tomorrow. If this fails, remove the mapping, not
    the test: money in a binary float is a defect that costs money. The allowlist is an executable
    literal, so a float sneaking INTO it is exactly what this catches.
    """
    source = _executable_source(physical_types)
    for banned in ("double", "float", "real", "money"):
        assert banned not in source, f"{banned!r} appears in executable code"


def test_the_prose_still_NAMES_the_refused_types():
    """The complement, so the test above cannot be satisfied by deleting the explanation: §6's
    REFUSED set is the reason this module exists, and a reader must be able to find it."""
    source = Path(physical_types.__file__).read_text(encoding="utf-8").lower()
    for refused in ("double precision", "real", "money", "float"):
        assert refused in source


def test_the_only_sql_types_this_module_can_publish_are_bigint_and_decimal():
    """The complementary half: every reachable success is one of the two §6 types."""
    formulas = [
        _count(AggregateFunction.COUNT_ROWS), _count(AggregateFunction.COUNT_NON_NULL),
        _count(AggregateFunction.COUNT_DISTINCT), _sum(), _ratio(), _difference(),
    ]
    published = {_resolved(f).sql_type for f in formulas}
    assert published == {"BIGINT", "DECIMAL(18,2)"}


# ── refusal discipline ───────────────────────────────────────────────────────────────────────────

def test_every_refusal_is_a_typed_task1_code_and_names_no_data_value():
    refusal = _refusal(_sum(), expr=_governed("real"))
    assert isinstance(refusal, MaterializationRefused)
    assert refusal.code is CompilationRefusalCode.PHYSICAL_TYPE_UNSUPPORTED
    assert refusal.detail


def test_every_operand_refusal_names_a_LOCATION_and_never_the_column():
    """§14's detail discipline: counts, types, hashes and LOCATIONS — never data. A body path is a
    location; the operand's logical ref is a catalog address, and naming it in every refusal would
    put the read set into an operator-facing message this module does not need to."""
    for evidence in (_governed("real"), _ungoverned(), _unavailable()):
        detail = _refusal(_ratio(), denominator=evidence).detail
        assert "body.denominator" in detail
        assert REF_AMT not in detail


def test_a_refusal_is_RETURNED_not_raised():
    """One refused feature is one governed verdict among many a compilation collects — the same
    shape ``compile_ir`` uses."""
    formula = _sum()
    assert isinstance(
        resolve_physical_type(formula,
                              operand_types=_evidence(formula, expr=_governed("real"))),
        MaterializationRefused)


def test_a_body_outside_child1s_closed_union_is_a_schema_error_not_a_refusal():
    """§14 has no member for a forged object, and ``ir.compile_ir`` already draws this line."""

    class _NotABody:
        pass

    formula = _formula(UnaryBody(expr=_expr(AggregateFunction.SUM)), output_type="numeric")
    forged = TypedFormulaV1(
        formula_schema_version=formula.formula_schema_version,
        operation_grammar_version=formula.operation_grammar_version,
        output_policy_version=formula.output_policy_version,
        canonicalization_version=formula.canonicalization_version,
        grain=formula.grain, body=_NotABody(),  # type: ignore[arg-type]
        parameters=(), decimal=formula.decimal, output=formula.output)
    with pytest.raises(SchemaError):
        resolve_physical_type(forged, operand_types={})


# ── identity ─────────────────────────────────────────────────────────────────────────────────────

def test_the_identity_payload_hashes_and_distinguishes_nullability():
    """The resolved type enters ``group_plan_hash`` (§6), so a nullability difference must be a
    hash difference — otherwise two plans that write different columns share one identity."""
    non_null = _resolved(_sum(empty_window=EmptyWindowResult.ZERO)).identity_payload()
    nullable = _resolved(_sum(empty_window=EmptyWindowResult.NULL)).identity_payload()
    assert materialize_hash(non_null) != materialize_hash(nullable)
    assert materialize_hash(non_null) == materialize_hash(
        _resolved(_sum(empty_window=EmptyWindowResult.ZERO)).identity_payload())


def test_the_identity_payload_distinguishes_the_sql_type_alone():
    """Two columns differing ONLY in width — same nullability, same rounding, same overflow — are
    different columns. Asserted separately because the ``BIGINT``/``DECIMAL`` pair also differs in
    rounding and overflow, so comparing those two cannot show that ``sql_type`` is in the payload.
    """
    wide = DecimalPolicy(precision=38, scale=2, rounding=RoundingMode.HALF_UP,
                         overflow=OverflowBehavior.ERROR)
    narrow, broad = _resolved(_sum()), _resolved(_sum(decimal=wide))
    assert (narrow.nullable, narrow.rounding, narrow.overflow) == (
        broad.nullable, broad.rounding, broad.overflow)
    assert materialize_hash(narrow.identity_payload()) != materialize_hash(
        broad.identity_payload())


def test_the_identity_payload_distinguishes_rounding_and_overflow():
    """Two columns typed ``DECIMAL(18,2)`` that round differently hold different numbers."""
    floor = DecimalPolicy(precision=18, scale=2, rounding=RoundingMode.FLOOR,
                          overflow=OverflowBehavior.ERROR)
    assert materialize_hash(_resolved(_sum()).identity_payload()) != materialize_hash(
        _resolved(_sum(decimal=floor)).identity_payload())


def test_the_physical_type_is_frozen():
    resolved = _resolved(_sum())
    with pytest.raises(Exception):
        resolved.sql_type = "BIGINT"  # type: ignore[misc]


# ── the three worked features ────────────────────────────────────────────────────────────────────

def test_the_worked_features_resolve_to_their_published_types():
    """The end-to-end check against REAL Child-1 formulas (``fixtures.authored_formula``), whose
    output policies are proven against the real resolver in ``test_fixtures.py``. All three declare
    ``empty_window=NULL``, so all three columns are nullable — including the count."""
    sum_type = _resolved(fixtures.authored_formula("total_debit_amount_30d"))
    assert (sum_type.sql_type, sum_type.nullable) == ("DECIMAL(38,6)", True)

    count_type = _resolved(fixtures.authored_formula("distinct_merchant_count_90d"))
    assert (count_type.sql_type, count_type.nullable) == ("BIGINT", True)

    ratio_type = _resolved(fixtures.authored_formula("cross_border_value_ratio_90d"))
    assert (ratio_type.sql_type, ratio_type.nullable) == ("DECIMAL(38,6)", True)
    assert ratio_type.overflow is OverflowBehavior.ERROR
    # HALF_UP, deliberately: the engine's own division pre-rounds HALF_UP, so a worked ratio
    # declaring half_even would refuse above — the fixture declares the mode Spark applies.
    assert ratio_type.rounding is RoundingMode.HALF_UP
