"""BR-24 — the rollout controls: flags, allowlists, the stage report and the canary gate.

Everything here is CONFIG and pure folds — flipping a flag changes what serving paths consult,
never what the registry contains (rollback disables activation without deleting a single V2
revision, structurally: this module holds no state and mutates nothing).

**The flags and their honest defaults.** Defaults encode the stage the platform has actually
reached, so a fresh deployment behaves exactly as today: contract v3 is AVAILABLE by explicit
query (stage 3 — the BR-8/BR-17 behavior); serving suggestions FROM the V2 registry, Formula-v2
authoring and V2 materialization are OFF (stages 5-7 are promotions, each behind its own flag
AND the per-family/per-catalog allowlists). The frozen-configuration test pins these defaults —
changing one is a reviewed rollout decision, never a drive-by.

**Promotion is per-family, per-catalog.** A family is active only when its flag is on AND the
family is allowlisted; a catalog receives canary behavior only when allowlisted. An aggregate
pass rate promotes nothing (the plan's rule) — the allowlist IS the per-family decision record.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

#: The six feature controls, with the stage-honest defaults described above.
FLAG_DEFAULTS: dict[str, bool] = {
    "FEATUREGEN_RECIPE_CONTRACT_V2": False,        # stage 5: V2 serves suggestions
    "FEATUREGEN_FORMULA_V2": False,                # stage 6: v2 authoring enabled
    "FEATUREGEN_SUGGESTION_CONTRACT_V3": True,     # stage 3: v3 by explicit query — TODAY
    "FEATUREGEN_RECIPE_V2_MATERIALIZATION": False,  # stage 7: approved-recipe execution
}


#: SE-14 (semantic-eligibility program) — the pipeline mode for hypothesis feature generation.
#: A closed-string MODE, not a boolean: it joins this config the way the CSV allowlists did —
#: its own typed field with its own parser and its own frozen default. `legacy` is today's
#: pipeline; `semantic_shadow` adds the deterministic semantic-plan comparison beside the
#: unchanged user response; `semantic_v1` serves the typed-intent + shared-binding response.
SEMANTIC_PLANNING_MODES = ("legacy", "semantic_shadow", "semantic_v1")
SEMANTIC_PLANNING_DEFAULT = "legacy"


def _truthy(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _closed_mode(raw: str | None) -> str:
    """An unknown or absent value falls back to the frozen default — a typo in an env var must
    degrade to today's behavior, never raise at import or silently enable a promotion."""
    value = (raw or "").strip().lower()
    return value if value in SEMANTIC_PLANNING_MODES else SEMANTIC_PLANNING_DEFAULT


def _csv(raw: str | None) -> tuple[str, ...]:
    return tuple(part.strip() for part in (raw or "").split(",") if part.strip())


@dataclass(frozen=True, slots=True)
class RecipeRolloutConfig:
    """One immutable read of the rollout environment."""

    recipe_contract_v2: bool = FLAG_DEFAULTS["FEATUREGEN_RECIPE_CONTRACT_V2"]
    formula_v2: bool = FLAG_DEFAULTS["FEATUREGEN_FORMULA_V2"]
    suggestion_contract_v3: bool = FLAG_DEFAULTS["FEATUREGEN_SUGGESTION_CONTRACT_V3"]
    recipe_v2_materialization: bool = FLAG_DEFAULTS["FEATUREGEN_RECIPE_V2_MATERIALIZATION"]
    active_families: tuple[str, ...] = field(default=())
    canary_catalogs: tuple[str, ...] = field(default=())
    semantic_planning: str = SEMANTIC_PLANNING_DEFAULT

    @classmethod
    def from_env(cls) -> RecipeRolloutConfig:
        env = os.environ
        return cls(
            semantic_planning=_closed_mode(env.get("FEATUREGEN_SEMANTIC_PLANNING")),
            recipe_contract_v2=_truthy(env.get("FEATUREGEN_RECIPE_CONTRACT_V2"),
                                       FLAG_DEFAULTS["FEATUREGEN_RECIPE_CONTRACT_V2"]),
            formula_v2=_truthy(env.get("FEATUREGEN_FORMULA_V2"),
                               FLAG_DEFAULTS["FEATUREGEN_FORMULA_V2"]),
            suggestion_contract_v3=_truthy(
                env.get("FEATUREGEN_SUGGESTION_CONTRACT_V3"),
                FLAG_DEFAULTS["FEATUREGEN_SUGGESTION_CONTRACT_V3"]),
            recipe_v2_materialization=_truthy(
                env.get("FEATUREGEN_RECIPE_V2_MATERIALIZATION"),
                FLAG_DEFAULTS["FEATUREGEN_RECIPE_V2_MATERIALIZATION"]),
            active_families=_csv(env.get("FEATUREGEN_RECIPE_V2_FAMILIES")),
            canary_catalogs=_csv(env.get("FEATUREGEN_RECIPE_V2_CANARY_CATALOGS")))

    def family_active(self, family: str) -> bool:
        """Per-family promotion: the flag AND the allowlist — never an aggregate pass."""
        return self.recipe_contract_v2 and family in self.active_families

    def catalog_in_canary(self, catalog_source: str) -> bool:
        return self.recipe_contract_v2 and catalog_source in self.canary_catalogs


def rollout_stage(config: RecipeRolloutConfig) -> int:
    """The stage the configuration HONESTLY encodes (the plan's ten stages). Registry
    population (stage 2) is a source-control fact, so a default config sits at stage 3;
    promotions climb only as their flags and allowlists turn on."""
    if config.recipe_v2_materialization:
        return 7
    if config.formula_v2:
        return 6
    if config.recipe_contract_v2 and (config.active_families or config.canary_catalogs):
        return 5
    if config.suggestion_contract_v3:
        return 3
    return 1


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
