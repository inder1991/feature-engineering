"""Phase-0 Task 2 — supporting dimension registries (closed vocabularies)."""
from __future__ import annotations

from itertools import combinations

from featuregen.overlay.upload.taxonomy.dimensions import (
    BUSINESS_OUTCOMES,
    DIMENSIONS,
    JOURNEY_STAGES,
    MEASURES,
    MODELLING_CONTEXTS,
    PRODUCT_CONTEXTS,
    TYPOLOGIES,
    is_known,
)


def test_membership_spot_checks():
    # One member from each of the six closed vocabularies (spec §1).
    assert "ifrs9" in MODELLING_CONTEXTS
    assert "tracking_error" in MEASURES
    assert "crypto_assets" in PRODUCT_CONTEXTS
    assert "crypto_asset_laundering" in TYPOLOGIES
    assert "unbundling" in JOURNEY_STAGES
    assert "cost_efficiency" in BUSINESS_OUTCOMES


def test_is_known_resolves_within_the_right_dimension():
    assert is_known("modelling_context", "ifrs9") is True
    # A real use-case-ish value in the wrong dimension is not "known" there.
    assert is_known("typology", "ifrs9") is False
    # An unknown dimension resolves to the empty set → False.
    assert is_known("not_a_dimension", "ifrs9") is False


def test_dimensions_maps_all_six_vocabularies():
    assert DIMENSIONS == {
        "modelling_context": MODELLING_CONTEXTS,
        "measure": MEASURES,
        "product_context": PRODUCT_CONTEXTS,
        "typology": TYPOLOGIES,
        "journey_stage": JOURNEY_STAGES,
        "business_outcome": BUSINESS_OUTCOMES,
    }


def test_vocabularies_are_pairwise_disjoint():
    # No value may appear in two dimensions (the import-time invariant, re-checked here).
    for (name_a, set_a), (name_b, set_b) in combinations(DIMENSIONS.items(), 2):
        overlap = set_a & set_b
        assert not overlap, (name_a, name_b, overlap)


def test_no_dimension_is_empty():
    for name, members in DIMENSIONS.items():
        assert members, name
