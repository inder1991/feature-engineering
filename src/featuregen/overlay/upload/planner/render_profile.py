"""The render layer: how a realized plan becomes files — plus the build-scoped generation
configuration and the single-owner per-member output contract.

Three contracts, three owners (plan §Identity chain):

* :class:`RenderProfileV1` — the rendering machinery's identity (engine, compiler, renderer,
  template versions). Changing a template version re-renders the SAME computation; the
  ``render_digest`` is a separate input to ``sealed_artifact_identity`` precisely so a renderer
  bump never masquerades as a computation change.
* :class:`GenerationConfigurationV1` — the BUILD-scoped configuration: population/spine, target,
  cadence, physical-type policy, the policy realization set, engine settings. It owns NO
  per-feature output field — those were REMOVED to :class:`MemberOutputContractV1`, whose single
  ownership is pinned by test (the two field sets must never intersect).
* :class:`MemberOutputContractV1` — ONE member's output surface: names, empty-window and
  not-applicable values, null-input behavior, physical type, scale, rounding, overflow. It rides
  ``member_execution_input_digest``, never the build configuration.

No field has a default: every output decision (including "the empty window produces NULL") is
written down, never assumed.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from featuregen.materialize.canonical import materialize_hash
from featuregen.overlay.upload.planner.logical_plan_v2 import (
    ContractDefect,
    _enum,
    _non_empty,
    _scalar,
)

__all__ = [
    "GenerationConfigurationV1",
    "MemberOutputContractV1",
    "NullInputBehaviorV1",
    "OverflowPolicyV1",
    "RenderProfileV1",
    "RoundingPolicyV1",
    "generation_configuration_digest",
    "render_digest",
]

RENDER_PROFILE_CONTRACT = "render_profile_v1"
GENERATION_CONFIGURATION_CONTRACT = "generation_configuration_v1"
MEMBER_OUTPUT_CONTRACT = "member_output_contract_v1"


@dataclass(frozen=True, slots=True)
class RenderProfileV1:
    """The rendering machinery's identity. ``template_versions`` are ``(name, version)`` pairs —
    a set keyed by template name (payload sorted, duplicate names refused)."""

    engine: str
    compiler_version: str
    renderer_version: str
    template_versions: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "engine", _non_empty(self.engine, what="engine"))
        object.__setattr__(self, "compiler_version", _non_empty(
            self.compiler_version, what="compiler_version"))
        object.__setattr__(self, "renderer_version", _non_empty(
            self.renderer_version, what="renderer_version"))
        templates = tuple(
            (_non_empty(name, what="template name"),
             _non_empty(version, what=f"template {name!r} version"))
            for name, version in self.template_versions)
        names = [name for name, _ in templates]
        if len(set(names)) != len(names):
            raise ContractDefect(f"template names must be distinct, got {names}")
        object.__setattr__(self, "template_versions", templates)

    def content_payload(self) -> dict[str, Any]:
        return {
            "contract": RENDER_PROFILE_CONTRACT,
            "engine": self.engine,
            "compiler_version": self.compiler_version,
            "renderer_version": self.renderer_version,
            "template_versions": [
                [name, version] for name, version in sorted(self.template_versions)],
        }


def render_digest(profile: RenderProfileV1) -> str:
    """The render stage's digest: sha256 hex over the profile's canonical serialization."""
    if not isinstance(profile, RenderProfileV1):
        raise ContractDefect("render_digest takes a RenderProfileV1")
    return materialize_hash(profile.content_payload())


@dataclass(frozen=True, slots=True)
class GenerationConfigurationV1:
    """The BUILD-scoped generation configuration — and deliberately NOTHING per-feature.

    ``policy_realization_revision_ids`` and ``engine_settings`` are sets (payload sorted,
    duplicates refused); setting values are JSON scalars, never structures a digest could
    order-depend on."""

    population_spine_ref: str
    target_mode: str
    target_ref: str
    cadence: str
    physical_type_policy: str
    policy_realization_revision_ids: tuple[str, ...]
    engine_settings: tuple[tuple[str, Any], ...]

    def __post_init__(self) -> None:
        for name in ("population_spine_ref", "target_mode", "target_ref", "cadence",
                     "physical_type_policy"):
            object.__setattr__(self, name, _non_empty(getattr(self, name), what=name))
        realizations = tuple(_non_empty(r, what="policy_realization_revision_id")
                             for r in self.policy_realization_revision_ids)
        if len(set(realizations)) != len(realizations):
            raise ContractDefect("policy_realization_revision_ids must be distinct")
        object.__setattr__(self, "policy_realization_revision_ids", realizations)
        settings = tuple(
            (_non_empty(key, what="engine setting key"),
             _scalar(value, what=f"engine setting {key!r} value"))
            for key, value in self.engine_settings)
        keys = [key for key, _ in settings]
        if len(set(keys)) != len(keys):
            raise ContractDefect(f"engine setting keys must be distinct, got {keys}")
        object.__setattr__(self, "engine_settings", settings)

    def content_payload(self) -> dict[str, Any]:
        return {
            "contract": GENERATION_CONFIGURATION_CONTRACT,
            "population_spine_ref": self.population_spine_ref,
            "target_mode": self.target_mode,
            "target_ref": self.target_ref,
            "cadence": self.cadence,
            "physical_type_policy": self.physical_type_policy,
            "policy_realization_revision_ids": sorted(self.policy_realization_revision_ids),
            "engine_settings": [[key, value] for key, value in
                                sorted(self.engine_settings, key=lambda kv: kv[0])],
        }


def generation_configuration_digest(configuration: GenerationConfigurationV1) -> str:
    """The generation configuration's digest — the third input to ``build_compilation_digest``."""
    if not isinstance(configuration, GenerationConfigurationV1):
        raise ContractDefect("generation_configuration_digest takes a GenerationConfigurationV1")
    return materialize_hash(configuration.content_payload())


