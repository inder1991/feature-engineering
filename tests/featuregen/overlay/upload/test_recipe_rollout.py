"""BR-24 — rollout controls: frozen defaults, per-family promotion, the canary fold."""
from __future__ import annotations

from featuregen.overlay.upload.recipe_rollout import (
    FLAG_DEFAULTS,
    CanaryGateInputsV1,
    RecipeRolloutConfig,
    canary_gate,
    rollout_metrics,
    rollout_stage,
)


def test_the_frozen_configuration_defaults_encode_the_reached_stage():
    """The frozen-configuration pin: v3 available by explicit query (stage 3 — today's
    behavior), everything beyond it OFF. Changing a default is a reviewed rollout decision."""
    assert FLAG_DEFAULTS == {
        "FEATUREGEN_RECIPE_CONTRACT_V2": False,
        "FEATUREGEN_FORMULA_V2": False,
        "FEATUREGEN_SUGGESTION_CONTRACT_V3": True,
        "FEATUREGEN_RECIPE_V2_MATERIALIZATION": False,
    }
    config = RecipeRolloutConfig()
    assert rollout_stage(config) == 3
    assert config.active_families == () and config.canary_catalogs == ()
    # SE-14: the semantic-planning MODE is frozen at `legacy` — a fresh deployment runs
    # today's pipeline; promotion to shadow or v1 is a reviewed decision, never a default.
    assert config.semantic_planning == "legacy"


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


def test_promotion_is_per_family_and_per_catalog_never_aggregate():
    config = RecipeRolloutConfig(recipe_contract_v2=True,
                                 active_families=("retail_churn",),
                                 canary_catalogs=("core_banking",))
    assert config.family_active("retail_churn")
    assert not config.family_active("credit_risk")          # flag on, family not promoted
    assert config.catalog_in_canary("core_banking")
    assert not config.catalog_in_canary("other_catalog")
    # the flag off disables activation WITHOUT touching the registry (rollback shape):
    off = RecipeRolloutConfig(active_families=("retail_churn",))
    assert not off.family_active("retail_churn")
    assert rollout_stage(config) == 5


def test_env_parsing_and_stage_climbing():
    import os
    from unittest import mock

    with mock.patch.dict(os.environ, {
            "FEATUREGEN_RECIPE_CONTRACT_V2": "on",
            "FEATUREGEN_FORMULA_V2": "true",
            "FEATUREGEN_RECIPE_V2_FAMILIES": "retail_churn, transaction_foundation",
    }, clear=False):
        config = RecipeRolloutConfig.from_env()
    assert config.recipe_contract_v2 and config.formula_v2
    assert config.active_families == ("retail_churn", "transaction_foundation")
    assert rollout_stage(config) == 6
    assert rollout_stage(RecipeRolloutConfig(recipe_v2_materialization=True)) == 7
    assert rollout_stage(RecipeRolloutConfig(suggestion_contract_v3=False)) == 1


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
