"""Authority predicate tree + pure evaluator (spec §4.1).

Pure unit tests — no DB fixture. Covers leaf membership, AnyOf disjunction, AllOf conjunction,
the LLM-proposed-never-satisfies-a-structural-rule invariant, and the empty-predicate rejection
(review item 15: `all([]) == True` would silently authorize everything)."""

import pytest

from featuregen.overlay.evidence import AssertionStrength as S
from featuregen.overlay.evidence import EvidenceProducer as P
from featuregen.overlay.field_authority import (
    AllOf,
    AnyOf,
    ConflictStrategy,
    Disqualifier,
    FieldEvidenceView,
    FieldPolicy,
    HasEvidence,
    InfluenceTier,
    ResolutionMode,
    evaluate,
    resolve_field_authority,
)


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


# ---------------------------------------------------------------------------
# Task 4: field-specific authority resolver (typed, conflict-strategy, influence-enforced)
# ---------------------------------------------------------------------------


def _pol(**kw):
    base = dict(influence_max=InfluenceTier.OPERATIONAL, display_rule=HasEvidence(P.LLM, S.PROPOSED),
                operational_rule=AnyOf((HasEvidence(P.HUMAN, S.CONFIRMED),)),
                disqualifiers=(Disqualifier.STALE_SELECTED_EVIDENCE,),
                resolution_mode=ResolutionMode.GENERIC_FIELD,
                conflict_strategy=ConflictStrategy.PREFER_CONFIRMED)
    base.update(kw)
    return FieldPolicy(**base)


def _ev(p, s, v):
    return FieldEvidenceView(p, s, v, f"e-{v}")


def test_display_proposal_operational_unresolved():
    r = resolve_field_authority([_ev(P.LLM, S.PROPOSED, "monetary_flow")], _pol(), frozenset())
    assert r.display_value == "monetary_flow" and r.load_bearing_value is None


def test_prefer_confirmed_selects_confirmed_value_not_highest_only():
    ev = [_ev(P.LLM, S.PROPOSED, "monetary_flow"), _ev(P.HUMAN, S.CONFIRMED, "monetary_stock")]
    r = resolve_field_authority(ev, _pol(), frozenset())
    assert r.load_bearing_value == "monetary_stock"        # confirmed value, chosen by strategy


def test_unresolved_on_conflict_blocks_when_values_disagree():
    # NOTE: the brief spelled the structural producer as P.STRUCTURAL_CONNECTOR, which is not a
    # member of EvidenceProducer (PROFILER/LLM/SOURCE/HUMAN/LEGACY). P.SOURCE is the structural
    # source producer and S.ATTESTED is "vouched for by a structural source" (evidence.py docstring),
    # so this is the same intent: two distinct values disagree under UNRESOLVED_ON_CONFLICT.
    pol = _pol(conflict_strategy=ConflictStrategy.UNRESOLVED_ON_CONFLICT,
               operational_rule=AnyOf((HasEvidence(P.SOURCE, S.ATTESTED),
                                       HasEvidence(P.HUMAN, S.CONFIRMED))))
    ev = [_ev(P.HUMAN, S.CONFIRMED, "account"), _ev(P.SOURCE, S.ATTESTED, "transaction")]
    r = resolve_field_authority(ev, pol, frozenset())
    assert r.load_bearing_value is None and r.unresolved_reason == "conflict"


def test_influence_max_below_operational_never_load_bearing():
    r = resolve_field_authority([_ev(P.HUMAN, S.CONFIRMED, "x")],
                                _pol(influence_max=InfluenceTier.RECOMMENDATION), frozenset())
    assert r.load_bearing_value is None and r.unresolved_reason == "influence_not_operational"


def test_disqualifier_blocks_even_when_satisfied():
    r = resolve_field_authority([_ev(P.HUMAN, S.CONFIRMED, "x")], _pol(),
                                frozenset({Disqualifier.STALE_SELECTED_EVIDENCE}))
    assert r.load_bearing_value is None and r.unresolved_reason.startswith("disqualified:")


def test_specialized_fact_mode_never_load_bearing():
    r = resolve_field_authority([_ev(P.HUMAN, S.CONFIRMED, "grain")],
                                _pol(resolution_mode=ResolutionMode.SPECIALIZED_FACT), frozenset())
    assert r.load_bearing_value is None and r.unresolved_reason == "specialized_fact"
