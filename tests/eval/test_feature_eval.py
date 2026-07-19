"""Slice 3A-iv Task 5: hermetic CI gate for the feature-gen quality metrics + the gold artifact.

Deliberately UNMARKED (no `eval` marker) and DB-free / SDK-free: this is the always-on gate, even
though the sweep that consumes these metrics (test_feature_gen_eval.py) only runs with a live key."""
from __future__ import annotations

from tests.eval.feature_eval import (
    GenFeature,
    cost_regression,
    is_relevant,
    relevance_lift,
    relevance_rate,
    restricted_leaks,
    token_total,
    unsafe_accepted,
)
from tests.eval.gold_features import DISPOSITIONS, GOLD, OPERATION_VOCAB

# ---- metric units -------------------------------------------------------------------------------

def _gen(name, derives, op="", status="DESIGN_CHECKED", reqs=0):
    return GenFeature(name=name, derives_from=tuple(derives), operation_kind=op,
                      validation_status=status, requirement_count=reqs)


def test_is_relevant_by_column_match():
    g = _gen("some_feature", ["public.transactions.amount"])
    assert is_relevant(g, frozenset({"public.transactions.amount"}), frozenset()) is True


def test_is_relevant_by_name_term():
    g = _gen("spend_drop_90d", [])
    assert is_relevant(g, frozenset(), frozenset({"spend"})) is True


def test_is_not_relevant_when_neither_matches():
    g = _gen("balance_avg", ["public.other.col"])
    assert is_relevant(g, frozenset({"public.transactions.amount"}), frozenset({"spend"})) is False


def test_relevance_rate_and_empty():
    gens = [_gen("spend_x", ["public.transactions.amount"]), _gen("noise", ["public.other.col"])]
    assert relevance_rate(gens, frozenset({"public.transactions.amount"}), frozenset()) == 0.5
    assert relevance_rate([], frozenset({"x"}), frozenset({"y"})) == 0.0


def test_relevance_lift_relative_and_edge_cases():
    assert relevance_lift(0.4, 0.5) == (0.5 - 0.4) / 0.4
    assert relevance_lift(0.0, 0.3) == float("inf")
    assert relevance_lift(0.0, 0.0) == 0.0


def test_unsafe_accepted_flags_design_checked_with_requirements():
    safe = _gen("a", ["c"], status="DESIGN_CHECKED", reqs=0)
    needs = _gen("b", ["c"], status="NEEDS_EXTERNAL_VALIDATION", reqs=1)
    unsafe = _gen("c", ["c"], status="DESIGN_CHECKED", reqs=1)
    result = unsafe_accepted([safe, needs, unsafe])
    assert result == [unsafe]


def test_token_total_and_cost_regression():
    assert token_total({"input_tokens": 100, "output_tokens": 40}) == 140
    assert token_total({}) == 0
    assert cost_regression(1000, 1200) == 0.2
    assert cost_regression(0, 0) == 0.0
    assert cost_regression(0, 5) == float("inf")


def test_restricted_leaks_finds_seeded_sentinel():
    payloads = ["clean context", "leaked SAMPLE:jane@acme.com here"]
    assert restricted_leaks(payloads, frozenset({"SAMPLE:jane@acme.com"})) == ["SAMPLE:jane@acme.com"]
    assert restricted_leaks(["clean"], frozenset({"SAMPLE:jane@acme.com"})) == []


# ---- gold-set invariants (the ">= 40 curated cases" gate) ---------------------------------------

def test_gold_has_at_least_40():
    assert len(GOLD) >= 40, f"gold set has only {len(GOLD)} cases; spec §9 requires >= 40"


def test_gold_objectives_are_unique():
    objectives = [g.objective for g in GOLD]
    assert len(objectives) == len(set(objectives)), "duplicate objective in the gold set"


def test_gold_operations_in_vocab_and_nonempty():
    for g in GOLD:
        assert g.expected_operations, f"{g.objective!r} has no expected_operations"
        assert g.expected_operations <= OPERATION_VOCAB, \
            f"{g.objective!r} uses off-vocab operations {g.expected_operations - OPERATION_VOCAB}"


def test_gold_dispositions_and_anchors_valid():
    for g in GOLD:
        assert g.expected_disposition in DISPOSITIONS, g.expected_disposition
        assert g.expected_columns, f"{g.objective!r} has no expected_columns"
        assert g.relevance_terms, f"{g.objective!r} has no relevance_terms"
