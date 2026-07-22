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


# ------------------------------------------- body / operand / decimal / literals

class TestBodyDiscriminator:
    def test_aggregation_rejects_final_operation(self):
        # FinalOperation.RATIO is a StrEnum member of the WRONG enum: the body
        # discriminator says `aggregation` is always an AggregateFunction.
        p = make_proposal(
            body=UnaryBody(expr=make_expr(aggregation=FinalOperation.RATIO))
        )
        with pytest.raises(SchemaError, match="AggregateFunction"):
            validate_semantics(p)

    def test_aggregation_rejects_raw_string(self):
        p = make_proposal(body=UnaryBody(expr=make_expr(aggregation="sum")))
        with pytest.raises(SchemaError, match="AggregateFunction"):
            validate_semantics(p)


class TestCountRowsOperand:
    def test_count_rows_with_operand_rejected(self):
        p = make_proposal(
            body=UnaryBody(
                expr=make_expr(aggregation=AggregateFunction.COUNT_ROWS, operand=AMT)
            )
        )
        with pytest.raises(SchemaError, match="count_rows"):
            validate_semantics(p)

    def test_non_count_rows_without_operand_rejected(self):
        p = make_proposal(
            body=UnaryBody(
                expr=make_expr(aggregation=AggregateFunction.COUNT_DISTINCT, operand=None)
            )
        )
        with pytest.raises(SchemaError, match="operand"):
            validate_semantics(p)


class TestDecimalPolicy:
    def test_negative_scale_rejected(self):
        p = make_proposal(
            decimal=DecimalPolicy(
                precision=18, scale=-1,
                rounding=RoundingMode.HALF_EVEN, overflow=s.OverflowBehavior.ERROR,
            )
        )
        with pytest.raises(SchemaError, match="scale"):
            validate_semantics(p)

    def test_precision_below_scale_rejected(self):
        p = make_proposal(
            decimal=DecimalPolicy(
                precision=2, scale=5,
                rounding=RoundingMode.HALF_EVEN, overflow=s.OverflowBehavior.ERROR,
            )
        )
        with pytest.raises(SchemaError, match="precision"):
            validate_semantics(p)


class TestTypedLiteralParse:
    def _filter_with_literal(self, lit: TypedLiteral, op=FilterPredicateOp.EQUAL):
        return proposal_with_filter(eq_pred(op=op, right_literal=lit))

    def test_integer_literal_must_parse(self):
        bad = TypedLiteral(type=LiteralType.INTEGER, value="1.5")
        with pytest.raises(SchemaError, match="integer"):
            validate_semantics(self._filter_with_literal(bad))
        ok = TypedLiteral(type=LiteralType.INTEGER, value="-42")
        assert validate_semantics(self._filter_with_literal(ok)) is None

    def test_decimal_literal_must_parse(self):
        for bad_value in ("abc", "NaN", "1e5"):
            bad = TypedLiteral(type=LiteralType.DECIMAL, value=bad_value)
            with pytest.raises(SchemaError, match="decimal"):
                validate_semantics(self._filter_with_literal(bad))
        ok = TypedLiteral(type=LiteralType.DECIMAL, value="10.25")
        assert validate_semantics(self._filter_with_literal(ok)) is None

    def test_boolean_literal_must_be_canonical(self):
        for bad_value in ("True", "1", "yes"):
            bad = TypedLiteral(type=LiteralType.BOOLEAN, value=bad_value)
            with pytest.raises(SchemaError, match="boolean"):
                validate_semantics(self._filter_with_literal(bad))
        ok = TypedLiteral(type=LiteralType.BOOLEAN, value="false")
        assert validate_semantics(self._filter_with_literal(ok)) is None

    def test_date_literal_must_be_iso(self):
        for bad_value in ("2026-13-40", "22/07/2026", "20260722"):
            bad = TypedLiteral(type=LiteralType.DATE, value=bad_value)
            with pytest.raises(SchemaError, match="date"):
                validate_semantics(self._filter_with_literal(bad))
        ok = TypedLiteral(type=LiteralType.DATE, value="2026-07-22")
        assert validate_semantics(self._filter_with_literal(ok)) is None

    def test_in_list_literals_are_each_parsed(self):
        entries = (
            TypedLiteral(type=LiteralType.INTEGER, value="1"),
            TypedLiteral(type=LiteralType.INTEGER, value="two"),
        )
        node = eq_pred(op=FilterPredicateOp.IN, right_literal=None, right_set=entries)
        with pytest.raises(SchemaError, match="integer"):
            validate_semantics(proposal_with_filter(node))


# ------------------------------------------- parameters + operator type compat

def make_param(**over) -> ParameterDecl:
    kw = dict(
        name="min_amount",
        type=LiteralType.DECIMAL,
        param_class=ParamClass.SEMANTIC,
        classification="internal",
        nullable=False,
        allowed_set=None,
        allowed_min=None,
        allowed_max=None,
    )
    kw.update(over)
    return ParameterDecl(**kw)


