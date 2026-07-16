from featuregen.overlay.upload.planner.contracts import (
    BindingPathSegmentV1,
    BindingPlanV1,
    BindingQuality,
    BindingSafety,
    CandidateRole,
    IngredientBindingV1,
    PathResolutionStatus,
    PlanResolutionStatus,
    PlanTier,
    SegmentKind,
)
from featuregen.overlay.upload.planner.order import order_plans


def _plan(pid, refs, *, status=PlanResolutionStatus.resolved, quality=BindingQuality.grain_and_role_fit,
          catalog="core"):
    # direct construction (not make_binding_plan): these tests pin hand-chosen physical ids ("a"/"z")
    # to assert deterministic tie-breaking, which a derived physical_plan_id would defeat.
    binds = tuple(IngredientBindingV1("t", f"r{i}", "c", (), "", "", catalog, r, "account", quality,
                                      BindingSafety.safe, ()) for i, r in enumerate(refs))
    return BindingPlanV1(pid, "t", "customer", PlanTier.tier_1_single_catalog, catalog, binds,
                         (BindingPathSegmentV1(SegmentKind.direct_catalog, catalog),), status, None, (),
                         BindingSafety.safe, -1, (),
                         participating_catalogs=(catalog,), bridge_count=0,
                         path_resolution_status=PathResolutionStatus.ingredient_binding_only,
                         candidate_role=CandidateRole.unranked)


def test_resolved_ranks_before_partial():
    ordered = order_plans([_plan("b", ("public.t.x",), status=PlanResolutionStatus.partially_resolved),
                           _plan("a", ("public.t.y",))]).plans
    assert ordered[0].resolution_status is PlanResolutionStatus.resolved and ordered[0].preference_rank == 0
    assert ordered[1].resolution_status is PlanResolutionStatus.partially_resolved


def test_higher_quality_ranks_first_among_resolved():
    ordered = order_plans([_plan("a", ("public.t.a",), quality=BindingQuality.weak),
                           _plan("b", ("public.t.b",), quality=BindingQuality.grain_and_role_fit)]).plans
    assert ordered[0].physical_plan_id == "b" and ordered[0].preference_rank == 0
    assert ordered[0].preference_reasons                      # ordering audit recorded


def test_full_tie_is_ambiguous_but_deterministic():
    res = order_plans([_plan("z", ("public.t.a",)), _plan("a", ("public.t.a",))])
    assert res.ambiguous is True
    assert [p.physical_plan_id for p in res.plans] == ["a", "z"]      # tie broken by physical_plan_id, stable


def test_resolved_with_ambiguity_ranks_as_a_successful_resolution():
    # B1 carry-forward: resolved_with_ambiguity is a SUCCESSFUL resolution — it must rank with
    # resolved (0), not sink to the .get(..., 9) default below partially_resolved.
    ordered = order_plans([
        _plan("b", ("public.t.x",), status=PlanResolutionStatus.partially_resolved),
        _plan("a", ("public.t.y",), status=PlanResolutionStatus.resolved_with_ambiguity)]).plans
    assert ordered[0].physical_plan_id == "a" and ordered[0].preference_rank == 0
    assert ordered[1].resolution_status is PlanResolutionStatus.partially_resolved
