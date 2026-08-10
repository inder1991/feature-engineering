"""BR-2 — the legacy adapter: every un-migrated Template projects as CONCEPTUAL-ONLY, always.

The compatibility law, mechanized: a legacy ``Template`` is an SME idea whose exact computation
was never reviewed — so its V2-era projection is ``conceptual_pattern`` / ``UNASSESSED``, carries
NO formula, and can never be promoted by this adapter. Promotion happens exactly one way: a
reviewed ``RecipeDefinitionV2`` naming the legacy id in ``replaces_legacy_ids``, at which point
:func:`project_recipe` returns THAT definition instead. "Never infer an executable formula from
legacy prose" is not a guideline here — the projection type has no formula field to fill.

``UNASSESSED`` lives on :class:`LegacyRecipeProjectionV1` and NOWHERE else — the V2 contract
forbids it at construction, so the unassessed state cannot leak into the production registry.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from featuregen.overlay.upload.recipe_contract_v2 import RecipeDefinitionV2


@dataclass(frozen=True, slots=True)
class LegacyRecipeProjectionV1:
    """The V2-era face of an un-migrated Template: honest about being an idea, structurally
    incapable of claiming a computation (no formula field exists to fill)."""

    recipe_id: str
    family: str
    intent: str
    computation_kind: str = "conceptual_pattern"
    readiness: str = "UNASSESSED"
    conceptual_reason: str = "legacy Template; no reviewed V2 definition exists"


def project_template(template) -> LegacyRecipeProjectionV1:
    """One Template → its conceptual-only projection. Total and constant-shaped: whatever the
    template claims in prose, the projection is a pattern, never a computation."""
    return LegacyRecipeProjectionV1(
        recipe_id=template.id, family=template.family, intent=template.intent)


def project_recipe(template, v2_by_legacy_id: Mapping[str, RecipeDefinitionV2] | None = None,
                   ) -> RecipeDefinitionV2 | LegacyRecipeProjectionV1:
    """The lookup the plan's compatibility design names: a reviewed V2 replacement wins;
    everything else is conceptual-only. No heuristic aliasing — the mapping is built from
    EXPLICIT ``replaces_legacy_ids`` declarations only."""
    if v2_by_legacy_id:
        replacement = v2_by_legacy_id.get(template.id)
        if replacement is not None:
            return replacement
    return project_template(template)


def v2_by_legacy_id(recipes: Sequence[RecipeDefinitionV2]) -> dict[str, RecipeDefinitionV2]:
    """The explicit replacement index. A legacy id claimed by TWO V2 recipes is a registry error
    (each legacy recipe has one successor set; BR-17's aliases handle one-to-many outputs)."""
    index: dict[str, RecipeDefinitionV2] = {}
    for recipe in recipes:
        for legacy_id in recipe.replaces_legacy_ids:
            if legacy_id in index:
                raise ValueError(
                    f"legacy id {legacy_id!r} replaced by both "
                    f"{index[legacy_id].recipe_id!r} and {recipe.recipe_id!r}")
            index[legacy_id] = recipe
    return index
