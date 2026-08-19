"""C-A2 — the Formula-**v3** parser: v2's exact order, against the v3 schema and vocabulary.

Shape (JSON Schema) → construction → semantics, in that order, exactly as v1 and v2 do it. Every
builder that touches a type v3 REUSES is imported from :mod:`parse_v2` rather than copied: a second
window or authority-refs builder would be a second chance to disagree about a shape both versions
share. Only the expression, the bodies and the proposal are rebuilt, because only those differ.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from functools import lru_cache
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from featuregen.formula.parse import (
    _build_expected_output,
    _build_filter,
    _build_parameter,
    _plain,
)
from featuregen.formula.parse_v2 import _build_authority_refs, _build_window_v2
from featuregen.formula.schema_leaves import (
    DecimalPolicy,
    Grain,
    OverflowBehavior,
    RoundingMode,
    SchemaError,
    SourceRelation,
    ZeroDenominator,
)
from featuregen.formula.schema_v2 import AggregateFunctionV2, FinalOperationV2
from featuregen.formula.schema_v3 import (
    AggregateExpressionV3,
    CompositeBodyV3,
    DiffBodyV3,
    FormulaBodyV3,
    RatioBodyV3,
    SelectionKind,
    SemanticRowSelectionV1,
    SignedTermV3,
    TypedFormulaProposalV3,
    UnaryBodyV3,
    validate_semantics_v3,
)

__all__ = ["parse_proposal_v3"]


@lru_cache(maxsize=1)
def _validator_v3() -> Draft202012Validator:
    raw = resources.files("featuregen.formula").joinpath("proposal_v3.schema.json").read_text()
    return Draft202012Validator(json.loads(raw))


def _build_selection(data: Mapping[str, Any]) -> SemanticRowSelectionV1:
    return SemanticRowSelectionV1(
        kind=SelectionKind(data["kind"]),
        role=data["role"],
        semantic_value=data["semantic_value"],
    )


def _build_expression_v3(data: dict[str, Any]) -> AggregateExpressionV3:
    filter_data = data.get("filter")
    return AggregateExpressionV3(
        aggregation=AggregateFunctionV2(data["aggregation"]),
        operand=data.get("operand"),
        source_relation=SourceRelation(table_ref=data["source_relation"]["table_ref"]),
        filter=_build_filter(filter_data) if filter_data is not None else None,
        window=_build_window_v2(data["window"]),
        aggregation_argument=data.get("aggregation_argument"),
        second_operand=data.get("second_operand"),
        authority_refs=_build_authority_refs(data.get("authority_refs")),
        row_selections=tuple(_build_selection(s) for s in data.get("row_selections", ())),
    )


def _build_body_v3(data: dict[str, Any]) -> FormulaBodyV3:
    final_operation = FinalOperationV2(data["final_operation"])
    if final_operation is FinalOperationV2.SIGNED_SUM:
        return CompositeBodyV3(terms=tuple(
            SignedTermV3(name=term["name"], sign=term["sign"],
                         expr=_build_expression_v3(term["expr"]))
            for term in data["terms"]))
    if final_operation is FinalOperationV2.IDENTITY:
        return UnaryBodyV3(expr=_build_expression_v3(data["expr"]))
    if final_operation is FinalOperationV2.RATIO:
        return RatioBodyV3(
            numerator=_build_expression_v3(data["numerator"]),
            denominator=_build_expression_v3(data["denominator"]),
            zero_denominator=ZeroDenominator(data["zero_denominator"]).value,
        )
    return DiffBodyV3(
        minuend=_build_expression_v3(data["minuend"]),
        subtrahend=_build_expression_v3(data["subtrahend"]),
    )


def parse_proposal_v3(raw: Mapping[str, Any]) -> TypedFormulaProposalV3:
    """Parse an untrusted raw dict into a validated ``TypedFormulaProposalV3``."""
    try:
        data = _plain(raw)
        error = best_match(_validator_v3().iter_errors(data))
        if error is not None:
            raise SchemaError(
                f"v3 proposal shape invalid at {error.json_path}: {error.message}")
        proposal = TypedFormulaProposalV3(
            formula_schema_version=data["formula_schema_version"],
            operation_grammar_version=data["operation_grammar_version"],
            canonicalization_version=data["canonicalization_version"],
            grain=Grain(entity=data["grain"]["entity"], keys=tuple(data["grain"]["keys"])),
            body=_build_body_v3(data["body"]),
            parameters=tuple(_build_parameter(p) for p in data["parameters"]),
            decimal=DecimalPolicy(
                precision=data["decimal"]["precision"],
                scale=data["decimal"]["scale"],
                rounding=RoundingMode(data["decimal"]["rounding"]),
                overflow=OverflowBehavior(data["decimal"]["overflow"])),
            expected_output=_build_expected_output(data.get("expected_output")),
            allocation_policy_ref=data.get("allocation_policy_ref", ""),
        )
        validate_semantics_v3(proposal)
    except SchemaError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise SchemaError(f"v3 proposal could not be parsed: {exc}") from exc
    return proposal
