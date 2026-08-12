"""BR-7A — the governed model-feature contract: a prediction is not a formula.

Propensities, forecasts, anomaly scores, ECL stages, VaR — the plan's review found them sitting
in the recipe library dressed as deterministic computations, one prose line from being believed.
This contract gives them their own typed home with the properties a MODEL output actually needs
declared: whose prediction, at what grain and timestamp, trained on what cutoff, targeting what
label over what window, scored how, validated by whom until when, monitored how, and falling back
to what. None of that fits a ``FormulaReferenceV2``, and none of it may be inferred from recipe
metadata — plan invariant: model performance, calibration and fairness are FACTS from governance
records, never vibes from a description.

Deterministic PREPROCESSING inputs may be Formula-v2 recipes — ``input_recipe_revisions`` is the
lineage — but the prediction itself lives here, and BR-7's readiness fold refuses to ladder it:
``model_feature_readiness`` owns these states.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum

CANONICAL_MODEL_FEATURE_VERSION = "canonical-model-feature-v1"

MODEL_FAMILIES = ("propensity", "probability_of_event", "projection", "anomaly_score",
                  "typology_score", "accounting_model_output", "risk_model_output",
                  "uplift", "loading")
SCORE_TYPES = ("probability", "score", "amount", "bucket", "rate")


class ModelFeatureContractError(ValueError):
    """An invalid spec — refused at construction, exactly like RecipeDefinitionV2."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ModelFeatureContractError(message)


@dataclass(frozen=True, slots=True)
class ModelFeatureSpecV1:
    """One governed model output. Every field is a DECLARATION a governance record can be held
    against — the readiness fold consumes verdicts about these, never re-derives them."""

    model_feature_id: str
    revision: int
    model_family: str                    # MODEL_FAMILIES
    model_ref: str                       # the registered model identity (registry key)
    model_version: str                   # "" = not yet registered — SPEC_BLOCKED, honestly
    owner: str
    prediction_grain: str                # customer | account | facility | ...
    prediction_timestamp_role: str       # when the score is AS OF
    training_data_cutoff_policy: str     # how training data is bounded
    inference_knowledge_time_policy: str
    target_definition: str               # the label, in reviewed words
    outcome_window_days: int
    input_feature_set_revision: str      # the deterministic input pack's revision hash
    input_recipe_revisions: tuple[str, ...] = ()   # lineage to Formula-v2 recipe revisions
    population: str = ""
    exclusions: str = ""
    permitted_purposes: tuple[str, ...] = ()
    score_type: str = "probability"      # SCORE_TYPES
    calibration_policy: str = ""
    valid_range: str = ""
    fairness_controls: str = ""
    privacy_controls: str = ""
    monitoring_policy: str = ""
    fallback_policy: str = ""
    leakage_classification: str = "near_label"   # model outputs default near-label

    def __post_init__(self) -> None:
        _require(bool(self.model_feature_id.strip()), "model_feature_id is mandatory")
        _require(self.revision >= 1, "revision starts at 1")
        _require(self.model_family in MODEL_FAMILIES,
                 f"model_family {self.model_family!r} not in {MODEL_FAMILIES}")
        _require(self.score_type in SCORE_TYPES,
                 f"score_type {self.score_type!r} not in {SCORE_TYPES}")
        _require(bool(self.model_ref.strip()), "model_ref is mandatory — a prediction without "
                 "a registered model identity is a rumor")
        _require(bool(self.owner.strip()), "an owner is mandatory")
        _require(bool(self.prediction_grain.strip()), "prediction_grain is mandatory")
        _require(bool(self.prediction_timestamp_role.strip()),
                 "prediction_timestamp_role is mandatory — a score without an as-of is unusable")
        _require(bool(self.training_data_cutoff_policy.strip()),
                 "training_data_cutoff_policy is mandatory")
        _require(bool(self.target_definition.strip()), "target_definition is mandatory")
        _require(self.outcome_window_days >= 0, "outcome_window_days cannot be negative")
        _require(bool(self.input_feature_set_revision.strip()),
                 "input_feature_set_revision is mandatory — a model whose inputs nobody can "
                 "name cannot be reasoned about")
        _require(self.leakage_classification in ("near_label", "outcome", "standard"),
                 "unknown leakage classification")


def _plain(value):
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name))
                for f in sorted(fields(value), key=lambda f: f.name)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_plain(v) for v in value]
    return value


def canonical_model_feature(spec: ModelFeatureSpecV1) -> dict:
    return {"version": CANONICAL_MODEL_FEATURE_VERSION, "spec": _plain(spec)}


def model_feature_revision_hash(spec: ModelFeatureSpecV1) -> str:
    """The revision identity — governance approvals key on it, so editing the target definition
    or the input pack stales every approval by lookup miss, exactly like recipes."""
    body = json.dumps(canonical_model_feature(spec), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()
