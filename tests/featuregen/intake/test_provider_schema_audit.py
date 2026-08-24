"""THE PROVIDER-SUBSET AUDIT — every schema this build can put on a provider's wire, walked
post-projection and held to the structured-output subset's invariants.

▲ **WHY THIS EXISTS, AND WHAT IT WOULD HAVE CAUGHT.** Live diagnostic 2026-08-24 (draft
``fd_01M0SZTAJCQDR0KG4JPV16T9ZP``): EVERY ``formula.author`` call this platform has ever made died
with ``anthropic rejected structured-output schema (HTTP 400, keyword=type)``. The cause was a
JSON-Schema idiom that is perfectly legal and perfectly common — a BARE ENUM,
``{"enum": ["string", "integer", ...]}`` with no ``"type"`` beside it. The members constrain the
value on their own, so ``jsonschema`` never minded; the provider's structured-output subset requires
every subschema to declare a type, so the provider refused the whole schema. Because the offending
property was literally NAMED ``type`` (``$defs.typedLiteral.properties.type``), the 400's
``keyword=type`` read as a keyword misattribution rather than as the plain statement of fact it was.

The 2026-08-14 ratchet in ``test_schema_projection.py`` swept the enrichment registry and passed
throughout, because it asked ``provider_incompatibilities`` — which counted ``enum`` as one of the
keys that make a node dispatchable, and so shared the exact blind spot. **That is why the walker
below is written out here rather than imported.** A pin that asks the production checker whether the
production checker is right can only ever confirm it; this one is an independent statement of the
subset's invariants, and it fails whether or not ``schema_projection`` agrees.

The three invariants asserted, and how far each is actually EVIDENCED:
  * **enums are typed** — verified against the live provider (the 400 above).
  * **no list-valued ``type`` survives projection** — the projection normalizes type arrays to
    ``anyOf``; a survivor is a projection bug regardless of whether the provider would take it
    (see ``schema_projection._normalize_type_array``'s own note on the 2026-08-14 misread).
  * **``properties``/``items`` carry a type** — defensive, on the same reasoning as the enum case:
    an undispatchable node is exactly the shape the provider rejected. Nothing in this build
    violates it today, so the assertion costs nothing and closes the rest of the class.
"""
from __future__ import annotations

import pytest

from featuregen.intake.schema_projection import (
    project_for_anthropic,
    provider_incompatibilities,
)

# Container keys, restated (not imported) for the independence the module docstring argues for.
_NESTED_SCHEMA_KEYS = ("properties", "$defs", "definitions", "patternProperties")
_COMBINATOR_KEYS = ("anyOf", "oneOf", "allOf")
_LIST_OF_SCHEMA_KEYS = ("prefixItems",)
_SINGLE_SUBSCHEMA_KEYS = ("additionalProperties", "not", "if", "then", "else")
#: What makes a node DISPATCHABLE for the provider: it declares its type outright, defers to a
#: ``$ref``, or is a union whose alternatives are walked (and typed) in their own right.
_DISPATCHABLE_KEYS = ("type", "$ref", "anyOf", "oneOf", "allOf")


def provider_subset_violations(node: object, path: str = "$") -> list[str]:
    """``["<violation> at <path>", ...]`` for `node` — `[]` means it satisfies the subset."""
    problems: list[str] = []
    if isinstance(node, list):
        for i, item in enumerate(node):
            problems += provider_subset_violations(item, f"{path}[{i}]")
        return problems
    if not isinstance(node, dict):
        return problems
    dispatchable = any(k in node for k in _DISPATCHABLE_KEYS)
    if not dispatchable:
        if "enum" in node:
            problems.append(f"untyped-enum at {path}")
        if "properties" in node:
            problems.append(f"untyped-object at {path}")
        if "items" in node:
            problems.append(f"untyped-array at {path}")
    if isinstance(node.get("type"), list):
        problems.append(f"type-array at {path}")
    for key in _NESTED_SCHEMA_KEYS:                        # dict-of-schemas
        if isinstance(node.get(key), dict):
            for name, sub in node[key].items():
                problems += provider_subset_violations(sub, f"{path}.{key}.{name}")
    if "items" in node:
        problems += provider_subset_violations(node["items"], f"{path}.items")
    for key in _COMBINATOR_KEYS + _LIST_OF_SCHEMA_KEYS:    # list-of-schemas
        if isinstance(node.get(key), list):
            for i, sub in enumerate(node[key]):
                problems += provider_subset_violations(sub, f"{path}.{key}[{i}]")
    for key in _SINGLE_SUBSCHEMA_KEYS:                     # single sub-schema (bool form skipped)
        if isinstance(node.get(key), dict):
            problems += provider_subset_violations(node[key], f"{path}.{key}")
    return problems


