"""C-C6 — ``formula-v2/physical-types@1``, with every row of the truth table asserted.

The gate is *"every row of the truth table asserted; V1 and V2 agree where they overlap"*, so the
arithmetic is parametrised rather than sampled, and the overlap with V1 is checked against V1's own
constants rather than against numbers copied into this file.
"""
from __future__ import annotations

import pytest

from featuregen.formula.schema_leaves import DecimalPolicy, OverflowBehavior, RoundingMode
from featuregen.materialize.physical_types import PHYSICAL_TYPE_POLICY_VERSION
from featuregen.materialize.physical_types_v2 import (
    FLOAT_OPERAND_REFUSED,
    MAX_DECIMAL_PRECISION,
    PHYSICAL_TYPE_POLICY_V2,
    PRECISION_EXCEEDED,
    SATURATE_UNSUPPORTED,
    SUM_GROWTH_DIGITS,
    DecimalTypeV2,
    PhysicalTypeRefusalV2,
    multiply_type_v2,
    product_sum_type_v2,
    refuse_inexact_operand_v2,
    sum_type_v2,
)

AMOUNT = DecimalTypeV2(precision=18, scale=2)
RATE = DecimalTypeV2(precision=9, scale=6)
DECLARED = DecimalPolicy(precision=38, scale=2, rounding=RoundingMode.HALF_EVEN,
                         overflow=OverflowBehavior.ERROR)


def _ok(value):
    assert not isinstance(value, PhysicalTypeRefusalV2), value
    return value


# ══ the multiplication rule ══════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("left,right,expected", [
    ((18, 2), (9, 6), (28, 8)),      # 18+9+1, 2+6 — the pilot's amount × booking_rate
    ((1, 0), (1, 0), (3, 0)),
    ((10, 4), (10, 4), (21, 8)),
    ((18, 18), (9, 9), (28, 27)),
])
def test_the_product_follows_SPARKS_rule(left, right, expected):
    """`DECIMAL(p1,s1) * DECIMAL(p2,s2)` → `DECIMAL(p1+p2+1, s1+s2)`, because Spark is the engine
    the artifact runs on and a policy that disagreed with it would describe a different number."""
    product = _ok(multiply_type_v2(DecimalTypeV2(*left), DecimalTypeV2(*right)))
    assert (product.precision, product.scale) == expected


def test_a_product_beyond_38_is_REFUSED_not_capped():
    """Spark's default would cap at 38 and REDUCE THE SCALE, changing every value in its last
    places without saying so. A refusal is a conversation; a silent scale reduction is a balance
    that does not reconcile and nobody knows why."""
    refusal = multiply_type_v2(DecimalTypeV2(30, 4), DecimalTypeV2(20, 4))
    assert isinstance(refusal, PhysicalTypeRefusalV2)
    assert refusal.code == PRECISION_EXCEEDED
    assert "REDUCE THE SCALE" in refusal.detail


# ══ SUM growth ═══════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("operand", [(18, 2), (1, 0), (28, 8), (28, 27)])
def test_the_sum_grows_by_exactly_ten_digits(operand):
    total = _ok(sum_type_v2(DecimalTypeV2(*operand)))
    assert total.precision == operand[0] + SUM_GROWTH_DIGITS
    assert total.scale == operand[1], "SUM never changes the scale"


def test_a_sum_that_would_exceed_38_is_refused():
    refusal = sum_type_v2(DecimalTypeV2(MAX_DECIMAL_PRECISION - 1, 2))
    assert isinstance(refusal, PhysicalTypeRefusalV2)
    assert refusal.code == PRECISION_EXCEEDED


# ══ the pilot, end to end, at BOTH rounding sites ════════════════════════════════════════════════
def test_rounding_AFTER_the_sum_keeps_the_full_intermediate():
    """amount(18,2) × rate(9,6) = (28,8); SUM grows it to (38,8) — exactly at the ceiling."""
    total = _ok(product_sum_type_v2(AMOUNT, RATE, DECLARED, round_per_row=False))
    assert (total.precision, total.scale) == (38, 8)


def test_rounding_PER_ROW_sums_the_DECLARED_type_instead():
    """Rounding first discards the intermediate width, so the sum grows from the declared decimal.
    A narrower declared policy is what makes this fit where the other site would not."""
    declared = DecimalPolicy(precision=20, scale=2, rounding=RoundingMode.HALF_EVEN,
                             overflow=OverflowBehavior.ERROR)
    total = _ok(product_sum_type_v2(AMOUNT, RATE, declared, round_per_row=True))
    assert (total.precision, total.scale) == (30, 2)


