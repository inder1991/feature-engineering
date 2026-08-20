"""C-D7 — the group-name allocator.

**The choice, stated: it IS ``hive_identifier``.** C-D7 offered two options — extend the existing
normalizer, or scope a new one to group names with a proof of non-collision. The first is correct
and the existing code already says why in its own docstring: ``hive_identifier`` is PUBLIC precisely
so *"the group plan (§9/§10.2) names the same columns and must reach the SAME answer"*, because *"a
second normalizer would be a second chance to disagree"*. Writing a group-name-only normalizer would
create exactly the disagreement that docstring exists to prevent, and it would have to be kept in
step by hand forever.

So this module allocates; it does not normalize. The ≤128 bound, the no-truncation rule and the
leading-letter requirement are all inherited, and the tests below prove them THROUGH the allocator
rather than restating them.

**Collision refuses; it does not suffix.** An allocator that turned a taken ``account_daily`` into
``account_daily_2`` would hand back a table nobody asked for, and the caller would publish to it
without noticing. The flat namespace makes this live rather than theoretical: V1 and V2 group names
share one space with no language discriminator (C-D6's migration is what adds the scoping), so a V2
group can collide with a V1 group that has been published for months.
"""
from __future__ import annotations

from collections.abc import Iterable

from featuregen.materialize.identifiers import FeatureNamePlanError, hive_identifier

__all__ = [
    "MAX_HIVE_IDENTIFIER_LENGTH",
    "GroupNameCollision",
    "allocate_group_name",
]

#: Hive's own bound, inherited from ``hive_identifier`` rather than re-declared. Stated here only so
#: the proof below can reference a name instead of a literal.
MAX_HIVE_IDENTIFIER_LENGTH = 128


class GroupNameCollision(ValueError):
    """The normalized name is already bound to another group.

    A distinct exception from ``FeatureNamePlanError`` because the two need different answers: a
    plan error means the name cannot be expressed at all and someone must choose another; a
    collision means the name is fine and already taken, which may be a duplicate submission rather
    than a mistake.
    """


def allocate_group_name(base_name: str, *, bound_names: Iterable[str]) -> str:
    """The Hive identifier this group will occupy, or a refusal.

    Args:
        base_name: the declared base name (``BuildDeclarationV1.base_name``).
        bound_names: every group name already bound. Normalized here before comparison, because a
            stored name that predates this allocator may not be normalized and comparing raw
            against folded would miss a real collision.

    Raises:
        FeatureNamePlanError: the name does not normalize to a Hive identifier — inherited
            unchanged from :func:`hive_identifier`, including the ≤128 bound and the refusal to
            truncate.
        GroupNameCollision: the normalized name is already bound.
    """
    allocated = hive_identifier(base_name)
    taken = {_normalized_or_raw(name) for name in bound_names}
    if allocated in taken:
        raise GroupNameCollision(
            f"group name {base_name!r} normalizes to {allocated!r}, which is already bound. This is "
            f"refused rather than suffixed: an allocator that returned {allocated + '_2'!r} would "
            f"hand back a table nobody asked for and the caller would publish to it without "
            f"noticing. Note the namespace is FLAT across V1 and V2 until C-D6's scoping migration, "
            f"so the holder may be a V1 group published months ago")
    return allocated


def _normalized_or_raw(name: str) -> str:
    """A stored name folded if it can be, kept as-is if it cannot.

    A pre-existing name that does not normalize is still a name that is TAKEN — refusing to compare
    against it because it is malformed would let a new group claim a slot an old one occupies.
    """
    try:
        return hive_identifier(name)
    except FeatureNamePlanError:
        return name.strip().lower()
