"""Phase-0 coverage report — which selectable use-case leaves the 153 recipes actually populate.

This inverts :func:`recipe_applicability` over ``ALL_TEMPLATES`` against ``selectable_leaves()`` to
answer, per leaf, *which recipes name it as their primary objective* (and, separately, as a secondary).
It is the human-readable audit behind the Phase-0 exit criteria and is read-only — nothing here touches
``templates.py`` or grounding.

The report distinguishes reviewed primary coverage, supporting-only coverage and true zero coverage.
Release gates decide which active leaves require a primary anchor; membership in a release list never
changes the measured tier. An :attr:`UseCase.intentionally_empty` leaf (a declared-future ``*``
objective) must carry **zero** recipes as primary *and* zero as secondary.

``coverage_report()`` returns:

* ``by_leaf``            — every selectable leaf → the recipe ids whose **primary** is that leaf ([] if none).
* ``secondary_by_leaf``  — every selectable leaf → the recipe ids that list it as a **secondary**.
* ``empty_intentional``  — the intentionally-empty selectable leaves (each must have 0 primary + 0 secondary).
* ``unpopulated``        — non-intentional selectable leaves with 0 primary recipes (informational; sizable).
* ``populated_count``    — how many selectable leaves have >= 1 primary recipe.
* ``leaf_count``         — total selectable leaves.

BR-9 adds the HONEST tiers (:data:`COVERAGE_TIERS` via :func:`coverage_tier` — supporting is never
coverage, a legacy-derived primary is debt), the executable/conceptual split
(``executable_primary_by_leaf`` / ``executable_covered_leaves`` — empty until a recipe passes the
gold gate, and honestly so), and the legacy-debt counters (``legacy_inferred_leaves``,
``legacy_derived_recipe_count`` — the number Task 17 drives to zero).
"""
from __future__ import annotations

from featuregen.overlay.upload import templates
from featuregen.overlay.upload.recipe_formula_expectations import RECIPE_FORMULA_EXPECTATIONS
from featuregen.overlay.upload.recipe_readiness import ReadinessInputsV1, fold_readiness
from featuregen.overlay.upload.taxonomy.recipe_applicability import (
    ApplicabilitySource,
    recipe_applicability,
)
from featuregen.overlay.upload.taxonomy.use_cases import USE_CASE_REGISTRY, selectable_leaves

#: BR-9's closed coverage tiers, in rank order. The names say what the count MEANS: a supporting
#: tag is a relevant INPUT to an objective, never coverage of it, and a legacy-derived primary is
#: migration debt no release gate may accept as owned coverage.
COVERAGE_TIERS = ("AUTHORED_PRIMARY", "AUTHORED_SUPPORTING", "LEGACY_INFERRED",
                  "INTENTIONALLY_EMPTY", "ZERO")

#: What "executable-covered" REQUIRES (the BR-9 acceptance): at least one primary recipe whose
#: execution readiness reached the gold-validated rung. FORMULA_AUTHORABLE is deliberately not
#: enough — an expectation nobody has proven against worked examples is not execution.
EXECUTABLE_READINESS_STATES = ("FORMULA_VALIDATED", "MATERIALIZATION_READY")


def coverage_tier(*, intentionally_empty: bool, authored_primary: bool, legacy_primary: bool,
                  supporting: bool) -> str:
    """One leaf's tier, as a PURE fold over four facts — the differential tests' seam. The order
    IS the semantics: supporting participates only after every primary question is answered, so
    adding a supporting tag can never move a leaf into (or out of) a primary tier."""
    if intentionally_empty:
        return "INTENTIONALLY_EMPTY"
    if authored_primary:
        return "AUTHORED_PRIMARY"
    if legacy_primary:
        return "LEGACY_INFERRED"
    if supporting:
        return "AUTHORED_SUPPORTING"
    return "ZERO"


def execution_readiness_of(recipe_id: str) -> str:
    """A legacy recipe's execution readiness, from the SAME machinery contract v3 renders (BR-7's
    fold over the reviewed expectation registry) — never a parallel opinion. Plain legacy recipes
    are UNASSESSED ideas; the reviewed expectation anchors rest at FORMULA_AUTHORABLE until the
    gold gate is proven."""
    if recipe_id not in RECIPE_FORMULA_EXPECTATIONS:
        return "UNASSESSED"
    return fold_readiness(ReadinessInputsV1(
        computation_kind="deterministic_formula",
        reviewed_expectation=True, grammar_verdict="ok")).state


