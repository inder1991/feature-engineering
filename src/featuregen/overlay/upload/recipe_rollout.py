"""Rollout controls — pared to what has consumers (pre-live simplification, 2026-08-11).

The BR-24 flag family (`FEATUREGEN_RECIPE_CONTRACT_V2` / `FORMULA_V2` / `MATERIALIZATION`,
the per-family and per-catalog allowlists, and the ten-stage report) is RETIRED: an external
review verified none of those levers had a runtime consumer, the tool is pre-live, and the
user's standing steer is that unconsumed flags are complexity, not safety. The behavior they
reserved arrives by direct cutover (validate in shadow → flip → delete the legacy path), not
staged promotion. What remains:

* ``semantic_planning`` — the semantic-eligibility program's ONE control, itself temporary:
  ``semantic_shadow`` validates the new engine beside the legacy Gate-1 path; once shadow and
  the gold suites hold, the cutover deletes the legacy path AND this mode.
* the ``canary_gate`` fold and ``rollout_metrics`` — pure measurement tools the cutover
  decision consumes; readings default to the failing side so an unmeasured gate blocks.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

#: The pipeline mode for hypothesis feature generation. A closed-string MODE, not a boolean.
#: `legacy` is today's pipeline; `semantic_shadow` adds the deterministic semantic-plan
#: comparison beside the unchanged user response; `semantic_v1` serves the typed-intent +
#: shared-binding response.
SEMANTIC_PLANNING_MODES = ("legacy", "semantic_shadow", "semantic_v1")
SEMANTIC_PLANNING_DEFAULT = "legacy"


def _closed_mode(raw: str | None) -> str:
    """An unknown or absent value falls back to the frozen default — a typo in an env var must
    degrade to today's behavior, never raise at import or silently enable a promotion."""
    value = (raw or "").strip().lower()
    return value if value in SEMANTIC_PLANNING_MODES else SEMANTIC_PLANNING_DEFAULT


@dataclass(frozen=True, slots=True)
class RecipeRolloutConfig:
    """One immutable read of the rollout environment — one field, on purpose."""

    semantic_planning: str = SEMANTIC_PLANNING_DEFAULT

    @classmethod
    def from_env(cls) -> RecipeRolloutConfig:
        return cls(semantic_planning=_closed_mode(os.environ.get("FEATUREGEN_SEMANTIC_PLANNING")))


@dataclass(frozen=True, slots=True)
class CanaryGateInputsV1:
    """The eight plan-named gate readings — each the OUTPUT of a measurement elsewhere,
    defaulting to the failing side so an unmeasured gate blocks rather than passes."""

    ambiguous_required_bindings: int = 1
    pit_compilation_errors: int = 1
    formula_gold_mismatches: int = 1
    read_scope_regressions: int = 1
    latency_within_budget: bool = False
    unexplained_empty_state_increase: bool = True
    unapproved_active_recipes: int = 1
    rollback_tested: bool = False


@dataclass(frozen=True, slots=True)
class CanaryGateVerdictV1:
    passed: bool
    failures: tuple[str, ...]


def canary_gate(inputs: CanaryGateInputsV1) -> CanaryGateVerdictV1:
    """The promotion gate, as a fold with every failure NAMED — a family promotes only on a
    verdict whose failure list is empty, never on an aggregate score."""
    failures = []
    if inputs.ambiguous_required_bindings:
        failures.append(f"ambiguous_required_bindings={inputs.ambiguous_required_bindings}")
    if inputs.pit_compilation_errors:
        failures.append(f"pit_compilation_errors={inputs.pit_compilation_errors}")
    if inputs.formula_gold_mismatches:
        failures.append(f"formula_gold_mismatches={inputs.formula_gold_mismatches}")
    if inputs.read_scope_regressions:
        failures.append(f"read_scope_regressions={inputs.read_scope_regressions}")
    if not inputs.latency_within_budget:
        failures.append("latency_budget_unmet")
    if inputs.unexplained_empty_state_increase:
        failures.append("unexplained_empty_states_increased")
    if inputs.unapproved_active_recipes:
        failures.append(f"unapproved_active_recipes={inputs.unapproved_active_recipes}")
    if not inputs.rollback_tested:
        failures.append("rollback_untested")
    return CanaryGateVerdictV1(passed=not failures, failures=tuple(failures))


