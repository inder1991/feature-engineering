"""Task A0 — the frozen decision's reviewed-expectation fact is keyed by EXPECTATION REF.

``RECIPE_FORMULA_V2_EXPECTATIONS`` is keyed by expectation ref; ``decision_facts_for_candidate``
used to ask it with a RECIPE ID. The two agree for exactly three recipes in the whole registry
(the ones whose ref happens to spell their id) and disagree for the other 295, so the fact the
activation policy freezes was answerable only by coincidence.
"""
from __future__ import annotations

import pytest

from featuregen.overlay.upload import recipe_formula_expectations_v2 as expectations_v2
from featuregen.overlay.upload.feature_assist import FeatureIdea
from featuregen.overlay.upload.feature_planning_contracts import (
    planning_request_from_recipe,
    planning_request_hash,
)
from featuregen.overlay.upload.recipe_planning_lens import (
    DatasetStoryV1,
    V2RecipeCandidateV1,
)
from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id
from featuregen.overlay.upload.semantic_option_decision import decision_facts_for_candidate

#: A registry recipe whose expectation ref is NOT its id — the shape 295 of 298 recipes carry.
REF_DIFFERS = "balance_slope"
REF_DIFFERS_EXPECTATION = "retail:balance_slope"
PINNED_FIXTURE = ("30_posted_debit_amount_exemplar.json",
                  "093de7a0e954122f2e0e5706eea9af65ec18b42ddb7b29f4dbd9860911930bbc")


def _candidate(recipe_id: str) -> V2RecipeCandidateV1:
    definition = v2_recipe_by_id(recipe_id)
    assert definition is not None, f"{recipe_id!r} is not a registry recipe"
    request = planning_request_from_recipe(definition)
    return V2RecipeCandidateV1(
        recipe_id=recipe_id, relationship="primary", planning_request=request,
        planning_request_hash=planning_request_hash(request),
        recipe_revision_hash="rev", verdicts=(), binding_state="missing",
        readiness=definition.readiness, temporal_pit_text="pit", temporal_blocker="",
        review_current=False, review_missing_roles=(), eligibility={},
        dataset_story=DatasetStoryV1(
            population_ref="accounts", population_basis="declared_grain",
            dataset_tables=("accounts",), cross_dataset=False, codes=()))


def _idea(recipe_id: str) -> FeatureIdea:
    return FeatureIdea(name=recipe_id, description="", derives_from=[], aggregation=None,
                       grain_table=None, source_definition_id=recipe_id)


def _reviewed(recipe_id: str) -> bool:
    facts = decision_facts_for_candidate(_candidate(recipe_id), _idea(recipe_id), None, "ctx")
    return facts["has_reviewed_formula_expectation"]


@pytest.fixture
def registry(monkeypatch):
    """A registry the test owns, so no case depends on which recipes are reviewed today."""
    def _register(**entries):
        monkeypatch.setattr(expectations_v2, "RECIPE_FORMULA_V2_EXPECTATIONS", dict(entries))
    return _register


def test_a_recipe_whose_expectation_ref_differs_from_its_id_is_recognised_by_its_ref(registry):
    """The registry key is the ref the definition declares — not the recipe's own name."""
    registry(**{REF_DIFFERS_EXPECTATION: PINNED_FIXTURE})
    assert _reviewed(REF_DIFFERS) is True


def test_a_registry_entry_spelling_the_recipe_id_is_not_a_reviewed_expectation(registry):
    """The converse, and the defect's actual bite: an entry keyed by recipe id names no
    expectation, so it must never flip the frozen fact."""
    registry(**{REF_DIFFERS: PINNED_FIXTURE})
    assert _reviewed(REF_DIFFERS) is False


def test_a_recipe_with_no_formula_reference_is_not_reviewed(registry):
    """A conceptual pattern declares no formula; there is nothing to have reviewed."""
    registry(**{REF_DIFFERS_EXPECTATION: PINNED_FIXTURE})
    assert _reviewed("salary_confidence") is False


def test_a_candidate_that_is_not_a_registry_recipe_is_not_reviewed(registry):
    """LLM-intent and user-defined candidates carry ids the recipe registry never minted."""
    registry(**{REF_DIFFERS_EXPECTATION: PINNED_FIXTURE})
    definition = v2_recipe_by_id(REF_DIFFERS)
    request = planning_request_from_recipe(definition)
    candidate = V2RecipeCandidateV1(
        recipe_id="llm:invented_feature", relationship="primary", planning_request=request,
        planning_request_hash=planning_request_hash(request), recipe_revision_hash="rev",
        verdicts=(), binding_state="missing", readiness="CONCEPTUAL_ONLY",
        temporal_pit_text="pit", temporal_blocker="", review_current=False,
        review_missing_roles=(), eligibility={})
    facts = decision_facts_for_candidate(
        candidate, _idea("llm:invented_feature"), None, "ctx")
    assert facts["has_reviewed_formula_expectation"] is False


def test_the_reviewed_registry_still_answers_the_three_anchors():
    """No monkeypatch: the real registries, asked through the serving path. The v1 anchors and
    the v2 exemplar all spell their ref as their id, which is why the defect stayed latent."""
    for recipe_id in ("merchant_mcc_diversity", "obligor_facility_count", "posted_debit_amount"):
        assert _reviewed(recipe_id) is True
