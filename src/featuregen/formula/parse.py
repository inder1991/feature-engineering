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
    SchemaError,
    TypedFormulaProposalV1,
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


def _build_proposal(data: dict[str, Any]) -> TypedFormulaProposalV1:
    raise NotImplementedError  # slice B: dataclass construction
