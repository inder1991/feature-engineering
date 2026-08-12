"""Task 0S — the frozen RelationshipKind VALUE vocabulary (freeze D3 / 0F-7).

The canonical ``RelationshipKind`` StrEnum is owned by the unlanded semantic plan's Task 1;
until it lands, relationship kinds travel as ``str`` drawn from the vocabulary frozen here.
This module may freeze VALUES only — never a competing enum or flat relationship model.
"""
from __future__ import annotations

import pytest

from featuregen.contracts.relationship_kinds import (
    RELATIONSHIP_KIND_VALUES,
    validate_relationship_kind,
)


def test_the_vocabulary_is_exactly_the_frozen_four():
    assert RELATIONSHIP_KIND_VALUES == frozenset(
        {"direct_equality", "crosswalk", "transformed", "semantic_only"})


def test_validate_returns_the_member_verbatim():
    assert validate_relationship_kind("crosswalk") == "crosswalk"


@pytest.mark.parametrize("bad", ["", "equality", "CROSSWALK", "direct-equality", None])
def test_unknown_kind_fails_loudly(bad):
    with pytest.raises(ValueError):
        validate_relationship_kind(bad)  # type: ignore[arg-type]
