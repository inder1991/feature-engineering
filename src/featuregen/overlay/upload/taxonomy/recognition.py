"""Phase-1A Task 1 — recognition contracts + the closed-taxonomy validator.

The shadow recognizer classifies a redacted hypothesis/goal into governed use-case *objectives*
drawn from the closed taxonomy (``use_cases.py``). This module is the contract layer:

* ``RecognitionStatus`` / ``UseCaseCandidate`` / ``RecognitionResult`` — the immutable value objects
  the recognizer (Task 2) produces and stamps with the version quintet.
* ``validate_recognition_output`` — the **semantic** half of the recognizer's validation, run on the
  **raw dict** the LLM returns (NOT the dataclass). Since the repair-seam plan's Task 3 it is handed
  to the audited seam as ``validate_semantics=``, so ``drive_structured_call`` runs it INSIDE the
  bounded repair loop — after the registered JSON Schema, which stays the fail-closed floor
  (``enrich_llm._compose_validation`` owns that ordering). The recognizer ALSO re-runs it after the
  call as a belt-and-braces floor: a repair that never succeeded must still never yield a scope.

  **This docstring used to claim that wiring already existed. It did not** — the audited wrapper
  hardcoded its validator to schema-only, so every rule below was checked AFTER the call, outside the
  loop, and the model was never asked to fix an answer the platform then discarded whole (the
  2026-08-15 live incident). Corrected in Task 3 of
  ``docs/superpowers/plans/2026-08-15-recognition-repair-seam.md``.

  It raises ``AttestedSchemaValidationError`` — a ``SchemaValidationError``, so it is the same doubt
  signal the seam already routes to bounded repair → fail-closed — carrying one of the CLOSED,
  VALUE-FREE codes below as its ``llm_safe_reason``. Every recognised id is checked against
  ``USE_CASE_REGISTRY``; the recognizer never invents ids.
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

from featuregen.contracts import AttestedSchemaValidationError
from featuregen.overlay.upload.taxonomy.dimensions import MODELLING_CONTEXTS, known_entities
from featuregen.overlay.upload.taxonomy.use_cases import (
    USE_CASE_REGISTRY,
    selectable_leaves,
    use_case,
)
from featuregen.overlay.upload.taxonomy.versions import (
    APPLICABILITY_MAPPING_VERSION,
    RECIPE_REGISTRY_VERSION,
)

# The version quintet stamped on every recognition result (governance §3). taxonomy_version +
# applicability_mapping_version (bumps when recipe_applicability changes) + recipe_registry_version are
# the three the *result* carries; the recognizer adds recognizer_model_id + prompt_version.
TAXONOMY_VERSION = "1.0.0"

# The version of the SEMANTIC validator below — ``validate_recognition_output`` and the closed bands,
# caps and leaf rule it enforces. It is NOT the taxonomy version (the taxonomy can grow without the
# rules changing) and NOT the output-schema version (the schema is structure; this is semantics).
# It is a leg of the recognition REQUEST IDENTITY: the same words, the same prompt and the same model
# can yield a different DISPOSITION under different validation rules, so a stored answer produced
# under one validator may not be served as the answer under another. Bump it whenever what this
# module ACCEPTS or REJECTS changes — the repair-seam plan's Tasks 2 and 4 both do exactly that.
RECOGNITION_VALIDATOR_VERSION = "1"

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

# Per-dimension warning codes (Phase-2B). The optional intent dimensions (``modelling_context`` /
# ``target_entity``) are validated PER-DIMENSION and NON-FATALLY: an invalid value is dropped/cleared
# and one of these codes is stamped on the result's ``warnings`` — it NEVER invalidates a valid
# use-case recognition (see ``normalize_dimensions``).
UNKNOWN_MODELLING_CONTEXT = "UNKNOWN_MODELLING_CONTEXT"
UNKNOWN_TARGET_ENTITY = "UNKNOWN_TARGET_ENTITY"

# ── the closed, value-free failure vocabulary of the SEMANTIC validator (repair seam, Task 3) ─────
# Each code names a RULE, never the value that broke it. They ride out of this module twice, and
# both destinations are why the rule is absolute:
#   * as ``AttestedSchemaValidationError.llm_safe_reason`` — re-prompted to the provider in the
#     repair turn and persisted verbatim into ``llm_call.repair_attempts`` (and, under a dispatch
#     audit, ``llm_dispatch.redacted_input``). Nothing downstream re-scans that text;
#   * as the ``ambiguity_note`` of a fail-open result, which is stored and shown to a human.
# The seam ENFORCES the shape (``enrich_llm._SEMANTIC_REPAIR_CODE`` = ``^[A-Z][A-Z0-9_]{0,63}$``);
# an author who interpolates a value here is scrubbed back to a dull constant rather than trusted.
# Candidate-LOCAL rules (one candidate is wrong) come first, then AGGREGATE rules (the SET is
# wrong) — a distinction Task 4's partition turns into policy: local defects may be dropped,
# aggregate defects refuse the whole result.
MALFORMED_CANDIDATE = "MALFORMED_CANDIDATE"                     # not an object at all
UNKNOWN_USE_CASE_ID = "UNKNOWN_USE_CASE_ID"                     # outside USE_CASE_REGISTRY
PRIMARY_NOT_A_LEAF_OBJECTIVE = "PRIMARY_NOT_A_LEAF_OBJECTIVE"   # a primary that scopes to 0 recipes
INVALID_RELATIONSHIP = "INVALID_RELATIONSHIP"
INVALID_CONFIDENCE = "INVALID_CONFIDENCE"
MALFORMED_EVIDENCE_SPANS = "MALFORMED_EVIDENCE_SPANS"
INVALID_STATUS = "INVALID_STATUS"
MALFORMED_CANDIDATE_LIST = "MALFORMED_CANDIDATE_LIST"
DUPLICATE_CANDIDATE = "DUPLICATE_CANDIDATE"
MULTIPLE_PRIMARY_CANDIDATES = "MULTIPLE_PRIMARY_CANDIDATES"
TOO_MANY_SECONDARY_CANDIDATES = "TOO_MANY_SECONDARY_CANDIDATES"
TOO_MANY_CANDIDATES = "TOO_MANY_CANDIDATES"
STATUS_REQUIRES_A_CANDIDATE = "STATUS_REQUIRES_A_CANDIDATE"
CLASSIFIED_REQUIRES_ONE_PRIMARY = "CLASSIFIED_REQUIRES_ONE_PRIMARY"

#: Every code this module can put on the wire, enumerable so a test can hold the whole vocabulary
#: to the seam's shape rule at once rather than one raise site at a time.
RECOGNITION_FAILURE_CODES: frozenset[str] = frozenset({
    MALFORMED_CANDIDATE, UNKNOWN_USE_CASE_ID, PRIMARY_NOT_A_LEAF_OBJECTIVE, INVALID_RELATIONSHIP,
    INVALID_CONFIDENCE, MALFORMED_EVIDENCE_SPANS, INVALID_STATUS, MALFORMED_CANDIDATE_LIST,
    DUPLICATE_CANDIDATE, MULTIPLE_PRIMARY_CANDIDATES, TOO_MANY_SECONDARY_CANDIDATES,
    TOO_MANY_CANDIDATES, STATUS_REQUIRES_A_CANDIDATE, CLASSIFIED_REQUIRES_ONE_PRIMARY,
})


def _reject(code: str, detail: str) -> AttestedSchemaValidationError:
    """Build the one exception shape this module raises: an ATTESTED failure whose wire reason is a
    closed code and whose message is author-literal text plus, at most, a candidate POSITION.

    A position is platform-arithmetic, not model content, so it is safe in the message — and the
    message is what the recognizer folds into a human-visible ``ambiguity_note``. The offending
    VALUE is never interpolated anywhere: it has not been through the §9.4 egress guard, and the
    message of a validator that fails a body is exactly the text most likely to quote it."""
    return AttestedSchemaValidationError(f"{code}: {detail}", llm_safe_reason=code)


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
    (taxonomy_version, recognizer_model_id, prompt_version).

    Phase-2B adds the two optional, human-confirmable intent DIMENSIONS the recognizer PROPOSES —
    ``modelling_contexts`` (0+ regulatory framework/regime ids) and a single soft ``target_entity``
    (the prediction grain) — plus non-fatal per-dimension ``warnings``. They default empty so a
    dimension-free recognition (and every Phase-1 caller) is unchanged."""

    status: RecognitionStatus
    candidates: tuple[UseCaseCandidate, ...]
    ambiguity_note: str | None
    taxonomy_version: str
    recognizer_model_id: str
    prompt_version: str
    applicability_mapping_version: str = APPLICABILITY_MAPPING_VERSION
    recipe_registry_version: str = RECIPE_REGISTRY_VERSION
    modelling_contexts: tuple[str, ...] = ()
    target_entity: str | None = None
    warnings: tuple[str, ...] = ()


