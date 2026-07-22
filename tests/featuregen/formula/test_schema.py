"""Tests for the TypedFormula authoring schema (Child-1 spec §A) + validate_semantics.

Offline authoring only — no execution, no Spark.
"""
from __future__ import annotations

import dataclasses

import pytest

from featuregen.formula import schema as s
from featuregen.formula.schema import (
    AggregateExpression,
    AggregateFunction,
    DecimalPolicy,
    DiffBody,
    ExpectedOutput,
    FilterBool,
    FilterBoolOp,
    FilterPredicate,
    FilterPredicateOp,
    FinalOperation,
    Grain,
    Inclusivity,
    LiteralType,
    ParamClass,
    ParameterDecl,
    ParameterRef,
    RatioBody,
    RoundingMode,
    SchemaError,
    SourceRelation,
    TypedFormulaProposalV1,
    TypedLiteral,
    UnaryBody,
    WindowPolicy,
    validate_semantics,
)

# ---------------------------------------------------------------- builders

TXN_TABLE = "core::bank.transactions"
AMT = "core::bank.transactions.amount"
CHANNEL = "core::bank.transactions.channel"
EVENT_TS = "core::bank.transactions.event_ts"
CUSTOMER_KEY = "core::bank.transactions.customer_id"
OTHER_TABLE_COL = "core::bank.customers.segment"


def make_window(**over) -> WindowPolicy:
    kw = dict(
        event_time_ref=EVENT_TS,
        basis=s.WindowBasis.TRAILING,
        length=30,
        unit=s.WindowUnit.DAY,
        start_inclusive=Inclusivity.INCLUSIVE,
        end_inclusive=Inclusivity.EXCLUSIVE,
        timezone="UTC",
        empty_window=s.EmptyWindowResult.NULL,
        null_input=s.NullInput.IGNORE,
    )
    kw.update(over)
    return WindowPolicy(**kw)


def make_expr(**over) -> AggregateExpression:
    kw = dict(
        aggregation=AggregateFunction.SUM,
        operand=AMT,
        source_relation=SourceRelation(table_ref=TXN_TABLE),
        filter=None,
        window=make_window(),
    )
    kw.update(over)
    return AggregateExpression(**kw)


def make_proposal(**over) -> TypedFormulaProposalV1:
    kw = dict(
        formula_schema_version=s.FORMULA_SCHEMA_VERSION,
        operation_grammar_version=s.OPERATION_GRAMMAR_VERSION,
        canonicalization_version=s.CANONICALIZATION_VERSION,
        grain=Grain(entity="customer", keys=(CUSTOMER_KEY,)),
        body=UnaryBody(expr=make_expr()),
        parameters=(),
        decimal=DecimalPolicy(
            precision=18,
            scale=2,
            rounding=RoundingMode.HALF_EVEN,
            overflow=s.OverflowBehavior.ERROR,
        ),
        expected_output=None,
    )
    kw.update(over)
    return TypedFormulaProposalV1(**kw)


def eq_pred(**over) -> FilterPredicate:
    kw = dict(
        op=FilterPredicateOp.EQUAL,
        left=CHANNEL,
        right_literal=TypedLiteral(type=LiteralType.STRING, value="pos"),
        right_param=None,
        right_set=None,
    )
    kw.update(over)
    return FilterPredicate(**kw)


def proposal_with_filter(node, **expr_over) -> TypedFormulaProposalV1:
    return make_proposal(body=UnaryBody(expr=make_expr(filter=node, **expr_over)))


# ---------------------------------------------------------------- §A shape

class TestEnumExactValues:
    def test_aggregate_function_values(self):
        assert {e.value for e in AggregateFunction} == {
            "sum", "count_rows", "count_non_null", "count_distinct",
        }
        assert AggregateFunction.SUM == "sum"
        assert AggregateFunction.COUNT_ROWS == "count_rows"

    def test_final_operation_values(self):
        assert {e.value for e in FinalOperation} == {"identity", "ratio", "difference"}
        assert FinalOperation.RATIO == "ratio"

    def test_window_enums(self):
        assert {e.value for e in s.WindowBasis} == {"trailing", "calendar_period"}
        assert {e.value for e in s.WindowUnit} == {"day", "week", "month", "quarter", "year"}
        assert {e.value for e in Inclusivity} == {"inclusive", "exclusive"}
        assert {e.value for e in s.EmptyWindowResult} == {"null", "zero", "error"}
        assert {e.value for e in s.NullInput} == {"ignore", "propagate", "zero"}
        assert {e.value for e in s.ZeroDenominator} == {"null", "zero", "error"}

    def test_numeric_policy_enums(self):
        assert {e.value for e in RoundingMode} == {
            "half_up", "half_even", "down", "up", "floor", "ceiling",
        }
        assert {e.value for e in s.OverflowBehavior} == {"error", "saturate"}

    def test_literal_and_param_enums(self):
        assert {e.value for e in LiteralType} == {
            "string", "integer", "decimal", "boolean", "date",
        }
        assert {e.value for e in ParamClass} == {"semantic", "operational"}

    def test_filter_enums(self):
        assert {e.value for e in s.FilterKind} == {"bool", "predicate"}
        assert {e.value for e in FilterBoolOp} == {"and", "or", "not"}
        assert {e.value for e in FilterPredicateOp} == {
            "equal", "not_equal", "greater_than", "greater_or_equal",
            "less_than", "less_or_equal", "in", "not_in", "is_null", "is_not_null",
        }
        assert FilterPredicateOp.GREATER_OR_EQUAL == "greater_or_equal"

    def test_additivity_class_values(self):
        assert {e.value for e in s.AdditivityClass} == {
            "additive", "non_additive", "semi_additive",
        }


