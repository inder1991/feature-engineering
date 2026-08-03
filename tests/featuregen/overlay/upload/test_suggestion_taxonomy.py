"""Task 1 — the discovery-taxonomy registries (suggestion_taxonomy.py).

Registry validation per the Task-1 brief and freeze 0F-5/0F-6: stable IDs, exact
recipe-family coverage, bounded safe text, order-independent version fingerprints,
an append-only owner/change log, and loud death on every forbidden shape.
"""
from __future__ import annotations

import pytest

from featuregen.contracts.contract_versions import contract_owner
from featuregen.overlay.upload.templates import ALL_TEMPLATES
from featuregen.overlay.upload.suggestion_taxonomy import (
    FEATURE_CATEGORY_REGISTRY,
    RECIPE_FAMILY_REGISTRY,
    TAXONOMY_CHANGE_LOG,
    TAXONOMY_OWNER,
    FeatureCategory,
    RecipeFamily,
    TaxonomyValidationError,
    change_log_content_hash,
    feature_category_registry_content_hash,
    recipe_family_registry_content_hash,
    safe_registry_text,
    validate_feature_categories,
    validate_recipe_families,
    validate_taxonomy_change_log,
)

_OWNER_MODULE = "featuregen.overlay.upload.suggestion_taxonomy"


# ── feature-category registry ──────────────────────────────────────────────────────────────────

def test_feature_category_registry_is_coarse_and_carries_plan_named_ids():
    # The plan names ratio/trend/recency/frequency/concentration; the rest is derived from the
    # real 157-template inventory. Coarse means ~8-15 entries, never one-per-family.
    assert {"ratio", "trend", "recency", "frequency", "concentration"} <= set(
        FEATURE_CATEGORY_REGISTRY)
    assert 8 <= len(FEATURE_CATEGORY_REGISTRY) <= 15


def test_feature_category_entries_are_stable_ids_with_display_and_description():
    for cid, cat in FEATURE_CATEGORY_REGISTRY.items():
        assert isinstance(cat, FeatureCategory)
        assert cat.id == cid  # dict key IS the identity — never a display name
        assert cat.id == cat.id.lower() and " " not in cat.id
        assert cat.display_name and cat.description


def test_feature_category_validator_rejects_display_name_keyed_identity():
    bad = {c.display_name: c for c in FEATURE_CATEGORY_REGISTRY.values()}
    with pytest.raises(TaxonomyValidationError):
        validate_feature_categories(bad)


def test_feature_category_validator_rejects_unsafe_text():
    cat = FeatureCategory(id="ratio", display_name="Ratio <script>", description="d")
    with pytest.raises(TaxonomyValidationError):
        validate_feature_categories({"ratio": cat})


# ── recipe-family display registry ─────────────────────────────────────────────────────────────

def test_recipe_family_registry_exactly_covers_authored_families():
    authored = {t.family for t in ALL_TEMPLATES}
    assert set(RECIPE_FAMILY_REGISTRY) == authored          # exact coverage, both directions
    assert len(RECIPE_FAMILY_REGISTRY) == 121               # 0F-2 verified count
    for fid, fam in RECIPE_FAMILY_REGISTRY.items():
        assert isinstance(fam, RecipeFamily)
        assert fam.id == fid
        assert fam.display_name


def test_recipe_family_validator_dies_on_missing_family():
    families = dict(RECIPE_FAMILY_REGISTRY)
    families.pop("recency")
    with pytest.raises(TaxonomyValidationError, match="recency"):
        validate_recipe_families(families)


def test_recipe_family_validator_dies_on_orphan_family():
    families = dict(RECIPE_FAMILY_REGISTRY)
    families["not_an_authored_family"] = RecipeFamily(
        id="not_an_authored_family", display_name="Nope")
    with pytest.raises(TaxonomyValidationError, match="not_an_authored_family"):
        validate_recipe_families(families)


