"""SE-0 — the semantic-eligibility program's migration baseline, pinned as tests.

Each pin here is TODAY'S behavior, deliberately frozen so the semantic-planning program
(docs/superpowers/plans/2026-08-11-semantic-eligibility-feature-generation-workflow.md) migrates
it knowingly. A pin failing means the boundary moved: either an SE task moved it on purpose
(update the pin in the SAME commit, cite the task) or something drifted by accident (fix the
drift). None of these pins is desired FINAL behavior — that is exactly why they exist.

The companion audit document is docs/architecture/2026-08-11-semantic-eligibility-se0-baseline-audit.md.
"""
from __future__ import annotations

import inspect
from collections import Counter

from featuregen.overlay.upload.recipe_registry_v2 import (
    LEGACY_ALIAS_MAP,
    V2_RECIPES,
)
from featuregen.overlay.upload.templates import ALL_TEMPLATES


def test_the_hypothesis_path_grounds_the_v2_registry_not_legacy_templates():
    """CUT OVER (E4, 2026-08-14) — this pin is updated in the cutover commit, per its own rule.

    The SE-0 baseline pinned Gate-1's recipe lens to the frozen legacy `Template` registry and
    said: "SE-7 cuts this over to atomic V2 recipes — when it does, THIS test is updated in the
    same commit to pin the new source instead." That commit is this one, so the pin now names
    the new source and the two facts that make it the ONLY one:

    1. the builder has no `semantic_mode` parameter left — there is no argument, env var or
       branch by which a caller could ask for the legacy lens instead;
    2. with a catalog and a confirmed scope (what every route request carries) the lens is
       `recipe_planning_lens.v2_recipe_candidates` over `V2_RECIPES`.

    `_template_candidates` still exists and still defaults to `ALL_TEMPLATES` — the per-table
    suggestions page and the scope-less builder call remain its callers — so this asserts what
    the HYPOTHESIS path does, which is the boundary SE-7 actually moved."""
    import featuregen.overlay.upload.contract.gate1 as gate1_mod

    assert "semantic_mode" not in inspect.signature(
        gate1_mod.build_considered_set).parameters
    source = inspect.getsource(gate1_mod.build_considered_set)
    scoped = source[source.index("if catalog_source is not None and scope is not None:"):]
    assert "v2_recipe_candidates" in scoped.split("elif catalog_source is not None:")[0]
    assert "recommend_feature_sets_report" not in source     # the free-form generator is gone


def test_v2_authority_floors_are_uniform_at_baseline():
    """Every one of the registry's operands requires `declared` to suggest and `governed` to
    execute. Load-bearing for the provisional-flood analysis (plan §1): on a catalog whose
    concept evidence is all llm/proposed, NOTHING clears a floor — which is why SE-5 stages
    floor enforcement behind the SE-4b funnel. A non-uniform floor appearing here is an
    authored decision some pack made — re-derive the flood analysis before relying on it."""
    floors = {(op.suggestion_authority, op.execution_authority)
              for recipe in V2_RECIPES for op in recipe.operands}
    assert floors == {("declared", "governed")}


def test_alias_map_covers_every_legacy_template_exactly():
    """BR-17's compatibility seam is complete: every legacy template id has V2 aliases and no
    alias points at a template that does not exist. The hypothesis path (SE-7) starts from
    atomic V2 ids and needs no alias — this pin protects the OLD contracts' resolution."""
    assert set(LEGACY_ALIAS_MAP) == {t.id for t in ALL_TEMPLATES}
    assert all(targets for targets in LEGACY_ALIAS_MAP.values())


def test_registry_population_partitions_by_computation_kind():
    """The registry only grows (317 at SE-0), and every recipe is exactly one of the three
    computation kinds — the planning-request adapter (SE-1) branches on this closed set."""
    kinds = Counter(recipe.computation_kind for recipe in V2_RECIPES)
    assert len(V2_RECIPES) >= 317
    assert sum(kinds.values()) == len(V2_RECIPES)
    assert set(kinds) == {"deterministic_formula", "conceptual_pattern", "governed_model_output"}


def test_every_v2_operand_carries_the_fields_se5_will_enforce():
    """SE-5's shape half enforces operand_class everywhere and allowed_source_grains on every
    EXECUTABLE recipe's operands (the V2 contract mandates them there; conceptual patterns may
    honestly omit grains). Pin that the data is present so enforcement cannot be vacuous."""
    for recipe in V2_RECIPES:
        for op in recipe.operands:
            assert op.operand_class, f"{recipe.recipe_id}:{op.role} has no operand_class"
        if recipe.computation_kind == "deterministic_formula":
            for op in recipe.operands:
                assert op.allowed_source_grains, \
                    f"{recipe.recipe_id}:{op.role} has no allowed_source_grains"
