from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

PII_ERASABLE = "pii-erasable"
GOVERNANCE_RETAINED = "governance-retained"
BODY_CLASSIFICATIONS: tuple[str, ...] = (PII_ERASABLE, GOVERNANCE_RETAINED)

_REF_RE = re.compile(r"^(blob|doc)_[A-Za-z0-9]+$")

# Cheap, deterministic high-confidence detectors for raw PII / secrets that must never be inlined
# in an event payload (§9: "No raw PII/secrets in events ... references only"). Kept narrow on
# purpose: each pattern is specific enough to avoid false positives on hashes/ULIDs/refs/metadata,
# so the guard can run on EVERY append without breaking legitimate reference-only payloads.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("US SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("PEM private key", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
)
# A 13-19 digit run (optionally space/dash grouped) that passes the Luhn checksum is treated as a
# payment card number (PAN). The Luhn gate keeps arbitrary long integers from tripping the guard.
_CARD_CANDIDATE_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")


class InlinePIIError(Exception):
    """Raised when a sensitive field carries an inline body instead of a reference (§9)."""


def validate_classification(classification: str) -> None:
    if classification not in BODY_CLASSIFICATIONS:
        raise ValueError(f"body classification {classification!r} not in {BODY_CLASSIFICATIONS}")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _iter_strings(value: object) -> Iterable[str]:
    """Yield every string leaf reachable in a JSON-shaped payload (dict/list/scalar)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for v in value.values():
            yield from _iter_strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _iter_strings(v)


def _detect_inline_secret(text: str) -> str | None:
    for label, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            return label
    for match in _CARD_CANDIDATE_RE.finditer(text):
        digits = re.sub(r"[ -]", "", match.group())
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return "payment card number"
    return None


def assert_no_inline_pii(payload: Mapping[str, object]) -> None:
    """Reject a payload carrying raw PII/secrets inline (§9). Scans every string leaf with cheap,
    deterministic high-confidence detectors (SSN, PEM private key, AWS key id, Luhn-valid PAN);
    sensitive bodies must be referenced (blob_/doc_), never inlined. Enforced for ALL callers."""
    for text in _iter_strings(payload):
        label = _detect_inline_secret(text)
        if label is not None:
            raise InlinePIIError(
                f"event payload contains inline {label}; sensitive content must be stored in an "
                f"encrypted blob and referenced (blob_/doc_), never inlined (§9)"
            )


def assert_references_only(
    payload: Mapping[str, object], *, sensitive_fields: tuple[str, ...]
) -> None:
    for name in sensitive_fields:
        if name not in payload:
            continue
        value = payload[name]
        if not isinstance(value, str) or not _REF_RE.match(value):
            raise InlinePIIError(
                f"sensitive field {name!r} must be a 'blob_'/'doc_' reference, not inline content (§9)"
            )