@dataclass(frozen=True, slots=True)
class SemanticPlanningGateInputsV1:
    """SE-14's cutover gate readings — ONE member more of the BR-24 gate family, never a
    parallel mechanism. Each field is the OUTPUT of a measurement made elsewhere (the shadow
    observation store, the gold suites, the eval fixtures, an operator's rollback drill) and
    every default is the FAILING side: an unmeasured gate blocks, and the cutover to
    ``semantic_v1`` needs a verdict whose failure list is EMPTY — never an aggregate score.

    The four zero-tolerance release gates are the plan's, verbatim: no protected/target-leaking
    candidate accepted, no identifier served as a generic measure, no event-window feature
    sourced only from a current snapshot, and no clearing decision made from llm/proposed
    metadata where the operand floor is declared-or-governed."""

    protected_or_target_accepted: int = 1
    identifier_as_measure_accepted: int = 1
    snapshot_only_event_features: int = 1
    proposed_cleared_declared_floor: int = 1
    gold_suite_failures: int = 1
    shadow_divergence_unexplained: int = 1
    observation_rows_present: bool = False       # the shadow actually ran on real catalogs
    rollback_tested: bool = False


def semantic_planning_gate(inputs: SemanticPlanningGateInputsV1) -> CanaryGateVerdictV1:
    """The cutover gate, as a fold with every failure NAMED — the same verdict type the canary
    gate returns, because two gate mechanisms is the rollout version of two binders."""
    failures = []
    if inputs.protected_or_target_accepted:
        failures.append(
            f"protected_or_target_accepted={inputs.protected_or_target_accepted}")
    if inputs.identifier_as_measure_accepted:
        failures.append(
            f"identifier_as_measure_accepted={inputs.identifier_as_measure_accepted}")
    if inputs.snapshot_only_event_features:
        failures.append(
            f"snapshot_only_event_features={inputs.snapshot_only_event_features}")
    if inputs.proposed_cleared_declared_floor:
        failures.append(
            f"proposed_cleared_declared_floor={inputs.proposed_cleared_declared_floor}")
    if inputs.gold_suite_failures:
        failures.append(f"gold_suite_failures={inputs.gold_suite_failures}")
    if inputs.shadow_divergence_unexplained:
        failures.append(
            f"shadow_divergence_unexplained={inputs.shadow_divergence_unexplained}")
    if not inputs.observation_rows_present:
        failures.append("no_shadow_observations_recorded")
    if not inputs.rollback_tested:
        failures.append("rollback_untested")
    return CanaryGateVerdictV1(passed=not failures, failures=tuple(failures))


def rollout_metrics() -> dict:
    """The registry-derived operational metrics — readiness described truthfully, and
    suggestion COUNT is deliberately absent (the plan: never use it as success)."""
    from collections import Counter

    from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES
    from featuregen.overlay.upload.taxonomy.coverage import coverage_report

    report = coverage_report()
    readiness = Counter(r.readiness for r in V2_RECIPES)
    return {
        "registry_count_by_readiness": dict(sorted(readiness.items())),
        "recipe_count": len(V2_RECIPES),
        "active_primary_coverage_leaves": sum(
            1 for tier in report["coverage_tier_by_leaf"].values()
            if tier == "AUTHORED_PRIMARY"),
        "executable_primary_coverage_leaves": len(report["executable_covered_leaves"]),
        "gold_linked_authorable_recipes": sorted(
            r.recipe_id for r in V2_RECIPES if r.readiness == "FORMULA_AUTHORABLE"),
    }
