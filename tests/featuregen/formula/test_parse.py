"""Tests for the strict dict→typed proposal parser (Child-1 Task 2).

parse_proposal_v1 is the ONLY place a TypedFormulaProposalV1 is constructed
from untrusted (LLM) input. Layer order under test: JSON-Schema shape gate
FIRST, then dataclass construction, then validate_semantics.
"""
from __future__ import annotations

import pytest

from featuregen.formula.parse import parse_proposal_v1
from featuregen.formula.schema import SchemaError

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
