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


def _cand(*, pid, catalog, table, keys, rank=0, bridges=1, hops=1, authority=0, fk="grain-fk"):
    """A convergence candidate. ``authority_key`` — (worst realizer authority, bridge_count, hops) — is
    the CROSS-RUN-COMPARABLE tuple convergence now ranks/ties on (Task-6 #T6), materialized by Task 5
    from the plan's OWN path; ``rank`` still sets the frontier's per-run ``preference_rank`` (a
    positional index) so a test can DIVERGE the two — the OLD (unsound) key summed ``preference_rank``,
    the NEW one sums ``authority_key``."""
    landing_ep = GovernedEndpointV1(
        catalog=catalog, table_ref=table, grain_key_refs=tuple(keys), grain_fact_key=fk)
    return OperandPathCandidateV1(
        binding_plan=_plan(pid, rank=rank, bridges=bridges),
        landing_catalog=catalog, landing_table_ref=table,
        authority_key=(authority, bridges, hops),
        landing_endpoint=landing_ep, governed_endpoints=(landing_ep,))


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


def test_large_product_but_small_intersection_is_not_truncated():
    # Task-6 MINOR: the theoretical product can be huge while the REALISED work (the intersection) is
    # tiny — a fully-captured run must NOT be tagged capture-incomplete. The OLD product-cap set
    # budget_truncated on product > MAX even here; the corrected cap records it ONLY on a genuine drop.
    shared = ("wealth.public.customers.customer_id",)

    def op(pfx):
        # 20 distinct landings per operand -> product 20*20 = 400 > MAX_OPERAND_COMBINATIONS (256),
        # but only the `shared` landing is common (the rest live in per-operand catalogs).
        cands = [_cand(pid=f"{pfx}_best", catalog="wealth", table="public.customers", keys=shared,
                       rank=0, bridges=1)]
        for i in range(19):
            cands.append(_cand(pid=f"{pfx}_{i}", catalog=pfx, table=f"public.t{i}",
                               keys=(f"{pfx}.public.t{i}.id",), rank=2, bridges=2))
        return _operand(cands)

    assert 20 * 20 > MAX_OPERAND_COMBINATIONS
    result = converge([op("a"), op("b")], bounds=_bounds())

    assert result.status is MultiSourceReason.resolved
    # only ONE landing is common -> nothing dropped -> the run is NOT falsely tagged truncated
    assert result.bounds.operand_combinations_truncated is False
    assert MultiSourceReason.budget_truncated not in result.reason_codes
    assert result.landed_combinations[0].landing.grain_key_refs == shared


def test_intersection_beyond_cap_records_the_bound():
    # Task-6 MINOR (the other side): when the MATERIALISED common landings genuinely exceed the cap,
    # budget_truncated IS recorded — an honest over-materialisation bound. Still fully ranked (never a
    # silent drop of the best): the unambiguous best landing (cheapest crossing) is still selected.
    n = MAX_OPERAND_COMBINATIONS + 1

    def op(pfx):
        # n DISTINCT landings shared by BOTH operands -> len(common) == n > MAX_OPERAND_COMBINATIONS.
        # t0 (bridges=1) is strictly best; the rest (bridges=2) are strictly worse.
        cands = [_cand(pid=f"{pfx}_0", catalog="wealth", table="public.t0",
                       keys=("wealth.public.t0.id",), authority=0, bridges=1)]
        for i in range(1, n):
            cands.append(_cand(pid=f"{pfx}_{i}", catalog="wealth", table=f"public.t{i}",
                               keys=(f"wealth.public.t{i}.id",), authority=0, bridges=2))
        return _operand(cands)

    result = converge([op("a"), op("b")], bounds=_bounds())

    assert result.bounds.operand_combinations_truncated is True
    assert MultiSourceReason.budget_truncated in result.reason_codes
    assert result.status is MultiSourceReason.resolved
    assert result.landed_combinations[0].landing.grain_key_refs == ("wealth.public.t0.id",)


def test_upstream_combination_truncation_propagates():
    # An upstream operand_combinations_truncated bound is preserved even on a small, fully-resolved run.
    keys = ("wealth.public.customers.customer_id",)
    op0 = _operand([_cand(pid="p0", catalog="wealth", table="public.customers", keys=keys)])
    op1 = _operand([_cand(pid="p1", catalog="wealth", table="public.customers", keys=keys)])

    result = converge([op0, op1], bounds=_bounds(operand_combinations_truncated=True))

    assert result.status is MultiSourceReason.resolved
    assert result.bounds.operand_combinations_truncated is True
    assert MultiSourceReason.budget_truncated in result.reason_codes


