from __future__ import annotations

import pytest

from featuregen.overlay.upload.planner.cause import (
    RESOLUTION_CATEGORY_MAP,
    ReasonCategory,
    ResolutionCause,
    assert_map_exhaustive,
    category_of,
    contextual_cause,
)
from featuregen.overlay.upload.planner.contracts import ReasonCode
from featuregen.overlay.upload.planner.shadow_review import (
    ReviewEntryV1,
    build_review_artifact,
    missing_shape_labels,
    review_gate_clean,
    shape_key,
)


# ── Layer A: the static map is EXHAUSTIVE over the whole ReasonCode registry ──
def test_layer_a_map_is_exhaustive_over_the_registry():
    assert_map_exhaustive()   # raises if ANY ReasonCode is unmapped
    assert set(RESOLUTION_CATEGORY_MAP) == set(ReasonCode)


def test_safety_reject_and_topology_are_not_internal():
    assert category_of(ReasonCode.binding_safety_rejected) is ReasonCategory.policy_or_catalog_state
    assert category_of(ReasonCode.ingredient_not_connected_to_path) is ReasonCategory.topology_or_model
    assert category_of(ReasonCode.planner_internal_error) is ReasonCategory.internal


def test_a_hypothetically_unmapped_code_would_fail_exhaustiveness(monkeypatch):
    # drop one entry -> assert_map_exhaustive must FAIL (proves the guard bites)
    trimmed = dict(RESOLUTION_CATEGORY_MAP)
    trimmed.pop(ReasonCode.grain_incompatible)
    monkeypatch.setattr("featuregen.overlay.upload.planner.cause.RESOLUTION_CATEGORY_MAP", trimmed)
    with pytest.raises(AssertionError):
        assert_map_exhaustive()


# ── Layer B: contextual cause needs an expert label; operationally_unmeasured != unknown ──
def test_contextual_cause_requires_an_expert_label():
    assert contextual_cause(ReasonCode.grain_incompatible, None) is ResolutionCause.unknown
    assert (contextual_cause(ReasonCode.grain_incompatible, ResolutionCause.unsupported_topology)
            is ResolutionCause.unsupported_topology)


def test_unmapped_code_is_operationally_unmeasured_not_unknown(monkeypatch):
    trimmed = dict(RESOLUTION_CATEGORY_MAP)
    trimmed.pop(ReasonCode.grain_incompatible)
    monkeypatch.setattr("featuregen.overlay.upload.planner.cause.RESOLUTION_CATEGORY_MAP", trimmed)
    # even WITH a label, an unmapped code is operationally_unmeasured (a registry gap)
    assert (contextual_cause(ReasonCode.grain_incompatible, ResolutionCause.expected)
            is ResolutionCause.operationally_unmeasured)


# ── Gate-2b review artifact ──
def _artifact(pairs):
    entries = [ReviewEntryV1(reason=r, evidence_shape=s, label=lab) for r, s, lab in pairs]
    return build_review_artifact(entries, reviewer="expert@bank")


def test_review_gate_clean_when_all_shapes_labelled_expected():
    art = _artifact([(ReasonCode.grain_incompatible, "no_column", ResolutionCause.unsupported_topology),
                     (ReasonCode.aggregation_strategy_missing, "non_additive", ResolutionCause.expected)])
    observed = {shape_key(ReasonCode.grain_incompatible, "no_column"),
                shape_key(ReasonCode.aggregation_strategy_missing, "non_additive")}
    assert review_gate_clean(observed, art)
    assert art.content_hash and art.signature is None   # signature filled by D8


def test_review_gate_fails_on_unlabelled_shape():
    art = _artifact([(ReasonCode.grain_incompatible, "no_column", ResolutionCause.unsupported_topology)])
    observed = {shape_key(ReasonCode.grain_incompatible, "no_column"),
                shape_key(ReasonCode.aggregation_weight_missing, "unbound_weight")}
    assert missing_shape_labels(observed, art) == (shape_key(ReasonCode.aggregation_weight_missing, "unbound_weight"),)
    assert not review_gate_clean(observed, art)


def test_review_gate_fails_on_a_classifier_defect_or_unknown():
    art = _artifact([(ReasonCode.grain_incompatible, "no_column", ResolutionCause.classifier_defect)])
    observed = {shape_key(ReasonCode.grain_incompatible, "no_column")}
    assert not review_gate_clean(observed, art)
    assert art.defect_keys == (shape_key(ReasonCode.grain_incompatible, "no_column"),)


def test_content_hash_is_stable_and_order_independent():
    a = _artifact([(ReasonCode.grain_incompatible, "x", ResolutionCause.expected),
                   (ReasonCode.concept_mismatch, "y", ResolutionCause.expected)])
    b = _artifact([(ReasonCode.concept_mismatch, "y", ResolutionCause.expected),
                   (ReasonCode.grain_incompatible, "x", ResolutionCause.expected)])
    assert a.content_hash == b.content_hash