class TestParameterRules:
    def test_allowed_min_above_max_rejected(self):
        p = make_proposal(
            parameters=(make_param(allowed_min="10.00", allowed_max="9.99"),)
        )
        with pytest.raises(SchemaError, match="allowed_min"):
            validate_semantics(p)

    def test_allowed_min_max_compared_as_typed_values_not_strings(self):
        # "9" > "10" lexicographically; as integers 9 <= 10 must PASS.
        p = make_proposal(
            parameters=(
                make_param(name="days", type=LiteralType.INTEGER, allowed_min="9", allowed_max="10"),
            )
        )
        assert validate_semantics(p) is None

    def test_allowed_set_empty_rejected(self):
        p = make_proposal(parameters=(make_param(allowed_set=()),))
        with pytest.raises(SchemaError, match="allowed_set"):
            validate_semantics(p)

    def test_allowed_bound_must_parse_to_declared_type(self):
        p = make_proposal(
            parameters=(make_param(type=LiteralType.INTEGER, allowed_min="1.5"),)
        )
        with pytest.raises(SchemaError, match="integer"):
            validate_semantics(p)

    def test_name_regex_enforced(self):
        for bad_name in ("MinAmount", "9lives", "_x", "a" * 65, ""):
            p = make_proposal(parameters=(make_param(name=bad_name),))
            with pytest.raises(SchemaError, match="name"):
                validate_semantics(p)
        p = make_proposal(parameters=(make_param(name="a1_ok"),))
        assert validate_semantics(p) is None

    def test_duplicate_names_rejected(self):
        p = make_proposal(parameters=(make_param(), make_param()))
        with pytest.raises(SchemaError, match="unique"):
            validate_semantics(p)

    def test_unresolved_parameter_ref_rejected(self):
        node = eq_pred(
            op=FilterPredicateOp.GREATER_OR_EQUAL,
            left=AMT,
            right_literal=None,
            right_param=ParameterRef(name="undeclared"),
        )
        with pytest.raises(SchemaError, match="undeclared"):
            validate_semantics(proposal_with_filter(node))


class TestOrderedComparisonTypeCompatibility:
    def test_ordered_op_rejects_string_literal(self):
        node = eq_pred(
            op=FilterPredicateOp.GREATER_THAN,
            right_literal=TypedLiteral(type=LiteralType.STRING, value="pos"),
        )
        with pytest.raises(SchemaError, match="greater_than"):
            validate_semantics(proposal_with_filter(node))

    def test_ordered_op_rejects_boolean_literal(self):
        node = eq_pred(
            op=FilterPredicateOp.LESS_OR_EQUAL,
            right_literal=TypedLiteral(type=LiteralType.BOOLEAN, value="true"),
        )
        with pytest.raises(SchemaError, match="less_or_equal"):
            validate_semantics(proposal_with_filter(node))

    def test_ordered_op_rejects_string_param(self):
        node = eq_pred(
            op=FilterPredicateOp.LESS_THAN,
            left=AMT,
            right_literal=None,
            right_param=ParameterRef(name="channel_name"),
        )
        p = make_proposal(
            body=UnaryBody(expr=make_expr(filter=node)),
            parameters=(make_param(name="channel_name", type=LiteralType.STRING),),
        )
        with pytest.raises(SchemaError, match="less_than"):
            validate_semantics(p)

    def test_ordered_op_accepts_date_literal(self):
        node = eq_pred(
            op=FilterPredicateOp.GREATER_OR_EQUAL,
            left=EVENT_TS,
            right_literal=TypedLiteral(type=LiteralType.DATE, value="2026-01-01"),
        )
        assert validate_semantics(proposal_with_filter(node)) is None


# ------------------------------------------- window policy + version pins

class TestWindowPolicy:
    def _with_window(self, **over):
        return make_proposal(body=UnaryBody(expr=make_expr(window=make_window(**over))))

    def test_length_below_one_rejected(self):
        with pytest.raises(SchemaError, match="length"):
            validate_semantics(self._with_window(length=0))

    def test_empty_timezone_rejected(self):
        with pytest.raises(SchemaError, match="timezone"):
            validate_semantics(self._with_window(timezone=""))

    def test_unknown_iana_timezone_rejected(self):
        with pytest.raises(SchemaError, match="timezone"):
            validate_semantics(self._with_window(timezone="Mars/Olympus_Mons"))

    def test_named_iana_timezone_accepted(self):
        assert validate_semantics(self._with_window(timezone="America/New_York")) is None

    def test_missing_start_inclusivity_rejected(self):
        with pytest.raises(SchemaError, match="start_inclusive"):
            validate_semantics(self._with_window(start_inclusive=None))

    def test_missing_end_inclusivity_rejected(self):
        with pytest.raises(SchemaError, match="end_inclusive"):
            validate_semantics(self._with_window(end_inclusive=None))


class TestVersionPins:
    def test_unknown_formula_schema_version_rejected(self):
        with pytest.raises(SchemaError, match="formula_schema_version"):
            validate_semantics(make_proposal(formula_schema_version=99))

    def test_unknown_operation_grammar_version_rejected(self):
        with pytest.raises(SchemaError, match="operation_grammar_version"):
            validate_semantics(make_proposal(operation_grammar_version=0))

    def test_unknown_canonicalization_version_rejected(self):
        with pytest.raises(SchemaError, match="canonicalization_version"):
            validate_semantics(make_proposal(canonicalization_version=-1))