def test_recipe_family_display_edit_does_not_move_identity():
    """Display names are presentation: editing one changes the display registry fingerprint but
    never the id set (identity is the authored family string, not the mutable display name)."""
    families = dict(RECIPE_FAMILY_REGISTRY)
    families["recency"] = RecipeFamily(id="recency", display_name="Recency (renamed)")
    validate_recipe_families(families)  # still valid — same ids
    assert set(families) == set(RECIPE_FAMILY_REGISTRY)
    assert (recipe_family_registry_content_hash(families)
            != recipe_family_registry_content_hash(RECIPE_FAMILY_REGISTRY))


# ── version fingerprints: registered contracts + must-survive reordering ───────────────────────

def test_registry_hash_contracts_are_registered_to_the_owner_module():
    assert contract_owner("feature-category-registry", "1") == _OWNER_MODULE
    assert contract_owner("recipe-family-registry", "1") == _OWNER_MODULE
    assert contract_owner("suggestion-taxonomy-change-log", "1") == _OWNER_MODULE


def test_registry_fingerprints_survive_dict_reordering():
    reordered_cats = dict(reversed(list(FEATURE_CATEGORY_REGISTRY.items())))
    assert (feature_category_registry_content_hash(reordered_cats)
            == feature_category_registry_content_hash(FEATURE_CATEGORY_REGISTRY))
    reordered_fams = dict(reversed(list(RECIPE_FAMILY_REGISTRY.items())))
    assert (recipe_family_registry_content_hash(reordered_fams)
            == recipe_family_registry_content_hash(RECIPE_FAMILY_REGISTRY))


def test_registry_fingerprint_moves_on_content_change():
    cats = dict(FEATURE_CATEGORY_REGISTRY)
    cats["ratio"] = FeatureCategory(id="ratio", display_name="Ratio & Share",
                                    description="changed description")
    assert (feature_category_registry_content_hash(cats)
            != feature_category_registry_content_hash(FEATURE_CATEGORY_REGISTRY))


# ── owner + append-only change log ─────────────────────────────────────────────────────────────

def test_taxonomy_owner_is_the_registry_module():
    assert TAXONOMY_OWNER == _OWNER_MODULE


def test_change_log_is_valid_and_baseline_entries_are_authored_only():
    validate_taxonomy_change_log(TAXONOMY_CHANGE_LOG)
    # No fabricated human review and no unrun LLM pass: the baseline log is authored-only.
    assert {r.change_kind for r in TAXONOMY_CHANGE_LOG} == {"authored"}


def test_change_log_rejects_unknown_kind_and_non_monotonic_seq():
    import dataclasses
    bad_kind = (dataclasses.replace(TAXONOMY_CHANGE_LOG[0], change_kind="reviewed"),)
    with pytest.raises(TaxonomyValidationError):
        validate_taxonomy_change_log(bad_kind)
    bad_seq = (TAXONOMY_CHANGE_LOG[0], TAXONOMY_CHANGE_LOG[0])
    with pytest.raises(TaxonomyValidationError):
        validate_taxonomy_change_log(bad_seq)


def test_change_log_prefix_hash_is_pinned_append_only():
    """Golden: the hash of the baseline log prefix. Appending entries keeps this passing;
    rewriting or deleting history breaks it — the append-only property, enforced."""
    assert change_log_content_hash(TAXONOMY_CHANGE_LOG[:4]) == (
        "9d3362422ca8b31760ffdbb21162922d0f1d04a6c041828161003ff2cd6e552c")


# ── bounded plain-text safety ──────────────────────────────────────────────────────────────────

def test_safe_registry_text_normalizes_nfc_for_identity():
    composed = "café"
    decomposed = "café"
    assert safe_registry_text(composed, field="t", max_len=10) == safe_registry_text(
        decomposed, field="t", max_len=10)


@pytest.mark.parametrize("bad", ["", "   ", "a\x00b", "a\nb", "x<u>y</u>", "a​b"])
def test_safe_registry_text_rejects_blank_control_and_html(bad):
    with pytest.raises(TaxonomyValidationError):
        safe_registry_text(bad, field="t", max_len=50)


def test_safe_registry_text_enforces_bound():
    with pytest.raises(TaxonomyValidationError):
        safe_registry_text("x" * 51, field="t", max_len=50)
