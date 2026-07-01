import jsonschema
import pytest

from featuregen.intake import contract


def test_closed_enum_vocabularies_are_the_authoritative_members():
    assert contract.OBSERVATION_INTENT_KINDS == ("point_in_time", "as_of_event")
    assert contract.METHOD_KINDS == (
        "rolling_aggregate", "point_snapshot", "ratio", "distribution_divergence",
    )
    assert contract.SCORE_SOURCES == ("llm", "default", "catalog")
    assert contract.ROUTED_TO == ("human", "auto")
    assert contract.DRAFT_STATUS == "NEEDS_CLARIFICATION"
    assert contract.CONFIRMED_STATUS == "CONFIRMED"


def test_unknown_and_mode_vocabularies_are_reused_from_sp0_verbatim():
    from featuregen.documents import draft as sp0_draft

    assert contract.UNKNOWN is sp0_draft.UNKNOWN
    assert contract.INTAKE_MODES == sp0_draft.INTAKE_MODES
    assert contract.RAW_INPUT_CLASSIFICATIONS == sp0_draft.RAW_INPUT_CLASSIFICATIONS


def test_all_three_content_schemas_are_valid_json_schemas():
    for schema in (
        contract.DRAFT_CONTENT_SCHEMA,
        contract.CONFIRMED_CONTRACT_JSON_SCHEMA,
        contract.ASSUMPTION_LEDGER_CONTENT_SCHEMA,
    ):
        # raises SchemaError if the schema itself is malformed
        jsonschema.Draft202012Validator.check_schema(schema)


def test_draft_schema_forbids_inline_raw_input_and_pins_status_const():
    props = contract.DRAFT_CONTENT_SCHEMA["properties"]
    assert props["status"] == {"const": "NEEDS_CLARIFICATION"}
    assert contract.DRAFT_CONTENT_SCHEMA["not"] == {"required": ["raw_input"]}
    assert "proposed_feature_name" in contract.DRAFT_CONTENT_SCHEMA["required"]
    # in the Draft, calculation_method is a STRING label (§4.1)
    assert props["feature_semantics"]["properties"]["calculation_method"] == {"type": "string"}


def test_confirmed_calculation_method_is_versioned_and_kind_discriminated():
    defs = contract.CONFIRMED_CONTRACT_JSON_SCHEMA["$defs"]
    cm = defs["calculation_method"]
    assert cm["required"] == ["method_version", "chosen"]
    variants = defs["method_variant"]["oneOf"]
    kinds = {v["properties"]["kind"]["const"] for v in variants}
    assert kinds == set(contract.METHOD_KINDS)
    assert contract.CONFIRMED_CONTRACT_JSON_SCHEMA["properties"]["status"] == {"const": "CONFIRMED"}


def test_ledger_top_level_array_is_assumptions_with_value_item_field():
    schema = contract.ASSUMPTION_LEDGER_CONTENT_SCHEMA
    assert schema["required"] == ["request_id", "assumptions"]
    item = schema["properties"]["assumptions"]["items"]
    assert item["required"] == ["field", "value", "rationale"]           # SP-0 required set
    assert item["properties"]["source"] == {"enum": ["default", "catalog", "llm"]}


def test_confirmed_rolling_aggregate_body_validates_and_a_bad_kind_is_rejected():
    # Validate the calculation_method against a root that carries the shared $defs (the tagged
    # method_variant / filter refs live at the schema root, so an isolated sub-schema cannot resolve
    # them). Reuse the real $defs via a spread so this exercises the shipped definitions.
    root = {**contract.CONFIRMED_CONTRACT_JSON_SCHEMA,
            "required": ["calculation_method"],
            "properties": {"calculation_method": {"$ref": "#/$defs/calculation_method"}}}
    good = {
        "method_version": 1,
        "chosen": {"kind": "rolling_aggregate", "aggregation": "count", "window": "90d",
                   "filter": {"concept": "declined card authorization",
                              "predicate": "card_authorizations.auth_result = 'D'"}},
        "considered": [{"kind": "rolling_aggregate", "aggregation": "count", "window": "90d"}],
    }
    jsonschema.validate({"calculation_method": good}, root)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {"calculation_method": {"method_version": 1, "chosen": {"kind": "made_up"}}}, root
        )
