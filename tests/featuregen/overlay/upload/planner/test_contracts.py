from featuregen.overlay.upload.planner import contracts as c
from featuregen.overlay.upload.planner.safety import evaluate_binding_safety


def test_enums_are_lowercase_snake_and_complete():
    assert c.PlanResolutionStatus.resolved == "resolved"
    assert {s.value for s in c.PlanResolutionStatus} == {
        "resolved", "partially_resolved", "unresolved", "safety_rejected",
        "not_applicable", "bounded_out", "internal_error"}
    assert c.ReplayStrength.conditional == "conditional"
    assert c.PlanTier.tier_1_single_catalog == "tier_1_single_catalog"
    assert c.BindingSafety.safe == "safe"
    # reserved codes for later phases are present so the registry shape is stable
    for reserved in ("missing_required_aggregation", "unsanctioned_bridge",
                     "ambiguous_equal_cross_catalog_paths"):
        assert reserved in {r.value for r in c.ReasonCode}


def test_version_constants_present():
    for v in (c.PLANNER_VERSION, c.REASON_CODE_REGISTRY_VERSION, c.READ_SCOPE_POLICY_VERSION,
              c.ROLE_RESOLUTION_VERSION, c.RECIPE_REGISTRY_VERSION, c.APPLICABILITY_MAPPING_VERSION,
              c.CONCEPT_REGISTRY_VERSION):
        assert isinstance(v, str) and v


def test_bounds_are_positive_ints():
    for b in (c.MAX_CANDIDATE_COLUMNS_PER_NEED_PER_CATALOG, c.MAX_PARTIAL_COMBINATIONS,
              c.MAX_PLANS_PER_RECIPE, c.MAX_AUTHORIZED_CATALOGS_CONSIDERED):
        assert isinstance(b, int) and b > 0


def test_contracts_are_frozen():
    stamp = c.CatalogStateStampV1(catalog_source="core", head_seq=1, last_completed_at="2026-07-14T00:00:00Z")
    import dataclasses
    assert dataclasses.is_dataclass(stamp)
    try:
        stamp.head_seq = 2  # type: ignore[misc]
        raise AssertionError("expected frozen")
    except dataclasses.FrozenInstanceError:
        pass


def test_evaluate_binding_safety_parity_with_safe_to_bind():
    # the boundary must agree with _safe_to_bind for every representative column
    from featuregen.overlay.upload.templates import _Col, _safe_to_bind
    reps = [
        _Col("s", "public.t.a", "t", "a", "text", False, False, "outcome_label", None, None, None, None),  # leakage anchor -> unsafe
        _Col("s", "public.t.b", "t", "b", "numeric", False, False, "monetary_stock", None, "semi_additive", None, "USD"),  # safe
        _Col("s", "public.t.c", "t", "c", "text", False, False, None, None, None, None, None),  # untagged -> safe
        _Col("s", "public.t.d", "t", "d", "text", False, False, "not_a_real_concept", None, None, None, None),  # unknown concept -> safe
    ]
    for col in reps:
        expected = c.BindingSafety.safe if _safe_to_bind(col) else c.BindingSafety.unsafe
        assert evaluate_binding_safety(col) is expected
