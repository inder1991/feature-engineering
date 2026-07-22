"""Task 4 — transparent confidence fusion (pure, no DB/LLM/gold). Mirrors ``test_grounding.py``'s
directness: ``GroundingV1`` instances are built by hand (it's a plain frozen dataclass), no
``overlay_conn`` fixture needed anywhere in this module."""
from __future__ import annotations

from featuregen.overlay.upload.attest.fusion import fuse
from featuregen.overlay.upload.attest.grounding import GroundingV1

_ALL_PASS = GroundingV1(
    checks={"type_consistency": "pass", "path_agreement": "pass", "sibling_consistency": "pass"},
    coverage=1.0, conflict=False,
)
_ZERO_COVERAGE = GroundingV1(checks={}, coverage=0.0, conflict=False)
_CONFLICTED = GroundingV1(
    checks={"type_consistency": "fail", "path_agreement": "pass", "sibling_consistency": "absent"},
    coverage=1.0, conflict=True,
)


def test_agreement_plus_full_grounding_yields_high_confidence() -> None:
    result = fuse(proposer_value="monetary_flow", reclassify_value="monetary_flow",
                   grounding=_ALL_PASS)

    assert result.confidence > 0.8
    assert result.agreement == {
        "proposer_reclassify_agree": True, "grounding_coverage": 1.0, "grounding_conflict": False,
    }


def test_disagreement_yields_low_confidence() -> None:
    result = fuse(proposer_value="monetary_flow", reclassify_value="customer_id",
                   grounding=_ALL_PASS)

    assert result.confidence < 0.3
    assert result.agreement["proposer_reclassify_agree"] is False


def test_grounding_conflict_caps_confidence_low_even_with_agreement() -> None:
    """Two agreeing LLMs cannot overrule a deterministic evidence contradiction: proposer and
    reclassifier agree, but a grounding check actively FAILED (conflict=True) -> capped low."""
    result = fuse(proposer_value="monetary_flow", reclassify_value="monetary_flow",
                   grounding=_CONFLICTED)

    assert result.confidence < 0.3
    assert result.agreement["proposer_reclassify_agree"] is True
    assert result.agreement["grounding_conflict"] is True


def test_zero_coverage_agreement_scores_strictly_below_full_coverage_agreement() -> None:
    """The decorrelation guard: two ungrounded LLMs agreeing must not reach the same confidence as
    two grounded LLMs agreeing — grounding coverage must be visible in the number, not just a
    HIGH/LOW bucket."""
    high_coverage = fuse(proposer_value="monetary_flow", reclassify_value="monetary_flow",
                          grounding=_ALL_PASS)
    zero_coverage = fuse(proposer_value="monetary_flow", reclassify_value="monetary_flow",
                          grounding=_ZERO_COVERAGE)

    assert zero_coverage.confidence < high_coverage.confidence


def test_none_proposer_or_reclassify_is_not_an_agreement() -> None:
    """A missing proposer or reclassify value must never be treated as an agreement — it is scored
    the same as an active disagreement, not as a neutral/absent case."""
    missing_proposer = fuse(proposer_value=None, reclassify_value="monetary_flow",
                             grounding=_ALL_PASS)
    missing_reclassify = fuse(proposer_value="monetary_flow", reclassify_value=None,
                               grounding=_ALL_PASS)
    both_missing = fuse(proposer_value=None, reclassify_value=None, grounding=_ALL_PASS)

    for result in (missing_proposer, missing_reclassify, both_missing):
        assert result.agreement["proposer_reclassify_agree"] is False
        assert result.confidence < 0.5
