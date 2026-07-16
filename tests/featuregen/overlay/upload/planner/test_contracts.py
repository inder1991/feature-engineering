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
    assert len(plan.physical_plan_id) > 3


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


def test_physical_plan_id_distinguishes_path_resolution_status():
    # Regression (b5 review): a tier-1 ingredient_binding_only plan and an immediate-dead-end
    # assembly reject (source_to_target_rejected) over the SAME recipe/catalog/refs/segments/tier
    # collided on the physical id because the hashed material omitted path_resolution_status. 3B.4
    # keys its store by physical_plan_id, so the collision would silently conflate a resolved plan
    # with a reject.
    def _plan(path_status: c.PathResolutionStatus) -> c.BindingPlanV1:
        return c.make_binding_plan(
            recipe_id="t", target_entity="customer", catalog_source="ops",
            ingredient_bindings=(),
            path_segments=(c.BindingPathSegmentV1(c.SegmentKind.direct_catalog, "ops"),),
            resolution_status=c.PlanResolutionStatus.unresolved,
            path_resolution_status=path_status,
            primary_reason_code=None, reason_codes=(), safety=c.BindingSafety.safe,
            preference_rank=0, preference_reasons=(), candidate_role=c.CandidateRole.rejected)

    binding_only = _plan(c.PathResolutionStatus.ingredient_binding_only)
    rejected = _plan(c.PathResolutionStatus.source_to_target_rejected)
    # same tier (both bridge-free -> tier_1) and same segments: only path_resolution_status differs
    assert binding_only.tier is rejected.tier is c.PlanTier.tier_1_single_catalog
    assert binding_only.physical_plan_id != rejected.physical_plan_id


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


# ---- 3B.3c (Task C1): version split + contract/declaration axes + contract_id identity ----

# Captured from HEAD (99e9bb5) BEFORE the version split, by running make_binding_plan on the
# pre-change code with exactly the inputs below. Proves the plan_id->physical_plan_id rename and
# the PLAN_CONTRACT_VERSION bump moved NO physical id (F1): the material tail swapped from
# PLAN_CONTRACT_VERSION ("3b3b.1.0.0" at capture time) to the frozen PHYSICAL_PLAN_VERSION,
# which is byte-identical.
_PINNED_3B3B_ID = "bp_60a22f0fbec4b0e6"


def _dt(y, m, d):
    from datetime import datetime
    return datetime(y, m, d)


def _replace(obj, **kw):
    import dataclasses
    return dataclasses.replace(obj, **kw)


def _compiled_plan(*, declaration_status, contract_resolution_status, contract_reason_codes):
    seg = c.BindingPathSegmentV1(c.SegmentKind.direct_catalog, "core")
    p = c.make_binding_plan(recipe_id="t", target_entity="cust", catalog_source="core",
        ingredient_bindings=(), path_segments=(seg,),
        resolution_status=c.PlanResolutionStatus.resolved,
        path_resolution_status=c.PathResolutionStatus.source_to_target_resolved,
        primary_reason_code=None, reason_codes=(), safety=c.BindingSafety.safe,
        preference_rank=0, preference_reasons=(), candidate_role=c.CandidateRole.selected)
    return _replace(p, declaration_status=declaration_status,
                    contract_resolution_status=contract_resolution_status,
                    contract_reason_codes=contract_reason_codes)


def test_version_split_keeps_physical_id_stable():
    # PHYSICAL_PLAN_VERSION freezes the physical material; PLAN_CONTRACT_VERSION may bump freely.
    assert c.PHYSICAL_PLAN_VERSION == "3b3b.1.0.0"
    assert c.PLAN_CONTRACT_VERSION == "3b3c.1.0.0"
    seg = c.BindingPathSegmentV1(c.SegmentKind.direct_catalog, "core")
    p = c.make_binding_plan(recipe_id="t", target_entity="cust", catalog_source="core",
        ingredient_bindings=(), path_segments=(seg,),
        resolution_status=c.PlanResolutionStatus.resolved,
        path_resolution_status=c.PathResolutionStatus.source_to_target_resolved,
        primary_reason_code=None, reason_codes=(), safety=c.BindingSafety.safe,
        preference_rank=0, preference_reasons=(), candidate_role=c.CandidateRole.selected)
    # this exact id was recorded from HEAD before the version split — proves the rename+bump didn't move it
    assert p.physical_plan_id == _PINNED_3B3B_ID
    assert p.contract_resolution_status is c.ContractResolutionStatus.not_compiled
    assert p.declaration_status is c.DeclarationStatus.not_compiled
    assert p.audit_envelope is None


def test_contract_id_excludes_freshness_and_time():
    p = _compiled_plan(declaration_status=c.DeclarationStatus.resolved,
                       contract_resolution_status=c.ContractResolutionStatus.unresolved_freshness,
                       contract_reason_codes=(c.ReasonCode.participating_catalog_stale,))
    a = c.make_contract_id(p, resolved_at_compilation=_dt(2026, 1, 1))
    # same DECLARATIONS, different freshness outcome + time → SAME contract_id
    p2 = _replace(p, contract_resolution_status=c.ContractResolutionStatus.resolved,
                  contract_reason_codes=())
    b = c.make_contract_id(p2, resolved_at_compilation=_dt(2099, 9, 9))
    assert a == b and a.startswith("cc_")


def test_new_enums_and_reason_codes():
    for r in ("ingredient_not_connected_to_path", "aggregation_strategy_missing",
              "aggregation_incompatible_with_additivity", "aggregation_weight_missing",
              "aggregation_components_missing", "aggregation_axis_unsupported",
              "aggregation_composition_unsupported", "semi_additive_temporal_strategy_missing",
              "temporal_anchor_missing", "temporal_anchor_ambiguous", "additivity_source_conflict",
              "physical_cardinality_unavailable", "safety_evaluation_incomplete", "leakage_anchor_read",
              "protected_attribute_read", "freshness_stamp_unavailable", "participating_catalog_stale",
              "projection_lagging", "catalog_mutated_during_compile", "compile_budget_exhausted"):
        assert r in {x.value for x in c.ReasonCode}
    assert c.to_additivity_class("n/a") is c.AdditivityClass.not_applicable
    assert c.to_additivity_class("garbage") is c.AdditivityClass.unknown   # never raises
    assert c.to_additivity_class("semi_additive") is c.AdditivityClass.semi_additive


def test_contract_status_is_declaration_status_plus_freshness():
    # ContractResolutionStatus (observed) = DeclarationStatus (identity-bearing) + unresolved_freshness;
    # the two axes must never drift apart in any other member.
    decl = {s.value for s in c.DeclarationStatus}
    contract = {s.value for s in c.ContractResolutionStatus}
    assert contract == decl | {"unresolved_freshness"}
