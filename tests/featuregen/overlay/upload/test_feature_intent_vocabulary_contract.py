"""T1 — the intent schema promises what the parser enforces, replayed against the run that proved
it did not.

The fixture is not invented: `_RECORDED_INTENTS` is the eight intents the provider actually
returned on 2026-08-24 (llm_call `llmc_01M0SR0D23B9THNH2FCFZKJG1B`, generation run
`grun_01M0SQYJ2AAEZY38M9X9XCNMB6` — an AML hypothesis against the `cib` catalog), copied field for
field from the immutable audit record. All eight were refused. The first wall was
`output_type`/`unit_kind`, but it was never the only one: the same schema left `operand_class`,
`anchor_kind` and `window_unit` as bare strings too, so repairing the output block alone still
left every intent refused — which is why the replay below drives the WHOLE parser, not a layer.

Two different promises are pinned here:

* the WIRE contract — `feature_intents` v2 publishes the parser's own vocabularies, so the next
  call cannot spell `days` at all;
* the RECOVERY — the recorded eight through `parse_feature_intent` end to end: six become
  validated intents, and the two the governed vocabulary genuinely lacks refuse by NAME.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from featuregen.intake.schema_projection import project_for_anthropic
from featuregen.overlay.upload.enrich_llm import canonical_output_schema
from featuregen.overlay.upload.feature_intent import FeatureIntentV1, parse_feature_intent
from featuregen.overlay.upload.feature_intent_generation import (
    _normalize_intent_vocabulary,
    _vocabulary_gaps,
)
from featuregen.overlay.upload.feature_planning_contracts import PlanningContractError
from featuregen.overlay.upload.recipe_contract_v2 import (
    ADDITIVITY,
    CUTOFF_INCLUSIVITY,
    OPERAND_CLASSES,
    OUTPUT_TYPES,
    TEMPORAL_ANCHOR_KINDS,
    UNIT_KINDS,
    WINDOW_UNITS,
    RecipeContractError,
)

_RECORDED_INTENTS: tuple[dict, ...] = tuple(json.loads(
    (pathlib.Path(__file__).parent / "fixtures"
     / "recorded_feature_intents_llmc_01M0SR0D23B9THNH2FCFZKJG1B.json").read_text())["intents"])

_PROVENANCE = {"prompt_ref": "feature_intents_v1",
               "output_schema_version": "feature_intents@2", "model": "replay",
               "call_ref": "llmc_01M0SR0D23B9THNH2FCFZKJG1B",
               "confirmed_scope_hash": "replay", "semantic_context_hash": "replay"}

#: What each recorded intent becomes: the (output_type, unit_kind) of the intent that parses, or
#: None where the governed vocabulary has no entry and only an owner may add one.
_EXPECTED: dict[str, tuple[str, str] | None] = {
    "cust_risk_rating_snapshot": None,                       # categorical / rating_scale
    "cust_pep_flag": ("boolean", "count"),
    "cust_new_to_bank_flag": ("boolean", "count"),
    "cust_relationship_tenure_recency": ("numeric", "duration_days"),
    "cust_crossborder_value_shift": None,                    # monetary_change
    "cust_highrisk_country_counterparties": ("integer", "count"),
    "cust_payment_channel_change": ("boolean", "count"),
    "cust_txn_velocity_spike": ("numeric", "rate"),
}


def _replay(item: dict) -> tuple[FeatureIntentV1 | None, str, tuple[dict, ...]]:
    """One recorded intent through the FULL parse seam: normalize, gap-check, parse.

    Deliberately the whole parser and not the output layer: an output spec that builds while its
    operands still refuse is a green light over a refused intent — the exact defect class this
    programme exists to remove."""
    doc, applied = _normalize_intent_vocabulary(
        {**item, "generation_provenance": dict(_PROVENANCE)})
    gap = _vocabulary_gaps(doc)
    if gap:
        return None, gap, applied
    try:
        return parse_feature_intent(doc), "", applied
    except (PlanningContractError, RecipeContractError) as error:
        raise AssertionError(f"no gap was named, yet the parser refused: {error}") from error


@pytest.mark.parametrize("item", _RECORDED_INTENTS,
                         ids=lambda i: i["output"]["output_id"])
def test_each_recorded_intent_either_parses_or_names_its_missing_entry(item):
    intent, gap, _applied = _replay(item)
    expected = _EXPECTED[item["output"]["output_id"]]
    if expected is None:
        assert intent is None and gap, "expected a vocabulary gap"
    else:
        assert intent is not None, gap
        assert (intent.output.output_type, intent.output.unit_kind) == expected


def test_six_of_the_eight_recorded_intents_parse_and_two_are_vocabulary_gaps():
    """The run's headline number, replayed end to end: 8 refused → 6 validated intents, 2 honest
    vocabulary gaps. These are FeatureIntentV1 objects — operands, temporal contract and all."""
    parsed = [i["output"]["output_id"] for i in _RECORDED_INTENTS if _replay(i)[0] is not None]
    gapped = [i["output"]["output_id"] for i in _RECORDED_INTENTS if _replay(i)[0] is None]
    assert len(parsed) == 6, parsed
    assert gapped == ["cust_risk_rating_snapshot", "cust_crossborder_value_shift"]


def test_the_repaired_intents_carry_the_operands_and_anchors_they_claimed():
    """The three walls past the output block, proven on the intents that cleared them."""
    by_id = {i["output"]["output_id"]: _replay(i)[0] for i in _RECORDED_INTENTS}

    tenure = by_id["cust_relationship_tenure_recency"]
    assert tenure.temporal.anchor_kind == "as_of"            # was "as_of_snapshot"
    assert tenure.temporal.window_unit == "days"             # was "day"
    classes = {op.role: op.operand_class for op in tenure.operands}
    assert classes == {"measured": "event_timestamp",        # origination_date, pit_role=event
                       "as_of_anchor": "as_of_timestamp"}    # as_of_date, pit_role=as_of

    velocity = by_id["cust_txn_velocity_spike"]
    assert velocity.temporal.anchor_kind == "event"          # was "event_window"
    assert {op.operand_class for op in velocity.operands} == {"entity_key", "event_timestamp"}

    pep = by_id["cust_pep_flag"]
    assert [op.operand_class for op in pep.operands] == ["status"]   # flag group, 11 of 11


def test_a_vocabulary_gap_names_every_missing_entry_and_says_it_is_a_gap():
    """Not INTENT_REJECTED_PARSE prose. An ordinal rating is a real feature shape the vocabulary
    has no entry for, and only an owner may add one — so the refusal names the entry, says
    'vocabulary gap', and says whose decision it is. Every missing entry is named at once, so an
    owner sees the whole bill for one intent rather than one field per round trip."""
    _intent, gap, _applied = _replay(_RECORDED_INTENTS[0])
    assert "vocabulary gap" in gap
    assert "'categorical'" in gap and "'rating_scale'" in gap
    assert "'attribute'" in gap and "customer_risk_rating" in gap    # the operand gap too
    assert "owner decision" in gap

    _intent, shift_gap, _applied = _replay(_RECORDED_INTENTS[4])
    assert "vocabulary gap" in shift_gap and "'monetary_change'" in shift_gap


def test_the_monetary_change_gap_is_not_quietly_mapped_to_monetary():
    """The counterfactual, executed rather than asserted: `monetary_change` → `monetary` would
    build an output spec that then fails for a MISSING CURRENCY POLICY. That trades a named gap an
    owner can act on for an anonymous refusal — so the mapping does not exist."""
    recorded = _RECORDED_INTENTS[4]["output"]
    assert "currency_policy" not in recorded
    with pytest.raises(RecipeContractError, match="currency policy"):
        parse_feature_intent({**_RECORDED_INTENTS[4],
                              "output": {**recorded, "unit_kind": "monetary"},
                              "generation_provenance": dict(_PROVENANCE)})


def test_every_repair_is_recorded_with_what_it_changed_and_why():
    """A repair nobody can see is a silent edit of the model's answer."""
    _intent, _gap, applied = _replay(_RECORDED_INTENTS[3])          # the tenure intent
    assert {entry["field"] for entry in applied} == {
        "output.unit_kind", "temporal.anchor_kind", "temporal.window_unit",
        "operands[0].operand_class", "operands[1].operand_class"}
    unit = next(e for e in applied if e["field"] == "output.unit_kind")
    assert (unit["from"], unit["to"]) == ("days", "duration_days")
    assert unit["reason"] == "the governed spelling of this unit"
    anchor = next(e for e in applied if e["field"] == "temporal.anchor_kind")
    assert (anchor["from"], anchor["to"]) == ("as_of_snapshot", "as_of")