class NullInputBehaviorV1(StrEnum):
    """What a NULL input contributes: propagated, treated as an empty window, or refused."""

    PROPAGATE_NULL = "propagate_null"
    TREAT_AS_EMPTY_WINDOW = "treat_as_empty_window"
    REFUSE = "refuse"


class RoundingPolicyV1(StrEnum):
    HALF_UP = "half_up"
    HALF_EVEN = "half_even"
    NO_ROUNDING = "no_rounding"


class OverflowPolicyV1(StrEnum):
    REFUSE = "refuse"
    NULL_ON_OVERFLOW = "null_on_overflow"


@dataclass(frozen=True, slots=True)
class MemberOutputContractV1:
    """ONE member's output surface — the SINGLE owner of every per-feature output decision.

    ``empty_window_value`` / ``not_applicable_value`` are explicit JSON scalars where ``None``
    is an explicit declared NULL, never an omission: "empty 30-day window produces 0" and
    "empty window produces NULL" are different features and the journey asserts, never assumes,
    which one was declared."""

    output_feature_name: str
    output_column_name: str
    empty_window_value: Any
    not_applicable_value: Any
    null_input_behavior: NullInputBehaviorV1
    physical_type: str
    decimal_scale: int | None
    rounding_policy: RoundingPolicyV1
    overflow_policy: OverflowPolicyV1

    def __post_init__(self) -> None:
        object.__setattr__(self, "output_feature_name", _non_empty(
            self.output_feature_name, what="output_feature_name"))
        object.__setattr__(self, "output_column_name", _non_empty(
            self.output_column_name, what="output_column_name"))
        _scalar(self.empty_window_value, what="empty_window_value")
        _scalar(self.not_applicable_value, what="not_applicable_value")
        object.__setattr__(self, "null_input_behavior", _enum(
            self.null_input_behavior, NullInputBehaviorV1, what="null_input_behavior"))
        object.__setattr__(self, "physical_type", _non_empty(
            self.physical_type, what="physical_type"))
        if self.decimal_scale is not None and (
                not isinstance(self.decimal_scale, int) or isinstance(self.decimal_scale, bool)
                or self.decimal_scale < 0):
            raise ContractDefect(
                f"decimal_scale must be an integer >= 0 or an explicit None, got "
                f"{self.decimal_scale!r}")
        object.__setattr__(self, "rounding_policy", _enum(
            self.rounding_policy, RoundingPolicyV1, what="rounding_policy"))
        object.__setattr__(self, "overflow_policy", _enum(
            self.overflow_policy, OverflowPolicyV1, what="overflow_policy"))

    def content_payload(self) -> dict[str, Any]:
        return {
            "contract": MEMBER_OUTPUT_CONTRACT,
            "output_feature_name": self.output_feature_name,
            "output_column_name": self.output_column_name,
            "empty_window_value": self.empty_window_value,
            "not_applicable_value": self.not_applicable_value,
            "null_input_behavior": self.null_input_behavior.value,
            "physical_type": self.physical_type,
            "decimal_scale": self.decimal_scale,
            "rounding_policy": self.rounding_policy.value,
            "overflow_policy": self.overflow_policy.value,
        }
