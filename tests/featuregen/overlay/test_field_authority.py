"""Authority predicate tree + pure evaluator (spec §4.1).

Pure unit tests — no DB fixture. Covers leaf membership, AnyOf disjunction, AllOf conjunction,
the LLM-proposed-never-satisfies-a-structural-rule invariant, and the empty-predicate rejection
(review item 15: `all([]) == True` would silently authorize everything)."""

import pytest

from featuregen.overlay.evidence import AssertionStrength as S
from featuregen.overlay.evidence import EvidenceProducer as P
from featuregen.overlay.field_authority import AllOf, AnyOf, HasEvidence, evaluate


def test_has_evidence_is_exact_pair_membership():
    pred = HasEvidence(P.PROFILER, S.SUPPORTED)
    assert evaluate(pred, frozenset({(P.PROFILER, S.SUPPORTED)})) is True
    assert evaluate(pred, frozenset({(P.PROFILER, S.PROPOSED)})) is False  # right producer, wrong strength
    assert evaluate(pred, frozenset()) is False                            # empty active set


def test_any_of_is_disjunction():
    pred = AnyOf((HasEvidence(P.HUMAN, S.SUPPORTED), HasEvidence(P.SOURCE, S.SUPPORTED)))
    assert evaluate(pred, frozenset({(P.SOURCE, S.SUPPORTED)})) is True    # one child matches
    assert evaluate(pred, frozenset({(P.HUMAN, S.SUPPORTED)})) is True
    assert evaluate(pred, frozenset({(P.LLM, S.PROPOSED)})) is False       # no child matches


def test_all_of_is_conjunction():
    pred = AllOf((HasEvidence(P.LLM, S.PROPOSED), HasEvidence(P.PROFILER, S.SUPPORTED)))
    both = frozenset({(P.LLM, S.PROPOSED), (P.PROFILER, S.SUPPORTED)})
    assert evaluate(pred, both) is True
    assert evaluate(pred, frozenset({(P.LLM, S.PROPOSED)})) is False       # missing the other child
    assert evaluate(pred, frozenset()) is False


def test_llm_proposal_never_satisfies_a_structural_rule():
    # A structural/operational rule demands source- or human-strength evidence. An active set that
    # holds ONLY an LLM proposal must never satisfy it — the whole point of the strength axis.
    structural = AnyOf((HasEvidence(P.SOURCE, S.SUPPORTED), HasEvidence(P.HUMAN, S.SUPPORTED)))
    assert evaluate(structural, frozenset({(P.LLM, S.PROPOSED)})) is False


def test_nested_tree_evaluates_recursively():
    # AllOf( AnyOf(source|human) , HasEvidence(profiler,supported) )
    pred = AllOf((
        AnyOf((HasEvidence(P.SOURCE, S.SUPPORTED), HasEvidence(P.HUMAN, S.SUPPORTED))),
        HasEvidence(P.PROFILER, S.SUPPORTED),
    ))
    assert evaluate(pred, frozenset({(P.HUMAN, S.SUPPORTED), (P.PROFILER, S.SUPPORTED)})) is True
    assert evaluate(pred, frozenset({(P.HUMAN, S.SUPPORTED)})) is False    # profiler leg missing


def test_empty_predicates_are_rejected():
    with pytest.raises(ValueError):
        AllOf(())    # all([]) == True would authorize everything
    with pytest.raises(ValueError):
        AnyOf(())
