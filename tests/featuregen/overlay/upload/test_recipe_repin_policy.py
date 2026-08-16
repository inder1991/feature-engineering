"""C-A9 — the re-pin gate: named owners, three roles, two humans, both hashes.

The load-bearing test is the development-fixture one: a sandbox seeded by
`scripts/seed_dev_recipe_reviews` can cover all three roles and must STILL report the re-pin as
un-approved, because `dev-fixture:*` identities exist to be transparently synthetic and counting
them here would spend that transparency on the one decision it was created to keep honest.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from featuregen.overlay.upload.recipe_repin_policy import (
    ACCOUNTABLE_OWNER,
    DEV_FIXTURE_PREFIX,
    EXECUTION_OWNER,
    HASH_NOT_REFERENCED,
    INSUFFICIENT_DISTINCT_HUMANS,
    MIN_DISTINCT_HUMAN_IDENTITIES,
    REQUIRED_APPROVING_ROLES,
    ROLE_NOT_COVERED,
    RepinApprovalVerdictV1,
    assess_repin_approval,
    is_human_identity,
)

RECIPE_HASH = "sha256:recipe-revision-current"
EXPECTATION_HASH = "sha256:v3-expectation-reauthored"


@dataclass(frozen=True)
class _Event:
    reviewer: str
    reviewer_role: str
    decision: str = "approved"
    recipe_revision_hash: str = RECIPE_HASH
    formula_expectation_hash: str | None = EXPECTATION_HASH


def _full_human_approval() -> list[_Event]:
    return [
        _Event("alice@bank.example", "banking_sme"),
        _Event("bob@bank.example", "data_semantic_owner"),
        _Event("bob@bank.example", "formula_engineering"),
    ]


def _assess(events, **overrides):
    kwargs = dict(canonical_recipe_v2_hash=RECIPE_HASH, expectation_hash=EXPECTATION_HASH)
    kwargs.update(overrides)
    return assess_repin_approval(events, **kwargs)


# ══ the owners are NAMED — C-A9's gate ═══════════════════════════════════════════════════════════
def test_the_owners_and_roles_are_named_constants():
    """"the re-pin task exists with an owner and reviewer roles named"."""
    assert ACCOUNTABLE_OWNER == "feature_generation_product_owner"
    assert EXECUTION_OWNER == "formula_engineering"
    assert REQUIRED_APPROVING_ROLES == {
        "banking_sme", "data_semantic_owner", "formula_engineering"}
    assert MIN_DISTINCT_HUMAN_IDENTITIES == 2


# ══ the happy path ═══════════════════════════════════════════════════════════════════════════════
def test_three_roles_across_two_humans_is_approved():
    """One person may hold two roles — the floor is on PEOPLE, not on role count."""
    verdict = _assess(_full_human_approval())
    assert verdict.approved
    assert verdict.refusal_codes == ()
    assert set(verdict.covered_roles) == REQUIRED_APPROVING_ROLES
    assert len(verdict.human_identities) == 2


# ══ development fixtures are not human approval ══════════════════════════════════════════════════
def test_A_FULLY_SEEDED_DEV_SANDBOX_IS_STILL_NOT_APPROVED():
    """The load-bearing case. All three roles covered, every identity synthetic."""
    seeded = [_Event(f"{DEV_FIXTURE_PREFIX}{role}", role) for role in sorted(
        REQUIRED_APPROVING_ROLES)]
    verdict = _assess(seeded)

    assert set(verdict.covered_roles) == REQUIRED_APPROVING_ROLES, "the roles ARE covered"
    assert not verdict.approved
    assert INSUFFICIENT_DISTINCT_HUMANS in verdict.refusal_codes
    assert "are development fixtures and are not human approval" in verdict.detail
    assert verdict.human_identities == ()


def test_a_dev_fixture_identity_is_not_human():
    assert not is_human_identity(f"{DEV_FIXTURE_PREFIX}banking_sme")
    assert not is_human_identity("   ")
    assert is_human_identity("alice@bank.example")


def test_MIXING_a_fixture_in_does_not_top_up_the_human_count():
    """One real human plus two fixtures is one human, not three."""
    mixed = [
        _Event("alice@bank.example", "banking_sme"),
        _Event(f"{DEV_FIXTURE_PREFIX}data_semantic_owner", "data_semantic_owner"),
        _Event(f"{DEV_FIXTURE_PREFIX}formula_engineering", "formula_engineering"),
    ]
    verdict = _assess(mixed)
    assert not verdict.approved
    assert verdict.human_identities == ("alice@bank.example",)


# ══ roles and people ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("missing", sorted(REQUIRED_APPROVING_ROLES))
def test_every_role_is_individually_required(missing):
    events = [e for e in _full_human_approval() if e.reviewer_role != missing]
    verdict = _assess(events)
    assert not verdict.approved
    assert ROLE_NOT_COVERED in verdict.refusal_codes
    assert missing in verdict.detail


def test_ONE_PERSON_HOLDING_ALL_THREE_ROLES_IS_REFUSED():
    """Roles are not people: without the floor, a single individual could approve their own work
    three times over and the record would look fully covered."""
    solo = [_Event("alice@bank.example", role) for role in sorted(REQUIRED_APPROVING_ROLES)]
    verdict = _assess(solo)

    assert set(verdict.covered_roles) == REQUIRED_APPROVING_ROLES
    assert not verdict.approved
    assert INSUFFICIENT_DISTINCT_HUMANS in verdict.refusal_codes


# ══ both hashes must be referenced ═══════════════════════════════════════════════════════════════
def test_an_approval_at_ANOTHER_RECIPE_REVISION_does_not_count():
    stale = [_Event(e.reviewer, e.reviewer_role, recipe_revision_hash="sha256:old")
             for e in _full_human_approval()]
    verdict = _assess(stale)
    assert not verdict.approved
    assert HASH_NOT_REFERENCED in verdict.refusal_codes


def test_an_approval_that_never_saw_the_REAUTHORED_EXPECTATION_does_not_count():
    """"An approval naming the revision but not the expectation has not been shown the thing that
    changed" — which is the entire reason S0's re-authoring needs a new gate."""
    blind = [_Event(e.reviewer, e.reviewer_role, formula_expectation_hash=None)
             for e in _full_human_approval()]
    verdict = _assess(blind)
    assert not verdict.approved
    assert HASH_NOT_REFERENCED in verdict.refusal_codes
    assert "has not been shown the thing that changed" in verdict.detail


