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


def test_plain_enum_and_plain_nullable_are_untouched():
    schema = {"type": "object", "properties": {
        "role": {"type": "string", "enum": ["a", "b"]},
        "note": {"type": ["string", "null"]},
    }}
    out = project_for_anthropic(schema)
    assert out["properties"]["role"]["enum"] == ["a", "b"]
    assert out["properties"]["note"]["type"] == ["string", "null"]


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
