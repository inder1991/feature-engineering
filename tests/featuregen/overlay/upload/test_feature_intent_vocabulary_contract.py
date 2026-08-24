"""T1 — the intent schema promises what the parser enforces, replayed against the run that proved
it did not.

The fixture is not invented: `_RECORDED_OUTPUTS` is the eight `output` blocks the provider
actually returned on 2026-08-24 (llm_call `llmc_01M0SR0D23B9THNH2FCFZKJG1B`, generation run
`grun_01M0SQYJ2AAEZY38M9X9XCNMB6` — an AML hypothesis against the `cib` catalog), copied field for
field from the immutable audit record. Every one of the eight was refused, all eight on
`output_type`/`unit_kind`, because the schema declared those two as bare strings while
`OutputSpecV2` closed them.

Two things are pinned here and they are different promises:

* the WIRE contract — `feature_intents` v2 publishes the parser's own vocabularies, so the next
  call cannot spell `days` at all;
* the RECOVERY — the recorded eight, replayed through the parse seam's normalization: six now
  build an output spec, and the two the governed vocabulary genuinely lacks refuse by NAME.
"""
from __future__ import annotations

import pytest

from featuregen.intake.schema_projection import project_for_anthropic
from featuregen.overlay.upload.enrich_llm import canonical_output_schema
from featuregen.overlay.upload.feature_intent_generation import (
    _normalize_output_vocabulary,
    _vocabulary_gap,
)
from featuregen.overlay.upload.recipe_contract_v2 import (
    ADDITIVITY,
    CUTOFF_INCLUSIVITY,
    OPERAND_CLASSES,
    OUTPUT_TYPES,
    TEMPORAL_ANCHOR_KINDS,
    UNIT_KINDS,
    WINDOW_UNITS,
    OutputSpecV2,
)

#: The eight recorded `output` blocks, verbatim from the audit record.
_RECORDED_OUTPUTS: tuple[dict, ...] = (
    {"output_id": "cust_risk_rating_snapshot", "unit_kind": "rating_scale",
     "additivity": "non_additive", "output_type": "categorical",
     "display_label": "Customer Risk Rating (As-Of Snapshot)",
     "null_input_policy": "treat_missing_as_unknown",
     "empty_population_policy": "return_null"},
    {"output_id": "cust_pep_flag", "unit_kind": "boolean", "additivity": "non_additive",
     "output_type": "boolean", "display_label": "Politically Exposed Person Indicator",
     "null_input_policy": "treat_missing_as_false", "empty_population_policy": "return_null"},
    {"output_id": "cust_new_to_bank_flag", "unit_kind": "boolean", "additivity": "non_additive",
     "output_type": "boolean", "display_label": "New-to-Bank Indicator",
     "null_input_policy": "treat_missing_as_false", "empty_population_policy": "return_null"},
    {"output_id": "cust_relationship_tenure_recency", "unit_kind": "days",
     "additivity": "non_additive", "output_type": "numeric",
     "display_label": "Time Since Relationship Origination",
     "null_input_policy": "treat_missing_as_unknown",
     "empty_population_policy": "return_null"},
    {"output_id": "cust_crossborder_value_shift", "unit_kind": "monetary_change",
     "additivity": "non_additive", "output_type": "numeric",
     "display_label": "Cross-Border Payment Value Shift",
     "null_input_policy": "treat_missing_as_unknown",
     "empty_population_policy": "return_null"},
    {"output_id": "cust_highrisk_country_counterparties", "unit_kind": "count",
     "additivity": "non_additive", "output_type": "count",
     "display_label": "Distinct Higher-Risk-Country Counterparties",
     "null_input_policy": "treat_missing_as_unknown",
     "empty_population_policy": "return_null"},
    {"output_id": "cust_payment_channel_change", "unit_kind": "boolean",
     "additivity": "non_additive", "output_type": "boolean",
     "display_label": "Sudden Payment Channel Change",
     "null_input_policy": "treat_missing_as_unknown",
     "empty_population_policy": "return_null"},
    {"output_id": "cust_txn_velocity_spike", "unit_kind": "count_rate",
     "additivity": "non_additive", "output_type": "numeric",
     "display_label": "Transaction Velocity Spike",
     "null_input_policy": "treat_missing_as_unknown",
     "empty_population_policy": "return_null"},
)

