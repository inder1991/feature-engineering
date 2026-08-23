"""BR-6 close — v2 output authority: the enforcement the declarations were carried FOR.

Pure resolution of one v2 proposal's OUTPUT — type, unit, currency, additivity — from the
operation rule table and injected per-operand governed facts, refusing what cannot be honest:

* **CURRENCY_CONVERSION_UNDECLARED** — a monetary operand whose source carries PER-ROW currency,
  in an expression declaring no governed conversion policy. Summing dirhams and dollars because
  nobody said otherwise is the exact defect class the audit counted 63 recipes wide; here it is
  a refusal, not a count.
* **MIXED_UNITS** — a difference or signed sum whose terms disagree on unit kind (amount − count
  is not a number anyone meant).
* Additivity is FOLDED, never asserted: unary bodies take their operation's rule-table additivity;
  ratios are non-additive; differences and signed sums take the WEAKEST of their terms
  (additive > semi_additive > non_additive) — a signed sum of last-known balances is honestly
  semi-additive, whatever its author hoped.

Facts are INJECTED (``OperandFactsV2``), not read live: this keeps the resolver pure and testable
now, and at the BR-17 cutover the caller wires the same governed ``read_operational_value`` facts
v1's resolver reads — same evidence, same verdicts, no second authority.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from featuregen.formula.operations_v2 import operation_rule
from featuregen.formula.schema_leaves import AdditivityClass
from featuregen.formula.schema_v2 import (
    CompositeBodyV2,
    DiffBodyV2,
    RatioBodyV2,
    UnaryBodyV2,
    body_expressions_v2,
)

OUTPUT_POLICY_VERSION_V2 = 1

CURRENCY_CONVERSION_UNDECLARED = "CURRENCY_CONVERSION_UNDECLARED"
MIXED_UNITS = "MIXED_UNITS"

_ADDITIVITY_STRENGTH = {AdditivityClass.ADDITIVE: 2, AdditivityClass.SEMI_ADDITIVE: 1,
                        AdditivityClass.NON_ADDITIVE: 0}


@dataclass(frozen=True, slots=True)
class OperandFactsV2:
    """The governed facts one operand's column carries: its logical type, unit kind, and currency
    posture — ``fixed:<CCY>`` (single-currency source) or ``per_row`` (a currency column governs
    each row) or ``""`` (non-monetary)."""

    logical_type: str = ""
    unit: str = ""                  # "monetary" | "count" | "" ...
    currency: str = ""              # "fixed:AED" | "per_row" | ""


@dataclass(frozen=True, slots=True)
class InvalidOutputV2:
    reason: str
    path: str = ""
    detail: str = ""


@dataclass(frozen=True, slots=True)
class FormulaOutputPolicyV2:
    output_type: str
    unit: str
    currency: str                   # "" | "fixed:<CCY>" | "converted:<policy ref>"
    output_additivity: AdditivityClass
    external_type_required: bool
    output_policy_version: int = OUTPUT_POLICY_VERSION_V2


def _weakest(classes: list[AdditivityClass]) -> AdditivityClass:
    return min(classes, key=lambda c: _ADDITIVITY_STRENGTH[c])


def resolve_output_v2(
    proposal,
    facts_by_ref: Mapping[str, OperandFactsV2],
) -> FormulaOutputPolicyV2 | InvalidOutputV2:
    """Resolve the output policy, or refuse. Total over valid proposals — a verdict either way.

    Takes a v2 OR v3 proposal: the output rules are about operands and declared conversions, and v3
    changed neither.
    """
    expressions = body_expressions_v2(proposal.body)

    # The conversion tooth runs per expression, FIRST — nothing downstream may launder it.
    for index, expr in enumerate(expressions):
        if expr.operand is None:
            continue
        facts = facts_by_ref.get(expr.operand, OperandFactsV2())
        if facts.unit == "monetary" and facts.currency == "per_row":
            refs = expr.authority_refs
            if refs is None or not refs.currency_conversion_ref.strip():
                return InvalidOutputV2(
                    CURRENCY_CONVERSION_UNDECLARED, path=f"body.expr[{index}]",
                    detail=(f"{expr.operand} carries per-row currency and the expression "
                            "declares no governed conversion policy — sums across currencies "
                            "are refused, never assumed"))

    body = proposal.body
    per_expr_additivity = [operation_rule(e.aggregation).additivity for e in expressions]
    per_expr_kinds = [operation_rule(e.aggregation).result_kind for e in expressions]

    # v2 OR v3 at each arm: v3 is the v2 LANGUAGE at a later wire version, so its four body shapes
    # mean exactly what v2's do here — output authority reads aggregations and operands, and v3
    # changed neither. Matching on v2 alone would refuse a valid v3 formula with an AssertionError,
    # which reads as a bug in this module rather than as anything about the formula.
    from featuregen.formula.schema_v3 import (
        CompositeBodyV3,
        DiffBodyV3,
        RatioBodyV3,
        UnaryBodyV3,
    )

    if isinstance(body, UnaryBodyV2 | UnaryBodyV3):
        additivity = per_expr_additivity[0]
        kind = per_expr_kinds[0]
    elif isinstance(body, RatioBodyV2 | RatioBodyV3):
        additivity, kind = AdditivityClass.NON_ADDITIVE, "dimensionless"
    elif isinstance(body, DiffBodyV2 | CompositeBodyV2 | DiffBodyV3 | CompositeBodyV3):
        units = set()
        for expr, expr_kind in zip(expressions, per_expr_kinds, strict=True):
            if expr_kind == "operand_valued" and expr.operand is not None:
                units.add(facts_by_ref.get(expr.operand, OperandFactsV2()).unit)
            else:
                units.add(expr_kind)
        if len(units) > 1:
            return InvalidOutputV2(
                MIXED_UNITS,
                detail=f"terms disagree on unit kind: {sorted(units)} — "
                       "amount minus count is not a number anyone meant")
        additivity = _weakest(per_expr_additivity)
        kind = per_expr_kinds[0]
    else:  # pragma: no cover — the body union is closed
        raise AssertionError(type(body).__name__)

    if kind == "count":
        return FormulaOutputPolicyV2("integer", "count", "", additivity, False)
    if kind == "flag":
        return FormulaOutputPolicyV2("boolean", "flag", "", additivity, False)
    if kind == "duration":
        return FormulaOutputPolicyV2("numeric", "days", "", additivity, False)
    if kind in ("rate", "dimensionless"):
        return FormulaOutputPolicyV2("numeric", kind, "", additivity, False)

    # operand-valued: the output inherits the (first) operand's governed facts
    first = next((e for e in expressions if e.operand is not None), None)
    facts = facts_by_ref.get(first.operand, OperandFactsV2()) if first else OperandFactsV2()
    currency = ""
    if facts.unit == "monetary":
        if facts.currency.startswith("fixed:"):
            currency = facts.currency
        elif facts.currency == "per_row":
            refs = first.authority_refs if first else None
            currency = f"converted:{refs.currency_conversion_ref}" if refs else ""
    return FormulaOutputPolicyV2(
        output_type=facts.logical_type or "numeric",
        unit=facts.unit,
        currency=currency,
        output_additivity=additivity,
        external_type_required=not facts.logical_type)
