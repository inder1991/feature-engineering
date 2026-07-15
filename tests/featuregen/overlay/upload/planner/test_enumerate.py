
from featuregen.overlay.upload.planner.candidates import CandidateDiscoveryV1
from featuregen.overlay.upload.planner.contracts import (
    BindingQuality,
    BindingSafety,
    CandidateRole,
    IngredientCandidateV1,
    PathResolutionStatus,
    PlanResolutionStatus,
    PlanTier,
    SegmentKind,
)
from featuregen.overlay.upload.planner.enumerate import enumerate_single_catalog_plans
from featuregen.overlay.upload.templates import Need, Template


def _cand(role, ref, *, eligible=True):
    return IngredientCandidateV1(recipe_id="t", need_role=role, concept="c", required_grains=(),
        join_role="", temporal_role="", catalog_source="core", object_ref=ref, actual_source_grain="account",
        binding_quality=BindingQuality.grain_and_role_fit, eligible=eligible,
        safety=BindingSafety.safe if eligible else BindingSafety.unsafe, reason_codes=())


def _tmpl(*roles, optional=()):
    return Template(id="t", family="f", intent="i",
                    needs=tuple(Need(role=r, concept="c", optional=(r in optional)) for r in roles),
                    params={}, aggregation="a", additivity="n/a", explain="L", use_cases=(), pit="p")


def test_single_eligible_per_need_yields_one_resolved_plan():
    tmpl = _tmpl("a", "b")
    disc = CandidateDiscoveryV1(candidates={"a": (_cand("a", "public.t.a"),), "b": (_cand("b", "public.t.b"),)},
                                candidate_columns_truncated=False, total_candidate_columns_considered=2)
    en = enumerate_single_catalog_plans(tmpl, "core", "customer", disc)
    assert len(en.plans) == 1
    p = en.plans[0]
    assert p.tier is PlanTier.tier_1_single_catalog and p.catalog_source == "core"
    assert p.resolution_status is PlanResolutionStatus.resolved
    assert len(p.path_segments) == 1 and p.path_segments[0].segment_kind is SegmentKind.direct_catalog
    assert {b.bound_object_ref for b in p.ingredient_bindings} == {"public.t.a", "public.t.b"}
    # 3B.3b derived fields: a tier-1 ingredient binding is single-catalog, zero-bridge,
    # ingredient_binding_only (the B5 assembler enriches it; the ranker resets candidate_role)
    assert p.participating_catalogs == ("core",) and p.bridge_count == 0
    assert p.path_resolution_status is PathResolutionStatus.ingredient_binding_only
    assert p.candidate_role is CandidateRole.rejected


def test_cartesian_product_of_eligible_candidates():
    tmpl = _tmpl("a", "b")
    disc = CandidateDiscoveryV1(
        candidates={"a": (_cand("a", "public.t.a1"), _cand("a", "public.t.a2")),
                    "b": (_cand("b", "public.t.b1"),)},
        candidate_columns_truncated=False, total_candidate_columns_considered=3)
    en = enumerate_single_catalog_plans(tmpl, "core", "customer", disc)
    assert len(en.plans) == 2                            # a1×b1, a2×b1
    assert len({p.plan_id for p in en.plans}) == 2       # deterministic distinct ids


def test_ineligible_only_need_gives_partial_plan_not_resolved():
    tmpl = _tmpl("a", "b")
    disc = CandidateDiscoveryV1(candidates={"a": (_cand("a", "public.t.a"),),
                                            "b": (_cand("b", "public.t.b", eligible=False),)},
                                candidate_columns_truncated=False, total_candidate_columns_considered=2)
    en = enumerate_single_catalog_plans(tmpl, "core", "customer", disc)
    # required need 'b' cannot bind (no eligible candidate) -> a partial plan is preserved, not resolved
    assert all(p.resolution_status is PlanResolutionStatus.partially_resolved for p in en.plans)


def test_optional_unbound_need_still_resolved():
    tmpl = _tmpl("a", "b", optional=("b",))
    disc = CandidateDiscoveryV1(candidates={"a": (_cand("a", "public.t.a"),), "b": ()},
                                candidate_columns_truncated=False, total_candidate_columns_considered=1)
    en = enumerate_single_catalog_plans(tmpl, "core", "customer", disc)
    assert len(en.plans) == 1 and en.plans[0].resolution_status is PlanResolutionStatus.resolved


def test_combination_bound_truncates_and_flags():
    # 3 needs x many eligible candidates each -> exceed MAX_PARTIAL_COMBINATIONS
    from featuregen.overlay.upload.planner.contracts import MAX_PARTIAL_COMBINATIONS
    roles = ("a", "b", "c")
    cands = {r: tuple(_cand(r, f"public.t.{r}{i}") for i in range(10)) for r in roles}   # 10^3 = 1000 > 256
    disc = CandidateDiscoveryV1(candidates=cands, candidate_columns_truncated=False,
                                total_candidate_columns_considered=30)
    en = enumerate_single_catalog_plans(_tmpl(*roles), "core", "customer", disc)
    assert en.combinations_truncated is True
    assert len(en.plans) <= MAX_PARTIAL_COMBINATIONS
