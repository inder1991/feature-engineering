"""Phase-2A Task A2 — tests for the deterministic presentation-priority ranker.

Covers the two SEPARATE projections (canonical rank vs initial view), the ordering invariants, the
binding-acceptability gate, the deterministic backfill relaxation (stage -> family cap -> semantic
group), the separate reason-code streams, and determinism / purity.
"""
from __future__ import annotations

import random

from featuregen.overlay.upload.taxonomy.ranking import (
    InitialViewReasonCode,
    RankedRecipe,
    RankReasonCode,
    RankSignals,
    rank_eligible,
)
from featuregen.overlay.upload.taxonomy.ranking_signals import (
    BindingQuality,
    EntityCompatibility,
    ModellingContextFit,
    PITCompleteness,
)


# ── factories ───────────────────────────────────────────────────────────────────────────────────
def _sig(
    recipe_id: str = "r",
    *,
    tier: str = "primary",
    binding: BindingQuality = BindingQuality.EXACT,
    context: ModellingContextFit = ModellingContextFit.NEUTRAL,
    pit: PITCompleteness = PITCompleteness.COMPLETE,
    explain: str = "H",
    family: str = "fam",
    model: str | None = None,
    stage: str | None = None,
    group: str | None = None,
    entity: EntityCompatibility = EntityCompatibility.UNKNOWN,
) -> RankSignals:
    return RankSignals(
        relevance_tier=tier,
        binding_quality=binding,
        modelling_context_fit=context,
        pit_completeness=pit,
        explainability=explain,
        family=family,
        journey_model_id=model,
        journey_stage_id=stage,
        semantic_group=group if group is not None else recipe_id,
        entity_compatibility=entity,
    )


def _rank(sigs: dict[str, RankSignals], **kw: object) -> list[RankedRecipe]:
    return rank_eligible(list(sigs), sigs, ranking_version="rank-v1", **kw)  # type: ignore[arg-type]


def _by_id(result: list[RankedRecipe]) -> dict[str, RankedRecipe]:
    return {r.recipe_id: r for r in result}


# ── canonical ordering invariants ─────────────────────────────────────────────────────────────────
def test_primary_outranks_supporting():
    sigs = {"sup": _sig("sup", tier="supporting"), "prim": _sig("prim", tier="primary")}
    ranked = _by_id(_rank(sigs))
    assert ranked["prim"].canonical_rank < ranked["sup"].canonical_rank


def test_exact_binding_outranks_strong_within_tier():
    sigs = {
        "strong": _sig("strong", binding=BindingQuality.STRONG),
        "exact": _sig("exact", binding=BindingQuality.EXACT),
    }
    ranked = _by_id(_rank(sigs))
    assert ranked["exact"].canonical_rank < ranked["strong"].canonical_rank


def test_binding_quality_outranks_explainability():
    # A high-explainability weak-binding recipe ranks BELOW a low-explainability strong-binding one
    # at the same tier/context: binding_quality is a higher-priority axis than explainability.
    sigs = {
        "weak_hi": _sig("weak_hi", binding=BindingQuality.ACCEPTABLE, explain="H"),
        "strong_lo": _sig("strong_lo", binding=BindingQuality.STRONG, explain="L"),
    }
    ranked = _by_id(_rank(sigs))
    assert ranked["strong_lo"].canonical_rank < ranked["weak_hi"].canonical_rank


def test_context_fit_outranks_lower_axes():
    # modelling_context_fit is axis 2 — a REQUIRED_MATCH outranks a NEUTRAL even when recipe_id would
    # order them the other way.
    sigs = {
        "z_required": _sig("z_required", context=ModellingContextFit.REQUIRED_MATCH),
        "a_neutral": _sig("a_neutral", context=ModellingContextFit.NEUTRAL),
    }
    ranked = _by_id(_rank(sigs))
    assert ranked["z_required"].canonical_rank < ranked["a_neutral"].canonical_rank
    assert RankReasonCode.REQUIRED_CONTEXT_MATCH in ranked["z_required"].rank_reasons