#: What each recorded block becomes: the (output_type, unit_kind) it builds with, or None where the
#: governed vocabulary has no entry and only an owner may add one.
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


def _replay(block: dict) -> tuple[OutputSpecV2 | None, str, tuple[dict, ...]]:
    """One recorded block through the parse seam's output path: normalize, gap-check, build."""
    fixed, applied = _normalize_output_vocabulary(block)
    gap = _vocabulary_gap(fixed)
    if gap:
        return None, gap, applied
    return OutputSpecV2(**fixed), "", applied


@pytest.mark.parametrize("block", _RECORDED_OUTPUTS, ids=lambda b: b["output_id"])
def test_each_recorded_output_either_builds_or_names_its_missing_entry(block):
    spec, gap, _applied = _replay(block)
    expected = _EXPECTED[block["output_id"]]
    if expected is None:
        assert spec is None and gap, f"{block['output_id']} was expected to be a vocabulary gap"
    else:
        assert spec is not None, gap
        assert (spec.output_type, spec.unit_kind) == expected


def test_six_of_the_eight_recorded_intents_recover_and_two_are_vocabulary_gaps():
    """The run's headline number, replayed: 8 refused → 6 build, 2 are honest vocabulary gaps."""
    built = [b["output_id"] for b in _RECORDED_OUTPUTS if _replay(b)[0] is not None]
    gapped = [b["output_id"] for b in _RECORDED_OUTPUTS if _replay(b)[0] is None]
    assert len(built) == 6, built
    assert gapped == ["cust_risk_rating_snapshot", "cust_crossborder_value_shift"]


def test_a_vocabulary_gap_names_the_missing_entry_and_says_it_is_a_gap():
    """Not INTENT_REJECTED_PARSE prose. An ordinal rating is a real feature shape the vocabulary
    has no entry for, and only an owner may add one — so the refusal names the entry, says
    'vocabulary gap', and says whose decision it is."""
    _spec, gap, _applied = _replay(_RECORDED_OUTPUTS[0])
    assert "vocabulary gap" in gap
    assert "'categorical'" in gap and "'rating_scale'" in gap    # BOTH, never one hiding the other
    assert "owner decision" in gap

    _spec, shift_gap, _applied = _replay(_RECORDED_OUTPUTS[4])
    assert "vocabulary gap" in shift_gap and "'monetary_change'" in shift_gap


def test_every_repair_is_recorded_with_what_it_changed_and_why():
    """A repair nobody can see is a silent edit of the model's answer."""
    _spec, _gap, applied = _replay(_RECORDED_OUTPUTS[3])            # unit_kind: days
    assert applied == ({"field": "unit_kind", "from": "days", "to": "duration_days",
                        "reason": "the governed spelling of this unit"},)

    _spec, _gap, flag = _replay(_RECORDED_OUTPUTS[1])               # unit_kind: boolean
    assert [entry["field"] for entry in flag] == ["unit_kind"]
    assert flag[0]["from"] == "boolean" and flag[0]["to"] == "count"


def test_a_value_the_model_meant_is_never_overwritten():
    """The swap repairs fire only where the receiving field is absent or already agrees — a
    disagreeing pair is left exactly as written, and refuses as the gap it is."""
    contested = {"output_id": "x", "display_label": "X", "output_type": "count",
                 "unit_kind": "monetary", "additivity": "additive",
                 "null_input_policy": "n", "empty_population_policy": "e"}
    fixed, applied = _normalize_output_vocabulary(contested)
    assert applied == () and fixed == contested
    assert "vocabulary gap" in _vocabulary_gap(fixed)


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
