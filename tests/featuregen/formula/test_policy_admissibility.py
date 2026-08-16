"""C-C9 — invariant 16's four-way table.

The gate is *"the four-way table is a total function over its inputs"*, so the last test enumerates
the input space rather than sampling it: every combination of governed and LLM candidate counts,
agreeing and disagreeing, must return a verdict — none may raise and none may fall through.
"""
from __future__ import annotations

import itertools

import pytest

from featuregen.formula.policy_admissibility import (
    NO_CANDIDATE,
    NO_DETERMINISTIC_WINNER,
    SOURCE_OVERRODE_LLM,
    TWO_GOVERNED_DECLARATIONS,
    AdmissibilityOutcomeV1,
    AdmissibilityVerdictV1,
    decide_admissibility,
)
from featuregen.formula.policy_realization import (
    PolicyRealizationRevisionV1,
    RealizationFamilyKeyV1,
    RealizationProvenanceV1,
)

FAMILY = RealizationFamilyKeyV1(
    policy_kind="direction_sign", policy_ref="direction_sign:foundation-signed-by-indicator",
    bound_dataset="hdfc::public.transactions", environment_id="hdfc-local",
    semantic_role="direction")
OCCURRENCE = "sha256:occurrence-1"


def _candidate(revision_id: str, provenance: RealizationProvenanceV1,
               executable: str = "sha256:D-means-debit") -> PolicyRealizationRevisionV1:
    return PolicyRealizationRevisionV1(
        revision_id=revision_id, family_key=FAMILY, executable_content_hash=executable,
        cas_pointer=f"cas://blob/{revision_id}", provenance=provenance,
        realizes_occurrences=(OCCURRENCE,))


SOURCE = RealizationProvenanceV1.SOURCE_DERIVED
LLM = RealizationProvenanceV1.LLM_PROPOSED
HUMAN = RealizationProvenanceV1.HUMAN_AUTHORED


# ══ case 1 — source beats LLM, conflict RETAINED and visible ═════════════════════════════════════
def test_a_source_beats_a_DISAGREEING_llm_and_the_conflict_is_retained():
    """Silently preferring the source destroys information a reviewer wants: "the model read this
    differently"."""
    verdict = decide_admissibility([
        _candidate("rev-source", SOURCE, "sha256:D-means-debit"),
        _candidate("rev-llm", LLM, "sha256:DR-means-debit")])

    assert verdict.outcome is AdmissibilityOutcomeV1.ADMITTED_EVIDENCE_LINKED
    assert verdict.winner.revision_id == "rev-source"
    assert [c.code for c in verdict.conflicts] == [SOURCE_OVERRODE_LLM]
    assert verdict.conflicts[0].resolved is True
    assert "retained rather than resolved away" in verdict.conflicts[0].detail


def test_a_source_that_AGREES_with_the_llm_records_no_conflict():
    """There is nothing to retain — they say the same thing."""
    verdict = decide_admissibility([
        _candidate("rev-source", SOURCE), _candidate("rev-llm", LLM)])
    assert verdict.outcome is AdmissibilityOutcomeV1.ADMITTED_EVIDENCE_LINKED
    assert verdict.conflicts == ()


def test_a_lone_source_is_admitted_evidence_linked():
    verdict = decide_admissibility([_candidate("rev-source", SOURCE)])
    assert verdict.outcome is AdmissibilityOutcomeV1.ADMITTED_EVIDENCE_LINKED
    assert verdict.winner.is_evidence_validated


# ══ case 2 — two governed declarations refuse, even agreeing ═════════════════════════════════════
def test_TWO_GOVERNED_DECLARATIONS_REFUSE_EVEN_WHEN_THEY_AGREE():
    """Not a tie to break — a governance defect in the source. Agreement today is not agreement
    tomorrow: the next edit to either diverges silently, and whichever this code picked becomes the
    answer nobody chose."""
    verdict = decide_admissibility([
        _candidate("rev-a", SOURCE, "sha256:same"),
        _candidate("rev-b", HUMAN, "sha256:same")])

    assert verdict.outcome is AdmissibilityOutcomeV1.REFUSED
    assert verdict.refusal_code == TWO_GOVERNED_DECLARATIONS
    assert verdict.winner is None
    assert "even when they agree" in verdict.conflicts[0].detail
    assert "fix is upstream" in verdict.conflicts[0].detail


def test_two_governed_declarations_that_DISAGREE_also_refuse():
    verdict = decide_admissibility([
        _candidate("rev-a", SOURCE, "sha256:one"), _candidate("rev-b", SOURCE, "sha256:two")])
    assert verdict.refusal_code == TWO_GOVERNED_DECLARATIONS


# ══ case 3 — two LLM-only with no deterministic winner refuse ════════════════════════════════════
def test_TWO_DISAGREEING_LLM_PROPOSALS_REFUSE():
    """A coin flip about which column carries a governed policy is not a governed answer."""
    verdict = decide_admissibility([
        _candidate("rev-1", LLM, "sha256:D-means-debit"),
        _candidate("rev-2", LLM, "sha256:DR-means-debit")])

    assert verdict.outcome is AdmissibilityOutcomeV1.REFUSED
    assert verdict.refusal_code == NO_DETERMINISTIC_WINNER
    assert "coin flip" in verdict.conflicts[0].detail


