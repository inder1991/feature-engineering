"""Discriminating tests for §F — the multi-axis ``AuthoringResult`` + the pure disposition fold.

Every disposition assertion pins the CARRIED artifacts too (candidate_formula /
candidate_proposal / hash), so a fold that reaches the right label with the wrong
artifact shape — the authority-laundering failure mode — cannot pass.
"""
from __future__ import annotations

import dataclasses

import pytest
from tests.featuregen.formula import factories as f

from featuregen.formula.canonical import formula_content_hash
from featuregen.formula.output_authority import ExternalRequirement
from featuregen.formula.result import (
    DISPOSITION_POLICY_VERSION,
    AuthoringAxes,
    AuthorityFailure,
    IncoherentResultError,
    derive_disposition,
)
from featuregen.formula.schema import TypedFormulaProposalV1

RUN_ID = "authoring-run-7f3a"


def _axes(**overrides: str) -> AuthoringAxes:
    """All-clear axes; each test overrides only the axis under test."""
    values: dict[str, str] = dict(
        structural_status="ok",
        capability_status="ok",
        output_status="resolved",
        expectation_status="match",
        critic_status="clean",
        technical_status="ok",
    )
    values.update(overrides)
    return AuthoringAxes(**values)  # type: ignore[arg-type]


def _proposal() -> TypedFormulaProposalV1:
    return TypedFormulaProposalV1(
        formula_schema_version=1,
        operation_grammar_version=1,
        canonicalization_version=1,
        grain=f.customer_grain(),
        body=f.ratio_of_sums(),
        parameters=(),
        decimal=f.default_decimal(),
        expected_output=None,
    )


# ---- UNSUPPORTED (unsupported != invalid) ----


def test_structural_unsupported_operation_folds_to_unsupported_not_rejected():
    result = derive_disposition(
        _axes(structural_status="unsupported_operation"), authoring_run_id=RUN_ID
    )
    assert result.authoring_disposition == "UNSUPPORTED"
    assert result.candidate_formula is None
    assert result.candidate_formula_hash is None
    assert result.authoring_run_id == RUN_ID
    assert result.disposition_policy_version == DISPOSITION_POLICY_VERSION


def test_unsupported_capability_folds_to_unsupported_and_carries_the_reason():
    result = derive_disposition(
        _axes(capability_status="unsupported_capability"),
        authoring_run_id=RUN_ID,
        capability_reason="cross_source_relation",
    )
    assert result.authoring_disposition == "UNSUPPORTED"
    assert result.capability_reason == "cross_source_relation"
    assert result.candidate_formula is None
    assert result.candidate_formula_hash is None


# ---- RESOLVED + reviewable NEEDS_REVIEW (a real TypedFormulaV1 exists) ----


def test_all_ok_folds_to_resolved_with_formula_and_computed_hash():
    formula = f.base_formula()
    result = derive_disposition(_axes(), authoring_run_id=RUN_ID, candidate_formula=formula)
    assert result.authoring_disposition == "RESOLVED"
    assert result.candidate_formula is formula
    assert result.candidate_formula_hash == formula_content_hash(formula)
    assert result.candidate_proposal is None
    assert result.authoring_run_id == RUN_ID


def test_blocking_critic_with_resolved_output_is_reviewable_and_carries_the_formula():
    formula = f.base_formula()
    result = derive_disposition(
        _axes(critic_status="blocking"),
        authoring_run_id=RUN_ID,
        candidate_formula=formula,
        critic_findings_hash="c1d2e3",
    )
    assert result.authoring_disposition == "NEEDS_REVIEW"
    assert result.candidate_formula is formula
    assert result.candidate_formula_hash == formula_content_hash(formula)
    assert result.critic_findings_hash == "c1d2e3"
    assert result.candidate_proposal is None
    assert result.authoring_run_id == RUN_ID