def test_pit_completeness_used_as_tiebreak():
    # Identical on every higher axis; differ only on pit. COMPLETE must beat PARTIAL even though the
    # recipe_id order would otherwise put the PARTIAL one first.
    sigs = {
        "z_complete": _sig("z_complete", pit=PITCompleteness.COMPLETE),
        "a_partial": _sig("a_partial", pit=PITCompleteness.PARTIAL),
    }
    ranked = _by_id(_rank(sigs))
    assert ranked["z_complete"].canonical_rank < ranked["a_partial"].canonical_rank


def test_ties_break_on_recipe_id_ascending():
    sigs = {"b": _sig("b"), "a": _sig("a")}
    result = _rank(sigs)
    assert [r.recipe_id for r in result] == ["a", "b"]
    assert result[0].canonical_rank == 1 and result[1].canonical_rank == 2


# ── canonical rank is IMMUTABLE under the diversity pass ────────────────────────────────────────────
def test_canonical_rank_immutable_under_family_cap():
    # 4 family-A recipes at ranks 1-4 + a family-B at 5, cap 3, initial_view_size 4
    # -> initial view = ranks 1,2,3,5 ; the capped recipe a4 KEEPS canonical_rank 4, b1 keeps 5.
    sigs = {
        "a1": _sig("a1", family="A"),
        "a2": _sig("a2", family="A"),
        "a3": _sig("a3", family="A"),
        "a4": _sig("a4", family="A"),
        "b1": _sig("b1", family="B"),
    }
    ranked = _by_id(_rank(sigs, initial_view_size=4, per_family_cap=3))
    assert [ranked[k].canonical_rank for k in ("a1", "a2", "a3", "a4", "b1")] == [1, 2, 3, 4, 5]
    assert ranked["a4"].canonical_rank == 4  # immutable under the cap
    assert ranked["b1"].canonical_rank == 5
    # Initial view is ranks 1,2,3,5 (a4 capped out despite a lower canonical rank than b1).
    assert {k for k, v in ranked.items() if v.selected_for_initial_view} == {"a1", "a2", "a3", "b1"}
    assert ranked["a4"].selected_for_initial_view is False
    assert ranked["a4"].initial_view_reasons == (
        InitialViewReasonCode.FAMILY_CAP_NOT_IN_INITIAL_VIEW,)


# ── binding-acceptability gate: ambiguous never in the initial view ─────────────────────────────────
def test_ambiguous_binding_never_in_initial_view_but_still_ranked():
    sigs = {
        "good": _sig("good", binding=BindingQuality.STRONG),
        "amb": _sig("amb", binding=BindingQuality.AMBIGUOUS),
    }
    ranked = _by_id(_rank(sigs))
    # It is still ranked (canonical rank assigned) ...
    assert ranked["amb"].canonical_rank == 2
    # ... but the gate keeps it out of the initial view regardless of headroom.
    assert ranked["amb"].selected_for_initial_view is False
    assert ranked["amb"].initial_view_reasons == (
        InitialViewReasonCode.AMBIGUOUS_BINDING_NOT_IN_INITIAL_VIEW,)
    assert ranked["good"].selected_for_initial_view is True


def test_ambiguous_gate_precedes_context_promotion():
    # A structurally weak (AMBIGUOUS) binding is NOT promoted into the view by a REQUIRED_MATCH context.
    sigs = {
        "amb_req": _sig("amb_req", binding=BindingQuality.AMBIGUOUS,
                        context=ModellingContextFit.REQUIRED_MATCH),
        "exact_neutral": _sig("exact_neutral", binding=BindingQuality.EXACT,
                              context=ModellingContextFit.NEUTRAL),
    }
    ranked = _by_id(_rank(sigs))
    assert ranked["amb_req"].selected_for_initial_view is False
    assert ranked["exact_neutral"].selected_for_initial_view is True


