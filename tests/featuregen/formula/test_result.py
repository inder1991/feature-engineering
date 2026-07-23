"""Discriminating tests for §F — the multi-axis ``AuthoringResult`` + the pure disposition fold.

Every disposition assertion pins the CARRIED artifacts too (candidate_formula /
candidate_proposal / hash), so a fold that reaches the right label with the wrong
artifact shape — the authority-laundering failure mode — cannot pass.
"""
from __future__ import annotations

import pytest
from tests.featuregen.formula import factories as f

from featuregen.formula.result import (
    DISPOSITION_POLICY_VERSION,
    AuthoringAxes,
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


def test_unsupported_result_echoes_the_axes_it_was_folded_from():
    result = derive_disposition(
        _axes(structural_status="unsupported_operation", critic_status="advisory"),
        authoring_run_id=RUN_ID,
    )
    assert result.structural_status == "unsupported_operation"
    assert result.capability_status == "ok"
    assert result.critic_status == "advisory"
    assert result.technical_status == "ok"