def _validate_candidate(candidate: Any, index: int) -> str:
    """Validate one raw candidate dict; return its ``relationship`` (for the aggregate shape caps).
    Raises on the first drift, with the closed code that names the rule (never the value)."""
    if not isinstance(candidate, Mapping):
        raise _reject(MALFORMED_CANDIDATE, f"candidate #{index} is not an object")

    uid = candidate.get("use_case_id")
    node = use_case(uid) if isinstance(uid, str) else None
    if node is None or uid not in USE_CASE_REGISTRY:
        raise _reject(UNKNOWN_USE_CASE_ID,
                      f"candidate #{index} names an id outside the closed taxonomy")

    relationship = candidate.get("relationship")
    if relationship not in _RELATIONSHIPS:
        raise _reject(INVALID_RELATIONSHIP,
                      f"candidate #{index} relationship is outside {sorted(_RELATIONSHIPS)}")

    # A primary must be a selectable LEAF objective — never a domain parent (financial_crime) and never
    # a non-leaf selectable parent (customer, credit, insurance.lapse), which would scope to zero recipes.
    if relationship == "primary" and uid not in _SELECTABLE_LEAVES:
        raise _reject(PRIMARY_NOT_A_LEAF_OBJECTIVE,
                      f"candidate #{index} is a primary that is not a selectable leaf objective")

    confidence = candidate.get("confidence")
    if confidence not in _CONFIDENCE_BANDS:
        raise _reject(INVALID_CONFIDENCE,
                      f"candidate #{index} confidence is outside {sorted(_CONFIDENCE_BANDS)}")

    spans = candidate.get("evidence_spans") or ()
    if not isinstance(spans, (list, tuple)):
        raise _reject(MALFORMED_EVIDENCE_SPANS,
                      f"candidate #{index} evidence_spans is not a list")
    for span in spans:
        if not isinstance(span, str) or not span.strip():
            raise _reject(
                MALFORMED_EVIDENCE_SPANS,
                f"candidate #{index} has an evidence span that is not a non-empty string")

    return relationship


