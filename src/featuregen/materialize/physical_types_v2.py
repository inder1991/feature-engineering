"""C-C6 — ``formula-v2/physical-types@1``: what ``SUM(amount × booking_rate)`` IS, completely.

**Why V1's policy does not cover this.** V1 types the SUM of a COLUMN. V2's pilot sums a PRODUCT,
and a product introduces three decisions V1 never had to make: what type the intermediate product
has, where the rounding happens, and how much the SUM grows. Leaving any of them to the engine's
defaults means the number depends on a Spark configuration flag rather than on a governed policy.

**Grounded in Spark's own decimal arithmetic**, because that is the engine the artifact runs on:

* ``DECIMAL(p1,s1) * DECIMAL(p2,s2)`` → ``DECIMAL(p1+p2+1, s1+s2)``
* ``SUM(DECIMAL(p,s))`` → ``DECIMAL(p+10, s)``

**The precision cap is a REFUSAL, not a cap.** Spark's default
``spark.sql.decimalOperations.allowPrecisionLoss=true`` caps a derived type at ``DECIMAL(38, s)``
and *reduces the scale* to fit — silently, per row, producing a number that is wrong in the last
places and never says so. This policy refuses instead. A refusal is a conversation about precision;
a silent scale reduction is a balance that does not reconcile and nobody knows why.

**Rounding SITE is governed, not incidental.** Rounding each product before summing and rounding
once after summing give different totals, and the difference grows with row count. Both are
defensible; which one happened must be a fact the artifact states, which is why C-C10a's
``DecimalMultiplicationV2`` carries ``round_per_row`` and why it is identity-bearing.

**Floats are refused outright.** A monetary operand typed ``FLOAT``/``DOUBLE``/``REAL`` cannot be
summed reproducibly — binary floating point makes the total depend on row order, so two runs over
the same window can disagree. V1 already requires exact numerics for arithmetic operands; this
states the same rule for the V2 product and names it.

**Nullability MIRRORS V1** rather than restating it: ``empty_window``, ``null_input`` and the
zero-denominator policy are the sources, and FX adds none — a missing rate is a REFUSAL (C-C10's
missing-rate gate), not a NULL, so conversion cannot make a non-nullable column nullable.
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.formula.schema import DecimalPolicy, OverflowBehavior

__all__ = [
    "FLOAT_OPERAND_REFUSED",
    "MAX_DECIMAL_PRECISION",
    "PHYSICAL_TYPE_POLICY_V2",
    "PRECISION_EXCEEDED",
    "SATURATE_UNSUPPORTED",
    "SUM_GROWTH_DIGITS",
    "DecimalTypeV2",
    "PhysicalTypeRefusalV2",
    "multiply_type_v2",
    "product_sum_type_v2",
    "refuse_inexact_operand_v2",
    "sum_type_v2",
]

#: The policy id `MaterializationContractV2` and `FeatureGroupPlanV2` stamp. ONE definition — the
#: boundary checks the SHAPE of a policy id; this module is what the shape refers to.
PHYSICAL_TYPE_POLICY_V2 = "formula-v2/physical-types@1"

#: Hive/Spark's hard ceiling. Beyond it there is no DECIMAL, only a silent scale reduction.
MAX_DECIMAL_PRECISION = 38

#: Spark's own SUM growth: ``SUM(DECIMAL(p,s))`` → ``DECIMAL(p+10, s)``. Ten digits is roughly ten
#: billion rows before the sum can overflow a type that held one row, which is the point of it.
SUM_GROWTH_DIGITS = 10

#: Exact numeric words this policy accepts for an arithmetic operand.
_EXACT_NUMERIC = frozenset({"decimal", "numeric", "bigint", "int", "integer", "smallint",
                            "tinyint", "long", "short", "byte"})
#: Inexact words, refused by name so the message can say WHY rather than "unsupported type".
_INEXACT_NUMERIC = frozenset({"float", "double", "real", "double precision"})

PRECISION_EXCEEDED = "PHYSICAL_TYPE_PRECISION_EXCEEDED"
FLOAT_OPERAND_REFUSED = "FLOAT_OPERAND_REFUSED"
SATURATE_UNSUPPORTED = "SATURATE_OVERFLOW_UNSUPPORTED"


@dataclass(frozen=True, slots=True)
class DecimalTypeV2:
    """A representable ``DECIMAL(p,s)``. Constructing one is the claim that it fits."""

    precision: int
    scale: int

    def __post_init__(self) -> None:
        if not 1 <= self.precision <= MAX_DECIMAL_PRECISION:
            raise ValueError(
                f"precision {self.precision} is outside 1..{MAX_DECIMAL_PRECISION}: there is no "
                f"such DECIMAL, so a type object for it would describe a column nothing can hold")
        if not 0 <= self.scale <= self.precision:
            raise ValueError(
                f"scale {self.scale} is outside 0..{self.precision} and does not describe a "
                f"representable DECIMAL")

    def __str__(self) -> str:
        return f"DECIMAL({self.precision},{self.scale})"


@dataclass(frozen=True, slots=True)
class PhysicalTypeRefusalV2:
    """A governed refusal, RETURNED rather than raised — one verdict among many a run collects."""

    code: str
    detail: str


def refuse_inexact_operand_v2(
    logical_type: str, logical_ref: str,
) -> PhysicalTypeRefusalV2 | None:
    """``None`` when ``logical_type`` may take part in the product, a refusal otherwise.

    Unknown words are refused too. An operand this policy cannot classify is one whose arithmetic
    it cannot promise, and defaulting to "probably fine" is how a DOUBLE reaches a sum.
    """
    word = logical_type.strip().lower().split("(")[0].strip()
    if word in _EXACT_NUMERIC:
        return None
    if word in _INEXACT_NUMERIC:
        return PhysicalTypeRefusalV2(
            FLOAT_OPERAND_REFUSED,
            f"{logical_ref} is typed {logical_type!r}, which is binary floating point. A sum over "
            f"it depends on the order the rows happen to arrive, so two runs over the same window "
            f"can disagree — and a monetary total that is not reproducible is not a total")
    return PhysicalTypeRefusalV2(
        FLOAT_OPERAND_REFUSED,
        f"{logical_ref} is typed {logical_type!r}, which this policy does not classify as an exact "
        f"numeric. An operand whose arithmetic cannot be promised is refused rather than assumed")


def multiply_type_v2(
    left: DecimalTypeV2, right: DecimalTypeV2,
) -> DecimalTypeV2 | PhysicalTypeRefusalV2:
    """``DECIMAL(p1+p2+1, s1+s2)`` — Spark's rule, or a refusal when it does not fit.

    The refusal is the whole point: Spark's default would cap this at 38 and REDUCE THE SCALE to
    make it fit, which changes every row's value in its last places without saying so.
    """
    precision = left.precision + right.precision + 1
    scale = left.scale + right.scale
    return _fit(precision, scale, what=f"the product of {left} and {right}")


def sum_type_v2(operand: DecimalTypeV2) -> DecimalTypeV2 | PhysicalTypeRefusalV2:
    """``DECIMAL(p+10, s)`` — Spark's SUM growth, or a refusal when it does not fit."""
    return _fit(operand.precision + SUM_GROWTH_DIGITS, operand.scale,
                what=f"the sum of {operand}")