def _provider_bound_schemas() -> list[tuple[str, dict]]:
    """Every schema this build can hand a provider as a structured-output format.

    Enumerated from the CLOSED registries wherever one exists — ``enrich_llm._SCHEMAS`` and
    ``AUTHOR_CONTRACT_BY_FORMULA_SCHEMA`` — so a schema added there is swept without anybody
    remembering to add it here. The rest are the one-off ``LLMRequest(output_schema=...)`` sites;
    they are named individually because there is no registry to read them from.

    NOT swept: ``formula.tools``' ``ToolSpec.output_schema``. Those describe what a governed
    catalog tool RETURNS to the orchestrator; nothing hands them to a provider as an output format,
    so holding them to the provider's subset would assert a constraint they do not live under.
    """
    from featuregen.analysis.intent import INTENT_SCHEMA
    from featuregen.formula.author import AUTHOR_CONTRACT_BY_FORMULA_SCHEMA
    from featuregen.formula.critic import CRITIC_SCHEMA
    from featuregen.formula.turns import AUTHOR_TURN_V1_SCHEMA
    from featuregen.overlay.upload.enrich_llm import _SCHEMAS
    from featuregen.overlay.upload.propose_concept_parents import PROPOSAL_SCHEMA
    from featuregen.overlay.upload.semantic_bindings.enrich import _SELECTION_SCHEMA

    assert _SCHEMAS, "the enrichment registry import moved — fix the sweep, never delete it"
    assert AUTHOR_CONTRACT_BY_FORMULA_SCHEMA, "the author contract map moved — fix the sweep"

    schemas: list[tuple[str, dict]] = [
        (f"enrich_llm:{schema_id}@{version}", schema)
        for (schema_id, version), schema in sorted(_SCHEMAS.items())
    ]
    schemas += [
        (f"author_turn:formula_schema_{v}({c.schema_id})", c.schema)
        for v, c in sorted(AUTHOR_CONTRACT_BY_FORMULA_SCHEMA.items())
    ]
    schemas += [
        ("author_turn:formula_schema_1(formula_author_turn)", AUTHOR_TURN_V1_SCHEMA),
        ("formula:critic_findings", CRITIC_SCHEMA),
        ("analysis:intent", INTENT_SCHEMA),
        ("overlay:concept_parent_proposal", PROPOSAL_SCHEMA),
        ("overlay:semantic_bindings_select", _SELECTION_SCHEMA),
    ]
    return schemas


# ── THE SWEEP ─────────────────────────────────────────────────────────────────────────────────────


def test_every_provider_bound_schema_is_a_provider_subset_after_projection():
    """The class-killer. Every schema that can reach a provider, projected, then walked.

    A failure here is a schema the provider will refuse — i.e. a whole capability dead on the
    first live call, which is precisely how the author seam spent its entire life. It is a BUILD
    failure, never a production discovery.
    """
    dirty: dict[str, list[str]] = {}
    for label, schema in _provider_bound_schemas():
        problems = provider_subset_violations(project_for_anthropic(schema))
        if problems:
            dirty[label] = sorted(set(problems))
    assert not dirty, (
        "schemas the provider's structured-output subset refuses — the 2026-08-24 author outage "
        "class, reintroduced:\n"
        + "\n".join(f"  {label}:\n" + "\n".join(f"    {p}" for p in problems)
                    for label, problems in dirty.items()))


# ── THE WALKER HAS TEETH ──────────────────────────────────────────────────────────────────────────
# A sweep whose walker silently returns [] passes forever and proves nothing. These pin that each
# invariant actually fires, on the exact shapes the sweep is there to catch.


@pytest.mark.parametrize(("node", "expected"), [
    ({"enum": ["a", "b"]}, "untyped-enum at $"),
    ({"properties": {"a": {"type": "string"}}}, "untyped-object at $"),
    ({"items": {"type": "string"}}, "untyped-array at $"),
    ({"type": ["string", "null"]}, "type-array at $"),
])
def test_the_walker_names_each_violation_it_is_here_to_catch(node, expected):
    assert provider_subset_violations(node) == [expected]


def test_the_walker_finds_a_bare_enum_buried_in_defs_and_items_and_anyof():
    """The real defect's depth: it sat under ``$defs.<name>.properties.<name>``, and one of its
    siblings sat a further two levels down under ``properties.terms.items.properties.sign``."""
    schema = {
        "type": "object",
        "$defs": {"lit": {"type": "object", "properties": {"type": {"enum": ["string", "date"]}}}},
        "properties": {
            "terms": {"type": "array",
                      "items": {"type": "object", "properties": {"sign": {"enum": [1, -1]}}}},
            "either": {"anyOf": [{"type": "null"}, {"enum": ["x"]}]},
        },
    }
    assert sorted(provider_subset_violations(schema)) == [
        "untyped-enum at $.$defs.lit.properties.type",
        "untyped-enum at $.properties.either.anyOf[1]",
        "untyped-enum at $.properties.terms.items.properties.sign",
    ]


def test_a_dispatchable_node_is_never_flagged():
    """The invariant is "declares a type OR defers to one" — a ``$ref`` and a typed enum are both
    fine, and a union is fine when each alternative carries its own."""
    assert provider_subset_violations({"$ref": "#/$defs/thing"}) == []
    assert provider_subset_violations({"type": "string", "enum": ["a"]}) == []
    assert provider_subset_violations(
        {"anyOf": [{"type": "string", "enum": ["a"]}, {"type": "null"}]}) == []