def validate_recognition_output(output: Mapping[str, Any]) -> None:
    """Raise :class:`AttestedSchemaValidationError` (a ``SchemaValidationError``, carrying one of the
    closed :data:`RECOGNITION_FAILURE_CODES`) if the raw recognition body drifts from the closed
    taxonomy or the classification shape. Passes silently (returns ``None``) on a well-formed body.

    This is what the recognizer hands the audited seam as ``validate_semantics``, so the model is
    ASKED to fix each of these inside the §9.2 repair budget before anything is discarded; the
    recognizer re-runs it after the call as the floor.

    Rejects: a missing/unknown ``status``; a candidate ``use_case_id`` outside ``USE_CASE_REGISTRY``;
    a ``primary`` that is not a selectable objective; more than one primary, more than two secondary,
    or more than three candidates total; a ``confidence``/``relationship`` outside its band; a
    ``classified``/``ambiguous`` body with zero candidates; an evidence span that is not a non-empty
    string. A ``unscoped``/``technical_failure`` body with empty candidates is valid."""
    status = output.get("status")
    if not isinstance(status, str) or status not in _STATUS_VALUES:
        raise _reject(INVALID_STATUS,
                      f"status is not one of {sorted(_STATUS_VALUES)}")

    candidates = output.get("candidates")
    if candidates is None:
        candidates = ()
    if not isinstance(candidates, (list, tuple)):
        raise _reject(MALFORMED_CANDIDATE_LIST, "candidates is not a list")

    seen_ids: set[str] = set()
    n_primary = 0
    n_secondary = 0
    for index, candidate in enumerate(candidates):
        relationship = _validate_candidate(candidate, index)
        uid = candidate["use_case_id"]
        if uid in seen_ids:                       # a secondary duplicating the primary, or a repeated id
            raise _reject(DUPLICATE_CANDIDATE, f"candidate #{index} repeats an earlier id")
        seen_ids.add(uid)
        if relationship == "primary":
            n_primary += 1
        else:
            n_secondary += 1

    if n_primary > _MAX_PRIMARY:
        raise _reject(MULTIPLE_PRIMARY_CANDIDATES,
                      f"more than {_MAX_PRIMARY} candidate is marked primary")
    if n_secondary > _MAX_SECONDARY:
        raise _reject(TOO_MANY_SECONDARY_CANDIDATES,
                      f"more than {_MAX_SECONDARY} secondary candidates")
    if len(candidates) > _MAX_CANDIDATES:
        raise _reject(TOO_MANY_CANDIDATES, f"more than {_MAX_CANDIDATES} candidates")

    if status in _CANDIDATE_BEARING and not candidates:
        raise _reject(STATUS_REQUIRES_A_CANDIDATE,
                      f"a {sorted(_CANDIDATE_BEARING)} status carries no candidate")
    # A CLASSIFIED result asserts a single objective — it MUST carry exactly one primary. (AMBIGUOUS may
    # carry only alternatives with no designated primary; scope_from_recognition treats that as unscoped.)
    if status == RecognitionStatus.CLASSIFIED.value and n_primary != _MAX_PRIMARY:
        raise _reject(CLASSIFIED_REQUIRES_ONE_PRIMARY,
                      "a 'classified' status does not carry exactly one primary candidate")


