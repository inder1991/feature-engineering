"""The frozen RelationshipKind VALUE vocabulary (Task 0S; freeze 0F-7 / deviation D3).

The canonical ``RelationshipKind`` **StrEnum is owned by the semantic-context plan's
Task 1** (ledger §2: "no second enum") and has not landed. Until it does, relationship
kinds travel as plain ``str`` drawn from the vocabulary below — values frozen NOW, the
Python type upgrades THEN. This module therefore freezes values only; it deliberately is
not an Enum and defines no relationship dataclass.

Handoff step (recorded for the landing plan): when the shared StrEnum lands, re-point
``RELATIONSHIP_KIND_VALUES = frozenset(m.value for m in RelationshipKind)`` at the owner
module and delete the literal set. The ownership tripwire in
``tests/featuregen/contracts/test_shared_contract_ownership.py`` fails loudly if the
landed enum's values disagree with this freeze.

Also recorded here because they share the same not-landed-yet status (D3/D9, ledger §2):
the selected directional-realization / ``JoinLegPinV1`` pins (profile plan, Release C
Task 11) and the ``dataset_profile_hash`` content-hash contract (profile plan, Release A
Task 1) have NO owner module at this baseline. There is nothing to import and nothing may
be substituted: Release-A relationship evidence comes from ``join_path.JoinStep``
(authority / approved_join_status / cardinality), and dataset-profile context stays
explicitly unavailable. This plan must never define those symbols (test-enforced).
"""
from __future__ import annotations

__all__ = ["RELATIONSHIP_KIND_VALUES", "validate_relationship_kind"]

#: Frozen by 0F-7 and the ledger §2 sketch; exactly these four, exactly these spellings.
RELATIONSHIP_KIND_VALUES: frozenset[str] = frozenset(
    {"direct_equality", "crosswalk", "transformed", "semantic_only"})


def validate_relationship_kind(value: str) -> str:
    """Return ``value`` verbatim iff it is in the frozen vocabulary; raise loudly otherwise."""
    if value not in RELATIONSHIP_KIND_VALUES:
        raise ValueError(
            f"unknown relationship kind {value!r}; the frozen vocabulary is "
            f"{sorted(RELATIONSHIP_KIND_VALUES)} (freeze 0F-7/D3)")
    return value