def test_THE_TWO_ROUNDING_SITES_GIVE_DIFFERENT_TYPES():
    """The reason the site is governed and identity-bearing rather than incidental: it is a
    different computation, and the difference grows with row count."""
    declared = DecimalPolicy(precision=20, scale=2, rounding=RoundingMode.HALF_EVEN,
                             overflow=OverflowBehavior.ERROR)
    per_row = _ok(product_sum_type_v2(AMOUNT, RATE, declared, round_per_row=True))
    at_end = _ok(product_sum_type_v2(AMOUNT, RATE, declared, round_per_row=False))
    assert (per_row.precision, per_row.scale) != (at_end.precision, at_end.scale)


def test_rounding_per_row_can_FIT_where_rounding_at_the_end_refuses():
    """The practical consequence, stated as a test: the choice is sometimes the difference between
    a feature that types and one that refuses — which is a decision, not a default."""
    wide_amount, wide_rate = DecimalTypeV2(24, 4), DecimalTypeV2(12, 6)
    declared = DecimalPolicy(precision=20, scale=4, rounding=RoundingMode.HALF_EVEN,
                             overflow=OverflowBehavior.ERROR)
    assert isinstance(
        product_sum_type_v2(wide_amount, wide_rate, declared, round_per_row=False),
        PhysicalTypeRefusalV2)
    _ok(product_sum_type_v2(wide_amount, wide_rate, declared, round_per_row=True))


# ══ float refusal ════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("word", ["float", "double", "real", "DOUBLE", "Double Precision"])
def test_binary_floating_point_operands_are_refused(word):
    """A sum over binary floating point depends on the order rows arrive, so two runs over the same
    window can disagree — and a monetary total that is not reproducible is not a total."""
    refusal = refuse_inexact_operand_v2(word, "bank::public.txns.txn_amt")
    assert isinstance(refusal, PhysicalTypeRefusalV2)
    assert refusal.code == FLOAT_OPERAND_REFUSED
    assert "not reproducible" in refusal.detail


@pytest.mark.parametrize("word", ["decimal", "DECIMAL(18,2)", "bigint", "int", "numeric"])
def test_exact_numerics_are_admitted(word):
    assert refuse_inexact_operand_v2(word, "bank::public.txns.txn_amt") is None


@pytest.mark.parametrize("word", ["string", "timestamp", "", "money", "varchar(10)"])
def test_an_UNCLASSIFIABLE_type_is_refused_not_assumed(word):
    """Defaulting to "probably fine" is how a DOUBLE reaches a sum."""
    assert isinstance(refuse_inexact_operand_v2(word, "r"), PhysicalTypeRefusalV2)


# ══ overflow, and agreement with V1 ══════════════════════════════════════════════════════════════
def test_SATURATE_is_refused_exactly_as_V1_refuses_it():
    declared = DecimalPolicy(precision=38, scale=2, rounding=RoundingMode.HALF_EVEN,
                             overflow=OverflowBehavior.SATURATE)
    refusal = product_sum_type_v2(AMOUNT, RATE, declared, round_per_row=False)
    assert isinstance(refusal, PhysicalTypeRefusalV2)
    assert refusal.code == SATURATE_UNSUPPORTED
    assert "V1 refuses this for the same reason" in refusal.detail


def test_the_two_policies_share_V1s_decimal_CEILING():
    """"V1 and V2 agree where they overlap" — read from V1's module, not copied as a number."""
    from featuregen.materialize import physical_types as v1

    assert MAX_DECIMAL_PRECISION == v1._MAX_DECIMAL_PRECISION


@pytest.mark.parametrize("bad", [(0, 0), (39, 2), (10, 11), (-1, 0)])
def test_an_unrepresentable_decimal_cannot_be_CONSTRUCTED(bad):
    """A type object for a column nothing can hold would let an unrepresentable type travel."""
    with pytest.raises(ValueError):
        DecimalTypeV2(*bad)


# ══ the policy id has ONE definition ═════════════════════════════════════════════════════════════
def test_the_policy_id_is_the_one_the_boundary_accepts():
    """`boundary_v2` checks the SHAPE of a policy id; this module is what the shape refers to. Two
    spellings would mean a contract stamped with an id no policy defines."""
    from featuregen.materialize.boundary_v2 import FeatureGroupPlanV2

    plan = FeatureGroupPlanV2(
        logical_group_name="g", materialization_contract_hash="c",
        entity_key_columns=("acct_id",), business_dt_column="business_dt",
        features=(), physical_type_policy=PHYSICAL_TYPE_POLICY_V2)
    assert plan.physical_type_policy == "formula-v2/physical-types@1"


def test_the_v2_policy_id_is_NOT_v1s_ordinal():
    assert PHYSICAL_TYPE_POLICY_V2 != str(PHYSICAL_TYPE_POLICY_VERSION)
    assert isinstance(PHYSICAL_TYPE_POLICY_VERSION, int)
