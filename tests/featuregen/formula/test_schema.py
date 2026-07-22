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

    def test_valid_filter_at_max_depth_passes(self):
        node: s.FilterNode = eq_pred()
        for _ in range(s.MAX_FILTER_DEPTH - 1):  # depth: predicate=1, each NOT +1
            node = FilterBool(op=FilterBoolOp.NOT, children=(node,))
        assert validate_semantics(proposal_with_filter(node)) is None

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


# ------------------------------------------- predicate invariants + limits

def lit_str(v: str = "pos") -> TypedLiteral:
    return TypedLiteral(type=LiteralType.STRING, value=v)


class TestPredicateInvariants:
    def test_is_null_rejects_any_right_side(self):
        node = FilterPredicate(
            op=FilterPredicateOp.IS_NULL, left=CHANNEL, right_literal=lit_str()
        )
        with pytest.raises(SchemaError, match="is_null"):
            validate_semantics(proposal_with_filter(node))

    def test_is_not_null_rejects_right_param(self):
        node = FilterPredicate(
            op=FilterPredicateOp.IS_NOT_NULL,
            left=CHANNEL,
            right_param=ParameterRef(name="x"),
        )
        with pytest.raises(SchemaError, match="is_not_null"):
            validate_semantics(proposal_with_filter(node))

    def test_in_requires_right_set(self):
        node = FilterPredicate(
            op=FilterPredicateOp.IN, left=CHANNEL, right_literal=lit_str()
        )
        with pytest.raises(SchemaError, match="right_set"):
            validate_semantics(proposal_with_filter(node))

    def test_in_rejects_empty_right_set(self):
        node = FilterPredicate(op=FilterPredicateOp.NOT_IN, left=CHANNEL, right_set=())
        with pytest.raises(SchemaError, match="non-empty"):
            validate_semantics(proposal_with_filter(node))

    def test_in_rejects_right_set_over_max_in_list(self):
        entries = tuple(lit_str(f"v{i:03d}") for i in range(s.MAX_IN_LIST + 1))
        node = FilterPredicate(op=FilterPredicateOp.IN, left=CHANNEL, right_set=entries)
        with pytest.raises(SchemaError, match="MAX_IN_LIST"):
            validate_semantics(proposal_with_filter(node))

    def test_comparison_rejects_no_right_operand(self):
        node = FilterPredicate(op=FilterPredicateOp.EQUAL, left=CHANNEL)
        with pytest.raises(SchemaError, match="exactly one"):
            validate_semantics(proposal_with_filter(node))

    def test_comparison_rejects_both_right_operands(self):
        node = FilterPredicate(
            op=FilterPredicateOp.EQUAL,
            left=CHANNEL,
            right_literal=lit_str(),
            right_param=ParameterRef(name="x"),
        )
        with pytest.raises(SchemaError, match="exactly one"):
            validate_semantics(proposal_with_filter(node))

    def test_comparison_rejects_right_set(self):
        node = FilterPredicate(
            op=FilterPredicateOp.EQUAL,
            left=CHANNEL,
            right_literal=lit_str(),
            right_set=(lit_str(),),
        )
        with pytest.raises(SchemaError, match="right_set"):
            validate_semantics(proposal_with_filter(node))


class TestFilterShapeLimits:
    def test_not_requires_exactly_one_child(self):
        node = FilterBool(op=FilterBoolOp.NOT, children=(eq_pred(), eq_pred()))
        with pytest.raises(SchemaError, match="exactly 1 child"):
            validate_semantics(proposal_with_filter(node))

    def test_and_requires_at_least_two_children(self):
        node = FilterBool(op=FilterBoolOp.AND, children=(eq_pred(),))
        with pytest.raises(SchemaError, match="at least 2"):
            validate_semantics(proposal_with_filter(node))

    def test_or_requires_at_least_two_children(self):
        node = FilterBool(op=FilterBoolOp.OR, children=())
        with pytest.raises(SchemaError, match="at least 2"):
            validate_semantics(proposal_with_filter(node))

    def test_filter_depth_over_max_rejected(self):
        node: s.FilterNode = eq_pred()
        for _ in range(s.MAX_FILTER_DEPTH):  # predicate=1 + 4 NOTs = depth 5
            node = FilterBool(op=FilterBoolOp.NOT, children=(node,))
        with pytest.raises(SchemaError, match="MAX_FILTER_DEPTH"):
            validate_semantics(proposal_with_filter(node))

    def test_predicate_count_over_max_rejected(self):
        node = FilterBool(
            op=FilterBoolOp.AND,
            children=tuple(eq_pred() for _ in range(s.MAX_PREDICATES + 1)),
        )
        with pytest.raises(SchemaError, match="MAX_PREDICATES"):
            validate_semantics(proposal_with_filter(node))


