"""C-A9 — the reviewed-expectation RE-PIN is a gated human act, with named owners.

**Why this exists.** S0's re-authoring changes the pilot exemplar's canonical hash, and therefore
the pinned registry entry that reviewers signed off against. Re-pinning it is not a code change that
happens to move a constant — it is a claim that humans with the right expertise looked at the NEW
expectation and agreed it is the one the recipe means. Without a gate, the re-pin is a commit, and
the signatures on the old hash silently become signatures on the new one.

**Owners, named** (product owner's decision, 2026-08-16):

* Accountable owner — :data:`ACCOUNTABLE_OWNER`. Answers for the re-pin having happened correctly.
* Execution owner — :data:`EXECUTION_OWNER`. Performs it.

**The approval requires all three roles AND at least two distinct human identities.** Roles are not
people: one person can hold two roles, and a rule stated only in roles would let a single individual
approve their own work three times over. Two distinct humans is the floor that makes "reviewed" mean
more than "written down".

**Development fixtures are usable and are NOT human approval.** ``scripts/seed_dev_recipe_reviews``
attributes to ``dev-fixture:<role>`` precisely so a sandbox is transparently synthetic. This module
makes that transparency mechanical: a ``dev-fixture:*`` reviewer never counts toward the human
floor, so a development environment can be fully approved and still report the re-pin as
un-approved — which is the honest answer.

**Both hashes must be referenced.** An approval that names the recipe revision but not the
re-authored expectation has not been shown the thing that changed, and one naming the expectation
but not the revision cannot be tied to a recipe.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "ACCOUNTABLE_OWNER",
    "DEV_FIXTURE_PREFIX",
    "EXECUTION_OWNER",
    "HASH_NOT_REFERENCED",
    "INSUFFICIENT_DISTINCT_HUMANS",
    "MIN_DISTINCT_HUMAN_IDENTITIES",
    "REQUIRED_APPROVING_ROLES",
    "ROLE_NOT_COVERED",
    "RepinApprovalVerdictV1",
    "assess_repin_approval",
    "is_human_identity",
]

#: Answers for the re-pin having happened correctly.
ACCOUNTABLE_OWNER = "feature_generation_product_owner"
#: Performs it.
EXECUTION_OWNER = "formula_engineering"

#: Every role that must appear among the approvals. All three, not any of three: the banking meaning,
#: the semantic binding and the formula mechanics are three different questions and no one reviewer
#: is expected to answer all of them.
REQUIRED_APPROVING_ROLES = frozenset({
    "banking_sme",
    "data_semantic_owner",
    "formula_engineering",
})

#: Roles are not people. Without this floor one individual holding two roles could approve their own
#: re-pin, and the record would look fully covered.
MIN_DISTINCT_HUMAN_IDENTITIES = 2

#: The transparently-synthetic reviewer prefix used by `scripts/seed_dev_recipe_reviews`.
DEV_FIXTURE_PREFIX = "dev-fixture:"

ROLE_NOT_COVERED = "ROLE_NOT_COVERED"
INSUFFICIENT_DISTINCT_HUMANS = "INSUFFICIENT_DISTINCT_HUMANS"
HASH_NOT_REFERENCED = "HASH_NOT_REFERENCED"


def is_human_identity(reviewer: str) -> bool:
    """Whether ``reviewer`` may count toward the human floor.

    A ``dev-fixture:*`` identity is deliberately not human. It exists so a sandbox is obviously
    synthetic, and treating it as approval here would spend that transparency on the one decision it
    was created to keep honest.
    """
    return bool(reviewer.strip()) and not reviewer.startswith(DEV_FIXTURE_PREFIX)


@dataclass(frozen=True, slots=True)
class RepinApprovalVerdictV1:
    """Whether the re-pin is approved, and precisely what is missing if not."""

    approved: bool
    refusal_codes: tuple[str, ...]
    detail: str
    covered_roles: tuple[str, ...]
    human_identities: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.approved and self.refusal_codes:
            raise ValueError(
                "an approved verdict cannot carry refusal codes: a caller reading only `approved` "
                "would re-pin against a record the policy rejected")
        if not self.approved and not self.refusal_codes:
            raise ValueError("a refusal with no code cannot be reported or acted on")


def assess_repin_approval(
    events: Sequence,
    *,
    canonical_recipe_v2_hash: str,
    expectation_hash: str,
) -> RepinApprovalVerdictV1:
    """Whether ``events`` constitute a valid human approval of THIS re-pin.

    Args:
        events: ``RecipeReviewEventV1`` values (anything carrying ``decision``, ``reviewer``,
            ``reviewer_role``, ``recipe_revision_hash`` and ``formula_expectation_hash``).
        canonical_recipe_v2_hash: the CURRENT recipe revision hash being re-pinned to.
        expectation_hash: the RE-AUTHORED V3 expectation hash.

    Returns:
        A :class:`RepinApprovalVerdictV1`. Approval requires every role in
        :data:`REQUIRED_APPROVING_ROLES`, at least :data:`MIN_DISTINCT_HUMAN_IDENTITIES` distinct
        non-fixture identities, and every counted event to reference BOTH hashes.
    """
    relevant = [
        event for event in events
        if getattr(event, "decision", "") == "approved"
        and getattr(event, "recipe_revision_hash", "") == canonical_recipe_v2_hash
        and getattr(event, "formula_expectation_hash", None) == expectation_hash
    ]
    covered = {getattr(event, "reviewer_role", "") for event in relevant}
    humans = {getattr(event, "reviewer", "") for event in relevant
              if is_human_identity(getattr(event, "reviewer", ""))}

    codes: list[str] = []
    details: list[str] = []

    missing_roles = sorted(REQUIRED_APPROVING_ROLES - covered)
    if missing_roles:
        codes.append(ROLE_NOT_COVERED)
        details.append(
            f"no approval at this recipe revision AND expectation hash from {missing_roles}. All "
            f"three roles are required because the banking meaning, the semantic binding and the "
            f"formula mechanics are three different questions")

    if len(humans) < MIN_DISTINCT_HUMAN_IDENTITIES:
        codes.append(INSUFFICIENT_DISTINCT_HUMANS)
        fixtures = sorted(
            {getattr(event, "reviewer", "") for event in relevant
             if not is_human_identity(getattr(event, "reviewer", ""))})
        details.append(
            f"{len(humans)} distinct human identity(ies), below the floor of "
            f"{MIN_DISTINCT_HUMAN_IDENTITIES}"
            + (f"; {fixtures} are development fixtures and are not human approval" if fixtures
               else "")
            + ". Roles are not people: one individual holding two roles could otherwise approve "
              "their own re-pin and the record would look fully covered")

    if not relevant and (events or True):
        codes.append(HASH_NOT_REFERENCED)
        details.append(
            f"no approval event references BOTH the recipe revision {canonical_recipe_v2_hash!r} "
            f"and the re-authored expectation {expectation_hash!r}. An approval naming the revision "
            f"but not the expectation has not been shown the thing that changed")

    if codes:
        return RepinApprovalVerdictV1(
            approved=False, refusal_codes=tuple(dict.fromkeys(codes)), detail="; ".join(details),
            covered_roles=tuple(sorted(covered)), human_identities=tuple(sorted(humans)))
    return RepinApprovalVerdictV1(
        approved=True, refusal_codes=(), detail="",
        covered_roles=tuple(sorted(covered)), human_identities=tuple(sorted(humans)))