def test_expectation_mismatch_with_resolved_output_is_reviewable_with_the_formula():
    formula = f.base_formula()
    result = derive_disposition(
        _axes(expectation_status="mismatch"), authoring_run_id=RUN_ID, candidate_formula=formula
    )
    assert result.authoring_disposition == "NEEDS_REVIEW"
    assert result.candidate_formula is formula
    assert result.candidate_formula_hash == formula_content_hash(formula)


def test_resolved_without_a_candidate_formula_raises_incoherent():
    with pytest.raises(IncoherentResultError, match="candidate_formula"):
        derive_disposition(_axes(), authoring_run_id=RUN_ID)


def test_resolved_with_a_candidate_proposal_raises_incoherent():
    with pytest.raises(IncoherentResultError, match="candidate_proposal"):
        derive_disposition(
            _axes(),
            authoring_run_id=RUN_ID,
            candidate_formula=f.base_formula(),
            candidate_proposal=_proposal(),
        )


def test_reviewable_needs_review_without_a_formula_raises_incoherent():
    with pytest.raises(IncoherentResultError, match="candidate_formula"):
        derive_disposition(_axes(critic_status="blocking"), authoring_run_id=RUN_ID)


# ---- unresolved output authority (the honesty core: NO authoritative formula exists) ----


def test_needs_authority_output_reviews_the_proposal_with_no_formula_and_no_policy():
    proposal = _proposal()
    failures = (
        AuthorityFailure(reason="hash_mismatch", operand="body.numerator", field="additivity"),
    )
    result = derive_disposition(
        _axes(output_status="needs_authority"),
        authoring_run_id=RUN_ID,
        candidate_proposal=proposal,
        authority_failures=failures,
    )
    assert result.authoring_disposition == "NEEDS_REVIEW"
    assert result.candidate_proposal is proposal
    assert result.authority_failures == failures
    # No authoritative formula exists — and no fabricated FormulaOutputPolicyV1 can
    # hide anywhere: the formula slot is empty and the proposal type has no output slot.
    assert result.candidate_formula is None
    assert result.candidate_formula_hash is None
    assert not hasattr(result.candidate_proposal, "output")


def test_external_requirement_output_reviews_the_proposal_with_the_typed_requirements():
    proposal = _proposal()
    requirements = (ExternalRequirement("UNIT_PROVISIONING_REQUIRED"),)
    result = derive_disposition(
        _axes(output_status="external_requirement"),
        authoring_run_id=RUN_ID,
        candidate_proposal=proposal,
        output_requirements=requirements,
    )
    assert result.authoring_disposition == "NEEDS_REVIEW"
    assert result.candidate_proposal is proposal
    assert result.output_requirements == requirements
    assert result.candidate_formula is None
    assert result.candidate_formula_hash is None


def test_unresolved_output_with_a_candidate_formula_raises_incoherent():
    for output_status in ("needs_authority", "external_requirement"):
        with pytest.raises(IncoherentResultError, match="[Uu]nresolved"):
            derive_disposition(
                _axes(output_status=output_status),
                authoring_run_id=RUN_ID,
                candidate_formula=f.base_formula(),
                candidate_proposal=_proposal(),
            )


def test_unresolved_output_without_a_candidate_proposal_raises_incoherent():
    with pytest.raises(IncoherentResultError, match="candidate_proposal"):
        derive_disposition(_axes(output_status="needs_authority"), authoring_run_id=RUN_ID)


def test_unsupported_result_echoes_the_axes_it_was_folded_from():
    result = derive_disposition(
        _axes(structural_status="unsupported_operation", critic_status="advisory"),
        authoring_run_id=RUN_ID,
    )
    assert result.structural_status == "unsupported_operation"
    assert result.capability_status == "ok"
    assert result.critic_status == "advisory"
    assert result.technical_status == "ok"


# ---- the §F precedence ladder (strict order, first match wins) ----

