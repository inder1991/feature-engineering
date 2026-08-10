"""BR-7A — model-feature readiness: its own closed ladder, fed by governance VERDICTS.

The states the plan names, and what each requires — mirroring BR-7's discipline (typed inputs
from deterministic gates; defaults are the honest floor; nothing promotes without its input):

    MODEL_SPEC_BLOCKED   the spec itself names an unresolved authority (no registered version,
                         or a governance verdict says the declaration is violated)
    MODEL_REGISTERED     a model version is registered; validation not current
    MODEL_VALIDATED      + a CURRENT model-risk validation decision covers THIS revision
    INFERENCE_READY      + inference capability proven at the declared grain, on data inside
                         the declared cutoffs
    MODEL_RETIRED        kept for resolution only

A valid deterministic input pack does NOT imply the model is approved (the plan's rule): the
input recipes' readiness and the model's readiness are separate ladders that meet only in
contract v3's presentation.
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.overlay.upload.model_feature_contract import ModelFeatureSpecV1

MODEL_READINESS_LADDER = ("MODEL_SPEC_BLOCKED", "MODEL_REGISTERED", "MODEL_VALIDATED",
                          "INFERENCE_READY", "MODEL_RETIRED")

BLOCKER_MODEL_VERSION_ABSENT = "model_version_absent"
BLOCKER_VALIDATION_ABSENT = "model_risk_validation_absent"
BLOCKER_VALIDATION_EXPIRED = "model_risk_validation_expired"
BLOCKER_PREDICTION_GRAIN_MISMATCH = "prediction_grain_mismatch"
BLOCKER_POST_CUTOFF_DATA = "training_or_inference_data_post_cutoff"
BLOCKER_INFERENCE_CAPABILITY_UNPROVEN = "inference_capability_unproven"


@dataclass(frozen=True, slots=True)
class ModelReadinessInputsV1:
    """Verdicts from the governance and inference layers — never re-derived here."""

    retired: bool = False
    validation_current: bool = False       # a model-risk decision covers THIS revision, unexpired
    validation_expired: bool = False       # a decision existed and lapsed (vs never existed)
    inference_grain: str = ""              # the grain the inference run actually produces
    data_within_cutoffs: bool = True       # the auditor's verdict on training/inference data
    inference_capability_proven: bool = False


@dataclass(frozen=True, slots=True)
class ModelFeatureReadinessV1:
    state: str
    blockers: tuple[str, ...] = ()


def fold_model_readiness(spec: ModelFeatureSpecV1,
                         inputs: ModelReadinessInputsV1) -> ModelFeatureReadinessV1:
    if inputs.retired:
        return ModelFeatureReadinessV1("MODEL_RETIRED")

    blockers: list[str] = []
    if not spec.model_version.strip():
        blockers.append(BLOCKER_MODEL_VERSION_ABSENT)
    if inputs.inference_grain and inputs.inference_grain != spec.prediction_grain:
        blockers.append(BLOCKER_PREDICTION_GRAIN_MISMATCH)
    if not inputs.data_within_cutoffs:
        blockers.append(BLOCKER_POST_CUTOFF_DATA)
    if blockers:
        return ModelFeatureReadinessV1("MODEL_SPEC_BLOCKED", blockers=tuple(blockers))

    if not inputs.validation_current:
        blocker = (BLOCKER_VALIDATION_EXPIRED if inputs.validation_expired
                   else BLOCKER_VALIDATION_ABSENT)
        return ModelFeatureReadinessV1("MODEL_REGISTERED", blockers=(blocker,))
    if not inputs.inference_capability_proven:
        return ModelFeatureReadinessV1("MODEL_VALIDATED",
                                       blockers=(BLOCKER_INFERENCE_CAPABILITY_UNPROVEN,))
    return ModelFeatureReadinessV1("INFERENCE_READY")
