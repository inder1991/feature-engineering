"""Tests for the strict dict→typed proposal parser (Child-1 Task 2).

parse_proposal_v1 is the ONLY place a TypedFormulaProposalV1 is constructed
from untrusted (LLM) input. Layer order under test: JSON-Schema shape gate
FIRST, then dataclass construction, then validate_semantics.
"""
from __future__ import annotations

import pytest

from featuregen.formula import schema as s
from featuregen.formula.parse import parse_proposal_v1
from featuregen.formula.schema import (
    AggregateExpression,
    AggregateFunction,
    DecimalPolicy,
    ExpectedOutput,
    FilterBool,
    FilterBoolOp,
    FilterPredicate,
    FilterPredicateOp,
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
    ZeroDenominator,
)

# ---------------------------------------------------------------- raw builders

TXN_TABLE = "core::bank.transactions"
AMT = "core::bank.transactions.amount"
CROSS_BORDER = "core::bank.transactions.is_cross_border"
CHANNEL = "core::bank.transactions.channel"
EVENT_TS = "core::bank.transactions.event_ts"
CUSTOMER_KEY = "core::bank.transactions.customer_id"


def raw_window(**over) -> dict:
    d = {
        "event_time_ref": EVENT_TS,
        "basis": "trailing",
        "length": 90,
        "unit": "day",
        "start_inclusive": "inclusive",
        "end_inclusive": "exclusive",
        "timezone": "UTC",
        "empty_window": "null",
        "null_input": "ignore",
    }
    d.update(over)
    return d


def raw_expr(**over) -> dict:
    d = {
        "aggregation": "sum",
        "operand": AMT,
        "source_relation": {"table_ref": TXN_TABLE},
        "window": raw_window(),
    }
    d.update(over)
    return d


def raw_ratio_proposal(**over) -> dict:
    """A fully-valid `cross_border_value_ratio_90d`-shaped raw dict."""
    d = {
        "formula_schema_version": 1,
        "operation_grammar_version": 1,
        "canonicalization_version": 1,
        "grain": {"entity": "customer", "keys": [CUSTOMER_KEY]},
        "body": {
            "final_operation": "ratio",
            "numerator": raw_expr(
                filter={
                    "kind": "predicate",
                    "op": "equal",
                    "left": CROSS_BORDER,
                    "right_literal": {"type": "boolean", "value": "true"},
                }
            ),
            "denominator": raw_expr(),
            "zero_denominator": "null",
        },
        "parameters": [],
        "decimal": {
            "precision": 18,
            "scale": 6,
            "rounding": "half_even",
            "overflow": "error",
        },
        "expected_output": {"output_type": "decimal", "unit": "ratio", "currency": None},
    }
    d.update(over)
    return d


def raw_unary_proposal(**expr_over) -> dict:
    return raw_ratio_proposal(
        body={"final_operation": "identity", "expr": raw_expr(**expr_over)}
    )


# ---------------------------------------------------------------- shape gate


class TestShapeGate:
    def test_unknown_top_level_field_rejected(self):
        raw = raw_ratio_proposal(critic_notes="looks great")
        with pytest.raises(SchemaError, match="critic_notes"):
            parse_proposal_v1(raw)

    def test_unknown_nested_field_rejected(self):
        raw = raw_unary_proposal()
        raw["body"]["expr"]["window"]["tz_hint"] = "UTC"
        with pytest.raises(SchemaError, match="tz_hint"):
            parse_proposal_v1(raw)

    def test_ratio_without_denominator_rejected(self):
        raw = raw_ratio_proposal()
        del raw["body"]["denominator"]
        with pytest.raises(SchemaError):
            parse_proposal_v1(raw)

    def test_dropped_required_grain_rejected(self):
        raw = raw_ratio_proposal()
        del raw["grain"]
        with pytest.raises(SchemaError, match="grain"):
            parse_proposal_v1(raw)

    def test_avg_aggregation_rejected_by_enum(self):
        raw = raw_unary_proposal(aggregation="avg")
        with pytest.raises(SchemaError, match="avg"):
            parse_proposal_v1(raw)

    def test_wrong_schema_version_rejected(self):
        raw = raw_ratio_proposal(formula_schema_version=2)
        with pytest.raises(SchemaError):
            parse_proposal_v1(raw)

    def test_empty_logical_ref_rejected(self):
        raw = raw_ratio_proposal(grain={"entity": "customer", "keys": [""]})
        with pytest.raises(SchemaError):
            parse_proposal_v1(raw)

    def test_non_object_root_rejected(self):
        with pytest.raises(SchemaError):
            parse_proposal_v1([raw_ratio_proposal()])  # type: ignore[arg-type]


# ---------------------------------------------------------------- construction

TYPED_WINDOW = WindowPolicy(
    event_time_ref=EVENT_TS,
    basis=s.WindowBasis.TRAILING,
    length=90,
    unit=s.WindowUnit.DAY,
    start_inclusive=Inclusivity.INCLUSIVE,
    end_inclusive=Inclusivity.EXCLUSIVE,
    timezone="UTC",
    empty_window=s.EmptyWindowResult.NULL,
    null_input=s.NullInput.IGNORE,
)


def typed_expr(**over) -> AggregateExpression:
    kw = dict(
        aggregation=AggregateFunction.SUM,
        operand=AMT,
        source_relation=SourceRelation(table_ref=TXN_TABLE),
        filter=None,
        window=TYPED_WINDOW,
    )
    kw.update(over)
    return AggregateExpression(**kw)


