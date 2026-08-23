"""The published column type for a V3 formula — the driver `physical_types_v2` never had.

`physical_types_v2` ships the arithmetic (`sum_type_v2`, `multiply_type_v2`, the overflow refusals).
Nothing walked a formula and decided its column type, so `PlannedFeature.physical_type` — which
hard-requires a resolved type — could never be filled for a V2 feature.

What these tests hold:

1. **The DECLARED decimal policy governs**, including for a sum. `sum_type_v2`'s widening is not
   applied: it needs the operand's real precision, and the compiled IR establishes a governed
   *word* (`"numeric"`), never a width. A wider type derived from a precision nobody read is a
   worse answer than the one the author declared.
2. **Nullability comes from the POLICIES**, never from the SQL type. A BIGINT count can be nullable.
3. **Every arithmetic operand must be a GOVERNED EXACT NUMERIC**, and the three ways that fails are
   three refusals, because they route to different people.
4. **The evidence must line up with the body**, both directions — an omitted body path would
   silently exempt one half of a ratio from the rule above.
5. **Aggregates the renderer cannot emit refuse BY NAME**, rather than typing cleanly and failing
   later.
"""
from __future__ import annotations

import pytest
from tests.featuregen.materialize.test_admission_v2_s13 import REF_AMT, _expr, _raw, _window

from featuregen.formula.parse_v2 import parse_proposal_v2
from featuregen.materialize.codes import MaterializationRefused
from featuregen.materialize.expression_ir import OperandTypeEvidence, OperandTypeStatus
from featuregen.materialize.resolve_physical_type_v3 import resolve_physical_type_v3


def _evidence(operand_ref=REF_AMT, *, status=OperandTypeStatus.GOVERNED,
              logical_type="decimal", read_status="resolved"):
    """What the compiled IR actually holds for one body path — a governed WORD, never a width."""
    return OperandTypeEvidence(operand_ref=operand_ref, status=status,
                               logical_type=logical_type, read_status=read_status)


#: Keyed by BODY PATH, exactly as `{e.expr_path: e.operand_type for e in ir.expressions}` is.
AMOUNT = {"body.expr": _evidence()}
NO_OPERAND = {"body.expr": _evidence(None, status=OperandTypeStatus.NO_OPERAND,
                                     logical_type=None, read_status=None)}


def _typed(raw=None, operands=None):
    return resolve_physical_type_v3(
        parse_proposal_v2(raw if raw is not None else _raw()),
        operand_types=AMOUNT if operands is None else operands)


# ══ THE DECLARED POLICY GOVERNS ════════════════════════════════════════════════════════════════
def test_A_SUM_PUBLISHES_THE_DECLARED_POLICY():
    """`sum_type_v2` widens DECIMAL(18,2) to (28,2) and is deliberately NOT applied here.

    Widening needs the operand's real precision and scale. What the compiled IR establishes is a
    governed WORD — `"decimal"`, `"numeric"` — not a width, so a widened type would be derived from
    a precision nobody read and published as though the author had asked for it. An earlier draft
    of this module took `DecimalTypeV2` values, which no caller in this codebase can produce: the
    signature was satisfiable only by a test that invented them, which is what this test used to do.
    """
    assert _typed().sql_type == "DECIMAL(38,6)"        # the fixture's own declared policy


def test_the_ROUNDING_AND_OVERFLOW_ARE_CARRIED_not_defaulted():
    """Generated code must round explicitly and must fail on overflow rather than take a NULL."""
    resolved = _typed()
    assert resolved.rounding is not None and resolved.overflow is not None


# ══ THE OPERAND MUST BE A GOVERNED EXACT NUMERIC ═══════════════════════════════════════════════
def test_an_UNREADABLE_operand_type_refuses_toward_THE_TYPE_AUTHORITY():
    """Unknown is not the same as known-and-unsupported, and the remedy differs: repair the read."""
    refusal = _typed(operands={"body.expr": _evidence(
        status=OperandTypeStatus.UNAVAILABLE, logical_type=None, read_status="fork")})
    assert isinstance(refusal, MaterializationRefused)
    assert "repair the type authority" in refusal.detail


