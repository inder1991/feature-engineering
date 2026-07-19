"""Phase 3C.2b-i-A · Task 6 — exact physical-landing convergence + deterministic ranking (§5 step 4, §8).

``converge`` intersects the per-operand landing SETS (Task 5's ``OperandPathCandidateV1``s) on FULL
``PhysicalLandingV1`` identity — (catalog, table_ref, composite grain_key_refs) — keeping the best
candidate per (operand, landing), ranks the common landings by the frontier's ``_AUTHORITY_RANK``-
derived semantic rank (authority of the crossings -> fewest total crossings), and selects the single
unambiguous best. A top-semantic-rank tie across DISTINCT landings surfaces as
``ambiguous_physical_grain`` (+ ``landing_ambiguous``) BEFORE any stable ordering; no common landing
is ``no_common_physical_grain``. Conn-free by contract (§8): these are pure-data unit tests that build
``OperandPathCandidateV1``s directly, no DB.
"""
from __future__ import annotations

from featuregen.overlay.upload.planner.contracts import (
    MAX_OPERAND_COMBINATIONS,
    BindingPlanV1,
    BindingSafety,
    CandidateRole,
    PathResolutionStatus,
    PlanResolutionStatus,
    PlanTier,
)
from featuregen.overlay.upload.planner.multisource_assembly import (
    ConvergenceResultV1,
    LandedCombinationV1,
    OperandEnumerationResultV1,
    OperandPathCandidateV1,
    converge,
)
from featuregen.overlay.upload.planner.multisource_contracts import (
    GovernedEndpointV1,
    MultiSourceBoundingMetricsV1,
    MultiSourceReason,
    PhysicalLandingV1,
)


# ── builders (pure data — convergence is conn-free) ────────────────────────────────────────────
def _bounds(**over):
    base = dict(paths_per_operand_truncated=False, operand_combinations_truncated=False,
                states_truncated=False, landing_ambiguous=False, total_states_expanded=0)
    base.update(over)
    return MultiSourceBoundingMetricsV1(**base)


def _plan(pid, *, rank, bridges):
    """A minimal RESOLVED single-source plan carrying only the fields convergence ranks on:
    ``preference_rank`` (the frontier's ``_AUTHORITY_RANK``-derived rank), ``bridge_count`` (total
    crossings), and the canonical ``physical_plan_id`` tiebreak."""
    return BindingPlanV1(
        physical_plan_id=pid, recipe_id="ms:test", target_entity="customer",
        tier=PlanTier.tier_2_one_bridge, catalog_source="core_banking",
        ingredient_bindings=(), path_segments=(),
        resolution_status=PlanResolutionStatus.resolved, primary_reason_code=None, reason_codes=(),
        safety=BindingSafety.safe, preference_rank=rank, preference_reasons=(),
        participating_catalogs=("core_banking",), bridge_count=bridges,
        path_resolution_status=PathResolutionStatus.source_to_target_resolved,
        candidate_role=CandidateRole.selected)


def _cand(*, pid, catalog, table, keys, rank=0, bridges=1, fk="grain-fk"):
    return OperandPathCandidateV1(
        binding_plan=_plan(pid, rank=rank, bridges=bridges),
        landing_catalog=catalog, landing_table_ref=table,
        landing_endpoint=GovernedEndpointV1(
            catalog=catalog, table_ref=table, grain_key_refs=tuple(keys), grain_fact_key=fk))


def _operand(candidates):
    return OperandEnumerationResultV1(
        candidates=tuple(candidates), status=MultiSourceReason.resolved,
        reason_codes=(), bounds=_bounds())


# ── tests ──────────────────────────────────────────────────────────────────────────────────────
def test_two_operands_one_common_landing_yields_one_combination():
    keys = ("wealth.public.customers.customer_id",)
    op0 = _operand([_cand(pid="p0", catalog="wealth", table="public.customers", keys=keys)])
    op1 = _operand([_cand(pid="p1", catalog="wealth", table="public.customers", keys=keys)])

    result = converge([op0, op1], bounds=_bounds())

    assert isinstance(result, ConvergenceResultV1)
    assert result.status is MultiSourceReason.resolved
    assert len(result.landed_combinations) == 1
    combo = result.landed_combinations[0]
    assert isinstance(combo, LandedCombinationV1)
    assert combo.landing == PhysicalLandingV1(
        catalog="wealth", table_ref="public.customers", grain_key_refs=keys)
    # one best candidate per operand, in input order
    assert len(combo.operand_candidates) == 2
    assert combo.operand_candidates[0].binding_plan.physical_plan_id == "p0"
    assert combo.operand_candidates[1].binding_plan.physical_plan_id == "p1"
    assert result.bounds.landing_ambiguous is False
    assert result.reason_codes == ()


def test_composite_grain_landing_preserved_end_to_end():
    keys = ("wealth.public.positions.customer_id", "wealth.public.positions.as_of_date")
    op0 = _operand([_cand(pid="p0", catalog="wealth", table="public.positions", keys=keys)])
    op1 = _operand([_cand(pid="p1", catalog="wealth", table="public.positions", keys=keys)])

    result = converge([op0, op1], bounds=_bounds())

    assert result.status is MultiSourceReason.resolved
    landing = result.landed_combinations[0].landing
    # composite (multi-column) grain preserved verbatim, order intact
    assert landing.grain_key_refs == keys
    assert len(landing.grain_key_refs) == 2


