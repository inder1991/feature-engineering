"""Phase-1A Task 1 — recognition contracts + closed-taxonomy validator.

Exercises ``validate_recognition_output`` (the raw-dict ``validate_output`` callback
``drive_structured_call`` will invoke in Task 2) against the closed use-case taxonomy, plus the
``unscoped_result`` fail-open constructor. See
``docs/superpowers/plans/2026-07-09-phase1a-shadow-recognizer.md`` Task 1.
"""
from __future__ import annotations

from typing import Any

import pytest

from featuregen.contracts import SchemaValidationError
from featuregen.overlay.upload.taxonomy.recognition import (
    TAXONOMY_VERSION,
    RecognitionStatus,
    unscoped_result,
    validate_recognition_output,
)

# A real selectable leaf (see use_cases.py) — the canonical well-formed primary.
CHURN = "customer.relationship_attrition.churn"
DEPOSIT = "customer.relationship_attrition.deposit_attrition"
PRIMACY = "customer.relationship_attrition.primacy_loss"


def _candidate(
    use_case_id: str,
    *,
    relationship: str = "primary",
    confidence: str = "high",
    evidence_spans: tuple[str, ...] = ("close their current account",),
    rationale: str = "clear attrition intent",
) -> dict[str, Any]:
    return {
        "use_case_id": use_case_id,
        "relationship": relationship,
        "confidence": confidence,
        "evidence_spans": list(evidence_spans),
        "rationale": rationale,
    }


def _classified(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    return {"status": "classified", "candidates": candidates, "ambiguity_note": None}


# ── the happy path ──────────────────────────────────────────────────────────────────────────────
def test_accepts_well_formed_classified_body():
    body = _classified([
        _candidate(CHURN, relationship="primary"),
        _candidate(DEPOSIT, relationship="secondary", confidence="medium"),
    ])
    # Returns None (raises on anything malformed).
    assert validate_recognition_output(body) is None


def test_accepts_unscoped_with_empty_candidates():
    # A unscoped/technical_failure status with NO candidates is valid.
    assert validate_recognition_output({"status": "unscoped", "candidates": []}) is None
    assert validate_recognition_output({"status": "technical_failure", "candidates": []}) is None


# ── the rejections ────────────────────────────────────────────────────────────────────────────────
def test_rejects_unknown_status():
    with pytest.raises(SchemaValidationError):
        validate_recognition_output({"status": "maybe", "candidates": []})
    with pytest.raises(SchemaValidationError):
        validate_recognition_output({"candidates": []})  # status missing


def test_rejects_unknown_use_case_id():
    with pytest.raises(SchemaValidationError):
        validate_recognition_output(_classified([_candidate("customer.not_a_real_leaf")]))


def test_rejects_non_selectable_primary():
    # financial_crime is the one non-selectable domain parent — never a valid primary objective.
    with pytest.raises(SchemaValidationError):
        validate_recognition_output(_classified([_candidate("financial_crime")]))


def test_rejects_two_primaries():
    with pytest.raises(SchemaValidationError):
        validate_recognition_output(_classified([
            _candidate(CHURN, relationship="primary"),
            _candidate(DEPOSIT, relationship="primary"),
        ]))


def test_rejects_three_secondaries():
    with pytest.raises(SchemaValidationError):
        validate_recognition_output(_classified([
            _candidate(CHURN, relationship="secondary"),
            _candidate(DEPOSIT, relationship="secondary"),
            _candidate(PRIMACY, relationship="secondary"),
        ]))


def test_rejects_more_than_three_candidates():
    with pytest.raises(SchemaValidationError):
        validate_recognition_output(_classified([
            _candidate(CHURN, relationship="primary"),
            _candidate(DEPOSIT, relationship="secondary"),
            _candidate(PRIMACY, relationship="secondary"),
            _candidate("customer.cross_sell", relationship="secondary"),
        ]))


def test_rejects_classified_with_no_candidates():
    with pytest.raises(SchemaValidationError):
        validate_recognition_output(_classified([]))
    with pytest.raises(SchemaValidationError):
        validate_recognition_output({"status": "ambiguous", "candidates": []})


def test_rejects_bad_confidence():
    with pytest.raises(SchemaValidationError):
        validate_recognition_output(_classified([_candidate(CHURN, confidence="very-high")]))


def test_rejects_bad_relationship():
    with pytest.raises(SchemaValidationError):
        validate_recognition_output(_classified([_candidate(CHURN, relationship="tertiary")]))


def test_rejects_empty_evidence_span():
    with pytest.raises(SchemaValidationError):
        validate_recognition_output(_classified([_candidate(CHURN, evidence_spans=("",))]))
    with pytest.raises(SchemaValidationError):
        validate_recognition_output(_classified([_candidate(CHURN, evidence_spans=(123,))]))  # type: ignore[arg-type]


# ── the fail-open constructor ─────────────────────────────────────────────────────────────────────
def test_unscoped_result_defaults_to_unscoped():
    result = unscoped_result("no target", model_id="m", prompt_version="1")
    assert result.status is RecognitionStatus.UNSCOPED
    assert result.candidates == ()
    assert result.ambiguity_note == "no target"
    assert result.taxonomy_version == TAXONOMY_VERSION
    assert result.recognizer_model_id == "m"
    assert result.prompt_version == "1"


def test_unscoped_result_technical_flag():
    result = unscoped_result("provider refused", model_id="m", prompt_version="1", technical=True)
    assert result.status is RecognitionStatus.TECHNICAL_FAILURE
    assert result.candidates == ()
