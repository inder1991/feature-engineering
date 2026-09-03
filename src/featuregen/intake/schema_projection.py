"""Provider-schema projection for Anthropic structured outputs.

The canonical enrichment schemas are strict JSON Schema, built for local `jsonschema` validation and
persistence. Anthropic's structured-output API accepts only a SUBSET of JSON Schema, so we project a
provider-compatible schema for the WIRE ONLY (this module) while the canonical schema remains the
source of truth for validating the model's RESPONSE (the driver's `reg.validate`, unchanged).

Three transforms carry the weight (each is documented at its step in `_project`, along with the
several wire-only strictness bargains that ride with them): (1) strip provider-unsupported
constraint keywords; (2) normalize a nullable-enum `{"type":["T","null"],"enum":[...,null]}` into
the accepted union `{"anyOf":[{"type":"T","enum":[...]},{"type":"null"}]}`; (3) declare the type a
bare enum already implies, because the provider requires every subschema to say what it is. Pure +
deterministic + SDK-independent so a static test can prove every outbound schema is clean before any
deploy."""
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
    # 1b) TYPE ARRAYS → anyOf. Defensive normalization (2026-08-14): a billing 400 was
    #     briefly misread as the provider rejecting list-valued "type" (the error envelope's
    #     own 'type' field tripped the keyword scan — see llm_claude._rejected_schema_keyword's
    #     guard). No rejection of type arrays was actually observed — but the anyOf form is
    #     semantically identical, universally accepted, and narrows the provider-grammar
    #     surface we depend on, so the normalization stays. Constraints ride the non-null
    #     variants; "null" gets its bare arm. Canonical schemas stay strict JSON Schema.
    node = _normalize_type_array(node)
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
    # 3b) wire-only strictness: the CANONICAL schema stays permissive so RESPONSE validation is lenient
    #     (a single incomplete item must not fail the whole response — the deterministic gauntlet filters
    #     per-item), but the WIRE must force the model to emit load-bearing keys (e.g. feature_ideas'
    #     `derives_from`, without which every idea is UNGROUNDED). `x-wire-required` carries that intent
    #     on the canonical (an unknown keyword the response validator ignores) and becomes `required`
    #     (a supported keyword) here on the wire only.
    if "x-wire-required" in node:
        node["required"] = node.pop("x-wire-required")
    # 3c) the same wire-only bargain for a CLOSED VOCABULARY. `enum` is provider-SUPPORTED, so a
    #     canonical `enum` would constrain the model AND be validated against the response — and on
    #     a permissive body (feature_ideas' grounding `role`) one off-vocabulary answer would then
    #     fail the WHOLE call for a value nothing branches on. `x-wire-enum` puts the vocabulary
    #     where it earns its keep — guiding generation — and leaves the response lenient, with the
    #     closure enforced per item in code. Use it ONLY where the code owns the closure; a value
    #     the code trusts belongs in a canonical `enum` so an off-vocabulary answer cannot be read.
    if "x-wire-enum" in node:
        node["enum"] = node.pop("x-wire-enum")
    # 3d) BARE ENUM → TYPED ENUM (2026-08-24). `{"enum": ["string", "integer", ...]}` with no
    #     `"type"` beside it is legal JSON Schema — the members constrain the value on their own,
    #     so `jsonschema` never minded — and the provider's structured-output subset REFUSES it:
    #     every subschema must declare a type. Unlike the type-array note above, this rejection was
    #     observed directly, and it had been fatal for the whole life of the formula-author seam:
    #     every live `formula.author` call died with `HTTP 400, keyword=type` against
    #     `$defs.typedLiteral.properties.type` and `$defs.parameterDecl.properties.type`, and the
    #     error read as a keyword misattribution only because the offending property is itself
    #     NAMED "type". The declaration is purely additive — a homogeneous enum's members already
    #     say what the type is, so the set of accepted values is identical before and after.
    #
    #     ▲ IT RUNS HERE, LAST, AND THAT POSITION IS THE POINT. An untyped enum reaches the wire
    #     through TWO doors: written that way in the canonical schema (the author turn schemas'
    #     46 of them), or minted right above by the `x-wire-enum` swap, which lifts a vocabulary
    #     onto a node that may carry no type of its own. Declaring at 1c — where this first sat —
    #     closed only the first door and left the second wide open to the same outage. One line
    #     between here and there DOES read `type` (step 3 compares it to "object"), but this
    #     transform can only emit boolean/integer/null/number/string — never "object" — so the
    #     read cannot move; running once, downstream of both producers, is sufficient and the
    #     only placement that stays correct (equivalence executed over all 71 provider-bound
    #     schemas: zero differ between the two positions except the second-door case itself).
    node = _declare_enum_type(node)
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


def _type_of_enum_members(members: list) -> str | None:
    """The JSON-Schema type a HOMOGENEOUS enum implies, or None when nothing can be inferred.

    The rule is: a MIXED enum returns None; a homogeneous one is named by its members' JSON type.
    Order matters twice over, and both are traps rather than taste:

    * `bool` is tested BEFORE `int` because `isinstance(True, int)` is True in Python — an
      all-boolean enum would otherwise be declared "integer", a type its members do not have.
    * `integer` is tested BEFORE `number` because an all-integer enum deserves the tighter of the
      two true declarations; `number` is for the mixed-numeric case (any float present), where
      "integer" would be a false statement about the members.

    None means "no single type to declare". Inventing one would NARROW the contract the model is
    held to, so these are left untouched and the schema audit reports them: widening the vocabulary
    or splitting the property is a human decision, not a normalization.
    """
    if not members:
        return None
    if all(isinstance(m, bool) for m in members):
        return "boolean"
    if all(m is None for m in members):
        return "null"
    if all(isinstance(m, int) and not isinstance(m, bool) for m in members):
        return "integer"
    if all(isinstance(m, (int, float)) and not isinstance(m, bool) for m in members):
        return "number"
    if all(isinstance(m, str) for m in members):
        return "string"
    return None