# ── THE PROJECTION CARRIES THE FIX, AND THE BOOTSTRAP GUARD SEES IT ───────────────────────────────


@pytest.mark.parametrize(("members", "declared"), [
    (["string", "integer", "decimal"], "string"),
    ([1, -1], "integer"),
    ([True, False], "boolean"),
])
def test_projection_declares_the_type_a_bare_enum_already_implies(members, declared):
    """Layer 2 of the fix: the members already say what the type is, so the wire says it too.
    Purely additive — the set of accepted values is identical before and after."""
    out = project_for_anthropic({"type": "object", "properties": {"f": {"enum": list(members)}}})
    assert out["properties"]["f"] == {"type": declared, "enum": list(members)}


def test_projection_leaves_a_mixed_enum_alone_rather_than_guessing():
    """A heterogeneous enum has no single type to infer. Inventing one would narrow the contract
    the model is held to, so the projection declines — and the sweep above reports it, which is a
    human decision (widen the vocabulary, or split the property), not a normalization."""
    mixed = {"type": "object", "properties": {"f": {"enum": ["a", 1]}}}
    out = project_for_anthropic(mixed)
    assert out["properties"]["f"] == {"enum": ["a", 1]}
    assert provider_subset_violations(out) == ["untyped-enum at $.properties.f"]


def test_the_author_turn_schemas_declare_their_enum_types_at_rest():
    """Layer 1, pinned SEPARATELY from layer 2 — and it has to be.

    The projection repairs a bare enum on the way out, so a sweep that only ever looks at the
    PROJECTED form is green whether or not the source declaration exists. The first cut of this
    fix handed `declare_enum_types` the ``$defs`` container rather than each definition — a schema
    walker traverses a name→schema map into nothing — and the sweep above passed anyway, on layer 2
    alone. This assertion is what caught it, and what keeps layer 1 from rotting unnoticed.

    Enum-specific on purpose: these schemas legitimately carry list-valued ``type`` at rest
    (``expectedOutput``'s nullable fields), which is strict JSON Schema the projection normalizes.
    """
    from featuregen.formula.turns_v2 import AUTHOR_TURN_V2_SCHEMA
    from featuregen.formula.turns_v3 import AUTHOR_TURN_V3_SCHEMA

    for label, schema in [("v2", AUTHOR_TURN_V2_SCHEMA), ("v3", AUTHOR_TURN_V3_SCHEMA)]:
        untyped = [p for p in provider_subset_violations(schema) if p.startswith("untyped-enum")]
        assert not untyped, f"{label} author turn schema carries bare enums at rest: {untyped}"

    # The two the live 400 named, spelled out — this is the defect, not a proxy for it.
    defs = AUTHOR_TURN_V3_SCHEMA["$defs"]
    members = ["string", "integer", "decimal", "boolean", "date"]
    assert defs["typedLiteral"]["properties"]["type"] == {"type": "string", "enum": members}
    assert defs["parameterDecl"]["properties"]["type"] == {"type": "string", "enum": members}
    # And the one that is NOT a string enum, so "declare a type" never became "declare string".
    assert defs["compositeBody"]["properties"]["terms"]["items"]["properties"]["sign"] == {
        "type": "integer", "enum": [1, -1]}


def test_the_canonical_proposal_schemas_are_still_never_touched():
    """``turns_v2``/``turns_v3`` both promise it in as many words: the strict
    ``proposal_v*.schema.json`` that ``parse_proposal_v*`` loads is the one gate a proposal is
    accepted through, and the wire relaxations ride a COPY. The type declarations are a wire-side
    transform like the rest of them."""
    import json
    from pathlib import Path

    import featuregen.formula as formula_pkg

    for name in ("proposal_v2.schema.json", "proposal_v3.schema.json"):
        canonical = json.loads(
            (Path(formula_pkg.__file__).with_name(name)).read_text(encoding="utf-8"))
        assert canonical["$defs"]["typedLiteral"]["properties"]["type"] == {
            "enum": ["string", "integer", "decimal", "boolean", "date"]}, (
            f"{name} was edited — the canonical response-validation contract must stay as it was")


def test_the_bootstrap_guard_now_refuses_an_untyped_enum():
    """``provider_incompatibilities`` fails closed at overlay bootstrap
    (``register_enrichment_schemas``). It counted ``enum`` as dispatchable and so waved the author
    schema's bare enums through for the whole life of the seam; it no longer does."""
    # Closed objects, so the only thing either assertion can be reporting is the enum.
    assert provider_incompatibilities({
        "type": "object", "additionalProperties": False,
        "properties": {"f": {"enum": ["a"]}}}) == ["untyped-enum at $.properties.f"]
    assert provider_incompatibilities({
        "type": "object", "additionalProperties": False,
        "properties": {"f": {"type": "string", "enum": ["a"]}}}) == []
