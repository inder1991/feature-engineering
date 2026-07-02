from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from featuregen.intake.scoring import SAFE_SOURCES

# Config-gated defaults (Decision 4), deliberately conservative — fail toward asking.
_DEFAULT_AMBIGUITY_MAX = 0.30
_DEFAULT_CONFIDENCE_MIN = 0.70


@dataclass(frozen=True, slots=True)
class RouterThresholds:
    ambiguity_max: float = _DEFAULT_AMBIGUITY_MAX
    confidence_min: float = _DEFAULT_CONFIDENCE_MIN


def default_thresholds() -> RouterThresholds:
    """Env-overridable thresholds (config-gated, spec §6.2). Bad/absent env values fall back to the
    conservative defaults."""

    def _f(name: str, default: float) -> float:
        try:
            return float(os.environ[name])
        except (KeyError, ValueError):
            return default

    return RouterThresholds(
        ambiguity_max=_f("FEATUREGEN_DOUBT_AMBIGUITY_MAX", _DEFAULT_AMBIGUITY_MAX),
        confidence_min=_f("FEATUREGEN_DOUBT_CONFIDENCE_MIN", _DEFAULT_CONFIDENCE_MIN),
    )


def route_field(
    *,
    ambiguity: float,
    confidence: float,
    source: str,
    has_value: bool,
    policy_sensitive: bool,
    is_calculation_method_choice: bool,
    thresholds: RouterThresholds | None = None,
) -> str:
    """One deterministic decision per field (spec §6.2):

        auto-resolve iff ambiguity ≤ max AND confidence ≥ min
                     AND a safe value exists (source ∈ SAFE_SOURCES and has_value)
                     AND the field is NOT policy-sensitive
                     AND the field is NOT a calculation-method CHOICE
        otherwise → must-ask-human

    Policy-sensitive fields and calc-method choices are must-ask REGARDLESS of score — they may never
    be auto-resolved (§6.2). Biased toward asking."""
    t = thresholds or default_thresholds()
    if policy_sensitive:
        return "human"
    if is_calculation_method_choice:
        return "human"
    if not has_value or source not in SAFE_SOURCES:
        return "human"
    if ambiguity <= t.ambiguity_max and confidence >= t.confidence_min:
        return "auto"
    return "human"


def _has_value(field: str, open_fields: Iterable[str]) -> bool:
    # An UNKNOWN sub-path (e.g. "filters.declined_status_encoding") stales its whole scored field
    # ("filters"): the field has no safe value until the sub-path is resolved.
    return not any(of == field or of.startswith(field + ".") for of in open_fields)


def route_draft(
    field_scores: Mapping[str, Mapping],
    open_fields: Iterable[str],
    *,
    mode: str,
    policy_sensitive_fields: Iterable[str] = (),
    thresholds: RouterThresholds | None = None,
) -> dict[str, str]:
    """Route every scored field. In hypothesis mode the `calculation_method` field is always a
    must-ask CHOICE (§6.3); in definition mode it is a faithful translation and may auto-resolve."""
    t = thresholds or default_thresholds()
    open_list = list(open_fields)
    policy = set(policy_sensitive_fields)
    decisions: dict[str, str] = {}
    for field, sc in field_scores.items():
        decisions[field] = route_field(
            ambiguity=float(sc["ambiguity"]),
            confidence=float(sc["confidence"]),
            source=str(sc.get("source", "llm")),
            has_value=_has_value(field, open_list),
            policy_sensitive=field in policy,
            is_calculation_method_choice=(mode == "hypothesis" and field == "calculation_method"),
            thresholds=t,
        )
    return decisions
