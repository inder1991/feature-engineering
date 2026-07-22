"""Strict dict→typed boundary for TypedFormula proposals (Child-1 Task 2).

``parse_proposal_v1`` is the ONLY place a ``TypedFormulaProposalV1`` is
constructed from untrusted (LLM) input. Layer order is normative:

1. JSON-Schema shape gate (``proposal_v1.schema.json``, Draft 2020-12,
   ``additionalProperties: false`` on every object, discriminated ``oneOf``
   on ``body.final_operation`` and ``filter.kind``);
2. frozen-dataclass construction (recursive, tuples for arrays);
3. Task-1 ``validate_semantics``.

Every failure raises ``SchemaError``. OFFLINE authoring only — no execution.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from featuregen.formula.schema import (
    AggregateExpression,
    AggregateFunction,
    DecimalPolicy,
    DiffBody,
    EmptyWindowResult,
    ExpectedOutput,
    FilterBool,
    FilterBoolOp,
    FilterKind,
    FilterNode,
    FilterPredicate,
    FilterPredicateOp,
    FinalOperation,
    FormulaBody,
    Grain,
    Inclusivity,
    LiteralType,
    NullInput,
    ParamClass,
    ParameterDecl,
    ParameterRef,
    RatioBody,
    RoundingMode,
    OverflowBehavior,
    SchemaError,
    SourceRelation,
    TypedFormulaProposalV1,
    TypedLiteral,
    UnaryBody,
    WindowBasis,
    WindowPolicy,
    WindowUnit,
    ZeroDenominator,
)

_SCHEMA_PATH = Path(__file__).with_name("proposal_v1.schema.json")


@cache
def _validator() -> Draft202012Validator:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _plain(value: Any) -> Any:
    """Recursively convert Mappings/sequences to plain dict/list for jsonschema."""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def parse_proposal_v1(raw: Mapping[str, Any]) -> TypedFormulaProposalV1:
    """Parse an untrusted raw dict into a validated TypedFormulaProposalV1.

    Order matters: JSON-Schema shape FIRST, then dataclass construction,
    then semantic validation. Raises SchemaError on any violation.
    """
    data = _plain(raw)
    error = best_match(_validator().iter_errors(data))
    if error is not None:
        raise SchemaError(
            f"proposal shape invalid at {error.json_path}: {error.message}"
        )
    return _build_proposal(data)


# ---- construction (shape-validated dict -> frozen dataclasses) ----


def _build_literal(data: dict[str, Any]) -> TypedLiteral:
    return TypedLiteral(type=LiteralType(data["type"]), value=data["value"])


def _build_filter(data: dict[str, Any]) -> FilterNode:
    if data["kind"] == FilterKind.BOOL:
        return FilterBool(
            op=FilterBoolOp(data["op"]),
            children=tuple(_build_filter(child) for child in data["children"]),
        )
    right_param = data.get("right_param")
    right_set = data.get("right_set")
    return FilterPredicate(
        op=FilterPredicateOp(data["op"]),
        left=data["left"],
        right_literal=(
            _build_literal(data["right_literal"])
            if data.get("right_literal") is not None
            else None
        ),
        right_param=(
            ParameterRef(name=right_param["name"]) if right_param is not None else None
        ),
        right_set=(
            tuple(_build_literal(entry) for entry in right_set)
            if right_set is not None
            else None
        ),
    )


def _build_window(data: dict[str, Any]) -> WindowPolicy:
    return WindowPolicy(
        event_time_ref=data["event_time_ref"],
        basis=WindowBasis(data["basis"]),
        length=data["length"],
        unit=WindowUnit(data["unit"]),
        start_inclusive=Inclusivity(data["start_inclusive"]),
        end_inclusive=Inclusivity(data["end_inclusive"]),
        timezone=data["timezone"],
        empty_window=EmptyWindowResult(data["empty_window"]),
        null_input=NullInput(data["null_input"]),
    )


def _build_expression(data: dict[str, Any]) -> AggregateExpression:
    filter_data = data.get("filter")
    return AggregateExpression(
        aggregation=AggregateFunction(data["aggregation"]),
        operand=data.get("operand"),
        source_relation=SourceRelation(table_ref=data["source_relation"]["table_ref"]),
        filter=_build_filter(filter_data) if filter_data is not None else None,
        window=_build_window(data["window"]),
    )


def _build_body(data: dict[str, Any]) -> FormulaBody:
    final_operation = FinalOperation(data["final_operation"])
    if final_operation is FinalOperation.IDENTITY:
        return UnaryBody(expr=_build_expression(data["expr"]))
    if final_operation is FinalOperation.RATIO:
        return RatioBody(
            numerator=_build_expression(data["numerator"]),
            denominator=_build_expression(data["denominator"]),
            zero_denominator=ZeroDenominator(data["zero_denominator"]),
        )
    return DiffBody(
        minuend=_build_expression(data["minuend"]),
        subtrahend=_build_expression(data["subtrahend"]),
    )


def _build_parameter(data: dict[str, Any]) -> ParameterDecl:
    allowed_set = data.get("allowed_set")
    return ParameterDecl(
        name=data["name"],
        type=LiteralType(data["type"]),
        param_class=ParamClass(data["param_class"]),
        classification=data["classification"],
        nullable=data["nullable"],
        allowed_set=tuple(allowed_set) if allowed_set is not None else None,
        allowed_min=data.get("allowed_min"),
        allowed_max=data.get("allowed_max"),
    )


def _build_expected_output(data: dict[str, Any] | None) -> ExpectedOutput | None:
    if data is None:
        return None
    return ExpectedOutput(
        output_type=data.get("output_type"),
        unit=data.get("unit"),
        currency=data.get("currency"),
    )


def _build_proposal(data: dict[str, Any]) -> TypedFormulaProposalV1:
    return TypedFormulaProposalV1(
        formula_schema_version=data["formula_schema_version"],
        operation_grammar_version=data["operation_grammar_version"],
        canonicalization_version=data["canonicalization_version"],
        grain=Grain(
            entity=data["grain"]["entity"], keys=tuple(data["grain"]["keys"])
        ),
        body=_build_body(data["body"]),
        parameters=tuple(_build_parameter(p) for p in data["parameters"]),
        decimal=DecimalPolicy(
            precision=data["decimal"]["precision"],
            scale=data["decimal"]["scale"],
            rounding=RoundingMode(data["decimal"]["rounding"]),
            overflow=OverflowBehavior(data["decimal"]["overflow"]),
        ),
        expected_output=_build_expected_output(data.get("expected_output")),
    )
