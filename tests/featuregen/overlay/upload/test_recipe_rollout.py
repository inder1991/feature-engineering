"""Rollout controls (pre-live slim form): the one mode, the canary fold, the metrics."""
from __future__ import annotations

from featuregen.overlay.upload.recipe_rollout import (
    CanaryGateInputsV1,
    RecipeRolloutConfig,
    canary_gate,
    rollout_metrics,
)


def test_the_frozen_default_is_legacy():
    """The semantic-planning MODE is frozen at `legacy` — a fresh deployment runs today's
    pipeline; moving to shadow or v1 is a reviewed decision, never a default."""
    assert RecipeRolloutConfig().semantic_planning == "legacy"


def test_the_semantic_planning_mode_is_closed_and_degrades_to_legacy():
    import os
    from unittest import mock

    from featuregen.overlay.upload.recipe_rollout import SEMANTIC_PLANNING_MODES

    assert SEMANTIC_PLANNING_MODES == ("legacy", "semantic_shadow", "semantic_v1")
    for raw, expected in (("semantic_shadow", "semantic_shadow"),
                          ("SEMANTIC_V1", "semantic_v1"),
                          ("on", "legacy"),            # a bool-style value is NOT a mode
                          ("typo_mode", "legacy"),
                          ("", "legacy")):
        with mock.patch.dict(os.environ, {"FEATUREGEN_SEMANTIC_PLANNING": raw}, clear=False):
            assert RecipeRolloutConfig.from_env().semantic_planning == expected, raw


def test_the_retired_br24_levers_stay_retired():
    """Pre-live simplification (2026-08-11): the unconsumed BR-24 flag family is GONE, not
    dormant — a config field with no runtime consumer misleads operators. This pin fails if
    someone reintroduces a lever without a consumer and a reviewed reason."""
    from dataclasses import fields

    from featuregen.overlay.upload import recipe_rollout

    assert [f.name for f in fields(RecipeRolloutConfig)] == ["semantic_planning"]
    for retired in ("FLAG_DEFAULTS", "rollout_stage", "family_active", "catalog_in_canary"):
        assert not hasattr(recipe_rollout, retired), retired


def test_the_canary_gate_defaults_to_blocking_and_names_every_failure():
    """An unmeasured gate BLOCKS: the inputs default to the failing side, and the verdict
    names each failure — promotion needs an empty failure list, never a score."""
    default = canary_gate(CanaryGateInputsV1())
    assert not default.passed and len(default.failures) == 8
    passing = canary_gate(CanaryGateInputsV1(
        ambiguous_required_bindings=0, pit_compilation_errors=0,
        formula_gold_mismatches=0, read_scope_regressions=0,
        latency_within_budget=True, unexplained_empty_state_increase=False,
        unapproved_active_recipes=0, rollback_tested=True))
    assert passing.passed and passing.failures == ()
    one_bad = canary_gate(CanaryGateInputsV1(
        ambiguous_required_bindings=0, pit_compilation_errors=0,
        formula_gold_mismatches=0, read_scope_regressions=0,
        latency_within_budget=True, unexplained_empty_state_increase=False,
        unapproved_active_recipes=3, rollback_tested=True))
    assert one_bad.failures == ("unapproved_active_recipes=3",)


def test_the_metrics_describe_readiness_truthfully_without_suggestion_counts():
    metrics = rollout_metrics()
    assert metrics["recipe_count"] >= 317
    assert metrics["registry_count_by_readiness"]["FORMULA_AUTHORABLE"] == 3
    assert metrics["executable_primary_coverage_leaves"] == 0   # honest: gold gates unrun
    assert metrics["active_primary_coverage_leaves"] == 75
    assert "suggestion_count" not in metrics                    # never a success metric
