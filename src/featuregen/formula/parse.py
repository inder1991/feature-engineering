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

from featuregen.formula.parse_leaves import (
    _build_expected_output,
    _build_filter,
    _build_parameter,
    _plain,
)
from featuregen.formula.schema import (
    AggregateExpression,
    AggregateFunction,
    DiffBody,
    FinalOperation,
    FormulaBody,
    RatioBody,
    TypedFormulaProposalV1,
    UnaryBody,
    WindowBasis,
    WindowPolicy,
    validate_semantics,
)
from featuregen.formula.schema_leaves import (
    DecimalPolicy,
    EmptyWindowResult,
    Grain,
    Inclusivity,
    NullInput,
    OverflowBehavior,
    RoundingMode,
    SchemaError,
    SourceRelation,
    WindowUnit,
    ZeroDenominator,
)

_SCHEMA_PATH = Path(__file__).with_name("proposal_v1.schema.json")


@cache
def _validator() -> Draft202012Validator:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)




def parse_proposal_v1(raw: Mapping[str, Any]) -> TypedFormulaProposalV1:
    """Parse an untrusted raw dict into a validated TypedFormulaProposalV1.

    Order matters: JSON-Schema shape FIRST, then dataclass construction,
    then semantic validation. Raises SchemaError on any violation.
    """
    try:
        data = _plain(raw)
        error = best_match(_validator().iter_errors(data))
        if error is not None:
            raise SchemaError(
                f"proposal shape invalid at {error.json_path}: {error.message}"
            )
        proposal = _build_proposal(data)
        validate_semantics(proposal)
    except RecursionError:
        # Untrusted input must not escape the boundary as a stack blowout:
        # nesting far beyond MAX_FILTER_DEPTH fails closed like any other
        # shape violation.
        raise SchemaError("proposal nesting too deep to validate") from None
    return proposal


# ---- construction (shape-validated dict -> frozen dataclasses) ----






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