class TestConstruction:
    def test_ratio_with_both_slots_accepted(self):
        parsed = parse_proposal_v1(raw_ratio_proposal())
        assert isinstance(parsed, TypedFormulaProposalV1)
        assert isinstance(parsed.body, RatioBody)

    def test_cross_border_value_ratio_90d_round_trip(self):
        parsed = parse_proposal_v1(raw_ratio_proposal())
        expected = TypedFormulaProposalV1(
            formula_schema_version=1,
            operation_grammar_version=1,
            canonicalization_version=1,
            grain=Grain(entity="customer", keys=(CUSTOMER_KEY,)),
            body=RatioBody(
                numerator=typed_expr(
                    filter=FilterPredicate(
                        op=FilterPredicateOp.EQUAL,
                        left=CROSS_BORDER,
                        right_literal=TypedLiteral(
                            type=LiteralType.BOOLEAN, value="true"
                        ),
                    )
                ),
                denominator=typed_expr(),
                zero_denominator=ZeroDenominator.NULL,
            ),
            parameters=(),
            decimal=DecimalPolicy(
                precision=18,
                scale=6,
                rounding=RoundingMode.HALF_EVEN,
                overflow=s.OverflowBehavior.ERROR,
            ),
            expected_output=ExpectedOutput(
                output_type="decimal", unit="ratio", currency=None
            ),
        )
        assert parsed == expected

    def test_full_surface_bool_filter_params_round_trip(self):
        raw = raw_unary_proposal(
            filter={
                "kind": "bool",
                "op": "and",
                "children": [
                    {
                        "kind": "predicate",
                        "op": "greater_or_equal",
                        "left": AMT,
                        "right_param": {"name": "min_amount"},
                    },
                    {
                        "kind": "predicate",
                        "op": "in",
                        "left": CHANNEL,
                        "right_set": [
                            {"type": "string", "value": "pos"},
                            {"type": "string", "value": "atm"},
                        ],
                    },
                ],
            }
        )
        raw["parameters"] = [
            {
                "name": "min_amount",
                "type": "decimal",
                "param_class": "semantic",
                "classification": "internal",
                "nullable": False,
                "allowed_min": "0",
            }
        ]
        raw["expected_output"] = None
        parsed = parse_proposal_v1(raw)
        expected = TypedFormulaProposalV1(
            formula_schema_version=1,
            operation_grammar_version=1,
            canonicalization_version=1,
            grain=Grain(entity="customer", keys=(CUSTOMER_KEY,)),
            body=UnaryBody(
                expr=typed_expr(
                    filter=FilterBool(
                        op=FilterBoolOp.AND,
                        children=(
                            FilterPredicate(
                                op=FilterPredicateOp.GREATER_OR_EQUAL,
                                left=AMT,
                                right_param=ParameterRef(name="min_amount"),
                            ),
                            FilterPredicate(
                                op=FilterPredicateOp.IN,
                                left=CHANNEL,
                                right_set=(
                                    TypedLiteral(
                                        type=LiteralType.STRING, value="pos"
                                    ),
                                    TypedLiteral(
                                        type=LiteralType.STRING, value="atm"
                                    ),
                                ),
                            ),
                        ),
                    )
                )
            ),
            parameters=(
                ParameterDecl(
                    name="min_amount",
                    type=LiteralType.DECIMAL,
                    param_class=ParamClass.SEMANTIC,
                    classification="internal",
                    nullable=False,
                    allowed_set=None,
                    allowed_min="0",
                    allowed_max=None,
                ),
            ),
            decimal=DecimalPolicy(
                precision=18,
                scale=6,
                rounding=RoundingMode.HALF_EVEN,
                overflow=s.OverflowBehavior.ERROR,
            ),
            expected_output=None,
        )
        assert parsed == expected

    def test_parse_does_not_mutate_raw_input(self):
        raw = raw_ratio_proposal()
        import copy

        snapshot = copy.deepcopy(raw)
        parse_proposal_v1(raw)
        assert raw == snapshot

    def test_parsed_enums_are_typed_not_raw_strings(self):
        parsed = parse_proposal_v1(raw_ratio_proposal())
        assert isinstance(parsed.body, RatioBody)
        assert isinstance(parsed.body.zero_denominator, ZeroDenominator)
        num = parsed.body.numerator
        assert isinstance(num.aggregation, AggregateFunction)
        assert isinstance(num.window.basis, s.WindowBasis)
        assert isinstance(num.window.start_inclusive, Inclusivity)
        assert isinstance(num.filter, FilterPredicate)
        assert isinstance(num.filter.op, FilterPredicateOp)
        assert isinstance(num.filter.right_literal.type, LiteralType)
        assert isinstance(parsed.grain.keys, tuple)
        assert isinstance(parsed.parameters, tuple)


# ------------------------------------------------- semantic-layer composition


class TestSemanticComposition:
    """Shape-valid dicts must still pass Task-1 validate_semantics."""

    def test_equal_with_both_right_literal_and_right_set_rejected(self):
        raw = raw_unary_proposal(
            filter={
                "kind": "predicate",
                "op": "equal",
                "left": CHANNEL,
                "right_literal": {"type": "string", "value": "pos"},
                "right_set": [{"type": "string", "value": "pos"}],
            }
        )
        with pytest.raises(SchemaError, match="right_set"):
            parse_proposal_v1(raw)

    def test_count_rows_with_operand_rejected_by_semantics(self):
        raw = raw_unary_proposal(aggregation="count_rows", operand=AMT)
        with pytest.raises(SchemaError, match="takes no operand"):
            parse_proposal_v1(raw)

    def test_cross_table_operand_rejected_by_semantics(self):
        raw = raw_unary_proposal(operand="core::bank.customers.balance")
        with pytest.raises(SchemaError, match="not contained"):
            parse_proposal_v1(raw)