def test_a_REJECTED_event_does_not_approve_anything():
    rejected = [_Event(e.reviewer, e.reviewer_role, decision="rejected")
                for e in _full_human_approval()]
    assert not _assess(rejected).approved


def test_no_events_at_all_refuses():
    verdict = _assess([])
    assert not verdict.approved
    assert HASH_NOT_REFERENCED in verdict.refusal_codes


# ══ the verdict cannot contradict itself ═════════════════════════════════════════════════════════
def test_an_approved_verdict_cannot_carry_refusal_codes():
    with pytest.raises(ValueError, match="cannot carry refusal codes"):
        RepinApprovalVerdictV1(approved=True, refusal_codes=(ROLE_NOT_COVERED,), detail="d",
                               covered_roles=(), human_identities=())


def test_a_refusal_must_say_why():
    with pytest.raises(ValueError, match="cannot be reported or acted on"):
        RepinApprovalVerdictV1(approved=False, refusal_codes=(), detail="",
                               covered_roles=(), human_identities=())


# ══ the seeder's convention is the one this policy reads ═════════════════════════════════════════
def test_the_prefix_matches_the_SEEDER_that_writes_it():
    """Two spellings would mean a sandbox identity that looks human to this gate."""
    from pathlib import Path

    seeder = Path("scripts/seed_dev_recipe_reviews.py").read_text()
    assert f'REVIEWER_TEMPLATE = "{DEV_FIXTURE_PREFIX}{{role}}"' in seeder
