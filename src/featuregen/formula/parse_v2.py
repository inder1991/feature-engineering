"""BR-6 increment 1 — the explicit v2 parser and the version dispatch.

The dispatch rule, verbatim from the plan: **never infer a version from body shape**. A raw
proposal states its ``formula_schema_version``; ``parse_versioned`` reads THAT FIELD and nothing
else — 1 goes to the frozen v1 parser, 2 comes here, anything else (or its absence) is a
``SchemaError``. A v2-shaped body claiming version 1, or vice versa, fails in its own parser's
shape gate rather than being quietly re-homed.

The v2 parser mirrors v1's order exactly — JSON-Schema shape first (``proposal_v2.schema.json``),
then frozen-dataclass construction, then :func:`schema_v2.validate_semantics_v2` — and reuses
v1's LEAF builders (filters, windows, parameters: frozen with v1, shared by design).
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
    _build_window,
    _plain,
    parse_proposal_v1,
)
from featuregen.formula.schema import (
    DecimalPolicy,
    OverflowBehavior,
    RoundingMode,
    SchemaError,
    SourceRelation,
    ZeroDenominator,
)
from featuregen.formula.schema_v2 import (
    AggregateExpressionV2,
    AggregateFunctionV2,
    DiffBodyV2,
    FinalOperationV2,
    FormulaBodyV2,
    RatioBodyV2,
    TypedFormulaProposalV2,
    UnaryBodyV2,
    validate_semantics_v2,
)


@lru_cache(maxsize=1)
def _validator_v2() -> Draft202012Validator:
    raw = resources.files("featuregen.formula").joinpath("proposal_v2.schema.json").read_text()
    return Draft202012Validator(json.loads(raw))


def _build_expression_v2(data: dict[str, Any]) -> AggregateExpressionV2:
    filter_data = data.get("filter")
    return AggregateExpressionV2(
        aggregation=AggregateFunctionV2(data["aggregation"]),
        operand=data.get("operand"),
        source_relation=SourceRelation(table_ref=data["source_relation"]["table_ref"]),
        filter=_build_filter(filter_data) if filter_data is not None else None,
        window=_build_window(data["window"]),
    )


def _build_body_v2(data: dict[str, Any]) -> FormulaBodyV2:
    final_operation = FinalOperationV2(data["final_operation"])
    if final_operation is FinalOperationV2.IDENTITY:
        return UnaryBodyV2(expr=_build_expression_v2(data["expr"]))
    if final_operation is FinalOperationV2.RATIO:
        return RatioBodyV2(
            numerator=_build_expression_v2(data["numerator"]),
            denominator=_build_expression_v2(data["denominator"]),
            zero_denominator=ZeroDenominator(data["zero_denominator"]).value,
        )
    return DiffBodyV2(
        minuend=_build_expression_v2(data["minuend"]),
        subtrahend=_build_expression_v2(data["subtrahend"]),
    )


def parse_proposal_v2(raw: Mapping[str, Any]) -> TypedFormulaProposalV2:
    """Parse an untrusted raw dict into a validated ``TypedFormulaProposalV2`` — v1's exact
    order (shape, construction, semantics), against the v2 schema and vocabulary."""
    from featuregen.formula.schema import Grain

    try:
        data = _plain(raw)
        error = best_match(_validator_v2().iter_errors(data))
        if error is not None:
            raise SchemaError(
                f"v2 proposal shape invalid at {error.json_path}: {error.message}")
        proposal = TypedFormulaProposalV2(
            formula_schema_version=data["formula_schema_version"],
            operation_grammar_version=data["operation_grammar_version"],
            canonicalization_version=data["canonicalization_version"],
            grain=Grain(entity=data["grain"]["entity"],
                        keys=tuple(data["grain"]["keys"])),
            body=_build_body_v2(data["body"]),
            parameters=tuple(_build_parameter(p) for p in data["parameters"]),
            decimal=DecimalPolicy(
                precision=data["decimal"]["precision"],
                scale=data["decimal"]["scale"],
                rounding=RoundingMode(data["decimal"]["rounding"]),
                overflow=OverflowBehavior(data["decimal"]["overflow"])),
            expected_output=_build_expected_output(data.get("expected_output")),
        )
        validate_semantics_v2(proposal)
    except RecursionError:
        raise SchemaError("proposal nesting too deep to validate") from None
    return proposal


def parse_versioned(raw: Mapping[str, Any]):
    """THE version dispatch: the declared ``formula_schema_version`` field decides, nothing else.
    Absent or unknown versions are refused loudly — a body shape is never sniffed."""
    version = raw.get("formula_schema_version") if isinstance(raw, Mapping) else None
    if version == 1:
        return parse_proposal_v1(raw)
    if version == 2:
        return parse_proposal_v2(raw)
    raise SchemaError(
        f"formula_schema_version must be declared as 1 or 2; got {version!r} — a version is "
        "stated, never inferred from body shape")