def test_equal_authority_distinct_landings_from_different_runs_is_ambiguous():
    """#T6 regression — HIDDEN TIE. Two DISTINCT landings of GENUINELY EQUAL authority must surface
    ``ambiguous_physical_grain`` even when their candidates carry DIFFERENT per-run ``preference_rank``
    (a positional index that resets per ``assemble_paths`` run and shifts when ungoverned siblings are
    dropped before it is assigned). The OLD key summed ``preference_rank`` -> the landings got DIFFERENT
    keys -> one silently won, tiebreaking away the real ambiguity. The NEW key sums the path-derived
    ``authority_key`` -> equal authority ties -> the ambiguity is surfaced. FAILS under the old key."""
    l1 = ("wealth.public.customers.customer_id",)
    l2 = ("wealth.public.households.household_id",)

    def op(pfx):
        return _operand([
            # SAME authority_key (0,1,1), DIFFERENT preference_rank (0 vs 3): the old Σ preference_rank
            # key would break the genuine tie; the new Σ authority_key key preserves it.
            _cand(pid=f"{pfx}_c", catalog="wealth", table="public.customers", keys=l1,
                  authority=0, bridges=1, hops=1, rank=0),
            _cand(pid=f"{pfx}_h", catalog="wealth", table="public.households", keys=l2,
                  authority=0, bridges=1, hops=1, rank=3),
        ])

    result = converge([op("a"), op("b")], bounds=_bounds())

    assert result.status is MultiSourceReason.ambiguous_physical_grain
    assert result.landed_combinations == ()            # the ambiguity is surfaced, not tiebroken
    assert result.bounds.landing_ambiguous is True
    assert MultiSourceReason.ambiguous_physical_grain in result.reason_codes


def test_strictly_more_authoritative_landing_resolves_not_ambiguous():
    """#T6 regression — FALSE AMBIGUITY. Two DISTINCT landings that are each best-in-their-own
    ``assemble_paths`` run (so BOTH carry ``preference_rank`` 0) but differ in REAL crossing authority —
    an ``APPROVED_JOIN`` (rank 0) vs an ``INFERRED_JOIN`` (rank 2) realizer — must resolve to the more
    authoritative landing, NOT report a false ambiguity. The OLD key summed ``preference_rank`` (0 == 0)
    -> a manufactured top-rank tie -> spurious ``ambiguous_physical_grain`` (the authority difference
    lived in the per-run rank key but was invisible across runs, the index having reset). The NEW key
    sums ``authority_key`` -> APPROVED strictly beats INFERRED -> resolved. FAILS under the old key."""
    approved = ("wealth.public.customers.customer_id",)     # APPROVED_JOIN crossing -> authority 0
    inferred = ("wealth.public.households.household_id",)    # INFERRED_JOIN crossing -> authority 2

    def op(pfx):
        return _operand([
            _cand(pid=f"{pfx}_a", catalog="wealth", table="public.customers", keys=approved,
                  authority=0, bridges=1, hops=1, rank=0),    # best-in-its-run -> preference_rank 0
            _cand(pid=f"{pfx}_i", catalog="wealth", table="public.households", keys=inferred,
                  authority=2, bridges=1, hops=1, rank=0),    # ALSO best-in-its-run -> preference_rank 0
        ])

    result = converge([op("a"), op("b")], bounds=_bounds())

    assert result.status is MultiSourceReason.resolved
    assert len(result.landed_combinations) == 1
    assert result.landed_combinations[0].landing.grain_key_refs == approved
    assert result.bounds.landing_ambiguous is False


def test_operand_with_no_candidates_fails_closed_to_no_common():
    op0 = _operand([_cand(pid="p0", catalog="wealth", table="public.customers",
                          keys=("wealth.public.customers.customer_id",))])
    op1 = _operand([])                                  # resolved nothing -> empty landing set

    result = converge([op0, op1], bounds=_bounds())

    assert result.landed_combinations == ()
    assert result.status is MultiSourceReason.no_common_physical_grain
