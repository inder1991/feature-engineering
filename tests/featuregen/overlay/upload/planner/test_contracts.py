from featuregen.overlay.upload.planner import contracts as c
from featuregen.overlay.upload.planner.safety import evaluate_binding_safety


def test_enums_are_lowercase_snake_and_complete():
    assert c.PlanResolutionStatus.resolved == "resolved"
    assert {s.value for s in c.PlanResolutionStatus} == {
        "resolved", "resolved_with_ambiguity", "partially_resolved", "unresolved",
        "safety_rejected", "not_applicable", "bounded_out", "internal_error"}
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


# ---- 3B.3b (Task B1): contract additions + canonical constructor ----

def test_new_enum_members():
    assert c.PlanTier.tier_2_one_bridge == "tier_2_one_bridge"
    assert c.PlanTier.tier_3_multi_bridge == "tier_3_multi_bridge"
    assert {s.value for s in c.PathResolutionStatus} == {
        "ingredient_binding_only", "source_to_target_resolved", "source_to_target_rejected"}
    assert c.PlanResolutionStatus.resolved_with_ambiguity == "resolved_with_ambiguity"
    for r in ("unsupported_multi_grain_ingredients", "ambiguous_semantic_path",
              "bounded_out_max_bridges", "bounded_out_max_frontier_states"):
        assert r in {x.value for x in c.ReasonCode}


def test_tier_from_bridge_count():
    assert c.tier_from_bridge_count(0) is c.PlanTier.tier_1_single_catalog
    assert c.tier_from_bridge_count(1) is c.PlanTier.tier_2_one_bridge
    assert c.tier_from_bridge_count(3) is c.PlanTier.tier_3_multi_bridge


def test_make_binding_plan_validates_and_derives():
    seg = c.BindingPathSegmentV1(c.SegmentKind.direct_catalog, "core")
    plan = c.make_binding_plan(
        recipe_id="t", target_entity="customer", catalog_source="core",
        ingredient_bindings=(), path_segments=(seg,),
        resolution_status=c.PlanResolutionStatus.resolved,
        path_resolution_status=c.PathResolutionStatus.source_to_target_resolved,
        primary_reason_code=None, reason_codes=(), safety=c.BindingSafety.safe,
        preference_rank=0, preference_reasons=(), candidate_role=c.CandidateRole.selected)
    assert plan.participating_catalogs == ("core",) and plan.bridge_count == 0
    assert plan.tier is c.PlanTier.tier_1_single_catalog
    assert len(plan.plan_id) > 3


def _bridge_seg(cat, e="account"):
    return c.BindingPathSegmentV1(c.SegmentKind.governed_bridge, cat, from_entity=e, to_entity=e,
                                  bridge_fact_key=f"b_{cat}")


def test_make_binding_plan_derives_multibridge_participation():
    # a 2-bridge path core->other->third derives ordered/deduped participation, bridge_count, tier_3
    segs = (c.BindingPathSegmentV1(c.SegmentKind.direct_catalog, "core"),
            _bridge_seg("other"), _bridge_seg("third"))
    plan = c.make_binding_plan(
        recipe_id="t", target_entity="customer", catalog_source="core",
        ingredient_bindings=(), path_segments=segs,
        resolution_status=c.PlanResolutionStatus.resolved,
        path_resolution_status=c.PathResolutionStatus.source_to_target_resolved,
        primary_reason_code=None, reason_codes=(), safety=c.BindingSafety.safe,
        preference_rank=0, preference_reasons=(), candidate_role=c.CandidateRole.selected)
    assert plan.participating_catalogs == ("core", "other", "third")
    assert plan.bridge_count == 2 and plan.tier is c.PlanTier.tier_3_multi_bridge


def test_make_binding_plan_rejects_resolved_path_with_unresolved_status():
    import pytest
    # a source_to_target_resolved plan whose resolution_status is unresolved is contradictory -> fail-closed
    seg = c.BindingPathSegmentV1(c.SegmentKind.direct_catalog, "core")
    with pytest.raises(ValueError):
        c.make_binding_plan(recipe_id="t", target_entity="c", catalog_source="core",
                            ingredient_bindings=(), path_segments=(seg,),
                            resolution_status=c.PlanResolutionStatus.unresolved,
                            path_resolution_status=c.PathResolutionStatus.source_to_target_resolved,
                            primary_reason_code=None, reason_codes=(), safety=c.BindingSafety.safe,
                            preference_rank=0, preference_reasons=(), candidate_role=c.CandidateRole.rejected)


def test_make_binding_plan_rejects_over_budget_bridges():
    import pytest
    # 3 governed bridges exceeds MAX_BRIDGES_PER_PLAN (2) -> fail-closed, never constructed
    segs = (c.BindingPathSegmentV1(c.SegmentKind.direct_catalog, "core"),
            _bridge_seg("c1"), _bridge_seg("c2"), _bridge_seg("c3"))
    with pytest.raises(ValueError):
        c.make_binding_plan(recipe_id="t", target_entity="c", catalog_source="core",
                            ingredient_bindings=(), path_segments=segs,
                            resolution_status=c.PlanResolutionStatus.resolved,
                            path_resolution_status=c.PathResolutionStatus.source_to_target_resolved,
                            primary_reason_code=None, reason_codes=(), safety=c.BindingSafety.safe,
                            preference_rank=0, preference_reasons=(), candidate_role=c.CandidateRole.selected)
