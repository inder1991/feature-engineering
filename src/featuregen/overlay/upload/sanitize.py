"""Fail-closed free-text sanitizer for uploaded glossary prose (FTR adapter Task 2).

An FTR glossary definition EMBEDS raw customer sample values in prose (account numbers, times,
decimals, short codes). Nothing raw may persist or egress, so the adapter routes every definition
through :func:`sanitize_definition` at parse time:

1. :func:`~featuregen.overlay.upload.sample_parser.parse_sample_profile` runs FIRST (on the raw
   text) to capture the SAFE derived facets — ``logical_representation`` / ``semantic_type`` —
   which later become parser evidence. Facets are types, never values.
2. :func:`~featuregen.overlay.upload.sample_parser.strip_sample_values` excises the recognized
   ``representative values such as ...`` clause.
3. The post-strip text is scanned for a sample-clause INTRODUCER phrase (round-4 resolution #2:
   precise PHRASES — ``e.g.``, ``such as``, ``for example``, ``examples include``,
   ``representative values``, ``sample values/profile`` — never bare words, so "sample population
   size" and "a representative office" stay clean). A surviving introducer means a clause the
   stripper could not handle (an unrecognized shape, or a SECOND clause past the first excision) —
   the field is blanked (``suspected_unhandled``). The row still ingests; identity is intact.
4. What survives is PII-redacted via :func:`~featuregen.intake.redaction.redact_free_text`;
   a redactor that fails closed (``.text is None``) blanks the field too.

Scanning the POST-strip text (not the raw text) is deliberately stricter than "introducer present
and nothing stripped": a recognized first clause must not vouch for an unrecognized second one.

:func:`redact_text` is the lighter companion for NON-definition free-text (term names, synonyms,
taxonomy paths): PII redaction only — those fields never carry a sample clause by contract.

Pure module: no DB, no LLM.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from featuregen.intake.redaction import redact_free_text
from featuregen.overlay.upload.sample_parser import parse_sample_profile, strip_sample_values

SANITIZER_VERSION = "ftr-sanitize-v1"

# Sample-clause INTRODUCER phrases (resolution #2): each precedes a list of example values. Precise
# PHRASES only — a bare `sample` or `representative` must never trigger (negative controls:
# "sample population size", "a representative office in the region").
_INTRODUCER_RE = re.compile(
    r"\be\.g\."
    r"|\bsuch\s+as\b"
    r"|\bfor\s+example\b"
    r"|\bexamples?\s+include\b"
    r"|\brepresentative\s+values?\b"
    r"|\bsample\s+(?:values?|profile)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class DefinitionSanitize:
    """The sanitized reading of one uploaded definition.

    ``clean`` — safe to persist/egress; ``""`` when the field was blanked (fail closed).
    ``state`` — ``"none"`` (plain prose) | ``"stripped"`` (a recognized clause was excised) |
    ``"suspected_unhandled"`` (an introducer survived → blanked).
    ``logical_representation`` / ``semantic_type`` — SAFE facets from ``parse_sample_profile``
    (``""`` when unknown); captured BEFORE stripping so they survive the excision.
    ``removed`` — sample clauses stripped/blanked + PII spans redacted.
    """

    clean: str
    state: str
    logical_representation: str
    semantic_type: str
    removed: int
    sanitizer_version: str
    redaction_version: str | None


def sanitize_definition(text: str | None) -> DefinitionSanitize:
    """Sanitize one definition per the module contract (parse → strip → introducer scan → redact)."""
    if not text:
        return DefinitionSanitize("", "none", "", "", 0, SANITIZER_VERSION, None)
    profile = parse_sample_profile(text)  # BEFORE stripping — the facets must survive the excision
    logical = profile.logical_representation or ""
    semantic = profile.semantic_type or ""
    stripped = strip_sample_values(text)
    clause_stripped = stripped != text
    if _INTRODUCER_RE.search(stripped):
        # An introducer survived the strip: an unhandled clause (or a second one past the first
        # excision). Raw values may follow it — blank the field. The unhandled clause counts.
        removed = 1 + (1 if clause_stripped else 0)
        return DefinitionSanitize(
            "", "suspected_unhandled", logical, semantic, removed, SANITIZER_VERSION, None
        )
    state = "stripped" if clause_stripped else "none"
    result = redact_free_text(stripped)
    if result.text is None:
        # Redactor failed closed — nothing provably safe to keep; the blanked field counts.
        removed = 1 + (1 if clause_stripped else 0)
        return DefinitionSanitize(
            "", state, logical, semantic, removed, SANITIZER_VERSION, result.redaction_version
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
