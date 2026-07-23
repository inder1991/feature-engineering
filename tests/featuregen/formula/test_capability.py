"""Tests for the v1 capability classifier (Child-1 Task 7).

The classifier runs AFTER structural validation (Task 1/2): it answers only
"within v1 authoring capability?" for an already-well-formed proposal. It
NEVER reports "invalid" and NEVER raises — the multi-source fixtures below
are first proven structurally VALID via ``validate_semantics``, then shown
to classify as ``"unsupported_capability"`` (valid-but-unsupported).
"""
from __future__ import annotations

from dataclasses import replace

from featuregen.formula.capability import (
    CAPABILITY_POLICY_VERSION,
    classify_formula_capability,
)
from featuregen.formula.schema import (
    AggregateExpression,
    AggregateFunction,
    DiffBody,
    Grain,
    SourceRelation,
    TypedFormulaProposalV1,
    UnaryBody,
    WindowBasis,
    WindowUnit,
    validate_semantics,
)
from tests.featuregen.formula import factories as f

# A second catalog source ("core"), distinct from the factories' "ftr" source.
OTHER_TABLE_REF = "core::bank.transactions"
OTHER_AMOUNT_REF = f"{OTHER_TABLE_REF}.amount"
OTHER_EVENT_TIME_REF = f"{OTHER_TABLE_REF}.event_ts"


def other_source_expression(
    aggregation: AggregateFunction = AggregateFunction.SUM,
) -> AggregateExpression:
    return AggregateExpression(
        aggregation=aggregation,
        operand=None if aggregation is AggregateFunction.COUNT_ROWS else OTHER_AMOUNT_REF,
        source_relation=SourceRelation(table_ref=OTHER_TABLE_REF),
        filter=None,
        window=f.trailing_90d_window(event_time_ref=OTHER_EVENT_TIME_REF),
    )


def make_proposal(body) -> TypedFormulaProposalV1:
    return TypedFormulaProposalV1(
        formula_schema_version=1,
        operation_grammar_version=1,
        canonicalization_version=1,
        grain=f.customer_grain(),
        body=body,
        parameters=(),
        decimal=f.default_decimal(),
        expected_output=None,
    )


class TestPolicyVersion:
    def test_capability_policy_version_is_pinned_to_1(self):
        assert CAPABILITY_POLICY_VERSION == 1


class TestSingleSourceIsOk:
    def test_unary_single_source_is_ok(self):
        proposal = make_proposal(UnaryBody(expr=f.sum_expression()))
        assert classify_formula_capability(proposal) == "ok"

    def test_ratio_single_source_is_ok(self):
        proposal = make_proposal(f.ratio_of_sums())
        assert classify_formula_capability(proposal) == "ok"

    def test_count_rows_none_operand_is_ok(self):
        # COUNT_ROWS has no operand — the source walk must skip None, not crash.
        expr = AggregateExpression(
            aggregation=AggregateFunction.COUNT_ROWS,
            operand=None,
            source_relation=SourceRelation(table_ref=f.TABLE_REF),
            filter=None,
            window=f.trailing_90d_window(),
        )
        proposal = make_proposal(UnaryBody(expr=expr))
        validate_semantics(proposal)
        assert classify_formula_capability(proposal) == "ok"

    def test_calendar_period_window_is_within_v1(self):
        # Both WindowBasis values are v1 capability — CALENDAR_PERIOD is ok.
        window = replace(
            f.trailing_90d_window(),
            basis=WindowBasis.CALENDAR_PERIOD,
            length=1,
            unit=WindowUnit.MONTH,
        )
        proposal = make_proposal(UnaryBody(expr=f.sum_expression(window=window)))
        validate_semantics(proposal)
        assert classify_formula_capability(proposal) == "ok"


class TestMultiSourceIsUnsupportedNotInvalid:
    def test_ratio_across_two_sources_is_unsupported_capability(self):
        proposal = make_proposal(
            f.ratio_of_sums(denominator=other_source_expression())
        )
        # Valid-but-unsupported: structural validation accepts it...
        validate_semantics(proposal)
        # ...and the classifier RETURNS the literal (no raise, no "invalid").
        assert classify_formula_capability(proposal) == "unsupported_capability"

    def test_diff_across_two_sources_is_unsupported_capability(self):
        body = DiffBody(
            minuend=f.sum_expression(),
            subtrahend=other_source_expression(),
        )
        proposal = make_proposal(body)
        validate_semantics(proposal)
        assert classify_formula_capability(proposal) == "unsupported_capability"

    def test_second_expression_source_is_not_ignored(self):
        # Guard against a first-expression-only walk: the FIRST expression's
        # refs alone look single-source; only the second introduces "core".
        proposal = make_proposal(
            f.ratio_of_sums(
                numerator=f.sum_expression(),
                denominator=other_source_expression(AggregateFunction.COUNT_ROWS),
            )
        )
        validate_semantics(proposal)
        assert classify_formula_capability(proposal) == "unsupported_capability"

    def test_diff_single_source_is_ok(self):
        body = DiffBody(
            minuend=f.sum_expression(filter_node=f.equal_predicate()),
            subtrahend=f.sum_expression(),
        )
        assert classify_formula_capability(make_proposal(body)) == "ok"


class TestNeverRaisesOnWellFormed:
    def test_multi_source_classification_never_raises(self):
        # The §F fold depends on a verdict, not an exception.
        proposal = make_proposal(
            f.ratio_of_sums(denominator=other_source_expression())
        )
        verdict = classify_formula_capability(proposal)
        assert verdict in ("ok", "unsupported_capability")

    def test_grain_keys_do_not_drive_the_source_gate(self):
        # The v1 gate is over expression operands + source_relations; grain
        # keys are column refs but not operands. A single-source body stays ok
        # regardless of grain key spelling (structure already validated).
        proposal = make_proposal(UnaryBody(expr=f.sum_expression()))
        proposal = replace(
            proposal, grain=Grain(entity="customer", keys=(f.CIF_KEY_REF,))
        )
        validate_semantics(proposal)
        assert classify_formula_capability(proposal) == "ok"
