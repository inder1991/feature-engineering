"""Phase-1A Task 2 — the LLM-only, fail-open use-case recognizer.

``recognize`` builds ONE ``LLMRequest`` over the *redacted* hypothesis/goal (NEVER catalog columns),
drives it through ``drive_structured_call`` — which already provides the bounded repair/retry/
fail-closed runtime contract against ``validate_recognition_output`` — then maps the outcome to a
``RecognitionResult``.

It is FAIL-OPEN by construction: it NEVER raises to its caller and NEVER blocks generation. Every
technical failure — a provider failure/refusal, a validation budget exhausted (``STATUS_FAILED``), a
dispatch exception, or an unexpected mapping — folds to a candidate-free ``TECHNICAL_FAILURE`` result
so grounding continues unfiltered. A well-formed ``unscoped`` body folds to ``UNSCOPED``. The output
is already validated by ``drive_structured_call``, so the mapping is total; the guards are belt-and-
braces to honour the never-raise contract.

Behaviour-neutral: recognition only produces a result object — nothing here filters grounding or
touches ``templates.py``. See ``docs/superpowers/plans/2026-07-09-phase1a-shadow-recognizer.md`` Task 2.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from featuregen.intake.llm import (
    STATUS_FAILED,
    LLMClient,
    LLMRequest,
    drive_structured_call,
)
from featuregen.overlay.upload.taxonomy.recognition import (
    TAXONOMY_VERSION,
    RecognitionResult,
    RecognitionStatus,
    UseCaseCandidate,
    unscoped_result,
    validate_recognition_output,
)
from featuregen.overlay.upload.taxonomy.recognizer_prompt import PROMPT_ID, PROMPT_VERSION

logger = logging.getLogger(__name__)

# The audited task name + the structured-output schema identity for this call. Kept equal to the
# prompt id so the recognizer reads as one coherent (task, prompt, schema) unit in the llm_call store.
RECOGNIZER_TASK = "use_case_recognition"
_OUTPUT_SCHEMA_ID = "use_case_recognition"
_OUTPUT_SCHEMA_VERSION = 1


def _result_from_output(output: Mapping[str, Any], *, model_id: str) -> RecognitionResult:
    """Map a validated recognition body to a ``RecognitionResult``. ``output`` has already passed
    ``validate_recognition_output`` (a valid enum ``status``; closed-taxonomy ids; banded
    ``relationship``/``confidence``; the shape caps), so this mapping is total."""
    status = RecognitionStatus(str(output["status"]))
    candidates = tuple(
        UseCaseCandidate(
            use_case_id=str(candidate["use_case_id"]),
            relationship=candidate["relationship"],
            confidence=candidate["confidence"],
            evidence_spans=tuple(candidate.get("evidence_spans") or ()),
            rationale=str(candidate.get("rationale", "")),
        )
        for candidate in (output.get("candidates") or ())
    )
    return RecognitionResult(
        status=status,
        candidates=candidates,
        ambiguity_note=output.get("ambiguity_note"),
        taxonomy_version=TAXONOMY_VERSION,
        recognizer_model_id=model_id,
        prompt_version=PROMPT_VERSION,
    )


def recognize(
    client: LLMClient,
    *,
    redacted_hypothesis: str,
    redacted_goal: str | None = None,
    model_id: str = "claude-opus-4-8",
) -> RecognitionResult:
    """Recognise the governed use-case scope of a *redacted* request. LLM-only and FAIL-OPEN: never
    raises to its caller. Any technical failure (provider failure/refusal, validation budget
    exhausted, dispatch/mapping error) folds to a candidate-free ``TECHNICAL_FAILURE``; a well-formed
    ``unscoped`` body folds to ``UNSCOPED``. The input carries only the redacted hypothesis and
    prediction goal — never catalog columns."""
    request = LLMRequest(
        task=RECOGNIZER_TASK,
        prompt_id=PROMPT_ID,
        prompt_version=int(PROMPT_VERSION),
        inputs={"hypothesis": redacted_hypothesis, "prediction_goal": redacted_goal},
        output_schema_id=_OUTPUT_SCHEMA_ID,
        output_schema_version=_OUTPUT_SCHEMA_VERSION,
        generation_settings={"provider": "anthropic", "model": model_id},
        output_schema=None,
    )

    try:
        outcome = drive_structured_call(client, request, validate_recognition_output)
    except Exception:
        # Fail-open: a dispatch-time error (e.g. provider transport, misconfigured client) must never
        # propagate — recognition is shadow and must never block generation.
        logger.exception("recognition dispatch raised; failing open to technical_failure")
        return unscoped_result(
            "recognition dispatch error", model_id=model_id, prompt_version=PROMPT_VERSION,
            technical=True)

    if outcome.status == STATUS_FAILED:
        reason = outcome.validation_result.get("reason", "recognition failed")
        return unscoped_result(
            reason, model_id=model_id, prompt_version=PROMPT_VERSION, technical=True)

    try:
        return _result_from_output(outcome.output, model_id=model_id)
    except Exception:
        # The output is already validated, so this should not happen; guard anyway to honour the
        # never-raise contract if the body drifts in a way validation did not cover.
        logger.exception("recognition output mapping raised; failing open to technical_failure")
        return unscoped_result(
            "recognition mapping error", model_id=model_id, prompt_version=PROMPT_VERSION,
            technical=True)
