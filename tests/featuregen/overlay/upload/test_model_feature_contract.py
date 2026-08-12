"""BR-7A — a prediction is not a formula: the model-feature contract, its revision identity, and
its own readiness ladder with the plan's four named refusals (absent model version, expired
validation, wrong prediction grain, post-cutoff data)."""
from __future__ import annotations

from dataclasses import replace

import pytest

from featuregen.overlay.upload.model_feature_contract import (
    ModelFeatureContractError,
    ModelFeatureSpecV1,
    model_feature_revision_hash,
)
from featuregen.overlay.upload.model_feature_readiness import (
    BLOCKER_MODEL_VERSION_ABSENT,
    BLOCKER_POST_CUTOFF_DATA,
    BLOCKER_PREDICTION_GRAIN_MISMATCH,
    BLOCKER_VALIDATION_ABSENT,
    BLOCKER_VALIDATION_EXPIRED,
    MODEL_READINESS_LADDER,
    ModelFeatureReadinessV1,
    ModelReadinessInputsV1,
    fold_model_readiness,
)
from featuregen.overlay.upload.model_feature_registry import (
    validate_model_feature_registry,
)

_SPEC = ModelFeatureSpecV1(
    model_feature_id="churn_propensity_90d",
    revision=1,
    model_family="propensity",
    model_ref="model:churn-gbm",
    model_version="3.2.1",
    owner="team:retail-analytics",
    prediction_grain="customer",
    prediction_timestamp_role="scored_at",
    training_data_cutoff_policy="features strictly before the label window opens",
    inference_knowledge_time_policy="latest-known-at-scoring",
    target_definition="no qualifying activity in the 90 days after the as-of",
    outcome_window_days=90,
    input_feature_set_revision="sha256:inputpack",
    input_recipe_revisions=("sha256:dormancy", "sha256:balance_slope"),
    permitted_purposes=("retention_targeting",))

_READY = ModelReadinessInputsV1(validation_current=True, inference_grain="customer",
                                inference_capability_proven=True)


def test_the_spec_constructs_and_its_revision_hash_moves_with_meaning():
    base = model_feature_revision_hash(_SPEC)
    assert base == model_feature_revision_hash(_SPEC)
    edited = replace(_SPEC, target_definition="a different label entirely")
    assert model_feature_revision_hash(edited) != base, \
        "editing the target stales every governance approval by lookup miss — like recipes"


def test_construction_refuses_the_unusable():
    with pytest.raises(ModelFeatureContractError, match="rumor"):
        replace(_SPEC, model_ref="  ")
    with pytest.raises(ModelFeatureContractError, match="as-of"):
        replace(_SPEC, prediction_timestamp_role="")
    with pytest.raises(ModelFeatureContractError, match="inputs nobody can name"):
        replace(_SPEC, input_feature_set_revision="")
    with pytest.raises(ModelFeatureContractError, match="model_family"):
        replace(_SPEC, model_family="astrology")


def test_the_four_named_refusals():
    # absent model version — a spec may exist before registration; readiness says BLOCKED
    unregistered = fold_model_readiness(replace(_SPEC, model_version=""), _READY)
    assert (unregistered.state, unregistered.blockers) == (
        "MODEL_SPEC_BLOCKED", (BLOCKER_MODEL_VERSION_ABSENT,))
    # expired validation — distinguished from never-validated
    expired = fold_model_readiness(_SPEC, replace(_READY, validation_current=False,
                                                  validation_expired=True))
    assert (expired.state, expired.blockers) == (
        "MODEL_REGISTERED", (BLOCKER_VALIDATION_EXPIRED,))
    never = fold_model_readiness(_SPEC, replace(_READY, validation_current=False))
    assert never.blockers == (BLOCKER_VALIDATION_ABSENT,)
    # wrong prediction grain
    wrong_grain = fold_model_readiness(_SPEC, replace(_READY, inference_grain="account"))
    assert BLOCKER_PREDICTION_GRAIN_MISMATCH in wrong_grain.blockers
    # post-cutoff training/inference data
    leaky = fold_model_readiness(_SPEC, replace(_READY, data_within_cutoffs=False))
    assert BLOCKER_POST_CUTOFF_DATA in leaky.blockers


def test_the_ladder_top_and_the_input_pack_rule():
    assert fold_model_readiness(_SPEC, _READY) == ModelFeatureReadinessV1("INFERENCE_READY")
    # a valid deterministic input pack does NOT imply approval: same spec, no validation verdict
    assert fold_model_readiness(_SPEC, ModelReadinessInputsV1()).state == "MODEL_REGISTERED"
    assert "UNASSESSED" not in MODEL_READINESS_LADDER


def test_registry_law():
    validate_model_feature_registry((_SPEC,))
    with pytest.raises(ModelFeatureContractError, match="duplicate"):
        validate_model_feature_registry((_SPEC, _SPEC))
