"""Rollout controls (post-cutover slim form): the canary fold and the metrics — no modes."""
from __future__ import annotations

import pathlib

from featuregen.overlay.upload.recipe_rollout import (
    CanaryGateInputsV1,
    canary_gate,
    rollout_metrics,
)


def test_no_pipeline_mode_survives_the_cutover():
    """E4 (2026-08-14): the semantic-planning MODE and its whole carrier are DELETED, not
    pinned to `semantic_v1`. A lever with one position is a lie about what a deployment can
    choose, and a parser still reading `FEATUREGEN_SEMANTIC_PLANNING` would let a stale
    manifest look meaningful. This pin fails if any of it comes back — and it is the unit-level
    half of the acceptance that the env var appears nowhere in `src/`."""
    from featuregen.overlay.upload import recipe_rollout

    for gone in ("RecipeRolloutConfig", "SEMANTIC_PLANNING_MODES", "SEMANTIC_PLANNING_DEFAULT",
                 "_closed_mode", "semantic_planning_gate", "SemanticPlanningGateInputsV1"):
        assert not hasattr(recipe_rollout, gone), gone
    assert "FEATUREGEN_SEMANTIC_PLANNING" not in pathlib.Path(
        recipe_rollout.__file__).read_text()


def test_the_retired_br24_levers_stay_retired():
    """Pre-live simplification (2026-08-11): the unconsumed BR-24 flag family is GONE, not
    dormant — a config field with no runtime consumer misleads operators. This pin fails if
    someone reintroduces a lever without a consumer and a reviewed reason."""
    from featuregen.overlay.upload import recipe_rollout

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
