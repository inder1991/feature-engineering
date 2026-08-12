"""BR-23 schema half — review evidence is append-only, attributable, and revision-specific.

Migration 1060 + the schema layer only: events land immutably (the 1034-idiom guards make UPDATE
and DELETE database errors, not policy), "current" is a read projection over the newest event for
one exact canonical-recipe-v2 hash (an edited definition makes approval a lookup MISS — the
"changed formula stales the approval" rule with no flag to forget), and the supersedes chain can
only bind events of the same recipe. The validity fold and APIs are BR-23 proper.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from featuregen.overlay.upload.recipe_grounding_context import canonical_recipe_v2_hash
from featuregen.overlay.upload.recipe_registry_v2 import PROBE_RECIPE
from featuregen.overlay.upload.recipe_review import (
    RecipeReviewError,
    current_review,
    record_review_event,
    review_events,
)

_HASH_R1 = canonical_recipe_v2_hash(PROBE_RECIPE)
_HASH_R2 = canonical_recipe_v2_hash(replace(PROBE_RECIPE, revision=2))


def _approve(db, **overrides) -> str:
    kwargs = dict(
        recipe_id=PROBE_RECIPE.recipe_id, recipe_revision_hash=_HASH_R1,
        decision="approved", reviewer="user:sme", reviewer_role="banking_sme",
        output_id=PROBE_RECIPE.output.output_id,
        reviewed_primary_objective=PROBE_RECIPE.primary_objective,
        gold_corpus_refs=("gold:probe-01",),
        policy_dependencies=("policy:eligible-posted-status",),
        rationale="the exemplar shape is correct")
    kwargs.update(overrides)
    return record_review_event(db, **kwargs)


def test_an_event_round_trips_with_every_field(db):
    event_id = _approve(db)
    (event,) = review_events(db, PROBE_RECIPE.recipe_id)
    assert event.event_id == event_id
    assert (event.decision, event.reviewer, event.reviewer_role) == (
        "approved", "user:sme", "banking_sme")
    assert event.recipe_revision_hash == _HASH_R1
    assert event.gold_corpus_refs == ("gold:probe-01",)
    assert event.policy_dependencies == ("policy:eligible-posted-status",)
    assert event.supersedes_event_id is None


def test_approval_is_revision_specific_by_lookup_miss(db):
    """The BR-23 rule with no moving part: editing the definition changes its canonical hash, and
    the old approval simply is not found — nothing needed flipping, so nothing can be forgotten."""
    _approve(db)
    assert current_review(db, recipe_id=PROBE_RECIPE.recipe_id,
                          recipe_revision_hash=_HASH_R1) is not None
    assert current_review(db, recipe_id=PROBE_RECIPE.recipe_id,
                          recipe_revision_hash=_HASH_R2) is None, \
        "revision 2 was never reviewed; its approval must be a MISS, not an inherited status"


def test_a_changed_mind_is_a_superseding_event_and_current_follows_it(db):
    first = _approve(db)
    second = _approve(db, decision="changes_required", rationale="the currency policy is vague",
                      supersedes_event_id=first)
    events = review_events(db, PROBE_RECIPE.recipe_id)
    assert [e.event_id for e in events] == [first, second], "history is immutable and ordered"
    current = current_review(db, recipe_id=PROBE_RECIPE.recipe_id,
                             recipe_revision_hash=_HASH_R1)
    assert current is not None and current.event_id == second
    assert current.supersedes_event_id == first


def test_the_store_is_append_only_at_the_database(db):
    """UPDATE and DELETE are database ERRORS, not conventions — evidence that can be rewritten
    proves nothing."""
    _approve(db)
    with pytest.raises(Exception, match="append-only"), db.transaction():
        db.execute("UPDATE recipe_review_event SET decision = 'rejected'")
    with pytest.raises(Exception, match="append-only"), db.transaction():
        db.execute("DELETE FROM recipe_review_event")
    (event,) = review_events(db, PROBE_RECIPE.recipe_id)
    assert event.decision == "approved", "the record survived both attempts untouched"


def test_the_supersedes_chain_is_validated(db):
    with pytest.raises(RecipeReviewError, match="unknown event"):
        _approve(db, supersedes_event_id="rre_does_not_exist")
    other = record_review_event(
        db, recipe_id="some_other_recipe", recipe_revision_hash="sha:other",
        decision="approved", reviewer="user:sme", reviewer_role="banking_sme")
    with pytest.raises(RecipeReviewError, match="SAME recipe"):
        _approve(db, supersedes_event_id=other)


def test_closed_vocabularies_and_attribution_are_enforced(db):
    with pytest.raises(RecipeReviewError, match="decision"):
        _approve(db, decision="looks_fine")
    with pytest.raises(RecipeReviewError, match="reviewer_role"):
        _approve(db, reviewer_role="enthusiast")
    with pytest.raises(RecipeReviewError, match="attributable"):
        _approve(db, reviewer="  ")
    with pytest.raises(RecipeReviewError, match="both permitted and prohibited"):
        _approve(db, permitted_stages=("monitoring",), prohibited_stages=("monitoring",))
    assert review_events(db, PROBE_RECIPE.recipe_id) == [], "nothing invalid was written"