# ------------------------------------------- ref arity + same-table containment

class TestRefArity:
    def test_table_ref_with_column_rejected(self):
        p = make_proposal(
            body=UnaryBody(expr=make_expr(source_relation=SourceRelation(table_ref=AMT)))
        )
        with pytest.raises(SchemaError, match="table"):
            validate_semantics(p)

    def test_operand_ref_without_column_rejected(self):
        p = make_proposal(body=UnaryBody(expr=make_expr(operand=TXN_TABLE)))
        with pytest.raises(SchemaError, match="column"):
            validate_semantics(p)

    def test_ref_without_source_separator_rejected(self):
        p = make_proposal(body=UnaryBody(expr=make_expr(operand="bank.transactions.amount")))
        with pytest.raises(SchemaError, match="::"):
            validate_semantics(p)

    def test_ref_with_empty_segment_rejected(self):
        p = make_proposal(body=UnaryBody(expr=make_expr(operand="core::bank..amount")))
        with pytest.raises(SchemaError):
            validate_semantics(p)

    def test_filter_left_must_be_column_ref(self):
        with pytest.raises(SchemaError, match="column"):
            validate_semantics(proposal_with_filter(eq_pred(left=TXN_TABLE)))

    def test_event_time_ref_must_be_column_ref(self):
        p = make_proposal(
            body=UnaryBody(expr=make_expr(window=make_window(event_time_ref=TXN_TABLE)))
        )
        with pytest.raises(SchemaError, match="column"):
            validate_semantics(p)

    def test_grain_key_must_be_column_ref(self):
        p = make_proposal(grain=Grain(entity="customer", keys=(TXN_TABLE,)))
        with pytest.raises(SchemaError, match="column"):
            validate_semantics(p)


class TestSameTableContainment:
    # Cross-table reachability is DEFERRED to governed planning (Child 3);
    # Child-1 enforces pure same-table containment only.
    def test_operand_from_other_table_rejected(self):
        p = make_proposal(body=UnaryBody(expr=make_expr(operand=OTHER_TABLE_COL)))
        with pytest.raises(SchemaError, match="source_relation"):
            validate_semantics(p)

    def test_filter_left_from_other_table_rejected(self):
        with pytest.raises(SchemaError, match="source_relation"):
            validate_semantics(proposal_with_filter(eq_pred(left=OTHER_TABLE_COL)))

    def test_nested_filter_left_from_other_table_rejected(self):
        node = FilterBool(
            op=FilterBoolOp.AND,
            children=(eq_pred(), FilterBool(op=FilterBoolOp.NOT, children=(eq_pred(left=OTHER_TABLE_COL),))),
        )
        with pytest.raises(SchemaError, match="source_relation"):
            validate_semantics(proposal_with_filter(node))

    def test_event_time_ref_from_other_table_rejected(self):
        p = make_proposal(
            body=UnaryBody(
                expr=make_expr(window=make_window(event_time_ref=OTHER_TABLE_COL))
            )
        )
        with pytest.raises(SchemaError, match="source_relation"):
            validate_semantics(p)

    def test_each_ratio_expression_checked_independently(self):
        body = RatioBody(
            numerator=make_expr(),
            denominator=make_expr(
                aggregation=AggregateFunction.COUNT_NON_NULL, operand=OTHER_TABLE_COL
            ),
            zero_denominator=s.ZeroDenominator.NULL,
        )
        with pytest.raises(SchemaError, match="body.denominator"):
            validate_semantics(make_proposal(body=body))
