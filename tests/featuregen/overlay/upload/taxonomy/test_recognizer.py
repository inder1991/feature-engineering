"""Phase-1A Task 2 — the LLM-only, fail-open use-case recognizer.

FakeLLM-scripted (keyed on ``RECOGNIZER_TASK``) exercises of ``recognize``'s outcome mapping: a clean
CLASSIFIED body, an unknown-id body that exhausts the repair budget, a provider refusal, and an
UNSCOPED body — asserting the recognizer folds every failure to ``UNSCOPED``/``TECHNICAL_FAILURE`` and
never raises, never surfaces an invalid id. Plus ``build_recognition_prompt`` offers the selectable
ids and never the non-selectable ``financial_crime`` domain parent. See
``docs/superpowers/plans/2026-07-09-phase1a-shadow-recognizer.md`` Task 2.
"""
from __future__ import annotations

from typing import Any

from featuregen.intake.llm import (
    PROVIDER_OK,
    PROVIDER_REFUSAL,
    FakeLLM,
    FakeResponse,
)
from featuregen.overlay.upload.taxonomy.recognition import (
    TAXONOMY_VERSION,
    RecognitionStatus,
)
from featuregen.overlay.upload.taxonomy.recognizer import RECOGNIZER_TASK, recognize
from featuregen.overlay.upload.taxonomy.recognizer_prompt import (
    PROMPT_VERSION,
    build_recognition_prompt,
)

# Real selectable leaves (see use_cases.py).
CHURN = "customer.relationship_attrition.churn"
DEPOSIT = "customer.relationship_attrition.deposit_attrition"

_MODEL = "claude-opus-4-8"


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


def _fake(output: dict[str, Any], *, provider_status: str = PROVIDER_OK) -> FakeLLM:
    # R19 task-key form: keyed on request.task (== RECOGNIZER_TASK). FakeLLM repeats the last
    # response once its sequence is exhausted, so a single response also drives the repair loop.
    return FakeLLM(script={RECOGNIZER_TASK: FakeResponse(output=output, provider_status=provider_status)})


def test_classified_output_maps_to_classified_result(db) -> None:
    output = {
        "status": "classified",
        "candidates": [
            _candidate(CHURN, relationship="primary"),
            _candidate(DEPOSIT, relationship="secondary", confidence="medium"),
        ],
        "ambiguity_note": None,
    }
    result = recognize(
        db, _fake(output),
        redacted_hypothesis="will this customer close their current account next quarter?")

    assert result.status is RecognitionStatus.CLASSIFIED
    primaries = [c for c in result.candidates if c.relationship == "primary"]
    assert len(primaries) == 1
    assert primaries[0].use_case_id == CHURN
    assert primaries[0].confidence == "high"
    assert primaries[0].evidence_spans == ("close their current account",)
    # Version quintet fields this phase owns are stamped on the result.
    assert result.taxonomy_version == TAXONOMY_VERSION
    assert result.recognizer_model_id == _MODEL
    assert result.prompt_version == PROMPT_VERSION


def test_unknown_use_case_id_fails_open_never_invalid(db) -> None:
    # An unknown id is structurally valid (passes the JSON schema) but fails the closed-taxonomy
    # semantic post-pass (validate_recognition_output) -> fail-open. Never an invalid id, never raises.
    output = {
        "status": "classified",
        "candidates": [_candidate("customer.not_a_real_leaf", relationship="primary")],
    }
    result = recognize(db, _fake(output), redacted_hypothesis="something vague and unmapped")

    assert result.status in (RecognitionStatus.TECHNICAL_FAILURE, RecognitionStatus.UNSCOPED)
    assert result.candidates == ()
    assert all(c.use_case_id != "customer.not_a_real_leaf" for c in result.candidates)


def test_provider_refusal_is_technical_failure(db) -> None:
    result = recognize(
        db, _fake({}, provider_status=PROVIDER_REFUSAL), redacted_hypothesis="anything at all")
    assert result.status is RecognitionStatus.TECHNICAL_FAILURE
    assert result.candidates == ()


def test_unscoped_output_maps_to_unscoped(db) -> None:
    result = recognize(
        db, _fake({"status": "unscoped", "candidates": []}),
        redacted_hypothesis="let's explore what is interesting in the data")
    assert result.status is RecognitionStatus.UNSCOPED
    assert result.candidates == ()


def test_prompt_lists_selectable_ids_not_financial_crime() -> None:
    prompt = build_recognition_prompt()
    # A known selectable leaf is offered as a pick.
    assert "credit.early_warning" in prompt
    # financial_crime is the non-selectable domain parent — never offered as a selectable choice.
    assert "financial_crime" not in prompt
