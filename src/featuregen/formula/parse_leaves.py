"""The parsing helpers every generation reuses — shared, and version-neutral.

`parse_v2` and `parse_v3` already import all four of these from `parse.py` UNCHANGED, which is the
standing evidence that they are not V1's: a v2 parser reusing a v1 filter builder verbatim means the
filter builder was never about v1. They return only shared structural leaves — `FilterNode`,
`TypedLiteral`, `ParameterDecl` — and construct nothing from the V1 language.

**`_build_literal` moves WITH `_build_filter`, not after it.** `_build_filter` calls it directly, so
splitting them across modules would not compile. An analysis pass described it inside `_build_filter`'s
prose rather than naming it, which is exactly how a dependency gets dropped by whoever executes the
list; an adversarial re-check promoted it to its own line.

**What stayed behind is V1 by TYPE**, not by name: `_build_expected_output` returns `ExpectedOutput`,
and `parse_proposal_v1` drags `_build_body`/`_build_expression`/`_build_window` and the v1 wire
schema with it. `_build_expected_output` in particular is a live v2 AND v3 dependency whose result
is walked into `proposal_content_hash_v2`, so relocating or reshaping it re-identifies sealed
artifacts — a governance decision, not an extraction.

**Moved verbatim.** `_plain` produces the object every `_build_*` reads, so its output becomes the
dataclass field values that the v2/v3 canonicalizers hash. It constructs nothing, but it is not off
the hash path.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from featuregen.formula.schema_leaves import (
    FilterBool,
    FilterBoolOp,
    FilterKind,
    FilterNode,
    FilterPredicate,
    FilterPredicateOp,
    LiteralType,
    ParamClass,
    ParameterDecl,
    ParameterRef,
    TypedLiteral,
)

__all__ = ["_build_filter", "_build_parameter", "_plain"]


def _plain(value: Any) -> Any:
    """Recursively convert Mappings/sequences to plain dict/list for jsonschema."""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


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
