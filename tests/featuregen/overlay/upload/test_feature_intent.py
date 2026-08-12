"""SE-1 — FeatureIntentV1: meaning only, closed operations, physical refs refused loudly."""
from __future__ import annotations

from dataclasses import asdict, replace

import pytest

from featuregen.overlay.upload.feature_intent import (
    FeatureIntentError,
    FeatureIntentV1,
    GenerationProvenanceV1,
    feature_intent_id,
    parse_feature_intent,
)
from featuregen.overlay.upload.feature_planning_contracts import RequiredOperandV1
from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id

EXEMPLAR = v2_recipe_by_id("posted_debit_amount")


def provenance() -> GenerationProvenanceV1:
    return GenerationProvenanceV1(
        prompt_ref="prompt:abc", output_schema_version="feature-intent-1",
        model="claude-x", call_ref="call:1", confirmed_scope_hash="scope:2")


def intent(**over) -> FeatureIntentV1:
    base = dict(
        display_name="Posted debit amount (model)",
        business_definition="Total posted debit amount over the window.",
        primary_objective=EXEMPLAR.primary_objective,
        computation_kind="deterministic_formula",
        operation_class="sum",
        output=EXEMPLAR.output,
        output_grain_entity=EXEMPLAR.output_grain,
        source_grain=EXEMPLAR.source_grain,
        operands=tuple(
            RequiredOperandV1(role=op.role, concept=op.concept,
                              operand_class=op.operand_class,
                              allowed_source_grains=op.allowed_source_grains)
            for op in EXEMPLAR.operands),
        temporal=EXEMPLAR.temporal,
        eligibility=EXEMPLAR.eligibility,
        leakage=EXEMPLAR.leakage,
        generation_provenance=provenance())
    base.update(over)
    return FeatureIntentV1(**base)


def test_a_well_formed_deterministic_intent_constructs():
    built = intent()
    assert built.operation_class == "sum"
    assert feature_intent_id(built) == feature_intent_id(intent())


def test_the_result_class_additivity_law_holds_for_intents_too():
    bad_output = replace(EXEMPLAR.output, additivity="non_additive")
    with pytest.raises(FeatureIntentError, match="incompatible"):
        intent(output=bad_output)
    with pytest.raises(FeatureIntentError, match="not a closed Formula-V2"):
        intent(operation_class="vibes_weighted_mean")


def test_conceptual_and_model_output_rules():
    conceptual = intent(computation_kind="conceptual_pattern", operation_class="",
                        conceptual_reason="needs cross-run state the grammar cannot express")
    assert conceptual.conceptual_reason
    with pytest.raises(FeatureIntentError, match="WHY it is conceptual-only"):
        intent(computation_kind="conceptual_pattern", operation_class="")
    with pytest.raises(FeatureIntentError, match="deterministic-only"):
        intent(computation_kind="conceptual_pattern", operation_class="sum",
               conceptual_reason="r")
    with pytest.raises(FeatureIntentError, match="may not invent one"):
        intent(computation_kind="governed_model_output", operation_class="")


def test_physical_binding_hints_are_refused_at_construction():
    hinted = tuple(
        replace(op, binding_hint_refs=("public.txns.txn_amt",)) if i == 0 else op
        for i, op in enumerate(intent().operands))
    with pytest.raises(FeatureIntentError, match="proposes meaning"):
        intent(operands=hinted)


def test_every_field_is_identity_bearing_including_rationale():
    a, b = intent(), intent(rationale="because the pattern predicts dormancy")
    assert feature_intent_id(a) != feature_intent_id(b)
    assert feature_intent_id(a) != feature_intent_id(
        intent(display_name="Different name"))


def _doc() -> dict:
    raw = asdict(intent())
    raw["operands"] = [dict(op) for op in raw["operands"]]
    return raw


def test_parse_round_trips_a_clean_document():
    parsed = parse_feature_intent(_doc())
    assert parsed == intent()


def test_parse_refuses_unknown_and_physical_keys_by_name():
    doc = _doc()
    doc["confidence"] = 0.97
    with pytest.raises(FeatureIntentError, match="unknown key 'confidence'"):
        parse_feature_intent(doc)
    doc = _doc()
    doc["derives_from"] = ["public.txns.txn_amt"]
    with pytest.raises(FeatureIntentError, match="physical reference key 'derives_from'"):
        parse_feature_intent(doc)
    doc = _doc()
    doc["operands"][0]["column_name"] = "txn_amt"
    with pytest.raises(FeatureIntentError, match="physical reference key 'column_name'"):
        parse_feature_intent(doc)
    doc = _doc()
    doc["temporal"]["table"] = "public.txns"
    with pytest.raises(FeatureIntentError, match="physical reference key 'table'"):
        parse_feature_intent(doc)


def test_parse_requires_operands_and_provenance():
    doc = _doc()
    doc["operands"] = []
    with pytest.raises(FeatureIntentError, match="non-empty list"):
        parse_feature_intent(doc)
    doc = _doc()
    doc["generation_provenance"]["model"] = " "
    with pytest.raises(FeatureIntentError, match="'model' is mandatory"):
        parse_feature_intent(doc)
