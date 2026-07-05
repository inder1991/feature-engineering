"""Phase 1 — intent intake for the hypothesis-driven feature contract.

Every feature request carries a **hypothesis** (the "why", MANDATORY) and, optionally, a **definition**
(the "what" — the anchor). A blank hypothesis is a *command-validation denial* (no run created; the
requester revises and resubmits) — not a terminal reject. `intake_mode` is fixed here and never mutates.
Both free-text fields are **redacted before anything downstream can reach the LLM** — reusing the SP-2
redactor, classified against its own scanner so classification and redaction stay consistent.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from featuregen.intake.redaction import DefaultIntentRedactor, IntentRedactor, _scan


class IntentValidationError(Exception):
    """Command-validation denial — no run is created; the requester revises and resubmits."""


@dataclass(frozen=True, slots=True)
class Intent:
    intent_id: str
    hypothesis: str
    definition: str
    intake_mode: str              # "definition" | "hypothesis" — fixed at submit, immutable
    redacted_hypothesis: str
    redacted_definition: str
    actor: object


def _classify(text: str) -> str:
    # Classify against the redactor's OWN scanner so classification and redaction never disagree.
    return "contains_pii" if _scan(text) else "clean"


def _redact_or_deny(redactor: IntentRedactor, label: str, text: str) -> str:
    result = redactor.redact(text, _classify(text))
    if result.disposition != "ok" or result.text is None:
        raise IntentValidationError(
            f"{label} contains content that cannot be safely redacted — revise and resubmit")
    return result.text


def submit_intent(*, hypothesis: str, definition: str = "", actor,
                  redactor: IntentRedactor | None = None) -> Intent:
    """Intake a feature request. Denies (no run) when the hypothesis is blank. Fixes `intake_mode`
    immutably. Redacts both free-text fields before they can flow to any downstream LLM node."""
    if not (hypothesis or "").strip():
        raise IntentValidationError("hypothesis is mandatory")
    redactor = redactor or DefaultIntentRedactor()
    mode = "definition" if (definition or "").strip() else "hypothesis"
    redacted_h = _redact_or_deny(redactor, "hypothesis", hypothesis)
    redacted_d = _redact_or_deny(redactor, "definition", definition) if mode == "definition" else ""
    return Intent(
        intent_id=uuid.uuid4().hex, hypothesis=hypothesis, definition=definition,
        intake_mode=mode, redacted_hypothesis=redacted_h, redacted_definition=redacted_d, actor=actor)
