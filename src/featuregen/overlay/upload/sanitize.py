"""Fail-closed free-text sanitizer for uploaded glossary prose (FTR adapter Task 2).

An FTR glossary definition EMBEDS raw customer sample values in prose (account numbers, times,
decimals, short codes). Nothing raw may persist or egress, so the adapter routes every definition
through :func:`sanitize_definition` at parse time:

1. :func:`~featuregen.overlay.upload.sample_parser.parse_sample_profile` runs FIRST (on the raw
   text) to capture the SAFE derived facets — ``logical_representation`` / ``semantic_type`` —
   which later become parser evidence. Facets are types, never values.
2. :func:`~featuregen.overlay.upload.sample_parser.strip_sample_values` excises the recognized
   ``representative values such as ...`` clause.
3. The post-strip RESIDUAL is judged by the VALUE-SHAPE RESIDUAL-SUSPICION GATE (round-4
   resolution #2 — replaces the v1 introducer-phrase whitelist, which both LEAKED lists behind
   non-whitelisted introducers and OVER-BLANKED legitimate concept prose):

   * ``unhandled_marker`` — a known sample-data marker phrase survived the strip
     (``representative values``, ``sample values/profile``, ``observed values/entries``,
     ``example values``): a clause the stripper could not consume → blank the WHOLE definition.
   * ``suspected_value_list`` — else, the residual carries >= 2 VALUE-SHAPED tokens (numeric run,
     time-of-day, short code, DOUBLE-quoted literal, all-caps entity run) TOGETHER WITH a list
     separator (``;`` or ``,`` ONLY — resolution #2 says "semicolons or commas") or a
     sample-context word (``values``/``entries``/``codes``/``observed``/``include``) → blank the
     WHOLE definition.

   Either way ``state="suspected_unhandled"`` and ``clean=""`` — the row still ingests; identity
   is intact. Individual values are NEVER deleted by shape (that would corrupt definitions);
   suspicion always blanks the whole field. Concept prose — a bare introducer (``such as tenor
   and rate``), a single acronym (``GDP``), a lowercase taxonomy list — passes through.
4. What survives is PII-redacted via :func:`~featuregen.intake.redaction.redact_free_text`;
   a redactor that fails closed (``.text is None``) blanks the field too.

:func:`redact_text` is the lighter companion for NON-definition free-text (term names, synonyms,
taxonomy paths): PII redaction only — those fields never carry a sample clause by contract.

Pure module: no DB, no LLM.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from featuregen.intake.redaction import redact_free_text
from featuregen.overlay.upload.sample_parser import parse_sample_profile, strip_sample_values

SANITIZER_VERSION = "ftr-sanitize-v2"

# Known SAMPLE-DATA marker phrases (resolution #2): if one survives the strip, a sample clause the
# stripper could not consume is still in the residual — fail closed. Precise PHRASES only, so
# "sample population size" and "a representative office" never trigger.
_UNHANDLED_MARKER_RE = re.compile(
    r"\brepresentative\s+values?\b"
    r"|\bsample\s+(?:values?|profile)\b"
    r"|\bobserved\s+(?:values?|entries)\b"
    r"|\bexample\s+values?\b",
    re.IGNORECASE,
)

# VALUE-SHAPED token classes. Each pattern matches one token; the classes are summed. A quoted
# number counts twice — over-counting only errs fail-closed. The entity-run pattern needs TWO OR
# MORE consecutive all-caps words (ARTKOM GLOBAL FZE), so a single acronym (GDP) never counts.
_VALUE_TOKEN_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b\d+\.\d+\b|\b\d{3,}\b"),  # numeric: decimal, or a 3+ digit run
    re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b"),  # time-of-day: 15:07[:08]
    re.compile(r"\b[A-Z]{2,}-?\d+[A-Z0-9]*\b"),  # short code: LON01 / AB-1 / EI0300357
    # Quoted literal: DOUBLE quotes only, bounded length ("OPN"). Single quotes are NOT value
    # evidence — possessive prose ("the client's ledger, the bank's records") would otherwise
    # match `'s ledger, the bank'` as a quoted span and over-blank legitimate definitions.
    re.compile(r"\"[^\"]{1,40}\""),
    re.compile(r"\b[A-Z][A-Z]+(?:\s+[A-Z][A-Z&]+)+\b"),  # all-caps entity run: NORDIC HOLDINGS AS
)

# List evidence: a separator or a sample-context word. Separators are ';' and ',' ONLY (resolution
# #2: "semicolons or commas"). Conjunctive ``and`` is NOT a separator — treating it as one blanked
# legitimate quantitative prose ("between 100 and 500 basis points", "periods 2019 and 2020").
# The accepted trade-off: a bare two-item ``X and Y`` value pair with no ';'/',', no marker, and no
# context word passes through (rare/ambiguous; every demonstrated real leak carries one of those).
_LIST_SEPARATOR_RE = re.compile(r"[;,]")
_SAMPLE_CONTEXT_RE = re.compile(r"\b(?:values|entries|codes|observed|include)\b", re.IGNORECASE)

_VALUE_TOKEN_THRESHOLD = 2


def _residual_suspicion(residual: str) -> str:
    """Judge a post-strip residual: ``"unhandled_marker"`` | ``"suspected_value_list"`` | ``""``."""
    if _UNHANDLED_MARKER_RE.search(residual):
        return "unhandled_marker"
    tokens = sum(sum(1 for _ in pattern.finditer(residual)) for pattern in _VALUE_TOKEN_RES)
    if tokens >= _VALUE_TOKEN_THRESHOLD and (
        _LIST_SEPARATOR_RE.search(residual) or _SAMPLE_CONTEXT_RE.search(residual)
    ):
        return "suspected_value_list"
    return ""


@dataclass(frozen=True, slots=True)
class DefinitionSanitize:
    """The sanitized reading of one uploaded definition.

    ``clean`` — safe to persist/egress; ``""`` when the field was blanked (fail closed).
    ``state`` — ``"none"`` (plain prose) | ``"stripped"`` (a recognized clause was excised) |
    ``"suspected_unhandled"`` (a suspicious residual → blanked).
    ``logical_representation`` / ``semantic_type`` — SAFE facets from ``parse_sample_profile``
    (``""`` when unknown); captured BEFORE stripping so they survive the excision.
    ``removed`` — sample clauses stripped/blanked + PII spans redacted.
    ``reason`` — why a ``suspected_unhandled`` field was blanked (``"unhandled_marker"`` |
    ``"suspected_value_list"``); ``""`` otherwise.
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
    """Sanitize one definition per the module contract (parse → strip → residual gate → redact)."""
    if not text:
        return DefinitionSanitize("", "none", "", "", 0, SANITIZER_VERSION, None)
    profile = parse_sample_profile(text)  # BEFORE stripping — the facets must survive the excision
    logical = profile.logical_representation or ""
    semantic = profile.semantic_type or ""
    stripped = strip_sample_values(text)
    clause_stripped = stripped != text
    reason = _residual_suspicion(stripped)
    if reason:
        # A suspicious residual (marker or value-shaped list): raw values may be present — blank
        # the whole field, never individual values. The blanked residual counts as removed.
        removed = 1 + (1 if clause_stripped else 0)
        return DefinitionSanitize(
            "", "suspected_unhandled", logical, semantic, removed, SANITIZER_VERSION, None,
            reason=reason,
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
