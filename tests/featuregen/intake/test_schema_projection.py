from featuregen.intake.schema_projection import (
    assert_schemas_provider_compatible,
    project_for_anthropic,
    provider_incompatibilities,
)


def test_strips_unsupported_keywords_everywhere():
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "maxLength": 40, "minLength": 1},
            "items": {"type": "array", "items": {"type": "string", "maxLength": 8},
                      "maxItems": 10, "minItems": 1},
            "n": {"type": "integer", "minimum": 0, "maximum": 5, "multipleOf": 1},
        },
        "required": ["name"],
    }
    out = project_for_anthropic(schema)
    assert provider_incompatibilities(out) == []
    # structure preserved
    assert out["required"] == ["name"]
    assert out["properties"]["name"]["type"] == "string"
    assert out["properties"]["items"]["items"]["type"] == "string"
    # minLength is allowed by the API and is kept
    assert out["properties"]["name"].get("minLength") == 1


def test_normalizes_nullable_enum_to_anyof():
    schema = {"type": "object", "properties": {
        "basis": {"type": ["string", "null"], "enum": ["event", "snapshot", None]}}}
    out = project_for_anthropic(schema)
    basis = out["properties"]["basis"]
    assert "enum" not in basis and basis.get("type") != ["string", "null"]
    variants = basis["anyOf"]
    string_variant = next(v for v in variants if v.get("type") == "string")
    assert string_variant["enum"] == ["event", "snapshot"]
    assert any(v.get("type") == "null" for v in variants)
    assert provider_incompatibilities(out) == []


def test_plain_enum_untouched_and_every_type_array_becomes_anyof():
    """Defensive normalization (2026-08-14): every list-valued "type" becomes anyOf on the
    wire — semantically identical, universally accepted, and a narrower provider-grammar
    surface. (Prompted by a billing 400 briefly misread as a type-array rejection; the
    normalization stays on its own merits.) Constraints ride the non-null arms only."""
    schema = {"type": "object", "properties": {
        "role": {"type": "string", "enum": ["a", "b"]},
        "note": {"type": ["string", "null"], "maxLength": 40},
    }}
    out = project_for_anthropic(schema)
    assert out["properties"]["role"]["enum"] == ["a", "b"]
    note = out["properties"]["note"]
    assert "type" not in note
    assert {"type": "null"} in note["anyOf"]
    assert {"type": "string"} in note["anyOf"]         # maxLength stripped as unsupported
    assert provider_incompatibilities(out) == []


def test_does_not_mutate_input_and_is_idempotent():
    schema = {"type": "object", "properties": {"x": {"type": "string", "maxLength": 3}}}
    once = project_for_anthropic(schema)
    twice = project_for_anthropic(once)
    assert "maxLength" in schema["properties"]["x"]      # input untouched
    assert once == twice                                 # idempotent


def test_incompatibilities_reports_paths():
    schema = {"type": "object", "properties": {"x": {"type": "string", "maxLength": 3}}}
    probs = provider_incompatibilities(schema)
    assert any("maxLength" in p for p in probs)


def test_assert_raises_when_projection_cannot_clean():
    # a schema node with no type/anyOf is unprojectable-clean → guard raises
    bad = {"type": "object", "properties": {"x": {"maxLength": 3}}}  # x has no 'type'
    try:
        assert_schemas_provider_compatible([("bad", project_for_anthropic(bad))])
    except ValueError as e:
        assert "bad" in str(e)
    else:
        raise AssertionError("expected ValueError")


# ── forward-looking hardening: schema-valued containers the recursion previously missed ────────────
# A stripped keyword hidden inside additionalProperties / patternProperties / prefixItems /
# if-then-else must be (a) stripped by project_for_anthropic AND (b) detected by
# provider_incompatibilities on the un-projected schema — else a future _SCHEMAS entry using one of
# these hides an incompatibility from BOTH sides (static test green, wire schema still 400s).


def test_maxlength_inside_additional_properties_is_stripped_and_detected():
    schema = {"type": "object",
              "additionalProperties": {"type": "string", "maxLength": 8}}
    assert any("maxLength" in p for p in provider_incompatibilities(schema))
    out = project_for_anthropic(schema)
    assert "maxLength" not in out["additionalProperties"]
    assert provider_incompatibilities(out) == []