# ── stage diversity + backfill relaxation ──────────────────────────────────────────────────────────
def _collections_recipes() -> dict[str, RankSignals]:
    # Three collections-journey recipes; two share a stage, one is distinct.
    return {
        "r1": _sig("r1", family="cA", model="collections", stage="early_dpd", group="g1"),
        "r2": _sig("r2", family="cB", model="collections", stage="early_dpd", group="g2"),
        "r3": _sig("r3", family="cC", model="collections", stage="mid_dpd", group="g3"),
    }


def test_stage_diversity_prefers_distinct_stage_within_model():
    # size 2: prefer covering a distinct journey_stage_id within the shared model, so r3 (mid_dpd) is
    # selected over the higher-ranked r2 (early_dpd, already covered by r1).
    ranked = _by_id(_rank(_collections_recipes(), initial_view_size=2))
    assert ranked["r1"].selected_for_initial_view is True
    assert ranked["r3"].selected_for_initial_view is True
    assert ranked["r2"].selected_for_initial_view is False
    assert ranked["r2"].initial_view_reasons == (InitialViewReasonCode.STAGE_DIVERSITY,)
    # canonical rank is unchanged by the diversity preference.
    assert ranked["r2"].canonical_rank == 2


def test_backfill_relaxes_stage_diversity_when_underfilled():
    # size 3: the stage-diversity preference is relaxed (pass 2) so the deferred r2 is admitted.
    ranked = _by_id(_rank(_collections_recipes(), initial_view_size=3))
    assert all(ranked[k].selected_for_initial_view for k in ("r1", "r2", "r3"))
    assert ranked["r2"].initial_view_reasons == (InitialViewReasonCode.SELECTED_INITIAL_VIEW,)


def test_family_cap_relaxation_is_fair_round_robin():
    # Two families of 5 (distinct groups, no journey), cap 3, size 8. After the strict pass each family
    # holds its cap (3+3=6). The family-cap relaxation adds ONE extra per family per round -> a4 AND b4
    # (not a4 + a5), demonstrating fair incremental relaxation rather than greedy canonical fill.
    sigs: dict[str, RankSignals] = {}
    for i in range(1, 6):
        sigs[f"a{i}"] = _sig(f"a{i}", family="A", group=f"gA{i}")
        sigs[f"b{i}"] = _sig(f"b{i}", family="B", group=f"gB{i}")
    ranked = _by_id(_rank(sigs, initial_view_size=8, per_family_cap=3))
    selected = {k for k, v in ranked.items() if v.selected_for_initial_view}
    assert selected == {"a1", "a2", "a3", "a4", "b1", "b2", "b3", "b4"}
    assert "a5" not in selected and "b5" not in selected


def test_backfill_relaxes_stage_before_family_cap():
    # One family, one journey model, two recipes share a stage. cap 1 forces the family cap to bind,
    # but stage diversity is relaxed FIRST: with size 2 and cap 1, the family cap (not stage) is what
    # ultimately blocks the third — proving the relaxation order stage -> family.
    sigs = {
        "r1": _sig("r1", family="F", model="collections", stage="early_dpd", group="g1"),
        "r2": _sig("r2", family="F", model="collections", stage="early_dpd", group="g2"),
        "r3": _sig("r3", family="F", model="collections", stage="mid_dpd", group="g3"),
    }
    # size 2, cap 1: pass 1 selects r1 (early), defers r2 (stage), r3 (mid) blocked by family cap (F=1).
    ranked = _by_id(_rank(sigs, initial_view_size=2, per_family_cap=1))
    # Under-filled -> pass 2 relaxes stage and admits r2 (family cap still 1? no: F now 1 -> blocked).
    # So only r1 fits under cap 1 until family cap relaxes in pass 3, which admits the next by canonical
    # rank: r2.
    assert ranked["r1"].selected_for_initial_view is True
    assert ranked["r2"].selected_for_initial_view is True  # admitted via stage-then-family relaxation
    assert ranked["r3"].selected_for_initial_view is False


