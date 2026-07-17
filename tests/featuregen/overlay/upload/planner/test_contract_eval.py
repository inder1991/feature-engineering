from __future__ import annotations

from featuregen.overlay.upload.planner.contract_eval import (
    ActualVerdict,
    CompileVerdict,
    ExpectedVerdict,
    SampleUnit,
    double_compile_stable,
    evaluate,
    stratified_sample,
)
from featuregen.overlay.upload.planner.contract_gold import (
    GOLD_CASES,
    GOLD_SET_HASH,
    run_gold_case,
)
from featuregen.overlay.upload.planner.contracts import ReasonCode
from featuregen.overlay.upload.planner.shadow_store import CompileStatus
from featuregen.overlay.upload.planner.strata import STRATA_VERSION, dimension_of, stratum_of


# ── 1. evaluate: exact-match + strict false-resolve ──
def _exp(decl, crs, reason, cause, valid):
    return ExpectedVerdict(decl, crs, reason, cause, valid)


def _act(decl, crs, reason, cause):
    return ActualVerdict(decl, crs, reason, cause)


def test_evaluate_exact_match_passes():
    rep = evaluate([("c1",
                     _exp("resolved", "resolved", None, "expected", True),
                     _act("resolved", "resolved", None, "expected"))])
    assert rep.passed and rep.results[0].passed and not rep.results[0].false_resolve


def test_evaluate_field_mismatch_fails():
    rep = evaluate([("c1",
                     _exp("unresolved_aggregation_declaration", "unresolved_aggregation_declaration",
                          "aggregation_strategy_missing", "expected", False),
                     _act("resolved", "resolved", None, "expected"))])
    assert not rep.passed and not rep.results[0].passed
    assert rep.results[0].mismatches   # declaration_status + primary + false-resolve all differ


def test_evaluate_false_resolve_is_a_hard_failure_even_if_fields_match():
    # the expert says this shape must NOT be a valid resolution; the classifier resolved it → FAILURE
    rep = evaluate([("c1",
                     _exp("resolved", "resolved", None, "expected", False),   # resolved_is_valid=False
                     _act("resolved", "resolved", None, "expected"))])
    assert not rep.passed
    assert rep.results[0].false_resolve and rep.false_resolves == ("c1",)


def test_evaluate_empty_is_not_passing():
    assert not evaluate([]).passed


# ── 2. fixed stratum registry (F18): deterministic, non-overlapping, versioned ──
def test_dimension_of_resolved_vs_reason_category():
    assert dimension_of("resolved", None) == "resolved"
    # a mapped reason resolves to its Layer-A category (aggregation_strategy_missing → missing_authoring)
    assert dimension_of("unresolved_aggregation_declaration",
                        str(ReasonCode.aggregation_strategy_missing)) == "missing_authoring"
    assert dimension_of("unresolved_aggregation_declaration",
                        str(ReasonCode.grain_incompatible)) == "topology_or_model"
    assert dimension_of("safety_rejected", "not_a_real_reason_code") == "operationally_unmeasured"
    assert dimension_of("unresolved_ingredient_connectivity", None) == "unclassified"


def test_stratum_is_deterministic_and_version_stamped():
    a = stratum_of(tier="tier_1_single_catalog", family="balance_stock",
                   contract_resolution_status="resolved", primary_reason_code=None)
    b = stratum_of(tier="tier_1_single_catalog", family="balance_stock",
                   contract_resolution_status="resolved", primary_reason_code=None)
    assert a == b and a.key == f"{STRATA_VERSION}:tier_1_single_catalog:balance_stock:resolved"


def test_strata_are_non_overlapping_across_outcomes():
    resolved = stratum_of(tier="t", family="f", contract_resolution_status="resolved",
                          primary_reason_code=None)
    rejected = stratum_of(tier="t", family="f",
                          contract_resolution_status="unresolved_aggregation_declaration",
                          primary_reason_code=str(ReasonCode.aggregation_strategy_missing))
    assert resolved != rejected   # same tier+family, different outcome → different stratum


# ── 3. stratified sampler: frame filter, dedup by shape, seeded, rare-stratum flag ──
def _unit(h, *, tier="tier_1_single_catalog", family="balance_stock", selected=True, complete=True,
          crs="resolved", reason=None):
    return SampleUnit(tier=tier, family=family, contract_resolution_status=crs,
                      primary_reason_code=reason, contract_input_hash=h,
                      is_selected=selected, is_complete=complete)


def test_sampler_dedups_repeated_shapes_and_flags_rare():
    units = [_unit("h1"), _unit("h1"), _unit("h1"), _unit("h2")]   # h1 clustered (3×), h2 once
    s = stratified_sample(units, seed=7, per_stratum=3)
    (stratum,) = s.strata
    assert stratum.distinct_shapes == 2          # deduped: only 2 distinct shapes
    assert stratum.rare and s.rare_strata        # 2 < per_stratum(3) → rare


