"""Rollout controls — pared to what has consumers (pre-live simplification, 2026-08-11).

The BR-24 flag family (`FEATUREGEN_RECIPE_CONTRACT_V2` / `FORMULA_V2` / `MATERIALIZATION`,
the per-family and per-catalog allowlists, and the ten-stage report) is RETIRED: an external
review verified none of those levers had a runtime consumer, the tool is pre-live, and the
user's standing steer is that unconsumed flags are complexity, not safety. The behavior they
reserved arrives by direct cutover (validate in shadow → flip → delete the legacy path), not
staged promotion.

``semantic_planning`` — the semantic-eligibility program's ONE control — was itself temporary
by charter, and E4 (2026-08-14) spent it: the shadow comparison and the gold/eval suites held,
the legacy Gate-1 free-form path is deleted, and the semantic engine is no longer A mode. There
is nothing left to select, so the mode, its closed parser and its ``RecipeRolloutConfig``
carrier are gone rather than pinned to a single value — a lever with one position is a lie
about what a deployment can choose. What remains:

* the ``canary_gate`` fold and ``rollout_metrics`` — pure measurement tools; readings default
  to the failing side so an unmeasured gate blocks. They survive the cutover because they
  measure, they do not switch. ``semantic_planning_gate`` does NOT survive it: a gate exists to
  decide ONE promotion, that promotion has now happened, and a gate over a mode that no longer
  exists can only ever be answered with a fiction.
"""
from __future__ import annotations

from dataclasses import dataclass


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