_ALL_WORST = dict(
    structural_status="invalid_formula",
    capability_status="unsupported_capability",
    output_status="invalid_output",
    expectation_status="mismatch",
    critic_status="blocking",
    technical_status="technical_failure",
)


def test_precedence_ladder_technical_then_rejected_then_unsupported_then_review():
    # 1. technical_failure beats EVERYTHING.
    worst = derive_disposition(_axes(**_ALL_WORST), authoring_run_id=RUN_ID)
    assert worst.authoring_disposition == "TECHNICAL_FAILURE"
    assert worst.candidate_formula is None
    assert worst.candidate_formula_hash is None

    # 2. clear technical → REJECTED (invalid beats unsupported).
    rejected = derive_disposition(
        _axes(**{**_ALL_WORST, "technical_status": "ok"}), authoring_run_id=RUN_ID
    )
    assert rejected.authoring_disposition == "REJECTED"
    assert rejected.candidate_formula is None
    assert rejected.candidate_formula_hash is None

    # 3. clear both REJECTED triggers → UNSUPPORTED (beats every review trigger).
    unsupported = derive_disposition(
        _axes(
            **{
                **_ALL_WORST,
                "technical_status": "ok",
                "structural_status": "unsupported_operation",
                "output_status": "needs_authority",
            }
        ),
        authoring_run_id=RUN_ID,
    )
    assert unsupported.authoring_disposition == "UNSUPPORTED"

    # 4. clear structural/capability → NEEDS_REVIEW (unresolved-output shape).
    review = derive_disposition(
        _axes(
            structural_status="ok",
            capability_status="ok",
            output_status="needs_authority",
            expectation_status="mismatch",
            critic_status="blocking",
            technical_status="ok",
        ),
        authoring_run_id=RUN_ID,
        candidate_proposal=_proposal(),
    )
    assert review.authoring_disposition == "NEEDS_REVIEW"
    assert review.candidate_formula is None

    # 5. clear the review triggers → RESOLVED.
    resolved = derive_disposition(
        _axes(), authoring_run_id=RUN_ID, candidate_formula=f.base_formula()
    )
    assert resolved.authoring_disposition == "RESOLVED"


def test_invalid_output_folds_to_rejected_not_needs_review():
    result = derive_disposition(_axes(output_status="invalid_output"), authoring_run_id=RUN_ID)
    assert result.authoring_disposition == "REJECTED"
    assert result.candidate_formula is None


def test_expectation_not_provided_never_blocks_resolution():
    result = derive_disposition(
        _axes(expectation_status="not_provided"),
        authoring_run_id=RUN_ID,
        candidate_formula=f.base_formula(),
    )
    assert result.authoring_disposition == "RESOLVED"


@pytest.mark.parametrize(
    "overrides",
    [
        dict(structural_status="invalid_formula"),  # REJECTED
        dict(technical_status="technical_failure"),  # TECHNICAL_FAILURE
        dict(capability_status="unsupported_capability"),  # UNSUPPORTED
    ],
)
def test_terminal_dispositions_with_a_candidate_formula_raise_incoherent(overrides):
    with pytest.raises(IncoherentResultError, match="candidate_formula"):
        derive_disposition(
            _axes(**overrides), authoring_run_id=RUN_ID, candidate_formula=f.base_formula()
        )


# ---- fail-closed axis vocabulary + immutability ----


def test_unknown_axis_value_fails_closed_instead_of_falling_through_to_resolved():
    with pytest.raises(IncoherentResultError, match="output_status"):
        derive_disposition(
            _axes(output_status="banana"),
            authoring_run_id=RUN_ID,
            candidate_formula=f.base_formula(),
        )


def test_result_is_frozen_and_slotted():
    result = derive_disposition(
        _axes(), authoring_run_id=RUN_ID, candidate_formula=f.base_formula()
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.authoring_disposition = "REJECTED"  # type: ignore[misc]
    # Slotted: no __dict__ to smuggle a fabricated output policy onto.
    assert not hasattr(result, "__dict__")
