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
import os
from collections.abc import Mapping
from typing import Any

from featuregen.contracts import SchemaValidationError
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.intake.llm import DEFAULT_LLM_MODEL, LLMClient
from featuregen.overlay.upload.enrich_llm import audited_structured_call
from featuregen.overlay.upload.taxonomy.recognition import (
    TAXONOMY_VERSION,
    RecognitionResult,
    RecognitionStatus,
    UseCaseCandidate,
    normalize_dimensions,
    unscoped_result,
    validate_recognition_output,
)
from featuregen.overlay.upload.taxonomy.recognizer_prompt import (
    PROMPT_ID,
    PROMPT_VERSION,
    build_recognition_prompt,
)

logger = logging.getLogger(__name__)

# The audited task name + the structured-output schema identity for this call. Kept equal to the
# prompt id so the recognizer reads as one coherent (task, prompt, schema) unit in the llm_call store.
RECOGNIZER_TASK = "use_case_recognition"
_OUTPUT_SCHEMA_ID = "use_case_recognition"
_OUTPUT_SCHEMA_VERSION = 1


def _result_from_output(output: Mapping[str, Any], *, model_id: str) -> RecognitionResult:
    """Map a validated recognition body to a ``RecognitionResult``. ``output`` has already passed
    ``validate_recognition_output`` (a valid enum ``status``; closed-taxonomy ids; banded
    ``relationship``/``confidence``; the shape caps), so the CORE mapping is total. The optional
    DIMENSIONS are cleaned per-dimension (non-fatally) via ``normalize_dimensions`` — an invalid
    ``modelling_context``/``target_entity`` is dropped/cleared and recorded in ``warnings``, and NEVER
    invalidates the (already valid) use-case recognition."""
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
    modelling_contexts, target_entity, warnings = normalize_dimensions(output)
    return RecognitionResult(
        status=status,
        candidates=candidates,
        ambiguity_note=output.get("ambiguity_note"),
        taxonomy_version=TAXONOMY_VERSION,
        recognizer_model_id=model_id,
        prompt_version=PROMPT_VERSION,
        modelling_contexts=modelling_contexts,
        target_entity=target_entity,
        warnings=warnings,
    )


def _recognition_instruction(redacted_hypothesis: str, redacted_goal: str | None) -> str:
    """The model-facing text: the closed-taxonomy prompt + the redacted request. This is passed as the
    audited seam's ``instruction`` (the reserved ``redacted_intent`` the adapter renders to the model);
    it carries ONLY the already-redacted hypothesis/goal — never catalog columns."""
    lines = [build_recognition_prompt(), "", "=== REQUEST TO CLASSIFY ===",
             f"HYPOTHESIS: {redacted_hypothesis}"]
    if redacted_goal:
        lines.append(f"PREDICTION GOAL: {redacted_goal}")
    return "\n".join(lines)


def recognize(
    conn,
    client: LLMClient,
    *,
    redacted_hypothesis: str,
    redacted_goal: str | None = None,
    model_id: str | None = None,
    actor: IdentityEnvelope | None = None,
) -> RecognitionResult:
    """Recognise the governed use-case scope of a *redacted* request. LLM-only and FAIL-OPEN: never
    raises to its caller. Routes through the platform's AUDITED seam (``audited_structured_call``) so a
    real provider gets the registered output-schema (never fails closed for lack of one), the egress
    guard scans the text, and the call is recorded in ``llm_call``. Any failure (egress block, provider
    failure/refusal, invalid body, dispatch/mapping error) folds to a candidate-free
    ``TECHNICAL_FAILURE``; a well-formed ``unscoped`` body folds to ``UNSCOPED``. The input carries only
    the redacted hypothesis + prediction goal (``catalog_metadata`` is empty — recognition never sees
    columns). ``model_id`` defaults to the env-configured model (matching the wired client)."""
    model = model_id or os.environ.get("FEATUREGEN_LLM_MODEL", DEFAULT_LLM_MODEL)
    instruction = _recognition_instruction(redacted_hypothesis, redacted_goal)

    try:
        output = audited_structured_call(
            conn, client, task=RECOGNIZER_TASK, prompt_id=PROMPT_ID,
            schema_id=_OUTPUT_SCHEMA_ID, catalog_metadata={}, instruction=instruction, actor=actor)
    except Exception:
        logger.exception("recognition dispatch raised; failing open to technical_failure")
        return unscoped_result(
            "recognition dispatch error", model_id=model, prompt_version=PROMPT_VERSION, technical=True)

    if not output:                              # egress block / provider failure / empty body
        return unscoped_result(
            "recognition failed or egress-blocked", model_id=model, prompt_version=PROMPT_VERSION,
            technical=True)

    try:
        validate_recognition_output(output)     # closed-taxonomy semantics (id in registry, primary leaf)
    except SchemaValidationError as exc:
        return unscoped_result(
            f"recognition output invalid: {exc}", model_id=model, prompt_version=PROMPT_VERSION,
            technical=True)

    try:
        return _result_from_output(output, model_id=model)
    except Exception:
        logger.exception("recognition output mapping raised; failing open to technical_failure")
        return unscoped_result(
            "recognition mapping error", model_id=model, prompt_version=PROMPT_VERSION, technical=True)