def test_an_UNATTESTED_operand_type_refuses_toward_GOVERNANCE():
    refusal = _typed(operands={"body.expr": _evidence(
        status=OperandTypeStatus.UNGOVERNED, logical_type=None, read_status="file_declared")})
    assert isinstance(refusal, MaterializationRefused)
    assert "nobody attested" in refusal.detail


def test_a_GOVERNED_BUT_INEXACT_operand_refuses_toward_THE_FORMULA():
    """A float aggregate is order-dependent under parallel execution, so no fixed-point conversion
    of it is reproducible — the number would differ run to run and look stable."""
    refusal = _typed(operands={"body.expr": _evidence(logical_type="double")})
    assert isinstance(refusal, MaterializationRefused)
    assert "not an exact numeric" in refusal.detail


# ══ THE EVIDENCE MUST LINE UP WITH THE BODY ════════════════════════════════════════════════════
def test_OMITTED_EVIDENCE_IS_A_CALLER_ERROR_not_a_verdict():
    """An omitted body path would silently exempt that operand from the rule above — the fail-open
    the argument exists to close. A call assembled wrongly is not a governed verdict."""
    with pytest.raises(ValueError, match="exactly the formula's expressions"):
        _typed(operands={})


def test_EVIDENCE_FOR_ANOTHER_FORMULA_IS_REFUSED():
    """Same body path, different operand: the column actually summed would be typed by a statement
    about a column it does not read."""
    with pytest.raises(ValueError, match="different operand"):
        _typed(operands={"body.expr": _evidence("hdfc::public.transactions.other_col")})


# ══ COUNTS ══════════════════════════════════════════════════════════════════════════════════════
def test_a_count_is_BIGINT():
    counting = _raw(body={"final_operation": "identity", "expr": _expr("count_rows", None)})
    assert _typed(counting, operands=NO_OPERAND).sql_type == "BIGINT"


def test_A_COUNT_CAN_STILL_BE_NULLABLE():
    """BIGINT says nothing about nullability.

    A count over a window declared NULL-when-empty is a nullable column — a grain key with no rows
    in range produces NULL, and publishing it NOT NULL would reject rows the pipeline legitimately
    writes.
    """
    counting = _raw(body={"final_operation": "identity", "expr": _expr("count_rows", None)})
    resolved = _typed(counting, operands=NO_OPERAND)
    assert resolved.sql_type == "BIGINT"
    assert resolved.nullable is True


def test_nullability_comes_from_the_POLICY_not_the_type():
    """The same shape with a window that declares ZERO when empty is NOT nullable."""
    never_empty = _raw(body={"final_operation": "identity",
                             "expr": _expr("count_rows", None,
                                           window=_window(empty_window="zero"))})
    assert _typed(never_empty, operands=NO_OPERAND).nullable is False


# ══ ARITHMETIC USES THE DECLARED POLICY ═════════════════════════════════════════════════════════
def _ratio(rounding: str = "half_up") -> dict:
    return _raw(body={"final_operation": "ratio",
                      "numerator": _expr("sum", REF_AMT),
                      "denominator": _expr("count_rows", None),
                      "zero_denominator": "null"},
                decimal={"precision": 38, "scale": 6, "rounding": rounding,
                         "overflow": "error"})


RATIO_EVIDENCE = {
    "body.numerator": _evidence(),
    "body.denominator": _evidence(None, status=OperandTypeStatus.NO_OPERAND,
                                  logical_type=None, read_status=None),
}


def test_a_RATIO_publishes_the_formulas_own_decimal_policy():
    """How the answer is REPRESENTED is a governed decision.

    Deriving a wider type from the operands would quietly overrule the policy somebody declared —
    the formula says what precision this number is reported at, not the columns it was computed
    from.
    """
    resolved = _typed(_ratio(), operands=RATIO_EVIDENCE)
    assert resolved.sql_type == "DECIMAL(38,6)"        # the fixture's declared policy
    assert resolved.rounding is not None and resolved.overflow is not None


