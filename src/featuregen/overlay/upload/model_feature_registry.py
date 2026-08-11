"""BR-7A — the model-feature registry: empty until the family migrations reclassify the
propensity / projection / anomaly / model-risk recipes (BR-11..BR-16). Registry law at import,
exactly like recipe_registry_v2: unique ids, and a lookup the recipe contract's
``governed_model_output`` recipes resolve their ``model_feature_ref`` against at the BR-17
cutover (an unresolvable ref is a cutover validation failure, not a construction one — the two
registries populate in different tasks and must not import-cycle)."""
from __future__ import annotations

from featuregen.overlay.upload.model_feature_contract import (
    ModelFeatureContractError,
    ModelFeatureSpecV1,
)
from featuregen.overlay.upload.recipes.cross_sell import CROSS_SELL_MODEL_FEATURES

MODEL_FEATURES: tuple[ModelFeatureSpecV1, ...] = (*CROSS_SELL_MODEL_FEATURES,)


def validate_model_feature_registry(
        specs: tuple[ModelFeatureSpecV1, ...] = MODEL_FEATURES) -> None:
    seen: set[str] = set()
    for spec in specs:
        if spec.model_feature_id in seen:
            raise ModelFeatureContractError(
                f"duplicate model feature id {spec.model_feature_id!r}")
        seen.add(spec.model_feature_id)


def model_feature_by_id(ref: str,
                        specs: tuple[ModelFeatureSpecV1, ...] = MODEL_FEATURES,
                        ) -> ModelFeatureSpecV1 | None:
    return next((s for s in specs if s.model_feature_id == ref), None)


validate_model_feature_registry()
