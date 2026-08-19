"""The published column type for a V3 formula — the driver `physical_types_v2` never had.

`physical_types_v2` ships the arithmetic (`sum_type_v2`, `multiply_type_v2`, the overflow refusals).
Nothing walked a formula and decided its column type, so `PlannedFeature.physical_type` — which
hard-requires a resolved type — could never be filled for a V2 feature.

What these tests hold:

1. **A sum WIDENS.** Publishing at the operand's own precision overflows on exactly the data the
   feature exists to measure.
2. **Nullability comes from the POLICIES**, never from the SQL type. A BIGINT count can be nullable.
3. **An operand with no resolved type is REFUSED**, not guessed. A column published on a guess is
   worse than one refused.
4. **Aggregates the renderer cannot emit refuse BY NAME**, rather than typing cleanly and failing
   later.
"""
from __future__ import annotations

from tests.featuregen.materialize.test_admission_v2_s13 import REF_AMT, _expr, _raw, _window

from featuregen.formula.parse_v2 import parse_proposal_v2
from featuregen.materialize.codes import MaterializationRefused
from featuregen.materialize.physical_types_v2 import DecimalTypeV2
from featuregen.materialize.resolve_physical_type_v3 import resolve_physical_type_v3

AMOUNT = {REF_AMT: DecimalTypeV2(precision=18, scale=2)}


def _typed(raw=None, operands=None):
    return resolve_physical_type_v3(
        parse_proposal_v2(raw if raw is not None else _raw()),
        operand_types=AMOUNT if operands is None else operands)


# ══ A SUM WIDENS ════════════════════════════════════════════════════════════════════════════════
def test_A_SUM_IS_WIDER_THAN_ITS_OPERAND():
    """Summing N rows of DECIMAL(18,2) needs headroom.

    Publishing at the operand's own precision would overflow on exactly the data the feature exists
    to measure — a bank with more transactions is a bank whose feature breaks first.
    """
    resolved = _typed()
    assert resolved.sql_type == "DECIMAL(28,2)"
    assert resolved.sql_type != "DECIMAL(18,2)", "the sum was published at the operand's precision"


def test_the_scale_is_PRESERVED_while_the_precision_grows():
    """Widening is about magnitude, not about inventing decimal places the money never had."""
    assert _typed().sql_type.endswith(",2)")


def test_an_operand_with_NO_RESOLVED_TYPE_is_refused(catalog=None):
    """A column published on a guess is worse than one refused: nobody downstream can tell it was
    guessed, and the guess is in the schema for good."""
    refusal = _typed(operands={})
    assert isinstance(refusal, MaterializationRefused)
    assert "cannot be typed by guessing" in refusal.detail


# ══ COUNTS ══════════════════════════════════════════════════════════════════════════════════════
def test_a_count_is_BIGINT():
    counting = _raw(body={"final_operation": "identity", "expr": _expr("count_rows", None)})
    assert _typed(counting, operands={}).sql_type == "BIGINT"


def test_A_COUNT_CAN_STILL_BE_NULLABLE():
    """BIGINT says nothing about nullability.

    A count over a window declared NULL-when-empty is a nullable column — a grain key with no rows
    in range produces NULL, and publishing it NOT NULL would reject rows the pipeline legitimately
    writes.
    """
    counting = _raw(body={"final_operation": "identity", "expr": _expr("count_rows", None)})
    resolved = _typed(counting, operands={})
    assert resolved.sql_type == "BIGINT"
    assert resolved.nullable is True


def test_nullability_comes_from_the_POLICY_not_the_type():
    """The same shape with a window that declares ZERO when empty is NOT nullable."""
    never_empty = _raw(body={"final_operation": "identity",
                             "expr": _expr("count_rows", None,
                                           window=_window(empty_window="zero"))})
    assert _typed(never_empty, operands={}).nullable is False


# ══ ARITHMETIC USES THE DECLARED POLICY ═════════════════════════════════════════════════════════
def test_a_RATIO_publishes_the_formulas_own_decimal_policy():
    """How the answer is REPRESENTED is a governed decision.

    Deriving a wider type from the operands would quietly overrule the policy somebody declared —
    the formula says what precision this number is reported at, not the columns it was computed
    from.
    """
    ratio = _raw(body={"final_operation": "ratio",
                       "numerator": _expr("sum", REF_AMT),
                       "denominator": _expr("count_rows", None),
                       "zero_denominator": "null"})
    resolved = _typed(ratio)
    assert resolved.sql_type == "DECIMAL(38,6)"        # the fixture's declared policy
    assert resolved.rounding is not None and resolved.overflow is not None


# ══ WHAT THIS BUILD CANNOT TYPE, IT REFUSES BY NAME ════════════════════════════════════════════
def test_AN_AGGREGATE_THE_RENDERER_CANNOT_EMIT_REFUSES():
    """Typing an operation nothing can render produces a column definition for code that will never
    exist — and wastes the reader's time twice, once here and once at render."""
    median = _raw(body={"final_operation": "identity", "expr": _expr("median", REF_AMT)})
    refusal = _typed(median)
    assert isinstance(refusal, MaterializationRefused)
    assert "median" in refusal.detail
    assert "same piece of work as rendering them" in refusal.detail


def test_the_refusal_is_RETURNED_not_raised():
    """One refused feature is one governed verdict among the many a compilation collects; raising
    would let the first bad feature hide every other verdict in the group."""
    median = _raw(body={"final_operation": "identity", "expr": _expr("median", REF_AMT)})
    assert isinstance(_typed(median), MaterializationRefused)     # returned, no pytest.raises