def _fit(precision: int, scale: int, *, what: str) -> DecimalTypeV2 | PhysicalTypeRefusalV2:
    if precision > MAX_DECIMAL_PRECISION:
        return PhysicalTypeRefusalV2(
            PRECISION_EXCEEDED,
            f"{what} needs DECIMAL({precision},{scale}), beyond Hive/Spark's maximum precision of "
            f"{MAX_DECIMAL_PRECISION}. Spark's default would cap the precision and REDUCE THE "
            f"SCALE to fit, silently changing every value in its last places; declare a narrower "
            f"decimal policy or round earlier, and choose that deliberately")
    if scale > precision:
        return PhysicalTypeRefusalV2(
            PRECISION_EXCEEDED,
            f"{what} needs DECIMAL({precision},{scale}), whose scale exceeds its precision and so "
            f"describes no representable type")
    return DecimalTypeV2(precision=precision, scale=scale)


def product_sum_type_v2(
    amount: DecimalTypeV2,
    rate: DecimalTypeV2,
    declared: DecimalPolicy,
    *,
    round_per_row: bool,
) -> DecimalTypeV2 | PhysicalTypeRefusalV2:
    """The complete type of ``SUM(amount × rate)`` under ``declared``, at the declared rounding site.

    ``round_per_row=True`` rounds each product to the declared decimal BEFORE summing, so the sum
    grows from the DECLARED type. ``round_per_row=False`` keeps the full intermediate product and
    rounds once after summing, so the sum grows from the INTERMEDIATE type — wider, more faithful,
    and the one that can exceed the ceiling.

    Both are defensible and they give different totals; this function does not choose, it types
    what was chosen.
    """
    if declared.overflow is OverflowBehavior.SATURATE:
        return PhysicalTypeRefusalV2(
            SATURATE_UNSUPPORTED,
            "the formula declares SATURATE overflow, which nothing here clamps: publishing the "
            "column would substitute a different overflow semantics for the one the formula asked "
            "for. V1 refuses this for the same reason, and the two policies agree")

    intermediate = multiply_type_v2(amount, rate)
    if isinstance(intermediate, PhysicalTypeRefusalV2):
        return intermediate

    if round_per_row:
        rounded = _fit(declared.precision, declared.scale,
                       what=f"the per-row rounded product under {declared.precision},{declared.scale}")
        if isinstance(rounded, PhysicalTypeRefusalV2):
            return rounded
        return sum_type_v2(rounded)
    return sum_type_v2(intermediate)