# ── semantic-group dedup: one variant unless the set cannot otherwise fill ───────────────────────────
def test_one_variant_per_semantic_group_when_alternatives_exist():
    sigs = {
        "g_a": _sig("g_a", group="g", family="fa"),
        "g_b": _sig("g_b", group="g", family="fb"),
        "h_a": _sig("h_a", group="h", family="fc"),
    }
    ranked = _by_id(_rank(sigs, initial_view_size=2))
    assert ranked["g_a"].selected_for_initial_view is True
    assert ranked["h_a"].selected_for_initial_view is True
    assert ranked["g_b"].selected_for_initial_view is False
    assert ranked["g_b"].initial_view_reasons == (
        InitialViewReasonCode.DUPLICATE_VARIANT_NOT_IN_INITIAL_VIEW,)


def test_all_one_semantic_group_fills_only_because_set_cannot_otherwise():
    # Every recipe shares one semantic group: the dedup is relaxed (last resort) so the view fills.
    sigs = {f"v{i}": _sig(f"v{i}", group="g", family=f"f{i}") for i in range(5)}
    ranked = _by_id(_rank(sigs, initial_view_size=15))
    assert all(v.selected_for_initial_view for v in ranked.values())


def test_all_one_family_relaxes_cap_to_fill():
    sigs = {f"r{i}": _sig(f"r{i}", family="F", group=f"g{i}") for i in range(5)}
    result = _rank(sigs, initial_view_size=15, per_family_cap=3)
    assert len(result) == 5
    assert all(r.selected_for_initial_view for r in result)


# ── separate reason streams (rank vs initial-view) ─────────────────────────────────────────────────
def test_rank_reasons_carry_positive_and_negative_factors():
    good = _sig("good", tier="primary", binding=BindingQuality.EXACT,
                context=ModellingContextFit.REQUIRED_MATCH, pit=PITCompleteness.COMPLETE, explain="H")
    weak = _sig("weak", tier="supporting", binding=BindingQuality.ACCEPTABLE,
                context=ModellingContextFit.NEUTRAL, pit=PITCompleteness.PARTIAL, explain="L")
    ranked = _by_id(_rank({"good": good, "weak": weak}))
    good_reasons = set(ranked["good"].rank_reasons)
    assert {
        RankReasonCode.PRIMARY_USE_CASE_MATCH,
        RankReasonCode.REQUIRED_CONTEXT_MATCH,
        RankReasonCode.EXACT_BINDING,
        RankReasonCode.PIT_COMPLETE,
        RankReasonCode.HIGH_EXPLAINABILITY,
        RankReasonCode.ENTITY_GRAIN_UNKNOWN,
    } <= good_reasons
    weak_reasons = set(ranked["weak"].rank_reasons)
    assert {
        RankReasonCode.SUPPORTING_MATCH,
        RankReasonCode.LOW_BINDING_QUALITY,
        RankReasonCode.PIT_METADATA_INCOMPLETE,
    } <= weak_reasons
    assert RankReasonCode.EXACT_BINDING not in weak_reasons
    assert RankReasonCode.PIT_COMPLETE not in weak_reasons


def test_rank_reasons_and_initial_view_reasons_are_independent_streams():
    # A capped-out recipe: its rank_reasons describe the ordering factors, its initial_view_reasons the
    # why-not. The two enum families are disjoint and stamped separately.
    sigs = {
        "a1": _sig("a1", family="A"),
        "a2": _sig("a2", family="A"),
        "a3": _sig("a3", family="A"),
        "a4": _sig("a4", family="A"),
    }
    ranked = _by_id(_rank(sigs, initial_view_size=3, per_family_cap=3))
    a4 = ranked["a4"]
    assert RankReasonCode.PRIMARY_USE_CASE_MATCH in a4.rank_reasons
    assert RankReasonCode.EXACT_BINDING in a4.rank_reasons
    assert a4.initial_view_reasons == (InitialViewReasonCode.FAMILY_CAP_NOT_IN_INITIAL_VIEW,)
    # The two vocabularies never bleed into each other.
    assert all(isinstance(rc, RankReasonCode) for rc in a4.rank_reasons)
    assert all(isinstance(ic, InitialViewReasonCode) for ic in a4.initial_view_reasons)


