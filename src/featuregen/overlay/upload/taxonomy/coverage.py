"""Phase-0 coverage report — which selectable use-case leaves the 153 recipes actually populate.

This inverts :func:`recipe_applicability` over ``ALL_TEMPLATES`` against ``selectable_leaves()`` to
answer, per leaf, *which recipes name it as their primary objective* (and, separately, as a secondary).
It is the human-readable audit behind the Phase-0 exit criteria and is read-only — nothing here touches
``templates.py`` or grounding.

Key framing (the corrected Phase-0 gate): the 153 recipes populate only a **subset** of the 88
selectable leaves. A selectable leaf with no authored recipe is normal — many governed objectives simply
have no recipe yet — so an *unpopulated non-intentional* leaf is **informational, not a failure**. The
one hard rule the report enforces is that an :attr:`UseCase.intentionally_empty` leaf (a declared-future
``*`` objective) must carry **zero** recipes as primary *and* zero as secondary.

``coverage_report()`` returns:

* ``by_leaf``            — every selectable leaf → the recipe ids whose **primary** is that leaf ([] if none).
* ``secondary_by_leaf``  — every selectable leaf → the recipe ids that list it as a **secondary**.
* ``empty_intentional``  — the intentionally-empty selectable leaves (each must have 0 primary + 0 secondary).
* ``unpopulated``        — non-intentional selectable leaves with 0 primary recipes (informational; sizable).
* ``populated_count``    — how many selectable leaves have >= 1 primary recipe.
* ``leaf_count``         — total selectable leaves.
"""
from __future__ import annotations

from featuregen.overlay.upload import templates
from featuregen.overlay.upload.taxonomy.recipe_applicability import recipe_applicability
from featuregen.overlay.upload.taxonomy.use_cases import USE_CASE_REGISTRY, selectable_leaves


def coverage_report() -> dict:
    """Invert per-recipe applicability over the selectable-leaf vocabulary into a coverage audit.

    Leaves are reported in ``selectable_leaves()`` (authoring/topological) order and recipe ids in
    ``ALL_TEMPLATES`` order, so the output is stable and diff-friendly. See the module docstring for the
    shape and semantics of every returned key.
    """
    leaves = selectable_leaves()

    by_leaf: dict[str, list[str]] = {leaf: [] for leaf in leaves}
    secondary_by_leaf: dict[str, list[str]] = {leaf: [] for leaf in leaves}

    for template in templates.ALL_TEMPLATES:
        spec = recipe_applicability(template)
        # primary is always a selectable leaf (Task-4 guarantee), so the key exists.
        by_leaf[spec.primary].append(template.id)
        for leaf in spec.secondary:
            if leaf in secondary_by_leaf:            # secondary is always a selectable leaf too
                secondary_by_leaf[leaf].append(template.id)

    empty_intentional = [
        leaf for leaf in leaves if USE_CASE_REGISTRY[leaf].intentionally_empty]
    unpopulated = [
        leaf for leaf in leaves
        if not by_leaf[leaf] and not USE_CASE_REGISTRY[leaf].intentionally_empty]
    populated_count = sum(1 for leaf in leaves if by_leaf[leaf])

    return {
        "by_leaf": by_leaf,
        "secondary_by_leaf": secondary_by_leaf,
        "empty_intentional": empty_intentional,
        "unpopulated": unpopulated,
        "populated_count": populated_count,
        "leaf_count": len(leaves),
    }