def coverage_report() -> dict:
    """Invert per-recipe applicability over the selectable-leaf vocabulary into a coverage audit.

    Leaves are reported in ``selectable_leaves()`` (authoring/topological) order and recipe ids in
    ``ALL_TEMPLATES`` order, so the output is stable and diff-friendly. See the module docstring for the
    shape and semantics of every returned key.
    """
    leaves = selectable_leaves()

    by_leaf: dict[str, list[str]] = {leaf: [] for leaf in leaves}
    secondary_by_leaf: dict[str, list[str]] = {leaf: [] for leaf in leaves}
    authored_primary_by_leaf: dict[str, list[str]] = {leaf: [] for leaf in leaves}
    legacy_primary_by_leaf: dict[str, list[str]] = {leaf: [] for leaf in leaves}
    formula_authoring_class_by_recipe: dict[str, str] = {}

    for template in templates.ALL_TEMPLATES:
        spec = recipe_applicability(template)
        # primary is always a selectable leaf (Task-4 guarantee), so the key exists.
        by_leaf[spec.primary].append(template.id)
        target = (
            authored_primary_by_leaf
            if spec.source is ApplicabilitySource.AUTHORED
            else legacy_primary_by_leaf
        )
        target[spec.primary].append(template.id)
        for leaf in spec.secondary:
            if leaf in secondary_by_leaf:            # secondary is always a selectable leaf too
                secondary_by_leaf[leaf].append(template.id)
        formula_authoring_class_by_recipe[template.id] = (
            template.formula_authoring_class.value)

    empty_intentional = [
        leaf for leaf in leaves if USE_CASE_REGISTRY[leaf].intentionally_empty]
    unpopulated = [
        leaf for leaf in leaves
        if not by_leaf[leaf] and not USE_CASE_REGISTRY[leaf].intentionally_empty]
    populated_count = sum(1 for leaf in leaves if by_leaf[leaf])
    effective_by_leaf = {
        leaf: list(dict.fromkeys([*by_leaf[leaf], *secondary_by_leaf[leaf]]))
        for leaf in leaves
    }
    active_zero_effective = [
        leaf for leaf in leaves
        if not effective_by_leaf[leaf] and not USE_CASE_REGISTRY[leaf].intentionally_empty
    ]
    coverage_quality_tier_by_leaf = {
        leaf: (
            "ZERO"
            if not effective_by_leaf[leaf]
            else "MINIMUM_ANCHOR"
            if by_leaf[leaf]
            else "SUPPORTING_ONLY"
        )
        for leaf in leaves
    }
    formula_deferred_requirements_by_leaf = {
        leaf: sorted({
            template.formula_authoring_class.value
            for template in templates.ALL_TEMPLATES
            if template.id in effective_by_leaf[leaf]
            and template.formula_authoring_class.value
            not in {"formula_v1_authorable", "unassessed"}
        })
        for leaf in leaves
    }

    # ── BR-9: the honest tiers, the executable/conceptual split, and legacy debt ────────────────
    coverage_tier_by_leaf = {
        leaf: coverage_tier(
            intentionally_empty=USE_CASE_REGISTRY[leaf].intentionally_empty,
            authored_primary=bool(authored_primary_by_leaf[leaf]),
            legacy_primary=bool(legacy_primary_by_leaf[leaf]),
            supporting=bool(secondary_by_leaf[leaf]))
        for leaf in leaves
    }
    execution_readiness_by_recipe = {
        template.id: execution_readiness_of(template.id) for template in templates.ALL_TEMPLATES}
    executable_primary_by_leaf = {
        leaf: [rid for rid in by_leaf[leaf]
               if execution_readiness_by_recipe[rid] in EXECUTABLE_READINESS_STATES]
        for leaf in leaves
    }
    executable_covered_leaves = [leaf for leaf in leaves if executable_primary_by_leaf[leaf]]
    conceptual_only_covered_leaves = [
        leaf for leaf in leaves if by_leaf[leaf] and not executable_primary_by_leaf[leaf]]
    legacy_inferred_leaves = [
        leaf for leaf in leaves if coverage_tier_by_leaf[leaf] == "LEGACY_INFERRED"]
    legacy_derived_recipe_count = sum(
        1 for template in templates.ALL_TEMPLATES
        if recipe_applicability(template).source is ApplicabilitySource.LEGACY_DERIVED)

    return {
        # BR-9 keys. `coverage_tier_by_leaf` is the release-quality vocabulary; the older
        # `coverage_quality_tier_by_leaf` below survives as the pre-tier informational view.
        "coverage_tier_by_leaf": coverage_tier_by_leaf,
        "execution_readiness_by_recipe": execution_readiness_by_recipe,
        "executable_primary_by_leaf": executable_primary_by_leaf,
        "executable_covered_leaves": executable_covered_leaves,
        "conceptual_only_covered_leaves": conceptual_only_covered_leaves,
        "legacy_inferred_leaves": legacy_inferred_leaves,
        "legacy_derived_recipe_count": legacy_derived_recipe_count,
        "by_leaf": by_leaf,
        "secondary_by_leaf": secondary_by_leaf,
        "primary_by_leaf": by_leaf,
        "supporting_by_leaf": secondary_by_leaf,
        "effective_by_leaf": effective_by_leaf,
        "active_zero_effective": active_zero_effective,
        "authored_primary_by_leaf": authored_primary_by_leaf,
        "legacy_primary_by_leaf": legacy_primary_by_leaf,
        "formula_authoring_class_by_recipe": formula_authoring_class_by_recipe,
        "formula_deferred_requirements_by_leaf": formula_deferred_requirements_by_leaf,
        "coverage_quality_tier_by_leaf": coverage_quality_tier_by_leaf,
        "empty_intentional": empty_intentional,
        "unpopulated": unpopulated,
        "populated_count": populated_count,
        "leaf_count": len(leaves),
    }