def test_a_value_the_model_meant_is_never_overwritten():
    """The swap repairs fire only where the receiving field is absent or already agrees — a
    disagreeing pair is left exactly as written, and refuses as the gap it is."""
    contested = {"output": {"output_id": "x", "display_label": "X", "output_type": "count",
                            "unit_kind": "monetary", "additivity": "additive",
                            "null_input_policy": "n", "empty_population_policy": "e"}}
    fixed, applied = _normalize_intent_vocabulary(contested)
    assert applied == () and fixed == contested
    assert "vocabulary gap" in _vocabulary_gaps(fixed)


def test_an_operand_class_the_registry_cannot_determine_is_a_gap_not_a_guess():
    """`attribute` is not translated — the platform has no attribute class to translate it into,
    and the concept's own group (categorical: status / dimension / policy_input / measure /
    direction) does not determine one. Recovery is never worth a guess."""
    doc = {"output_grain_entity": "customer",
           "operands": [{"role": "measured", "concept": "customer_risk_rating",
                         "operand_class": "attribute"}]}
    fixed, applied = _normalize_intent_vocabulary(doc)
    assert applied == ()
    gap = _vocabulary_gaps(fixed)
    assert "'attribute'" in gap and "does not determine" in gap


# ── the wire contract: the schema publishes the parser's own vocabularies ─────────────────────

