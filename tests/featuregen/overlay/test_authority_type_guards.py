import pytest

from featuregen.overlay.authority import resolve_authority
from featuregen.overlay.identity import ApprovedJoinRef, CatalogObjectRef


class _FakeAdapter:
    def owner_of(self, ref):
        return None


def _obj():
    return CatalogObjectRef(catalog_source="c", object_kind="table", schema="s", table="t")


def test_approved_join_with_object_ref_raises_typeerror():
    # F13: the union narrowing must be an explicit `raise TypeError`, not a bare `assert`
    # (asserts are stripped under `python -O`). A CatalogObjectRef in the approved_join branch
    # is a contract/type mismatch -> TypeError, deterministically, optimized or not.
    with pytest.raises(TypeError):
        resolve_authority(None, _FakeAdapter(), _obj(), "approved_join")


def test_object_fact_with_join_ref_raises_typeerror():
    # F13: the fall-through branch (any non-approved_join, non-policy_tag fact) must reject a
    # mis-typed ApprovedJoinRef with TypeError rather than silently mis-resolving authority
    # under `python -O` (an SoD-relevant defect).
    join = ApprovedJoinRef(from_ref=_obj(), to_ref=_obj(), column_pairs=(), cardinality="1:1")
    with pytest.raises(TypeError):
        resolve_authority(None, _FakeAdapter(), join, "descriptive_stat")