def test_sampler_excludes_unselected_and_incomplete_from_the_frame():
    units = [_unit("h1", selected=False), _unit("h2", complete=False), _unit("h3")]
    s = stratified_sample(units, seed=1, per_stratum=1)
    (stratum,) = s.strata
    assert stratum.distinct_shapes == 1 and stratum.sampled == ("h3",)


def test_sampler_surfaces_a_fully_out_of_frame_stratum_as_zero_coverage():
    # fail-closed: a stratum whose WHOLE population is out-of-frame (all incomplete) must NOT vanish —
    # it surfaces as an explicit zero-coverage rare stratum the gate can fail on.
    units = [_unit("h1", family="in_frame"),                              # one in-frame stratum
             _unit("g1", family="truncated", complete=False),            # all out-of-frame
             _unit("g2", family="truncated", complete=False)]
    s = stratified_sample(units, seed=1, per_stratum=1)
    truncated = next(st for st in s.strata if st.stratum.family == "truncated")
    assert truncated.distinct_shapes == 0 and truncated.sampled == () and truncated.rare
    assert truncated.stratum in s.rare_strata     # observable → fails the gate for that stratum


def test_sampler_is_seeded_and_deterministic():
    units = [_unit(f"h{i}") for i in range(20)]
    a = stratified_sample(units, seed=42, per_stratum=5)
    b = stratified_sample(units, seed=42, per_stratum=5)
    assert a.strata[0].sampled == b.strata[0].sampled and len(a.strata[0].sampled) == 5
    other = stratified_sample(units, seed=99, per_stratum=5)
    assert other.strata[0].sampled != a.strata[0].sampled   # a different seed draws differently


# ── 4. double-compile determinism procedure ──
def _v(key, status, cid, decl="resolved"):
    return CompileVerdict(key=key, compile_status=status, contract_id=cid, declaration_status=decl)


def test_double_compile_identical_is_stable():
    first = [_v("p1", CompileStatus.complete, "cid1"), _v("p2", CompileStatus.complete, "cid2")]
    second = [_v("p1", CompileStatus.complete, "cid1"), _v("p2", CompileStatus.complete, "cid2")]
    r = double_compile_stable(first, second)
    assert r.stable and r.compared == 2 and r.mismatched_keys == ()


def test_double_compile_divergent_is_unstable():
    first = [_v("p1", CompileStatus.complete, "cid1")]
    second = [_v("p1", CompileStatus.complete, "cid_DIFFERENT")]
    r = double_compile_stable(first, second)
    assert not r.stable and r.mismatched_keys == ("p1",)


def test_double_compile_plan_set_divergence_is_unstable():
    # fail-closed: a plan comparable in one compile but dropped from the other is a plan-set
    # divergence, NOT a silently-tolerated stable pass.
    first = [_v("p1", CompileStatus.complete, "cid1"), _v("p2", CompileStatus.complete, "cid2")]
    second = [_v("p1", CompileStatus.complete, "cid1")]                 # p2 vanished
    r = double_compile_stable(first, second)
    assert not r.stable and r.mismatched_keys == ("p2",) and r.compared == 1
    # symmetric: a plan ADDED in the second compile is equally a divergence
    r2 = double_compile_stable([_v("p1", CompileStatus.complete, "cid1")], first)
    assert not r2.stable and "p2" in r2.mismatched_keys


def test_double_compile_empty_comparison_fails():
    assert not double_compile_stable([], []).stable                       # no evidence
    # a pair where one side is budget-truncated (incomplete) is NOT comparable → still empty → fails
    first = [_v("p1", CompileStatus.incomplete, "cid1")]
    second = [_v("p1", CompileStatus.complete, "cid1")]
    r = double_compile_stable(first, second)
    assert not r.stable and r.compared == 0


# ── 5. gold set against the REAL classifier ──
def test_gold_set_hash_is_stable():
    assert GOLD_SET_HASH and len(GOLD_SET_HASH) == 64


def test_every_gold_case_matches_the_real_classifier(db):
    triples = [run_gold_case(db, case) for case in GOLD_CASES]
    rep = evaluate(triples)
    assert rep.passed, [(r.case_id, r.mismatches) for r in rep.results if not r.passed]
    assert rep.false_resolves == ()   # nothing the expert forbids resolved


def test_gold_case_run_is_reproducible(db):
    a = run_gold_case(db, GOLD_CASES[1])   # take_latest_without_ordering
    b = run_gold_case(db, GOLD_CASES[1])
    assert a[2] == b[2]   # identical actual verdict across two runs
    assert a[2].primary_reason_code == str(ReasonCode.aggregation_ordering_column_missing)