def _wire_item() -> dict:
    return project_for_anthropic(
        canonical_output_schema("feature_intents", 2))["properties"]["intents"]["items"]


def test_the_v2_wire_schema_publishes_exactly_the_vocabularies_the_parser_closes():
    """The whole point of the version. Each enum is compared against the CONTRACT's tuple, so a
    schema that drifts from the parser fails here rather than in a live run's rejection count."""
    item = _wire_item()["properties"]
    output = item["output"]["properties"]
    temporal = item["temporal"]["properties"]
    assert output["output_type"]["enum"] == list(OUTPUT_TYPES)
    assert output["unit_kind"]["enum"] == list(UNIT_KINDS)
    assert output["additivity"]["enum"] == list(ADDITIVITY)
    assert item["operands"]["items"]["properties"]["operand_class"]["enum"] == list(OPERAND_CLASSES)
    assert temporal["anchor_kind"]["enum"] == list(TEMPORAL_ANCHOR_KINDS)
    assert temporal["window_unit"]["enum"] == list(WINDOW_UNITS)
    assert temporal["cutoff_inclusivity"]["enum"] == list(CUTOFF_INCLUSIVITY)


def test_the_published_vocabularies_are_frozen_so_growth_needs_a_v3():
    """`register_schema` upserts, so a derived enum that quietly grew would make one version
    number mean two things across deployments. Growing a vocabulary must break this test and be
    published as v3 — which is also the point at which extending it is an owner's decision."""
    output = _wire_item()["properties"]["output"]["properties"]
    assert output["output_type"]["enum"] == ["numeric", "integer", "boolean", "date"]
    assert output["unit_kind"]["enum"] == ["monetary", "count", "ratio", "duration_days",
                                           "rate", "score"]


def test_v1_stays_byte_frozen_because_recorded_calls_egressed_under_it():
    """History is not relabelled: v1 keeps the bare strings every stored `llm_call` was produced
    against. The vocabularies arrive as v2, and the dispatch stamps v2."""
    import json

    from featuregen.overlay.upload.feature_intent_generation import FEATURE_INTENT_SCHEMA_VERSION

    v1 = canonical_output_schema("feature_intents", 1)
    output = v1["properties"]["intents"]["items"]["properties"]["output"]["properties"]
    assert output["output_type"] == {"type": "string", "maxLength": 16}
    assert output["unit_kind"] == {"type": "string", "maxLength": 16}
    assert "x-wire-enum" not in json.dumps(v1)
    assert FEATURE_INTENT_SCHEMA_VERSION == 2


def test_v2_differs_from_v1_in_the_vocabularies_and_nothing_else():
    """Derived, not restated — so the two bodies cannot drift apart in shape."""
    import copy

    v2 = canonical_output_schema("feature_intents", 2)
    stripped = copy.deepcopy(v2)
    item = stripped["properties"]["intents"]["items"]["properties"]
    for node in (item["output"]["properties"]["output_type"],
                 item["output"]["properties"]["unit_kind"],
                 item["output"]["properties"]["additivity"],
                 item["operands"]["items"]["properties"]["operand_class"],
                 item["temporal"]["properties"]["anchor_kind"],
                 item["temporal"]["properties"]["window_unit"],
                 item["temporal"]["properties"]["cutoff_inclusivity"]):
        node.pop("x-wire-enum")
    assert stripped == canonical_output_schema("feature_intents", 1)