def _declare_enum_type(node: dict) -> dict:
    """Give an enum-only node the type its members imply. Already-dispatchable nodes are untouched."""
    enum = node.get("enum")
    if not isinstance(enum, list):
        return node
    if any(k in node for k in ("type", "$ref") + _COMBINATOR_KEYS):
        return node
    declared = _type_of_enum_members(enum)
    if declared is None:
        return node
    return {"type": declared, **node}


def declare_enum_types(schema: dict) -> dict:
    """A deep copy of `schema` in which every enum-only subschema declares the type it implies.

    The same transform `project_for_anthropic` applies on the wire, offered on its own so a schema
    can be built ALREADY honest rather than repaired on the way out — see `formula/turns_v3.py`.
    Provider-independent: an enum that does not say what it is is under-specified for any reader.
    """
    return _walk_declaring_enum_types(copy.deepcopy(schema))


def _walk_declaring_enum_types(node: object) -> object:
    if isinstance(node, list):
        return [_walk_declaring_enum_types(x) for x in node]
    if not isinstance(node, dict):
        return node
    node = _declare_enum_type(node)
    for key in _NESTED_SCHEMA_KEYS:                        # dict-of-schemas
        if isinstance(node.get(key), dict):
            node[key] = {k: _walk_declaring_enum_types(v) for k, v in node[key].items()}
    if isinstance(node.get("items"), (dict, list)):
        node["items"] = _walk_declaring_enum_types(node["items"])
    for key in _COMBINATOR_KEYS + _LIST_OF_SCHEMA_KEYS:    # list-of-schemas
        if isinstance(node.get(key), list):
            node[key] = [_walk_declaring_enum_types(v) for v in node[key]]
    for key in _SINGLE_SUBSCHEMA_KEYS:                     # single sub-schema (bool form skipped)
        if isinstance(node.get(key), dict):
            node[key] = _walk_declaring_enum_types(node[key])
    return node


def _normalize_type_array(node: dict) -> dict:
    t = node.get("type")
    if not isinstance(t, list):
        return node
    keep = {k: v for k, v in node.items() if k != "type"}
    variants: list[dict] = []
    for st in t:
        if st == "null":
            variants.append({"type": "null"})
        else:
            variants.append({"type": st, **{k: v for k, v in keep.items()
                                            if k not in ("anyOf", "oneOf", "allOf")}})
    carried = {k: v for k, v in keep.items() if k in ("anyOf", "oneOf", "allOf")}
    return {**carried, "anyOf": variants}


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
    # An UNTYPED ENUM (2026-08-24). TWO blind spots let this reach production, and they are worth
    # keeping straight because only the second one lives in this function:
    #   1. COVERAGE, and it was the decisive one. Every auditor of this invariant — the 2026-08-14
    #      ratchet and `enrich_llm.register_enrichment_schemas`' fail-closed bootstrap guard alike
    #      — iterates `enrich_llm._SCHEMAS`, which carries no `formula_author_turn*` id at all.
    #      The author turn schemas were never examined by anything, so no detector could have
    #      spoken. That is what `test_provider_schema_audit.py` exists to fix.
    #   2. DETECTION, a real second defect. Had the sweep reached them, this function would still
    #      have passed them: it counted `enum` among the keys that make a node dispatchable — see
    #      `_SCHEMA_SHAPE_KEYS` — so a bare enum read as well-formed. Fixed here.
    # `enum` stays in `_SCHEMA_SHAPE_KEYS` (a bare enum is not a SHAPELESS node, and reporting it
    # twice would say so); it is reported here under its own name instead. A survivor after
    # `project_for_anthropic` is a heterogeneous enum the projection declined to guess at.
    if "enum" in schema and not any(k in schema for k in ("type", "$ref") + _COMBINATOR_KEYS):
        problems.append(f"untyped-enum at {_path}")
    for kw in schema:
        if kw in PROVIDER_UNSUPPORTED_KEYWORDS:
            problems.append(f"{kw} at {_path}")
    t = schema.get("type")
    if isinstance(t, list):
        # Defensive (2026-08-14): type arrays are normalized to anyOf on the wire, so one
        # surviving projection is a projection bug — flag them all.
        problems.append(f"type-array at {_path}")
    # Anthropic rejects an OPEN object (`additionalProperties: true`, or the open default of absence).
    # This is the guard's blind spot that let permissive feature schemas reach the wire.
    if (schema.get("type") == "object" or "properties" in schema) \
            and schema.get("additionalProperties", True) is True:
        problems.append(f"open-object at {_path}")
    # A DICT-VALUED `additionalProperties` — the JSON Schema way to say "a map of arbitrary keys to
    # this shape". Anthropic rejects it outright (HTTP 400, keyword=type), and this guard used to
    # wave it through with a comment saying the recursion below would handle it: the recursion
    # checks the sub-schema is well-formed, which it was, and never asks whether the CONSTRUCT is
    # supported at all. A live call proved otherwise. Express a map as an array of {key, value}
    # pairs instead — closed objects all the way down, which is what the subset requires.
    if isinstance(schema.get("additionalProperties"), dict):
        problems.append(f"map-object at {_path}")
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
