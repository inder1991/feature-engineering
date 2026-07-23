"""Versioned v1 capability classifier — Child-1 Task 7.

Answers exactly one question about an ALREADY-STRUCTURALLY-VALID proposal:
is it within the v1 authoring capability, or valid-but-unsupported? The
verdict feeds the §F disposition fold's ``unsupported_operation →
UNSUPPORTED`` path.

Load-bearing boundary: **unsupported != invalid.** Structural invalidity is
Task 1/2's job (``validate_semantics`` already passed before this runs), so
this classifier NEVER reports "invalid" and NEVER raises on a well-formed
proposal — it only returns one of the two verdict literals.

v1 capability:
- a SINGLE catalog source across ALL operands and source relations (the
  guard that keeps cross-source formulas out until a later child adds them);
- a single trailing/calendar window per expression — structurally guaranteed
  (each ``AggregateExpression`` carries exactly one ``WindowPolicy`` and both
  ``WindowBasis`` values are in v1), so it is not re-litigated here.
"""
from __future__ import annotations

from typing import Literal

from featuregen.formula.schema import (
    AggregateExpression,
    DiffBody,
    FormulaBody,
    LogicalRef,
    RatioBody,
    TypedFormulaProposalV1,
    UnaryBody,
)

# Version pin for THIS classifier's policy. Deliberately NOT part of the
# TypedFormulaV1 identity object [c7] — capability is a policy verdict about
# a formula, not part of what the formula *is*.
CAPABILITY_POLICY_VERSION = 1


def classify_formula_capability(
    proposal: TypedFormulaProposalV1,
) -> Literal["ok", "unsupported_capability"]:
    """Classify a structurally valid proposal against the v1 capability.

    Returns ``"ok"`` iff every operand and every ``source_relation.table_ref``
    across ALL body expressions resolves to one single catalog source;
    otherwise ``"unsupported_capability"``. Total on well-formed proposals:
    never raises, never reports invalidity.
    """
    expressions = _body_expressions(proposal.body)
    if expressions is None:
        # Unreachable after Task 1/2 structural validation (the body union is
        # closed). A body shape v1 does not know is out of v1 capability —
        # fail toward the verdict, never toward an exception.
        return "unsupported_capability"
    sources: set[str] = set()
    for expr in expressions:
        sources.add(_ref_source(expr.source_relation.table_ref))
        if expr.operand is not None:  # COUNT_ROWS carries no operand
            sources.add(_ref_source(expr.operand))
    if len(sources) > 1:
        return "unsupported_capability"
    return "ok"


def _body_expressions(body: FormulaBody) -> tuple[AggregateExpression, ...] | None:
    """Every aggregate expression in the body union — never just the first.

    ``None`` (not an empty tuple) signals a body outside the known union so
    the caller can classify it out-of-capability rather than vacuously ok.
    """
    if isinstance(body, UnaryBody):
        return (body.expr,)
    if isinstance(body, RatioBody):
        return (body.numerator, body.denominator)
    if isinstance(body, DiffBody):
        return (body.minuend, body.subtrahend)
    return None


def _ref_source(ref: LogicalRef) -> str:
    """The catalog source of a canonical ``source::schema.table[.column]`` ref.

    Refs are guaranteed canonical here (structural validation already ran),
    so the source is simply the prefix before ``::``.
    """
    return ref.split("::", 1)[0]