def test_two_AGREEING_llm_proposals_are_ONE_answer_arrived_at_twice():
    """Agreement is decidable — `executable_content_hash` is exactly "what this realization does"."""
    verdict = decide_admissibility([
        _candidate("rev-2", LLM), _candidate("rev-1", LLM)])
    assert verdict.outcome is AdmissibilityOutcomeV1.ADMITTED_LLM_PROPOSED
    assert verdict.winner.revision_id == "rev-1", "deterministic by revision id"


# ══ case 4 — one valid LLM-only is usable, and never called evidence-validated ═══════════════════
def test_ONE_LLM_PROPOSAL_IS_USABLE_UNDER_ITS_OWN_NAME():
    """Invariant 16's shape: not discarded for being a proposal, not laundered into evidence."""
    verdict = decide_admissibility([_candidate("rev-llm", LLM)])
    assert verdict.outcome is AdmissibilityOutcomeV1.ADMITTED_LLM_PROPOSED
    assert verdict.winner.revision_id == "rev-llm"
    assert not verdict.winner.is_evidence_validated


def test_an_LLM_winner_CANNOT_be_reported_as_evidence_linked():
    """The exact laundering invariant 16 forbids, refused at the verdict's own construction."""
    with pytest.raises(ValueError, match="exact laundering invariant 16 forbids"):
        AdmissibilityVerdictV1(
            outcome=AdmissibilityOutcomeV1.ADMITTED_EVIDENCE_LINKED,
            winner=_candidate("rev-llm", LLM), refusal_code="", conflicts=())


def test_a_source_winner_cannot_be_reported_as_llm_proposed():
    with pytest.raises(ValueError, match="disagree about what this realization is"):
        AdmissibilityVerdictV1(
            outcome=AdmissibilityOutcomeV1.ADMITTED_LLM_PROPOSED,
            winner=_candidate("rev-source", SOURCE), refusal_code="", conflicts=())


# ══ no candidate at all ══════════════════════════════════════════════════════════════════════════
def test_no_candidate_refuses_by_its_own_code():
    verdict = decide_admissibility([])
    assert verdict.refusal_code == NO_CANDIDATE
    assert "no executable answer at all" in verdict.conflicts[0].detail


# ══ the verdict cannot contradict itself ═════════════════════════════════════════════════════════
def test_a_refusal_cannot_name_a_winner():
    with pytest.raises(ValueError, match="cannot name a winner"):
        AdmissibilityVerdictV1(outcome=AdmissibilityOutcomeV1.REFUSED,
                               winner=_candidate("r", SOURCE), refusal_code="X", conflicts=())


def test_an_admission_cannot_carry_a_refusal_code():
    with pytest.raises(ValueError, match="cannot tell whether it was admitted or refused"):
        AdmissibilityVerdictV1(outcome=AdmissibilityOutcomeV1.ADMITTED_EVIDENCE_LINKED,
                               winner=_candidate("r", SOURCE), refusal_code="X", conflicts=())


def test_an_admission_with_no_winner_is_refused():
    with pytest.raises(ValueError, match="admits nothing"):
        AdmissibilityVerdictV1(outcome=AdmissibilityOutcomeV1.ADMITTED_LLM_PROPOSED,
                               winner=None, refusal_code="", conflicts=())


# ══ THE GATE — total over its inputs ═════════════════════════════════════════════════════════════
@pytest.mark.parametrize("governed,llm,agree", list(itertools.product([0, 1, 2, 3], [0, 1, 2, 3],
                                                                      [True, False])))
def test_THE_TABLE_IS_TOTAL(governed, llm, agree):
    """Enumerated, not sampled. A decision table with a hole admits whatever the hole happens to
    evaluate to."""
    candidates = [
        _candidate(f"src-{i}", SOURCE, "sha256:same" if agree else f"sha256:src-{i}")
        for i in range(governed)
    ] + [
        _candidate(f"llm-{i}", LLM, "sha256:same" if agree else f"sha256:llm-{i}")
        for i in range(llm)
    ]
    verdict = decide_admissibility(candidates)

    assert isinstance(verdict, AdmissibilityVerdictV1)
    assert isinstance(verdict.outcome, AdmissibilityOutcomeV1)
    if verdict.outcome is AdmissibilityOutcomeV1.REFUSED:
        assert verdict.refusal_code in {
            TWO_GOVERNED_DECLARATIONS, NO_DETERMINISTIC_WINNER, NO_CANDIDATE}
        assert verdict.conflicts, "a refusal always says why"
    else:
        assert verdict.winner is not None


def test_the_table_covers_every_invariant_16_case_by_name():
    """The four cases, mapped to outcomes, so a future edit that drops one is visible here."""
    cases = {
        "source beats llm": decide_admissibility([
            _candidate("s", SOURCE, "a"), _candidate("l", LLM, "b")]).outcome,
        "two governed": decide_admissibility([
            _candidate("s1", SOURCE, "a"), _candidate("s2", SOURCE, "a")]).outcome,
        "two llm no winner": decide_admissibility([
            _candidate("l1", LLM, "a"), _candidate("l2", LLM, "b")]).outcome,
        "one valid llm": decide_admissibility([_candidate("l", LLM)]).outcome,
    }
    assert cases == {
        "source beats llm": AdmissibilityOutcomeV1.ADMITTED_EVIDENCE_LINKED,
        "two governed": AdmissibilityOutcomeV1.REFUSED,
        "two llm no winner": AdmissibilityOutcomeV1.REFUSED,
        "one valid llm": AdmissibilityOutcomeV1.ADMITTED_LLM_PROPOSED,
    }
