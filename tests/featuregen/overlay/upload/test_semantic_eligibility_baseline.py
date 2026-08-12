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


def test_hypothesis_path_still_grounds_legacy_templates_today():
    """MIGRATION BASELINE, not desired behavior: Gate-1's recipe lens defaults to the frozen
    legacy Template registry. SE-7 cuts this over to atomic V2 recipes — when it does, THIS
    test is updated in the same commit to pin the new source instead."""
    from featuregen.overlay.upload.contract import gate1

    default = inspect.signature(gate1._template_candidates).parameters["templates"].default
    assert default is ALL_TEMPLATES


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