def test_operands_sharing_no_landing_is_no_common_physical_grain():
    op0 = _operand([_cand(pid="p0", catalog="wealth", table="public.customers",
                          keys=("wealth.public.customers.customer_id",))])
    op1 = _operand([_cand(pid="p1", catalog="retail", table="public.parties",
                          keys=("retail.public.parties.party_id",))])

    result = converge([op0, op1], bounds=_bounds())

    assert result.landed_combinations == ()            # never a bare empty tuple...
    assert result.status is MultiSourceReason.no_common_physical_grain
    assert MultiSourceReason.no_common_physical_grain in result.reason_codes


def test_two_distinct_landings_tied_at_top_semantic_rank_is_ambiguous():
    l1 = ("wealth.public.customers.customer_id",)
    l2 = ("wealth.public.households.household_id",)

    def op(pfx):
        # both operands reach BOTH landings with IDENTICAL rank -> a true top-rank tie
        return _operand([
            _cand(pid=f"{pfx}_c", catalog="wealth", table="public.customers", keys=l1,
                  rank=0, bridges=1),
            _cand(pid=f"{pfx}_h", catalog="wealth", table="public.households", keys=l2,
                  rank=0, bridges=1),
        ])

    result = converge([op("a"), op("b")], bounds=_bounds())

    assert result.status is MultiSourceReason.ambiguous_physical_grain
    assert result.landed_combinations == ()            # the ambiguity is surfaced, not tiebroken
    assert result.bounds.landing_ambiguous is True
    assert MultiSourceReason.ambiguous_physical_grain in result.reason_codes


def test_unambiguous_best_landing_selected_when_semantic_ranks_differ():
    l1 = ("wealth.public.customers.customer_id",)       # cheaper crossing -> strictly better
    l2 = ("wealth.public.households.household_id",)      # more authority-cost + more crossings

    def op(pfx):
        return _operand([
            _cand(pid=f"{pfx}_c", catalog="wealth", table="public.customers", keys=l1,
                  rank=0, bridges=1),
            _cand(pid=f"{pfx}_h", catalog="wealth", table="public.households", keys=l2,
                  rank=1, bridges=2),
        ])

    result = converge([op("a"), op("b")], bounds=_bounds())

    assert result.status is MultiSourceReason.resolved
    assert len(result.landed_combinations) == 1
    assert result.landed_combinations[0].landing.grain_key_refs == l1
    assert result.bounds.landing_ambiguous is False


def test_duplicate_candidates_same_landing_keep_best_ranked():
    # distinct bridge-key plans re-derive to the SAME landing (Task-5 note) -> keep the best-ranked
    keys = ("wealth.public.customers.customer_id",)
    op0 = _operand([
        _cand(pid="p0_worse", catalog="wealth", table="public.customers", keys=keys,
              rank=5, bridges=3),
        _cand(pid="p0_best", catalog="wealth", table="public.customers", keys=keys,
              rank=0, bridges=1),
    ])
    op1 = _operand([_cand(pid="p1", catalog="wealth", table="public.customers", keys=keys)])

    result = converge([op0, op1], bounds=_bounds())

    assert result.status is MultiSourceReason.resolved
    combo = result.landed_combinations[0]
    assert combo.operand_candidates[0].binding_plan.physical_plan_id == "p0_best"


def test_large_combination_space_sets_truncation_bound():
    shared = ("wealth.public.customers.customer_id",)

    def op(pfx):
        # 20 distinct landings per operand -> product 20*20 = 400 > MAX_OPERAND_COMBINATIONS (256),
        # but only the rank-0 `shared` landing is common (the rest live in per-operand catalogs).
        cands = [_cand(pid=f"{pfx}_best", catalog="wealth", table="public.customers", keys=shared,
                       rank=0, bridges=1)]
        for i in range(19):
            cands.append(_cand(pid=f"{pfx}_{i}", catalog=pfx, table=f"public.t{i}",
                               keys=(f"{pfx}.public.t{i}.id",), rank=2, bridges=2))
        return _operand(cands)

    assert 20 * 20 > MAX_OPERAND_COMBINATIONS
    result = converge([op("a"), op("b")], bounds=_bounds())

    assert result.status is MultiSourceReason.resolved
    assert result.bounds.operand_combinations_truncated is True
    assert MultiSourceReason.budget_truncated in result.reason_codes
    assert result.landed_combinations[0].landing.grain_key_refs == shared


def test_operand_with_no_candidates_fails_closed_to_no_common():
    op0 = _operand([_cand(pid="p0", catalog="wealth", table="public.customers",
                          keys=("wealth.public.customers.customer_id",))])
    op1 = _operand([])                                  # resolved nothing -> empty landing set

    result = converge([op0, op1], bounds=_bounds())

    assert result.landed_combinations == ()
    assert result.status is MultiSourceReason.no_common_physical_grain
