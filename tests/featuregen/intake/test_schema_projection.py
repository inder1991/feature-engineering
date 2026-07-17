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
