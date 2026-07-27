"""Task 8 — the versioned physical type adapter (spec §6).

The property under test throughout: **the OPERATION decides the physical type, not the logical
word.** ``FormulaOutputPolicyV1.output_type`` is a logical type (``numeric`` / ``integer`` /
``decimal``); mapping that word straight onto a Hive type is the defect this module exists to
prevent. A ``COUNT_DISTINCT`` is logically ``integer`` and physically ``BIGINT``; a ``SUM`` is
``DECIMAL(p,s)`` from the formula's OWN ``DecimalPolicy`` whatever word Child-1 resolved.

The formulas here are built by hand rather than taken from ``fixtures.py`` because the fixtures are
the three worked features — they cover one nullability combination between them, and this module's
whole surface is the combinations. The worked features are asserted too (as the end of the file),
so the synthetic builders cannot drift away from a real Child-1 formula.
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
)
from featuregen.materialize import physical_types
from featuregen.materialize.canonical import materialize_hash
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
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


def _resolved(formula: TypedFormulaV1) -> PhysicalType:
    """Resolve, asserting success — a refusal here is a test failure, not a value."""
    result = resolve_physical_type(formula)
    assert isinstance(result, PhysicalType), result
    return result


def _refusal(formula: TypedFormulaV1) -> MaterializationRefused:
    result = resolve_physical_type(formula)
    assert isinstance(result, MaterializationRefused), result
    return result


# ── the policy version ───────────────────────────────────────────────────────────────────────────

def test_the_policy_version_is_a_declared_constant():
    """§6: it enters ``FeatureGroupPlanV1``/``group_plan_hash`` and the contract hash (§5.5)."""
    assert PHYSICAL_TYPE_POLICY_VERSION == 1


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


def test_a_difference_of_two_counts_is_decimal_though_no_operand_type_is_readable():
    """§6 keys the table on the FINAL operation, and a count's operands are integral by
    construction — so the logical word (``unknown``, because a COUNT_ROWS has no operand for
    Child-1 to read a type from) is not evidence about anything here and cannot refuse it."""
    assert _resolved(
        _difference(minuend=AggregateFunction.COUNT_ROWS,
                    subtrahend=AggregateFunction.COUNT_ROWS,
                    output_type="unknown")).sql_type == "DECIMAL(18,2)"


# ── the logical word as OPERAND evidence (never as a physical type) ──────────────────────────────

def test_a_sum_over_an_unreadable_operand_type_is_refused():
    """``"unknown"`` is Child-1's ``_UNKNOWN_TYPE``: the operand's type was absent or NOT numeric.
    Publishing DECIMAL over it would be a numeric claim nobody governed."""
    assert _refusal(_sum(output_type="unknown")).code is (
        CompilationRefusalCode.PHYSICAL_TYPE_UNSUPPORTED)


def test_a_sum_over_an_inexact_operand_type_is_refused():
    """A binary floating-point operand is not an exact numeric: its sum is order-dependent under
    parallel execution, so the DECIMAL published from it is not reproducible."""
    for inexact in ("double precision", "real", "float"):
        assert _refusal(_sum(output_type=inexact)).code is (
            CompilationRefusalCode.PHYSICAL_TYPE_UNSUPPORTED)


def test_the_two_operand_refusals_are_distinguishable_by_an_operator():
    """"Child-1 could read no type at all" and "the type is readable but inexact" are different
    problems — one is a governance gap in the catalog, the other a data-model choice — and only the
    detail carries that. Without this the ``unknown`` branch is unobservable: an unreadable type is
    also absent from the exact-numeric allowlist, so the second check would refuse it anyway with
    the wrong explanation.
    """
    unreadable = _refusal(_sum(output_type="unknown")).detail
    inexact = _refusal(_sum(output_type="real")).detail
    assert "no readable governed type" in unreadable
    assert "not an exact numeric" in inexact
    assert "no readable governed type" not in inexact


def test_a_parameterised_or_upper_case_operand_type_is_normalised_not_refused():
    """Child-1 carries ``logical_representation`` VERBATIM, so the word may arrive parameterised
    (``numeric(18,2)``) or upper-cased — neither is an unknown type."""
    assert _resolved(_sum(output_type="NUMERIC(18,2)")).sql_type == "DECIMAL(18,2)"
    assert _resolved(_sum(output_type=" decimal ")).sql_type == "DECIMAL(18,2)"


def test_a_difference_whose_minuend_is_a_sum_over_an_unreadable_operand_is_refused():
    assert _refusal(
        _difference(minuend=AggregateFunction.SUM, subtrahend=AggregateFunction.COUNT_ROWS,
                    output_type="unknown")).code is (
        CompilationRefusalCode.PHYSICAL_TYPE_UNSUPPORTED)


def test_a_difference_whose_minuend_is_a_count_is_not_refused_by_the_word():
    """Child-1 derives a DIFFERENCE's word from the MINUEND only (``_resolve_difference``), so an
    ``unknown`` word says nothing when the minuend is a count."""
    assert _resolved(
        _difference(minuend=AggregateFunction.COUNT_NON_NULL,
                    subtrahend=AggregateFunction.SUM,
                    output_type="unknown")).sql_type == "DECIMAL(18,2)"


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

def test_the_module_source_never_names_a_binary_float_type():
    """§6: "Never silently map ambiguous numerics to ``DOUBLE``." Ambiguity refuses.

    Asserted against the SOURCE because a mapping can only be proven absent by its absence — a
    table with no DOUBLE row today can grow one tomorrow. If this fails, remove the mapping, not
    the test: money in a binary float is a defect that costs money.
    """
    source = Path(physical_types.__file__).read_text(encoding="utf-8")
    assert "DOUBLE" not in source
    assert "double" not in source.lower()


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
    refusal = _refusal(_sum(output_type="unknown"))
    assert isinstance(refusal, MaterializationRefused)
    assert refusal.code is CompilationRefusalCode.PHYSICAL_TYPE_UNSUPPORTED
    assert refusal.detail


def test_a_refusal_is_RETURNED_not_raised():
    """One refused feature is one governed verdict among many a compilation collects — the same
    shape ``compile_ir`` uses."""
    assert isinstance(resolve_physical_type(_sum(output_type="unknown")), MaterializationRefused)


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
        resolve_physical_type(forged)


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
    assert ratio_type.rounding is RoundingMode.HALF_EVEN
