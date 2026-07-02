"""SP-2 no-PII boundary (spec §9.4): the IntentRedactor seam + default impl + the reserved
LLM-safe `inputs` vocabulary + the fail-closed egress guard.

Ownership split (spec §9.4): SP-0 CLASSIFIES the raw intent (raw_input_classification);
SP-2 REDACTS here (fails closed on un-redactable / `unscanned`); SP-2 GUARDS EGRESS
(`assert_llm_safe`, Task 3.2). The redactor produces the ONLY LLM-safe rendering of the intent
ever placed in LLMRequest.inputs. `input_redaction` records span TYPES/POSITIONS, never values.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# Reserved keys that structure LLMRequest.inputs. The model-facing content is INTENT + CATALOG;
# the rest is provenance the egress guard + call_llm read. Provenance keys carry no data values.
INPUT_KEY_INTENT = "redacted_intent"
INPUT_KEY_CATALOG = "catalog_metadata"
INPUT_KEY_CLASSIFICATION = "raw_input_classification"
INPUT_KEY_REDACTION_VERSION = "redaction_version"
INPUT_KEY_REDACTION = "input_redaction"

REDACTION_VERSION = "default-redactor@1"

# Deterministic PII detectors shared by the redactor AND the egress backstop (Task 3.2) — one source
# of truth so widening the set hardens BOTH the redactor and `assert_llm_safe`'s egress guard. The
# set is conservative-and-testable, not exhaustive: every pattern is bounded so legitimate feature
# values (window literals "90d", counts, thresholds, a bare "90") never false-match — PHONE/DOB
# require real separator/date shapes, ACCOUNT requires a 9+ digit run, ADDRESS requires a
# number+street-suffix shape. Placeholders are digit/at-free so a residual scan of a redacted string
# never re-matches. N2 (SP-2): phone / IBAN / generic bank-account / DOB / postal-address added.
#
# DEFERRED (not built here): NER-grade personal-NAME detection ("Jane Doe"). Regex cannot do it
# safely; the drop-in seam is the `IntentRedactor` Protocol below — register a NER-backed redactor
# via register_intent_redactor(...) to layer it in without touching this deterministic set.
_PII_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("PAN", re.compile(r"\b\d{4}[ \-]?\d{4}[ \-]?\d{4}[ \-]?\d{1,4}\b")),
    # IBAN: 2-letter country + 2 check digits + 11-30 alnum (BBAN). Distinctive; ordered before
    # ACCOUNT/PHONE so its embedded digit run is labelled IBAN, not a bare account.
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")),
    # PHONE: NANP-style NNN·NNN·NNNN with REAL separators (never bare digits → no "90-day" match),
    # or a `+`-prefixed international run (the leading + keeps it off unprefixed feature numbers).
    ("PHONE", re.compile(
        r"(?<![\w.])(?:\+\d{1,3}[\s.\-]?)?\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}(?![\d.])"
        r"|(?<![\w.])\+\d{1,3}(?:[\s.\-]\d{2,5}){2,5}(?![\d.])"
    )),
    # ACCOUNT: a bare 9-17 digit run (routing/account numbers). Min 9 clears windows/counts/
    # thresholds and 8-digit compact dates; lookarounds bar decimal- and digit-adjacent partials.
    ("ACCOUNT", re.compile(r"(?<![\w.])\d{9,17}(?![\d.])")),
    # DOB: a D/M/Y (or ISO Y/M/D) date with two real separators — a bare "90" or "1.5" cannot form it.
    ("DOB", re.compile(
        r"(?<![\w.])(?:\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}|\d{4}[/.\-]\d{1,2}[/.\-]\d{1,2})(?![\d.])"
    )),
    # ADDRESS: house-number + optional capitalised name(s) + a street-type suffix, or a P.O. Box.
    # The leading number + capitalised suffix keeps it off prose like "30 Day window".
    ("ADDRESS", re.compile(
        r"\b\d{1,6}\s+(?:[A-Z][A-Za-z0-9.'\-]*\s+){0,4}"
        r"(?:Street|St|Avenue|Ave|Boulevard|Blvd|Road|Rd|Lane|Ln|Drive|Dr|Court|Ct|Terrace|Ter|Place|Pl)\b\.?"
        r"|P\.?\s?O\.?\s?Box\s+\d{1,7}\b"
    )),
)


class EgressViolation(Exception):
    """A payload that must never reach the LLM (unscanned, data values, or un-redacted PII), or
    a redactor that failed closed. A HARD failure — call_llm routes it to the security-audit
    stream (§9.4); it is never a warning."""


@dataclass(frozen=True)
class RedactionResult:
    text: str | None            # the ONLY LLM-safe rendering placed in inputs; None ⟹ fail closed
    redaction_version: str      # stamped onto the llm_call record
    redacted_spans: tuple       # ({"type","start","end"}, ...) — types/positions, NEVER values
    disposition: str            # "ok" | "fail_into_clarification"


@runtime_checkable
class IntentRedactor(Protocol):
    def redact(self, raw_intent: str, raw_input_classification: str) -> RedactionResult: ...


def _scan(text: str) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for label, pat in _PII_PATTERNS:
        for m in pat.finditer(text):
            spans.append({"type": label, "start": m.start(), "end": m.end()})
    return spans


class DefaultIntentRedactor:
    """Default IntentRedactor. `clean` passes through; `contains_pii` scrubs the located spans and
    fails closed if it cannot locate any (cannot prove safety); `unscanned` fails closed outright.
    Never emits text for an un-redactable or unscanned intent (§9.4)."""

    def redact(self, raw_intent: str, raw_input_classification: str) -> RedactionResult:
        if raw_input_classification == "unscanned":
            return RedactionResult(None, REDACTION_VERSION, (), "fail_into_clarification")
        if raw_input_classification == "clean":
            return RedactionResult(raw_intent, REDACTION_VERSION, (), "ok")
        if raw_input_classification == "contains_pii":
            spans = _scan(raw_intent)
            if not spans:
                # classified PII but nothing locatable to scrub → cannot prove safe → fail closed
                return RedactionResult(None, REDACTION_VERSION, (), "fail_into_clarification")
            redacted = raw_intent
            for label, pat in _PII_PATTERNS:
                redacted = pat.sub(f"[REDACTED:{label}]", redacted)
            if _scan(redacted):  # defense in depth: residual PII ⟹ fail closed
                return RedactionResult(None, REDACTION_VERSION, (), "fail_into_clarification")
            return RedactionResult(redacted, REDACTION_VERSION, tuple(spans), "ok")
        # unknown classification: fail closed (never guess)
        return RedactionResult(None, REDACTION_VERSION, (), "fail_into_clarification")


def build_llm_inputs(
    redaction: RedactionResult,
    *,
    catalog_metadata: Mapping[str, Any],
    raw_input_classification: str,
) -> dict:
    """Assemble the reserved-keyed LLMRequest.inputs from a RedactionResult + catalog METADATA.
    Refuses (EgressViolation) when the redactor failed closed — no unsafe payload is ever built."""
    if redaction.text is None:
        raise EgressViolation(
            "redactor failed closed; no LLM-safe text to dispatch (fail into clarification)"
        )
    return {
        INPUT_KEY_INTENT: redaction.text,
        INPUT_KEY_CATALOG: dict(catalog_metadata),
        INPUT_KEY_CLASSIFICATION: raw_input_classification,
        INPUT_KEY_REDACTION_VERSION: redaction.redaction_version,
        INPUT_KEY_REDACTION: {"redacted_spans": [dict(s) for s in redaction.redacted_spans]},
    }


# Keys that carry DATA VALUES (rows / samples / profiled value-sets / extrema) rather than
# METADATA. Actual value/status-code sets are SP-1 profiling + SP-3 grounding (§4.4) — they must
# NEVER reach the LLM. Their presence in an outbound payload is a hard egress violation.
_FORBIDDEN_INPUT_KEYS = (
    "raw_input", "data_values", "column_values", "value_set",
    "rows", "samples", "profile", "extrema", "min", "max",
)


def _iter_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for v in value.values():
            yield from _iter_strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _iter_strings(v)


def _first_pii(*values: Any) -> str | None:
    for value in values:
        for s in _iter_strings(value):
            for label, pat in _PII_PATTERNS:
                if pat.search(s):
                    return label
    return None


def assert_llm_safe(request) -> None:
    """Egress hard-backstop (§9.4). Deterministic pre-send check on an LLMRequest: refuses
    `unscanned`/unclassified content, data-value keys, a `contains_pii` payload that never went
    through redaction, or any un-redacted PII in the model-facing content. Raises EgressViolation
    (a HARD failure) — the conn-holding caller (call_llm) records it in the security-audit stream.
    Never mutates; never a warning."""
    inputs = request.inputs
    cls = inputs.get(INPUT_KEY_CLASSIFICATION)
    if cls == "unscanned":
        raise EgressViolation("refusing to dispatch an `unscanned` intent to the LLM")
    if cls not in ("clean", "contains_pii"):
        raise EgressViolation(f"missing/invalid {INPUT_KEY_CLASSIFICATION}: {cls!r}")
    present = [k for k in _FORBIDDEN_INPUT_KEYS if k in inputs]
    if present:
        raise EgressViolation(f"payload carries data-value keys, not metadata: {present}")
    if cls == "contains_pii" and not inputs.get(INPUT_KEY_REDACTION_VERSION):
        raise EgressViolation("`contains_pii` payload lacks a redaction_version (never redacted)")
    hit = _first_pii(inputs.get(INPUT_KEY_INTENT), inputs.get(INPUT_KEY_CATALOG))
    if hit:
        raise EgressViolation(f"un-redacted {hit} detected in outbound payload")


# ---- R10 collaborator DI seam (module-global; mirrors overlay/catalog.py) --------------------
# The ONE holder for the active IntentRedactor. P4 (submit_intent) resolves the redactor via
# current_intent_redactor(); P9 registers it via register_intent_redactor(...). Fail-closed if
# unset — the platform never silently redacts with a default the caller did not choose (§9.4).
_INTENT_REDACTOR: IntentRedactor | None = None


def register_intent_redactor(redactor: IntentRedactor) -> None:
    """Register the process-wide IntentRedactor (last writer wins). P9 wires DefaultIntentRedactor."""
    global _INTENT_REDACTOR
    _INTENT_REDACTOR = redactor


def current_intent_redactor() -> IntentRedactor:
    """Return the registered IntentRedactor; fail closed (RuntimeError) if none is registered."""
    if _INTENT_REDACTOR is None:
        raise RuntimeError(
            "no IntentRedactor registered; call register_intent_redactor(...) "
            "(register_sp2()/_wire does this)"
        )
    return _INTENT_REDACTOR
