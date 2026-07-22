"""Provider-schema projection for Anthropic structured outputs.

The canonical enrichment schemas are strict JSON Schema, built for local `jsonschema` validation and
persistence. Anthropic's structured-output API accepts only a SUBSET of JSON Schema, so we project a
provider-compatible schema for the WIRE ONLY (this module) while the canonical schema remains the
source of truth for validating the model's RESPONSE (the driver's `reg.validate`, unchanged).

Two transforms: (1) strip provider-unsupported constraint keywords; (2) normalize a nullable-enum
`{"type":["T","null"],"enum":[...,null]}` into the accepted union `{"anyOf":[{"type":"T",
"enum":[...]},{"type":"null"}]}`. Pure + deterministic + SDK-independent so a static test can prove
every outbound schema is clean before any deploy."""
from __future__ import annotations

import copy
from collections.abc import Iterable

# Constraint keywords Anthropic's json_schema output format rejects. Length/array-size/numeric bounds.
PROVIDER_UNSUPPORTED_KEYWORDS = frozenset({
    "maxLength", "maxItems", "minItems", "minimum", "maximum",
    "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
})

# dict-of-schemas containers — every VALUE is a sub-schema (patternProperties too: regex → schema).
_NESTED_SCHEMA_KEYS = ("properties", "$defs", "definitions", "patternProperties")
# list-of-schemas containers — the combinators plus prefixItems (positional tuple validation).
_COMBINATOR_KEYS = ("anyOf", "oneOf", "allOf")
_LIST_OF_SCHEMA_KEYS = ("prefixItems",)
# Applicators whose value is a SINGLE sub-schema (dict). `additionalProperties` may instead be a bool
# (no sub-schema) — recursion is skipped for that form by the isinstance(dict) guard at each site.
_SINGLE_SUBSCHEMA_KEYS = ("additionalProperties", "not", "if", "then", "else")
# Keys that make a node a well-formed provider schema node. A node declaring none of these has no
# type/union the API can dispatch on (e.g. an empty node left after stripping) → incompatible.
_SCHEMA_SHAPE_KEYS = ("type", "anyOf", "oneOf", "allOf", "$ref", "enum", "const", "not")


def project_for_anthropic(schema: dict) -> dict:
    """Return a deep-copied, Anthropic-compatible projection of `schema`."""
    return _project(copy.deepcopy(schema))


def _project(node: object) -> object:
    if not isinstance(node, dict):
        if isinstance(node, list):
            return [_project(x) for x in node]
        return node
    # 1) nullable-enum → anyOf union (before stripping, so we don't touch enum on plain strings)
    node = _normalize_nullable_enum(node)
    # 2) drop unsupported constraint keywords at this level
    for kw in list(node):
        if kw in PROVIDER_UNSUPPORTED_KEYWORDS:
            del node[kw]
    # 3) Anthropic structured output requires CLOSED objects: an object with `additionalProperties`
    #    true (or the open default, absent) is rejected — it must be false. Force it closed on the
    #    wire; a typed sub-schema (dict) form is LEFT for the recursion below (a legitimate map the
    #    model may return). The canonical schema keeps its permissive shape for RESPONSE validation.
    if (node.get("type") == "object" or "properties" in node) \
            and node.get("additionalProperties", True) is True:
        node["additionalProperties"] = False
    # 4) recurse into nested schema containers
    for key in _NESTED_SCHEMA_KEYS:                        # dict-of-schemas
        if isinstance(node.get(key), dict):
            node[key] = {k: _project(v) for k, v in node[key].items()}
    if isinstance(node.get("items"), (dict, list)):
        node["items"] = _project(node["items"])
    for key in _COMBINATOR_KEYS + _LIST_OF_SCHEMA_KEYS:    # list-of-schemas
        if isinstance(node.get(key), list):
            node[key] = [_project(v) for v in node[key]]
    for key in _SINGLE_SUBSCHEMA_KEYS:                     # single sub-schema (bool add'lProps skipped)
        if isinstance(node.get(key), dict):
            node[key] = _project(node[key])
    return node


def _normalize_nullable_enum(node: dict) -> dict:
    t, enum = node.get("type"), node.get("enum")
    if not (isinstance(t, list) and "null" in t and isinstance(enum, list)):
        return node
    non_null_types = [x for x in t if x != "null"]
    members = [m for m in enum if m is not None]
    variants: list[dict] = []
    for st in non_null_types:
        variants.append({"type": st, "enum": members})
    variants.append({"type": "null"})
    rebuilt = {k: v for k, v in node.items() if k not in ("type", "enum")}
    rebuilt["anyOf"] = variants
    return rebuilt


def provider_incompatibilities(schema: object, _path: str = "$") -> list[str]:
    """List `"<keyword> at <path>"` for every provider-incompatibility in `schema` ([] = clean)."""
    problems: list[str] = []
    if isinstance(schema, list):
        for i, x in enumerate(schema):
            problems += provider_incompatibilities(x, f"{_path}[{i}]")
        return problems
    if not isinstance(schema, dict):
        return problems
    if not any(k in schema for k in _SCHEMA_SHAPE_KEYS):
        problems.append(f"missing-type at {_path}")
    for kw in schema:
        if kw in PROVIDER_UNSUPPORTED_KEYWORDS:
            problems.append(f"{kw} at {_path}")
    t, enum = schema.get("type"), schema.get("enum")
    if isinstance(t, list) and "null" in t and isinstance(enum, list):
        problems.append(f"nullable-enum at {_path}")
    # Anthropic rejects an OPEN object (`additionalProperties: true`, or the open default of absence).
    # This is the guard's blind spot that let permissive feature schemas reach the wire. A typed
    # sub-schema (dict) form is left to the recursion below, not flagged here.
    if (schema.get("type") == "object" or "properties" in schema) \
            and schema.get("additionalProperties", True) is True:
        problems.append(f"open-object at {_path}")
    for key in _NESTED_SCHEMA_KEYS:                        # dict-of-schemas
        if isinstance(schema.get(key), dict):
            for k, v in schema[key].items():
                problems += provider_incompatibilities(v, f"{_path}.{key}.{k}")
    if "items" in schema:
        problems += provider_incompatibilities(schema["items"], f"{_path}.items")
    for key in _COMBINATOR_KEYS + _LIST_OF_SCHEMA_KEYS:    # list-of-schemas
        if isinstance(schema.get(key), list):
            for i, v in enumerate(schema[key]):
                problems += provider_incompatibilities(v, f"{_path}.{key}[{i}]")
    for key in _SINGLE_SUBSCHEMA_KEYS:                     # single sub-schema (bool add'lProps skipped)
        if isinstance(schema.get(key), dict):
            problems += provider_incompatibilities(schema[key], f"{_path}.{key}")
    return problems


def assert_schemas_provider_compatible(schemas: Iterable[tuple[str, dict]]) -> None:
    """Raise ValueError if any already-projected schema is still provider-incompatible."""
    for name, schema in schemas:
        problems = provider_incompatibilities(schema)
        if problems:
            raise ValueError(f"schema {name!r} is not Anthropic-compatible after projection: "
                             f"{', '.join(problems)}")