class TestDataclassShape:
    def test_hard_limit_constants(self):
        assert s.MAX_FILTER_DEPTH == 4
        assert s.MAX_PREDICATES == 16
        assert s.MAX_IN_LIST == 64

    def test_dataclasses_frozen(self):
        p = make_proposal()
        with pytest.raises(dataclasses.FrozenInstanceError):
            p.formula_schema_version = 2  # type: ignore[misc]
        with pytest.raises(dataclasses.FrozenInstanceError):
            p.body.expr.window.length = 5  # type: ignore[misc]

    def test_dataclasses_slotted(self):
        assert not hasattr(make_proposal(), "__dict__")
        assert not hasattr(make_window(), "__dict__")
        assert not hasattr(eq_pred(), "__dict__")

    def test_body_discriminators_fixed(self):
        assert UnaryBody(expr=make_expr()).final_operation is FinalOperation.IDENTITY
        ratio = RatioBody(
            numerator=make_expr(),
            denominator=make_expr(),
            zero_denominator=s.ZeroDenominator.NULL,
        )
        assert ratio.final_operation is FinalOperation.RATIO
        diff = DiffBody(minuend=make_expr(), subtrahend=make_expr())
        assert diff.final_operation is FinalOperation.DIFFERENCE

    def test_filter_kind_discriminators_fixed(self):
        assert eq_pred().kind is s.FilterKind.PREDICATE
        node = FilterBool(op=FilterBoolOp.AND, children=(eq_pred(), eq_pred()))
        assert node.kind is s.FilterKind.BOOL

    def test_authoritative_identity_object_exists(self):
        formula = s.TypedFormulaV1(
            formula_schema_version=s.FORMULA_SCHEMA_VERSION,
            operation_grammar_version=s.OPERATION_GRAMMAR_VERSION,
            output_policy_version=s.OUTPUT_POLICY_VERSION,
            canonicalization_version=s.CANONICALIZATION_VERSION,
            grain=Grain(entity="customer", keys=(CUSTOMER_KEY,)),
            body=UnaryBody(expr=make_expr()),
            parameters=(),
            decimal=DecimalPolicy(
                precision=18, scale=2,
                rounding=RoundingMode.HALF_EVEN, overflow=s.OverflowBehavior.ERROR,
            ),
            output=s.FormulaOutputPolicyV1(
                output_type="decimal(18,2)",
                unit=None,
                currency="USD",
                output_additivity=s.AdditivityClass.ADDITIVE,
                external_type_required=False,
            ),
        )
        assert not hasattr(formula, "__dict__")


class TestValidProposals:
    def test_valid_sum_proposal_passes(self):
        assert validate_semantics(make_proposal()) is None

    def test_valid_count_rows_proposal_passes(self):
        p = make_proposal(
            body=UnaryBody(
                expr=make_expr(aggregation=AggregateFunction.COUNT_ROWS, operand=None)
            )
        )
        assert validate_semantics(p) is None

    def test_valid_filtered_ratio_proposal_passes(self):
        param = ParameterDecl(
            name="min_amount",
            type=LiteralType.DECIMAL,
            param_class=ParamClass.SEMANTIC,
            classification="internal",
            nullable=False,
            allowed_set=None,
            allowed_min="0.00",
            allowed_max="100000.00",
        )
        flt = FilterBool(
            op=FilterBoolOp.AND,
            children=(
                eq_pred(),
                eq_pred(
                    op=FilterPredicateOp.GREATER_OR_EQUAL,
                    left=AMT,
                    right_literal=None,
                    right_param=ParameterRef(name="min_amount"),
                ),
            ),
        )
        body = RatioBody(
            numerator=make_expr(filter=flt),
            denominator=make_expr(aggregation=AggregateFunction.COUNT_ROWS, operand=None),
            zero_denominator=s.ZeroDenominator.NULL,
        )
        assert validate_semantics(make_proposal(body=body, parameters=(param,))) is None