def test_bool_additional_properties_is_untouched_and_clean():
    # additionalProperties: false is a bool (not a sub-schema) — must not be recursed or flagged.
    schema = {"type": "object", "properties": {"x": {"type": "string"}},
              "additionalProperties": False}
    assert provider_incompatibilities(schema) == []
    out = project_for_anthropic(schema)
    assert out["additionalProperties"] is False


def test_maxlength_inside_pattern_properties_is_stripped_and_detected():
    schema = {"type": "object",
              "patternProperties": {"^x": {"type": "string", "maxLength": 8}}}
    assert any("maxLength" in p for p in provider_incompatibilities(schema))
    out = project_for_anthropic(schema)
    assert "maxLength" not in out["patternProperties"]["^x"]
    assert provider_incompatibilities(out) == []


def test_maxlength_inside_prefix_items_is_stripped_and_detected():
    schema = {"type": "array",
              "prefixItems": [{"type": "string", "maxLength": 8}, {"type": "integer"}]}
    assert any("maxLength" in p for p in provider_incompatibilities(schema))
    out = project_for_anthropic(schema)
    assert "maxLength" not in out["prefixItems"][0]
    assert provider_incompatibilities(out) == []


def test_maxlength_inside_if_then_branch_is_stripped_and_detected():
    schema = {
        "type": "object",
        "properties": {"kind": {"type": "string"}},
        "if": {"type": "object", "properties": {"kind": {"const": "a"}}},
        "then": {"type": "object", "properties": {"tag": {"type": "string", "maxLength": 8}}},
        "else": {"type": "object", "properties": {"tag": {"type": "string"}}},
    }
    assert any("maxLength" in p for p in provider_incompatibilities(schema))
    out = project_for_anthropic(schema)
    assert "maxLength" not in out["then"]["properties"]["tag"]
    assert provider_incompatibilities(out) == []


def test_maxitems_inside_not_is_stripped_and_detected():
    schema = {"type": "array", "not": {"type": "array", "maxItems": 3}}
    assert any("maxItems" in p for p in provider_incompatibilities(schema))
    out = project_for_anthropic(schema)
    assert "maxItems" not in out["not"]
    assert provider_incompatibilities(out) == []


def test_x_wire_enum_becomes_enum_on_the_wire_and_never_reaches_the_response_validator():
    """The `x-wire-required` bargain, applied to a CLOSED VOCABULARY (Task 6c).

    `enum` is provider-SUPPORTED, so it is the one strictness keyword that would be enforced in
    BOTH directions from a canonical schema — constraining generation (wanted) and failing the
    whole response on one off-vocabulary answer (not wanted, on a body whose leniency is the point).
    `x-wire-enum` splits the two: the wire carries the vocabulary, the canonical stays a plain
    string, and the closure is enforced per item in code.
    """
    canonical = {"type": "object",
                 "properties": {"role": {"type": "string", "x-wire-enum": ["a", "b"]}}}
    wire = project_for_anthropic(canonical)
    assert wire["properties"]["role"] == {"type": "string", "enum": ["a", "b"]}
    # The canonical is NOT mutated — the response validator still sees a plain string.
    assert canonical["properties"]["role"] == {"type": "string", "x-wire-enum": ["a", "b"]}
    assert provider_incompatibilities(wire) == []


# ── THE RATCHET (2026-08-14): every provider-bound schema, proven clean ────────────────────────────
# Prompted by an outage where every provider call 400ed at once (in the end: credit exhaustion
# misreported as a schema rejection by our own keyword scan — both since fixed). The sweep stays
# because the failure class it guards is real regardless: every registered schema must project to
# a provider-clean wire form, so a schema the provider's grammar cannot accept is a BUILD failure,
# never a production discovery.


def test_every_registered_schema_projects_provider_clean():
    from featuregen.overlay.upload.enrich_llm import _SCHEMAS

    assert _SCHEMAS, "the registry import moved — fix the sweep, never delete it"
    dirty: dict[str, list[str]] = {}
    for (schema_id, version), schema in sorted(_SCHEMAS.items()):
        problems = provider_incompatibilities(project_for_anthropic(schema))
        if problems:
            dirty[f"{schema_id}@{version}"] = problems
    assert not dirty, (
        "provider-incompatible wire schemas — the 2026-08-14 outage class, reintroduced:\n"
        + "\n".join(f"  {k}: {v}" for k, v in dirty.items()))
