"""C-A5 — "the critic did not run" is sayable, and never spelled `clean`.

Deterministic recipe authoring instantiates a blueprint a human already reviewed, so asking an LLM
critic to re-judge it spends a provider call to re-derive a verdict the review already carries. But
``AuthoringResultV2.critic_status`` was a mandatory three-member ``Literal``, so the only way to
record that run was ``"clean"`` — which states the critic RAN and found nothing. That is false, and
these tests exist to keep it unsayable.
"""
from __future__ import annotations

import dataclasses

import pytest

from featuregen.formula.result import AuthoringAxes, CriticStatus, IncoherentResultError
from featuregen.formula.result_v2 import (
    AuthoringAxesV2,
    CriticExecutedV2,
    ReviewedBlueprintBypassV2,
    derive_disposition_v2,
)


def _proposal():
    """A real parsed v3 proposal — an unresolved-output NEEDS_REVIEW requires one ("there is
    nothing to review without it"), and a bypass is exactly what a recipe-authored v3 run produces.
    """
    import json
    from pathlib import Path

    from featuregen.formula.parse_v3 import parse_proposal_v3
    raw = json.loads(
        (Path(__file__).parent / "gold_v2" / "01_avg_txn_amt_90d.json").read_text())["proposal"]
    raw["formula_schema_version"] = 3
    raw["body"]["expr"]["authority_refs"] = {
        "direction_policy_ref": "direction_sign:foundation-signed-by-indicator"}
    raw["body"]["expr"]["row_selections"] = [
        {"kind": "transaction_direction", "role": "direction", "semantic_value": "debit"}]
    return parse_proposal_v3(raw)


_BYPASS = ReviewedBlueprintBypassV2(
    blueprint_revision="sha256:reviewed-blueprint", expectation_hash="sha256:expectation")


def _axes_v2(review, **over):
    base = dict(structural_status="ok", capability_status="ok", output_status="needs_authority",
                expectation_status="match", technical_status="ok")
    return AuthoringAxesV2(review=review, **{**base, **over})


def _axes_v1(critic: CriticStatus = "clean", **over):
    base = dict(structural_status="ok", capability_status="ok", output_status="needs_authority",
                expectation_status="match", technical_status="ok")
    return AuthoringAxes(critic_status=critic, **{**base, **over})


def _out(axes, **kw):
    """An UNRESOLVED output deliberately: it needs no artifact pair, so these tests isolate the
    REVIEW axis instead of re-proving the artifact-coherence rules that guard the resolved arm."""
    kw.setdefault("reviewed_expectation_hash", _BYPASS.expectation_hash)
    return derive_disposition_v2(
        axes, authoring_run_id="run_ca5", candidate_proposal=_proposal(), **kw)


# ── the whole point: a bypass is not "clean" ────────────────────────────────────────────────────
def test_a_bypass_reports_no_critic_status_at_all():
    result = _out(_axes_v2(_BYPASS))
    assert result.critic_status is None, (
        'a bypass must not report a critic status — "clean" would say the critic ran and found '
        "nothing")
    assert result.review == _BYPASS
    assert result.review.blueprint_revision == "sha256:reviewed-blueprint"


def test_an_executed_critic_keeps_its_status_and_its_evidence():
    executed = CriticExecutedV2(status="advisory", findings_hash="sha256:findings")
    result = _out(_axes_v2(executed))
    assert result.critic_status == "advisory"
    assert result.review == executed


# ── a bypass is NEUTRAL for the fold, exactly as a clean critic is ──────────────────────────────
def test_a_bypass_folds_identically_to_a_clean_critic():
    """Neutral in the §F precedence — the disposition must not differ merely because review was
    obtained by reviewing the blueprint rather than by running the critic."""
    assert _out(_axes_v2(_BYPASS)).authoring_disposition == \
        _out(_axes_v1("clean")).authoring_disposition


def test_a_blocking_critic_still_dominates():
    """The bypass must not become a way to launder a blocking finding.

    Exercised on the RESOLVED arm deliberately: with an unresolved output the output axis already
    forces NEEDS_REVIEW and would MASK the critic axis entirely, so the test would pass while
    proving nothing about review.
    """
    from featuregen.formula.output_authority_v2 import FormulaOutputPolicyV2

    policy = FormulaOutputPolicyV2(
        output_type="numeric", unit="monetary", currency="fixed:AED",
        output_additivity="additive", external_type_required=False)

    def resolved(review):
        return derive_disposition_v2(
            _axes_v2(review, output_status="resolved"), authoring_run_id="run_ca5",
            candidate_proposal=_proposal(), candidate_output=policy,
            reviewed_expectation_hash=_BYPASS.expectation_hash)

    assert resolved(_BYPASS).authoring_disposition == "RESOLVED"
    assert resolved(CriticExecutedV2(status="blocking", findings_hash="sha256:bad")
                    ).authoring_disposition == "NEEDS_REVIEW"


# ── v1 is untouched ─────────────────────────────────────────────────────────────────────────────
def test_v1_shaped_axes_still_fold_and_carry_no_review():
    result = _out(_axes_v1("clean"))
    assert result.critic_status == "clean"
    assert result.review is None, "v1 axes have no concept of a bypass"


def test_the_shared_v1_axes_type_still_requires_a_critic_status():
    """`AuthoringAxes` is deliberately NOT widened: a v1 run must not be able to claim a bypass."""
    assert {f.name for f in dataclasses.fields(AuthoringAxes)} >= {"critic_status"}
    assert "review" not in {f.name for f in dataclasses.fields(AuthoringAxes)}
    with pytest.raises(TypeError):
        AuthoringAxes(structural_status="ok", capability_status="ok", output_status="resolved",
                      expectation_status="match", technical_status="ok")   # no critic_status


