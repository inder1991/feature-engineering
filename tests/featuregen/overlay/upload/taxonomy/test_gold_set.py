"""Phase-1A Task 4 — structural gates on the authored gold evaluation set.

These tests do NOT judge recognition quality (that is Task 5's harness against a real LLM). They
assert only that the gold set is internally well-formed and references real closed-taxonomy leaves
and real recipe ids — so a downstream metric never silently scores against a typo. The set itself is
flagged in its module docstring as authored and pending expert review.
"""
from __future__ import annotations

from collections import Counter

from tests.featuregen.overlay.upload.taxonomy import gold_recognition
from tests.featuregen.overlay.upload.taxonomy.gold_recognition import GOLD

from featuregen.overlay.upload.taxonomy.use_cases import selectable_leaves
from featuregen.overlay.upload.templates import ALL_TEMPLATES

_LEAVES = frozenset(selectable_leaves())
_RECIPE_IDS = frozenset(t.id for t in ALL_TEMPLATES)
_CATEGORIES = frozenset(
    {"straightforward", "synonym", "ambiguous", "unscoped", "regulated", "multi_use_case"})

# Per-category minimum coverage the plan requires.
_CATEGORY_MINIMUMS = {
    "synonym": 4,
    "ambiguous": 3,
    "unscoped": 3,
    "regulated": 3,
    "multi_use_case": 2,
}


def test_at_least_24_cases_with_unique_ids():
    assert len(GOLD) >= 24, len(GOLD)
    ids = [c.id for c in GOLD]
    assert len(ids) == len(set(ids)), "gold case ids must be unique"


def test_every_category_is_valid():
    for c in GOLD:
        assert c.category in _CATEGORIES, (c.id, c.category)


def test_category_coverage_minimums():
    counts = Counter(c.category for c in GOLD)
    for category, minimum in _CATEGORY_MINIMUMS.items():
        assert counts[category] >= minimum, (category, counts[category], "<", minimum)


def test_expected_primary_is_a_real_selectable_leaf():
    for c in GOLD:
        if c.expected_primary is not None:
            assert c.expected_primary in _LEAVES, (c.id, c.expected_primary)


def test_permitted_secondary_are_real_selectable_leaves():
    for c in GOLD:
        for leaf in c.permitted_secondary:
            assert leaf in _LEAVES, (c.id, leaf)


def test_expected_relevant_recipes_are_real_recipe_ids():
    for c in GOLD:
        for rid in c.expected_relevant_recipes:
            assert rid in _RECIPE_IDS, (c.id, rid)


def test_non_unscoped_cases_have_a_primary_and_at_least_two_recipes():
    for c in GOLD:
        if c.category != "unscoped":
            assert c.expected_primary is not None, c.id
            assert len(c.expected_relevant_recipes) >= 2, (c.id, c.expected_relevant_recipes)


def test_unscoped_cases_have_no_primary_and_no_recipes():
    for c in GOLD:
        if c.category == "unscoped":
            assert c.expected_primary is None, c.id
            assert c.permitted_secondary == (), c.id
            assert c.expected_relevant_recipes == (), c.id


def test_relevant_recipes_are_unique_within_a_case():
    for c in GOLD:
        recipes = c.expected_relevant_recipes
        assert len(recipes) == len(set(recipes)), (c.id, "duplicate relevant recipe")


def test_module_marks_the_set_as_authored_pending_expert_review():
    doc = (gold_recognition.__doc__ or "").upper()
    assert "AUTHORED" in doc and "PENDING EXPERT REVIEW" in doc
