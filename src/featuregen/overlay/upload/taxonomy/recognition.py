"""Phase-1A Task 1 — recognition contracts + the closed-taxonomy validator.

The shadow recognizer classifies a redacted hypothesis/goal into governed use-case *objectives*
drawn from the closed taxonomy (``use_cases.py``). This module is the contract layer:

* ``RecognitionStatus`` / ``UseCaseCandidate`` / ``RecognitionResult`` — the immutable value objects
  the recognizer (Task 2) produces and stamps with the version quintet.
* ``validate_recognition_output`` — the ``validate_output`` callback ``drive_structured_call`` invokes
  on the **raw dict** the LLM returns (NOT the dataclass). It raises ``SchemaValidationError`` — the
  same doubt signal ``drive_structured_call`` already routes to bounded repair → fail-closed — whenever
  the body drifts from the closed taxonomy or the classification shape. Every recognised id is checked
  against ``USE_CASE_REGISTRY``; the recognizer never invents ids.
* ``unscoped_result`` — the fail-open constructor: any technical failure maps to
  ``TECHNICAL_FAILURE``/``UNSCOPED`` with no candidates, so grounding continues unfiltered.

Behaviour-neutral: read-only over the taxonomy registry; nothing here touches ``templates.py`` or
grounding. See ``docs/superpowers/plans/2026-07-09-phase1a-shadow-recognizer.md`` Task 1.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from featuregen.contracts import SchemaValidationError
from featuregen.overlay.upload.taxonomy.use_cases import (
    USE_CASE_REGISTRY,
    selectable_leaves,
    use_case,
)

# The version quintet stamped on every recognition result (governance §3). taxonomy_version +
# applicability_mapping_version (bumps when recipe_applicability changes) + recipe_registry_version are
# the three the *result* carries; the recognizer adds recognizer_model_id + prompt_version.
TAXONOMY_VERSION = "1.0.0"
APPLICABILITY_MAPPING_VERSION = "1.0.0"
RECIPE_REGISTRY_VERSION = "1.0.0"

# The selectable LEAVES (terminal objectives). A primary MUST be one of these — the applicability layer
# scopes on leaves, so a non-leaf selectable parent (e.g. "customer", "credit") would scope to zero recipes.
_SELECTABLE_LEAVES: frozenset[str] = frozenset(selectable_leaves())

# Closed classification bands. A candidate that drifts from either is malformed structure.
_RELATIONSHIPS: frozenset[str] = frozenset({"primary", "secondary"})
_CONFIDENCE_BANDS: frozenset[str] = frozenset({"high", "medium", "low"})

# Shape caps: at most one primary objective, two supporting secondaries, three candidates total.
_MAX_PRIMARY = 1
_MAX_SECONDARY = 2
_MAX_CANDIDATES = 3


class RecognitionStatus(StrEnum):
    """The outcome of one recognition. ``CLASSIFIED``/``AMBIGUOUS`` carry candidates; ``UNSCOPED``
    (nothing clearly applies) and ``TECHNICAL_FAILURE`` (a provider/validation failure — fail-open)
    carry none. The string values ARE the wire contract the LLM returns."""

    CLASSIFIED = "classified"
    AMBIGUOUS = "ambiguous"
    UNSCOPED = "unscoped"
    TECHNICAL_FAILURE = "technical_failure"


# Statuses that MUST carry at least one candidate (a classification with nothing classified is malformed).
_CANDIDATE_BEARING: frozenset[str] = frozenset(
    {RecognitionStatus.CLASSIFIED.value, RecognitionStatus.AMBIGUOUS.value})
_STATUS_VALUES: frozenset[str] = frozenset(s.value for s in RecognitionStatus)


@dataclass(frozen=True, slots=True)
class UseCaseCandidate:
    """One recognised use-case objective and its relationship to the request."""

    use_case_id: str
    relationship: Literal["primary", "secondary"]
    confidence: Literal["high", "medium", "low"]
    evidence_spans: tuple[str, ...]
    rationale: str


@dataclass(frozen=True, slots=True)
class RecognitionResult:
    """The immutable recognition outcome, stamped with the version quintet fields this phase owns
    (taxonomy_version, recognizer_model_id, prompt_version)."""

    status: RecognitionStatus
    candidates: tuple[UseCaseCandidate, ...]
    ambiguity_note: str | None
    taxonomy_version: str
    recognizer_model_id: str
    prompt_version: str
    applicability_mapping_version: str = APPLICABILITY_MAPPING_VERSION
    recipe_registry_version: str = RECIPE_REGISTRY_VERSION


def _validate_candidate(candidate: Any, index: int) -> str:
    """Validate one raw candidate dict; return its ``relationship`` (for the aggregate shape caps).
    Raises ``SchemaValidationError`` on the first drift."""
    if not isinstance(candidate, Mapping):
        raise SchemaValidationError(
            f"recognition candidate #{index} must be an object, got {type(candidate).__name__}")

    uid = candidate.get("use_case_id")
    node = use_case(uid) if isinstance(uid, str) else None
    if node is None or uid not in USE_CASE_REGISTRY:
        raise SchemaValidationError(
            f"recognition candidate #{index} use_case_id {uid!r} is not in the closed taxonomy")

    relationship = candidate.get("relationship")
    if relationship not in _RELATIONSHIPS:
        raise SchemaValidationError(
            f"recognition candidate #{index} relationship {relationship!r} not in "
            f"{sorted(_RELATIONSHIPS)}")

    # A primary must be a selectable LEAF objective — never a domain parent (financial_crime) and never
    # a non-leaf selectable parent (customer, credit, insurance.lapse), which would scope to zero recipes.
    if relationship == "primary" and uid not in _SELECTABLE_LEAVES:
        raise SchemaValidationError(
            f"recognition candidate #{index} primary {uid!r} is not a selectable leaf objective")

    confidence = candidate.get("confidence")
    if confidence not in _CONFIDENCE_BANDS:
        raise SchemaValidationError(
            f"recognition candidate #{index} confidence {confidence!r} not in "
            f"{sorted(_CONFIDENCE_BANDS)}")

    spans = candidate.get("evidence_spans") or ()
    if not isinstance(spans, (list, tuple)):
        raise SchemaValidationError(
            f"recognition candidate #{index} evidence_spans must be a list, got "
            f"{type(spans).__name__}")
    for span in spans:
        if not isinstance(span, str) or not span.strip():
            raise SchemaValidationError(
                f"recognition candidate #{index} has an evidence span that is not a non-empty "
                f"string: {span!r}")

    return relationship


def validate_recognition_output(output: Mapping[str, Any]) -> None:
    """Raise ``SchemaValidationError`` if the raw recognition body drifts from the closed taxonomy or
    the classification shape. Passes silently (returns ``None``) on a well-formed body.

    Rejects: a missing/unknown ``status``; a candidate ``use_case_id`` outside ``USE_CASE_REGISTRY``;
    a ``primary`` that is not a selectable objective; more than one primary, more than two secondary,
    or more than three candidates total; a ``confidence``/``relationship`` outside its band; a
    ``classified``/``ambiguous`` body with zero candidates; an evidence span that is not a non-empty
    string. A ``unscoped``/``technical_failure`` body with empty candidates is valid."""
    status = output.get("status")
    if not isinstance(status, str) or status not in _STATUS_VALUES:
        raise SchemaValidationError(
            f"recognition status {status!r} is not one of {sorted(_STATUS_VALUES)}")

    candidates = output.get("candidates")
    if candidates is None:
        candidates = ()
    if not isinstance(candidates, (list, tuple)):
        raise SchemaValidationError(
            f"recognition candidates must be a list, got {type(candidates).__name__}")

    seen_ids: set[str] = set()
    n_primary = 0
    n_secondary = 0
    for index, candidate in enumerate(candidates):
        relationship = _validate_candidate(candidate, index)
        uid = candidate["use_case_id"]
        if uid in seen_ids:                       # a secondary duplicating the primary, or a repeated id
            raise SchemaValidationError(f"recognition has a duplicate candidate id {uid!r}")
        seen_ids.add(uid)
        if relationship == "primary":
            n_primary += 1
        else:
            n_secondary += 1

    if n_primary > _MAX_PRIMARY:
        raise SchemaValidationError(
            f"recognition has {n_primary} primary candidates (at most {_MAX_PRIMARY} allowed)")
    if n_secondary > _MAX_SECONDARY:
        raise SchemaValidationError(
            f"recognition has {n_secondary} secondary candidates (at most {_MAX_SECONDARY} allowed)")
    if len(candidates) > _MAX_CANDIDATES:
        raise SchemaValidationError(
            f"recognition has {len(candidates)} candidates (at most {_MAX_CANDIDATES} allowed)")

    if status in _CANDIDATE_BEARING and not candidates:
        raise SchemaValidationError(
            f"recognition status {status!r} requires at least one candidate")
    # A CLASSIFIED result asserts a single objective — it MUST carry exactly one primary. (AMBIGUOUS may
    # carry only alternatives with no designated primary; scope_from_recognition treats that as unscoped.)
    if status == RecognitionStatus.CLASSIFIED.value and n_primary != _MAX_PRIMARY:
        raise SchemaValidationError(
            f"recognition status 'classified' requires exactly one primary candidate, got {n_primary}")


def unscoped_result(
    reason: str,
    *,
    model_id: str,
    prompt_version: str,
    technical: bool = False,
) -> RecognitionResult:
    """The fail-open constructor: a candidate-free result whose ``ambiguity_note`` carries ``reason``.
    ``technical=True`` marks a provider/validation failure (``TECHNICAL_FAILURE``); otherwise the
    recognizer simply found nothing in scope (``UNSCOPED``). Either way grounding continues unfiltered."""
    return RecognitionResult(
        status=RecognitionStatus.TECHNICAL_FAILURE if technical else RecognitionStatus.UNSCOPED,
        candidates=(),
        ambiguity_note=reason,
        taxonomy_version=TAXONOMY_VERSION,
        recognizer_model_id=model_id,
        prompt_version=prompt_version,
    )