def test_HALF_EVEN_ON_A_RATIO_REFUSES_here_too():
    """V1's finding, and it is about the ENGINE rather than about a language, so V2 inherits it.

    Spark's decimal divide wraps its result in `CheckOverflow`, which rounds HALF_UP at the division
    result scale BEFORE any explicit rounding the generated code performs — the ties are gone before
    the emitted `bround` runs. A declaration the engine silently ignores is refused rather than
    recorded as applied.

    Found by running it: the V2 fixture declares `half_even`, so the first version of the test above
    was refused rather than typed, which is exactly the right answer to the wrong question.
    """
    refusal = _typed(_ratio("half_even"), operands=RATIO_EVIDENCE)
    assert isinstance(refusal, MaterializationRefused)
    assert "rounds HALF_UP at the result scale" in refusal.detail


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


# ══ STEP 11 — THE ORDINARY AGGREGATES ══════════════════════════════════════════════════════════
@pytest.mark.parametrize("aggregation", ["avg", "min", "max"])
def test_the_ORDINARY_AGGREGATES_publish_the_declared_policy(aggregation):
    """They need no new typing RULE: each publishes the formula's declared decimal policy exactly as
    a sum does. Adding them is a renderer question, and this is the answer to the typing half."""
    raw = _raw(body={"final_operation": "identity", "expr": _expr(aggregation, REF_AMT)})
    assert _typed(raw).sql_type == "DECIMAL(38,6)"


@pytest.mark.parametrize("aggregation", ["avg", "min", "max"])
def test_an_ORDINARY_AGGREGATE_over_an_INEXACT_operand_refuses(aggregation):
    """A `min` over a DATE is meaningful and is still refused, because the published type is a
    DECIMAL: typing it would describe a column the value does not fit."""
    raw = _raw(body={"final_operation": "identity", "expr": _expr(aggregation, REF_AMT)})
    refusal = _typed(raw, operands={"body.expr": _evidence(logical_type="double")})
    assert isinstance(refusal, MaterializationRefused)
    assert "not an exact numeric" in refusal.detail


# ══ THE FOURTH NULLABILITY SOURCE — MISSING UNTIL STEP 11 ══════════════════════════════════════
@pytest.mark.parametrize("aggregation", ["sum", "avg", "min", "max"])
def test_IGNORE_ON_A_NON_COUNT_IS_NULLABLE(aggregation):
    """A NON-EMPTY window in which every operand is NULL aggregates to NULL, and the renderer
    deliberately does not coalesce it.

    This source was absent from the V2 resolver until step 11 — it would have published NOT NULL for
    a column the pipeline legitimately writes NULLs into. It bites hardest on exactly the aggregates
    step 11 adds, since all three are non-counts.
    """
    raw = _raw(body={"final_operation": "identity", "expr": _expr(
        aggregation, REF_AMT, window=_window(empty_window="zero", null_input="ignore"))})
    assert _typed(raw).nullable is True


def test_A_COUNT_IS_EXEMPT_FROM_THAT_SOURCE_AND_NO_OTHER():
    """Every COUNT answers an all-null group with 0, so `ignore` does not make it nullable — while
    a window declared NULL-when-empty still does. The exemption is one source wide, not general."""
    counting = _raw(body={"final_operation": "identity", "expr": _expr(
        "count_rows", None, window=_window(empty_window="zero", null_input="ignore"))})
    assert _typed(counting, operands=NO_OPERAND).nullable is False

    empty_is_null = _raw(body={"final_operation": "identity", "expr": _expr(
        "count_rows", None, window=_window(empty_window="null", null_input="ignore"))})
    assert _typed(empty_is_null, operands=NO_OPERAND).nullable is True
