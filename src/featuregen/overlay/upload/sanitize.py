"""Fail-closed free-text sanitizer for uploaded glossary prose (FTR adapter Task 2).

An FTR glossary definition EMBEDS raw customer sample values in prose (account numbers, times,
decimals, short codes). Nothing raw may persist or egress, so the adapter routes every definition
through :func:`sanitize_definition` at parse time:

1. :func:`~featuregen.overlay.upload.sample_parser.parse_sample_profile` runs FIRST (on the raw
   text) to capture the SAFE derived facets — ``logical_representation`` / ``semantic_type`` —
   which later become parser evidence. Facets are types, never values.
2. :func:`~featuregen.overlay.upload.sample_parser.strip_sample_values` excises the recognized
   ``representative values such as ...`` clause — the REAL file's run showed 100% of actual
   sample values live in this one canonical shape (round-5 resolution R5-2).
3. FAIL-CLOSED DATA-MARKER SCAN on the residual: a phrase that implies ACTUAL DATA
   (``representative values``, ``sample values``, ``observed values/entries``, ``example
   values``) surviving the strip means a sample clause the stripper could not consume →
   ``state="suspected_unhandled"``, ``reason="unhandled_marker"``, ``clean=""``. The row still
   ingests; identity is intact. Individual values are NEVER deleted by shape (that would corrupt
   definitions); suspicion always blanks the whole field. Bare ``sample profile`` is NOT a
   marker — 41 real definitions say "sample profile has no non-blank values" and are SAFE.

   The v2 VALUE-SHAPE guesser (token counting + list separators) is GONE (R5-2): it over-blanked
   those 41 rows plus a legitimate payment definition with numbers, while still missing bare
   code-lists. Accepted, documented tradeoff: a bare non-canonical value list with no marker is
   not auto-caught (the real file never does this; distinguishing it from prose is intractable).
4. What survives is PII-redacted via :func:`~featuregen.intake.redaction.redact_free_text`;
   a redactor that fails closed (``.text is None``) blanks the field too
   (``reason="pii_redaction_failed"``).

:func:`redact_text` is the lighter companion for NON-definition free-text (term names, synonyms,
taxonomy paths): PII redaction only — those fields never carry a sample clause by contract.

Pure module: no DB, no LLM.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from featuregen.intake.redaction import redact_free_text
from featuregen.overlay.upload.sample_parser import parse_sample_profile, strip_sample_values

SANITIZER_VERSION = "ftr-sanitize-v3"

# DATA-implying marker phrases (R5-2): if one survives the strip, a sample clause the stripper
# could not consume is still in the residual — fail closed. Precise PHRASES only, so "sample
# population size", "a representative office", and the SAFE "sample profile has no non-blank
# values" (41 real rows) never trigger. Bare ``sample profile`` is deliberately NOT a marker.
_UNHANDLED_MARKER_RE = re.compile(
    r"\brepresentative\s+values?\b"
    r"|\bsample\s+values?\b"
    r"|\bobserved\s+(?:values?|entries)\b"
    r"|\bexample\s+values?\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class DefinitionSanitize:
    """The sanitized reading of one uploaded definition.

    ``clean`` — safe to persist/egress; ``""`` when the field was blanked (fail closed).
    ``state`` — ``"none"`` (plain prose) | ``"stripped"`` (a recognized clause was excised) |
    ``"suspected_unhandled"`` (a data marker survived the strip → blanked).
    ``logical_representation`` / ``semantic_type`` — SAFE facets from ``parse_sample_profile``
    (``""`` when unknown); captured BEFORE stripping so they survive the excision.
    ``removed`` — sample clauses stripped/blanked + PII spans redacted.
    ``reason`` — why a field was blanked (``"unhandled_marker"`` | ``"pii_redaction_failed"``);
    ``""`` otherwise.
    """

    clean: str
    state: str
    logical_representation: str
    semantic_type: str
    removed: int
    sanitizer_version: str
    redaction_version: str | None
    reason: str = ""


def sanitize_definition(text: str | None) -> DefinitionSanitize:
    """Sanitize one definition per the module contract (parse → strip → marker scan → redact)."""
    if not text:
        return DefinitionSanitize("", "none", "", "", 0, SANITIZER_VERSION, None)
    profile = parse_sample_profile(text)  # BEFORE stripping — the facets must survive the excision
    logical = profile.logical_representation or ""
    semantic = profile.semantic_type or ""
    stripped = strip_sample_values(text)
    clause_stripped = stripped != text
    if _UNHANDLED_MARKER_RE.search(stripped):
        # A data-implying marker survived the strip: a sample clause the stripper could not
        # consume — blank the whole field, never individual values. The blanked residual counts.
        removed = 1 + (1 if clause_stripped else 0)
        return DefinitionSanitize(
            "", "suspected_unhandled", logical, semantic, removed, SANITIZER_VERSION, None,
            reason="unhandled_marker",
        )
    state = "stripped" if clause_stripped else "none"
    result = redact_free_text(stripped)
    if result.text is None:
        # Redactor failed closed — nothing provably safe to keep; the blanked field counts.
        removed = 1 + (1 if clause_stripped else 0)
        return DefinitionSanitize(
            "", state, logical, semantic, removed, SANITIZER_VERSION, result.redaction_version,
            reason="pii_redaction_failed",
        )
    removed = len(result.redacted_spans) + (1 if clause_stripped else 0)
    return DefinitionSanitize(
        result.text, state, logical, semantic, removed, SANITIZER_VERSION, result.redaction_version
    )


def redact_text(text: str | None) -> tuple[str, str | None]:
    """PII-redact a NON-definition free-text field (term name, synonym, taxonomy path).

    Returns ``(clean, redaction_version)``; ``("", version)`` when the redactor fails closed —
    the caller must not persist or egress the original value.
    """
    if not text:
        return "", None
    result = redact_free_text(text)
    if result.text is None:
        return "", result.redaction_version
    return result.text, result.redaction_version