def test_selected_recipe_stamped_with_selected_reason():
    ranked = _by_id(_rank({"only": _sig("only")}))
    assert ranked["only"].initial_view_reasons == (InitialViewReasonCode.SELECTED_INITIAL_VIEW,)


# ── determinism / purity / edge cases ──────────────────────────────────────────────────────────────
def _varied_signals() -> dict[str, RankSignals]:
    tiers = ("primary", "supporting")
    bindings = (BindingQuality.EXACT, BindingQuality.STRONG, BindingQuality.ACCEPTABLE)
    pits = (PITCompleteness.COMPLETE, PITCompleteness.PARTIAL, PITCompleteness.NOT_APPLICABLE)
    explains = ("H", "M", "L")
    sigs: dict[str, RankSignals] = {}
    for i in range(24):
        sigs[f"rec_{i:02d}"] = _sig(
            f"rec_{i:02d}",
            tier=tiers[i % 2],
            binding=bindings[i % 3],
            pit=pits[i % 3],
            explain=explains[i % 3],
            family=f"fam_{i % 4}",
            group=f"grp_{i % 6}",
        )
    return sigs


def test_deterministic_under_shuffled_input():
    sigs = _varied_signals()
    baseline = rank_eligible(list(sigs), sigs, ranking_version="v1")
    rng = random.Random(1234)
    for _ in range(5):
        ids = list(sigs)
        rng.shuffle(ids)
        shuffled_map = {k: sigs[k] for k in ids}  # a different dict iteration order too
        again = rank_eligible(ids, shuffled_map, ranking_version="v1")
        assert again == baseline


def test_pure_function_repeated_call_is_identical():
    sigs = _varied_signals()
    assert rank_eligible(list(sigs), sigs, ranking_version="v1") == rank_eligible(
        list(sigs), sigs, ranking_version="v1")


def test_ranking_version_does_not_mutate_a_prior_projection():
    # A ranking_version change never reorders/mutates the projection (it is provenance, not an input to
    # the ordering) — a pure function of the ids + signals only.
    sigs = _varied_signals()
    v1 = rank_eligible(list(sigs), sigs, ranking_version="v1")
    v2 = rank_eligible(list(sigs), sigs, ranking_version="v2-completely-different")
    assert v1 == v2


def test_zero_eligible_returns_empty():
    assert rank_eligible([], {}, ranking_version="v1") == []


def test_fewer_than_size_returns_all_eligible():
    sigs = {"a": _sig("a", group="ga"), "b": _sig("b", group="gb"), "c": _sig("c", group="gc")}
    result = _rank(sigs, initial_view_size=15)
    assert len(result) == 3
    assert all(r.selected_for_initial_view for r in result)


def test_missing_signal_ids_are_skipped():
    # An id in the rankable set with no signal cannot be ranked; it is deterministically dropped.
    sigs = {"a": _sig("a"), "b": _sig("b")}
    result = rank_eligible(["a", "ghost", "b"], sigs, ranking_version="v1")
    assert {r.recipe_id for r in result} == {"a", "b"}
    assert [r.canonical_rank for r in result] == [1, 2]


def test_duplicate_rankable_ids_ranked_once():
    sigs = {"a": _sig("a"), "b": _sig("b")}
    result = rank_eligible(["a", "b", "a", "b"], sigs, ranking_version="v1")
    assert [r.recipe_id for r in result] == ["a", "b"]


def test_canonical_ranks_are_dense_and_one_based():
    sigs = _varied_signals()
    result = _rank(sigs)
    assert [r.canonical_rank for r in result] == list(range(1, len(result) + 1))