def normalize_dimensions(
    output: Mapping[str, Any],
) -> tuple[tuple[str, ...], str | None, tuple[str, ...]]:
    """Validate the OPTIONAL recognition dimensions PER-DIMENSION and NON-FATALLY, returning the cleaned
    ``(modelling_contexts, target_entity, warnings)``.

    Unlike :func:`validate_recognition_output` — which fails the WHOLE recognition on a malformed core
    (status/candidates) or an invalid primary use-case — a bad dimension NEVER fails the recognition:

    * drops any ``modelling_context`` outside :data:`MODELLING_CONTEXTS` (warns ``UNKNOWN_MODELLING_CONTEXT``),
      keeping every valid one in order (deduplicated);
    * clears a ``target_entity`` outside :func:`known_entities` (warns ``UNKNOWN_TARGET_ENTITY``); an
      absent/``null`` ``target_entity`` is clean — the model simply proposed no grain (no warning).

    A drift so gross the value is not even the declared JSON type (e.g. a non-list ``modelling_contexts``)
    is treated as an unknown value and dropped with a warning — the whole recognition still survives."""
    warnings: list[str] = []

    raw_contexts = output.get("modelling_contexts")
    contexts: list[str] = []
    if isinstance(raw_contexts, (list, tuple)):
        for value in raw_contexts:
            if isinstance(value, str) and value in MODELLING_CONTEXTS:
                if value not in contexts:          # dedupe, preserving first-seen order
                    contexts.append(value)
            elif UNKNOWN_MODELLING_CONTEXT not in warnings:
                warnings.append(UNKNOWN_MODELLING_CONTEXT)
    elif raw_contexts:                             # a present-but-non-list value is itself a drift
        warnings.append(UNKNOWN_MODELLING_CONTEXT)

    raw_entity = output.get("target_entity")
    target_entity: str | None = None
    if isinstance(raw_entity, str) and raw_entity:
        if raw_entity in known_entities():
            target_entity = raw_entity
        else:
            warnings.append(UNKNOWN_TARGET_ENTITY)
    # A None/absent target_entity (no proposed grain) is clean — no warning.

    return tuple(contexts), target_entity, tuple(warnings)


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