def test_an_unknown_critic_status_still_fails_closed():
    """The fail-closed vocabulary check survives the projection."""
    with pytest.raises(IncoherentResultError):
        _out(_axes_v2(CriticExecutedV2(status="invented", findings_hash="x")))


# ── the downstream readers ──────────────────────────────────────────────────────────────────────
def test_the_gold_gates_blocking_check_is_unaffected_by_none():
    """`gold.py` asks `critic_status == "blocking"`. A bypass answers None — which is not blocking,
    and is not silently clean either."""
    result = _out(_axes_v2(_BYPASS))
    assert (result.critic_status == "blocking") is False
    assert result.critic_status is not "clean"  # noqa: F632 — identity is the point


# ── the fail-closed variant check ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("bogus", [None, "clean", 0, object(), ("critic", "clean")])
def test_a_review_that_is_neither_variant_fails_closed(bogus):
    """THE defect this projection would otherwise have.

    An `else: "clean"` would make review=None — or any object at all — fold as NEUTRAL: the exact
    fail-open `_validate_axes` exists to prevent, and the exact confusion this type exists to end.
    """
    with pytest.raises(IncoherentResultError, match="neither CriticExecutedV2"):
        _out(_axes_v2(bogus))


# ── neutrality is EARNED, not asserted (the adversarial-review blocker) ─────────────────────────
def test_a_bypass_without_a_reviewed_expectation_refuses():
    """Measured before this check existed: ONE bypass value was accepted verbatim on two proposals
    with different content hashes, both folding RESOLVED. A bypass whose coverage nobody checked is
    an unreviewed formula folding as neutral."""
    with pytest.raises(IncoherentResultError, match="requires reviewed_expectation_hash"):
        derive_disposition_v2(_axes_v2(_BYPASS), authoring_run_id="r",
                              candidate_proposal=_proposal())


def test_a_bypass_covering_a_DIFFERENT_expectation_refuses():
    with pytest.raises(IncoherentResultError, match="covered a different formula"):
        _out(_axes_v2(_BYPASS), reviewed_expectation_hash="sha256:some-other-expectation")


@pytest.mark.parametrize("field", ["blueprint_revision", "expectation_hash"])
def test_an_empty_bypass_refuses_at_construction(field):
    """A bypass with nothing to check is worse than the "clean" lie it replaced: it also carries a
    false claim of checkability."""
    kw = {"blueprint_revision": "sha256:b", "expectation_hash": "sha256:e", field: "  "}
    with pytest.raises(IncoherentResultError, match=field):
        ReviewedBlueprintBypassV2(**kw)


def test_a_bypass_cannot_carry_critic_findings():
    """A critic that did not run produced no findings; evidence of findings contradicts the fold."""
    with pytest.raises(IncoherentResultError, match="carries no critic_findings_hash"):
        _out(_axes_v2(_BYPASS), critic_findings_hash="sha256:findings-that-cannot-exist")


def test_the_executed_findings_hash_is_DERIVED_not_caller_supplied():
    """One home for the evidence. Two independent copies are two things that can disagree — the
    same rule `candidate_proposal_hash` already obeys."""
    executed = CriticExecutedV2(status="advisory", findings_hash="sha256:AAA")
    result = _out(_axes_v2(executed), critic_findings_hash="sha256:BBB")
    assert result.critic_findings_hash == "sha256:AAA"


# ── the replay round-trip (the finding that gutted the feature's own use case) ──────────────────
def _round_trip(review):
    """Persist a result's review the way `_terminal_payload` does, and restore it the way
    `_restore_terminal_result` does."""
    from featuregen.formula.replay_authoring_v2 import _plain, _restore_review
    return _restore_review(_plain(review))


def test_a_bypass_survives_the_replay_round_trip():
    """Measured before the fix: a bypass-authored run raised RecoveryRequiresReconciliation on
    restore, because `critic_status=None` reached the v1-shaped axes builder and `_validate_axes`
    rejected it. Deterministic recipe authoring — the bypass's WHOLE use case — was therefore the
    one path that could never be replayed."""
    assert _round_trip(_BYPASS) == _BYPASS


def test_an_executed_critic_survives_the_replay_round_trip():
    """And measured before the fix: an executed-critic run restored with `review=None`, which per
    the field's own docstring means "folded from v1-shaped axes" — something the run was not."""
    executed = CriticExecutedV2(status="advisory", findings_hash="sha256:f")
    assert _round_trip(executed) == executed


def test_an_unrecognised_review_shape_is_reconciliation_not_a_silent_v1_fallback():
    from featuregen.formula.control import RecoveryRequiresReconciliation
    from featuregen.formula.replay_authoring_v2 import _restore_review

    assert _restore_review(None) is None            # a genuine v1-shaped run
    with pytest.raises(RecoveryRequiresReconciliation, match="neither an executed critic"):
        _restore_review({"something": "else"})
    with pytest.raises(RecoveryRequiresReconciliation, match="not an object"):
        _restore_review("bypass")


def test_the_frozen_seal_covers_the_review_projection():
    """`shared_axes()` now decides the critic axis for every v2 run, so a change there — a
    fail-open `else`, or altering what a bypass folds to — must move the seal."""
    import inspect

    from featuregen.formula import frozen_configuration
    from featuregen.formula.result_v2 import AuthoringAxesV2

    source = inspect.getsource(frozen_configuration)
    assert "review_projection_sha256" in source
    assert "AuthoringAxesV2.shared_axes" in source
    assert "fail-open" in inspect.getdoc(AuthoringAxesV2.shared_axes)
