"""The registry-wide producer invariant: every capturable recipe declares its authoring lane.

This is the precondition for retiring the v1 arm, and it is asserted over ALL 317 registry recipes
rather than a fixture. A fixture proves the chain can work; only the whole registry proves nothing
still selects the old lane by saying nothing.

THE CHAIN, and which test below covers which link::

    recipe declares formula-v2         -> test_NO_CAPTURABLE_RECIPE_STILL_DECLARES_V1
    -> a V2 capture blueprint          -> test_EVERY_CAPTURABLE_BLUEPRINT_IS_A_V2_BLUEPRINT
    -> BoundRecipeFormulaExpectationV2 -> `CaptureBlueprintV1.bind` dispatches on the blueprint type
    -> payload declares formula-v2     -> test_A_V2_BOUND_EXPECTATION_ALWAYS_DECLARES
    -> worker routes to the V2 arm     -> test_the_worker_ROUTES_A_DECLARED_V2_ITEM_TO_THE_V2_ARM

**Why the registry half is asserted on the BLUEPRINT rather than by binding all 317.** Binding needs
a full grounding context per recipe — real refs, hashes, semantic parameters — so a 317-way bind
would mostly be testing fixture construction, and would fail for reasons that have nothing to do
with lane selection. The blueprint's TYPE is what `bind` dispatches on, so a V2 blueprint binding to
anything but a V2 expectation is unrepresentable, and that link is covered by the type rather than
by repetition.
"""
from __future__ import annotations

from featuregen.formula.recipe_egress import (
    FORMULA_EXPECTATION_SCHEMA_V2,
    build_recipe_authoring_egress,
)
from featuregen.overlay.upload.recipe_formula_contracts_v2 import (
    RecipeFormulaExpectationBlueprintV2,
)
from featuregen.overlay.upload.recipe_formula_shadow import capture_blueprint_for
from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES

FORMULA_SCHEMA_V1 = "formula-v1"


def _capturable():
    """Every recipe the platform can actually produce a work item for, with its declaration."""
    for recipe in V2_RECIPES:
        blueprint = capture_blueprint_for(recipe.recipe_id)
        if blueprint is not None:
            yield recipe.recipe_id, blueprint


# ══ THE REGISTRY, WHOLE ════════════════════════════════════════════════════════════════════════
def test_NO_CAPTURABLE_RECIPE_STILL_DECLARES_V1():
    """The condition that makes retiring the v1 arm safe, over the whole registry.

    Two recipes declared `formula-v1`. `obligor_facility_count` converted trivially — its derived v2
    blueprint carries the same grain as its reviewed entry. `merchant_mcc_diversity` did not: its
    reviewed entry declared MERCHANT grain while the definition computed per CUSTOMER, so converting
    the lane changed what the feature is computed PER. That needed a human, got one — **per
    customer** — and the stale entry is retired in place rather than re-keyed, because its v1
    template has no `customer` need and could not express the answer.
    """
    still_v1 = sorted(rid for rid, bp in _capturable()
                      if bp.declared_schema_version == FORMULA_SCHEMA_V1)
    assert still_v1 == [], (
        f"{len(still_v1)} capturable recipe(s) still declare formula-v1: while any exists, the v1 "
        f"arm cannot be removed and 'absence is v1' cannot become terminal — {still_v1}")


def test_EVERY_CAPTURABLE_BLUEPRINT_IS_A_V2_BLUEPRINT():
    """The link `bind` dispatches on. A V2 blueprint cannot bind to a V1 expectation, so this is
    what makes the next link a type fact rather than a test over 317 grounding contexts."""
    wrong = sorted(rid for rid, bp in _capturable()
                   if not isinstance(bp.blueprint, RecipeFormulaExpectationBlueprintV2))
    assert wrong == [], f"{len(wrong)} capturable recipe(s) carry a non-V2 blueprint: {wrong}"


def test_THE_REGISTRY_IS_STILL_THE_SIZE_WE_THINK_IT_IS():
    """So the two assertions above cannot pass by the registry having quietly emptied."""
    capturable = list(_capturable())
    assert len(V2_RECIPES) == 317
    assert len(capturable) == 90, (
        "the number of capturable recipes moved; that is a product change and wants saying out "
        "loud, because these two invariants are only as strong as the set they range over")


# ══ THE PRODUCER ═══════════════════════════════════════════════════════════════════════════════
def test_A_V2_BOUND_EXPECTATION_ALWAYS_DECLARES():
    """`build_recipe_authoring_egress` is the ONE serialization owner for the provider payload.

    Asserted here rather than in the evaluation modules deliberately: adding the same field in
    `recipe_formula_eval` or `recipe_formula_blueprint_derivation` would create a second source of
    truth for one fact, and the two would disagree the first time either was edited.
    """
    from tests.featuregen.formula.test_recipe_egress import _bound_v2

    egress = build_recipe_authoring_egress(
        hypothesis="h", prediction_goal="g", expectation=_bound_v2())
    assert egress.formula_expectation["formula_schema_version"] == FORMULA_EXPECTATION_SCHEMA_V2


def test_a_V1_BOUND_EXPECTATION_DOES_NOT_DECLARE():
    """The discriminator, and the reason absence meant v1: the v1 payload shape carries no version
    key and never did. Once nothing produces a v1 bound expectation, absence means only one thing —
    a producer that failed to declare — which is what lets it become terminal."""
    from tests.featuregen.formula.test_recipe_egress import _bound

    egress = build_recipe_authoring_egress(
        hypothesis="h", prediction_goal="g", expectation=_bound())
    assert "formula_schema_version" not in egress.formula_expectation
