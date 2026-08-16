"""C-D7 — the group-name allocator.

The gate is *"the choice is stated and the ≤128 bound proved"*. The choice is option one: it IS
`hive_identifier`, so these tests prove the bound THROUGH the allocator rather than restating it,
and one test asserts structurally that no second normalizer was written.
"""
from __future__ import annotations

import inspect

import pytest

from featuregen.materialize.admission import FeatureNamePlanError, hive_identifier
from featuregen.overlay.upload import group_name_allocator
from featuregen.overlay.upload.group_name_allocator import (
    MAX_HIVE_IDENTIFIER_LENGTH,
    GroupNameCollision,
    allocate_group_name,
)


# ══ THE CHOICE — one normalizer, stated and structural ═══════════════════════════════════════════
def test_THE_ALLOCATOR_USES_HIVE_IDENTIFIER_AND_DEFINES_NO_SECOND_NORMALIZER():
    """`hive_identifier`'s own docstring says it is public so the group plan reaches the SAME
    answer, because "a second normalizer would be a second chance to disagree"."""
    source = inspect.getsource(group_name_allocator)
    assert "hive_identifier(" in source
    for reimplementation in ("NFKC", "unicodedata", "re.sub", "sub(", "translate("):
        assert reimplementation not in source, reimplementation


def test_the_allocator_agrees_with_the_column_normalizer_exactly():
    for name in ("Account Daily", "account-daily", "ACCOUNT__daily", "açcount daily"):
        assert allocate_group_name(name, bound_names=()) == hive_identifier(name)


# ══ the ≤128 bound, proved THROUGH the allocator ═════════════════════════════════════════════════
def test_a_name_at_the_bound_is_allocated():
    at_bound = "a" * MAX_HIVE_IDENTIFIER_LENGTH
    assert allocate_group_name(at_bound, bound_names=()) == at_bound
    assert len(allocate_group_name(at_bound, bound_names=())) == 128


def test_A_NAME_OVER_THE_BOUND_REFUSES_AND_IS_NOT_TRUNCATED():
    """Truncating would map two distinct names onto one table — the very thing the collision check
    exists to prevent."""
    over = "a" * (MAX_HIVE_IDENTIFIER_LENGTH + 1)
    with pytest.raises(FeatureNamePlanError, match="128"):
        allocate_group_name(over, bound_names=())


@pytest.mark.parametrize("bad", ["", "   ", "9lives", "_leading", "!!!"])
def test_a_name_that_cannot_be_expressed_is_a_PLAN_ERROR(bad):
    """Not a name to invent a mangling for."""
    with pytest.raises(FeatureNamePlanError):
        allocate_group_name(bad, bound_names=())


def test_allocation_is_IDEMPOTENT():
    once = allocate_group_name("Account Daily", bound_names=())
    assert allocate_group_name(once, bound_names=()) == once


# ══ collision REFUSES, never suffixes ════════════════════════════════════════════════════════════
def test_A_TAKEN_NAME_REFUSES():
    with pytest.raises(GroupNameCollision, match="already bound"):
        allocate_group_name("account_daily", bound_names=("account_daily",))


def test_the_refusal_explains_why_it_does_not_SUFFIX():
    """An allocator returning `account_daily_2` hands back a table nobody asked for."""
    with pytest.raises(GroupNameCollision, match="hand back a table nobody asked for"):
        allocate_group_name("account_daily", bound_names=("account_daily",))


def test_collision_is_detected_AFTER_normalization():
    """`Account Daily` and `account_daily` are one table."""
    with pytest.raises(GroupNameCollision):
        allocate_group_name("Account Daily", bound_names=("account_daily",))


def test_a_bound_name_stored_UNNORMALIZED_still_collides():
    """Comparing raw against folded would miss a real collision."""
    with pytest.raises(GroupNameCollision):
        allocate_group_name("account_daily", bound_names=("Account Daily",))


def test_a_MALFORMED_bound_name_does_not_break_the_comparison():
    """A stored name that cannot normalize (it predates the allocator) is compared on its lowered
    form rather than skipped — skipping it would let a new group claim a slot an old one occupies.
    Here `9lives` is unrelated, so allocation proceeds; the point is that it does not raise."""
    assert allocate_group_name("account_daily", bound_names=("9lives",)) == "account_daily"


def test_a_bound_name_differing_only_in_CASE_still_collides():
    with pytest.raises(GroupNameCollision):
        allocate_group_name("account_daily", bound_names=("ACCOUNT_DAILY",))


def test_an_unrelated_bound_name_does_not_block():
    assert allocate_group_name("account_daily", bound_names=("customer_daily",)) == "account_daily"


def test_THE_NAMESPACE_IS_FLAT_ACROSS_V1_AND_V2_and_the_message_says_so():
    """Live rather than theoretical: V1 and V2 group names share one space with no language
    discriminator until C-D6's scoping migration, so a V2 group can collide with a V1 group
    published months ago."""
    with pytest.raises(GroupNameCollision, match="FLAT across V1 and V2"):
        allocate_group_name("account_daily", bound_names=("account_daily",))


def test_a_collision_is_a_DIFFERENT_exception_from_a_plan_error():
    """They need different answers: a plan error means choose another name; a collision means the
    name is fine and already taken, which may be a duplicate submission."""
    assert not issubclass(GroupNameCollision, FeatureNamePlanError)
    assert issubclass(GroupNameCollision, ValueError)
