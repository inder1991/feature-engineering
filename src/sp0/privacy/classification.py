from __future__ import annotations

import re
from typing import Mapping

PII_ERASABLE = "pii-erasable"
GOVERNANCE_RETAINED = "governance-retained"
BODY_CLASSIFICATIONS: tuple[str, ...] = (PII_ERASABLE, GOVERNANCE_RETAINED)

_REF_RE = re.compile(r"^(blob|doc)_[A-Za-z0-9]+$")


class InlinePIIError(Exception):
    """Raised when a sensitive field carries an inline body instead of a reference (§9)."""


def validate_classification(classification: str) -> None:
    if classification not in BODY_CLASSIFICATIONS:
        raise ValueError(f"body classification {classification!r} not in {BODY_CLASSIFICATIONS}")


def assert_references_only(payload: Mapping[str, object], *, sensitive_fields: tuple[str, ...]) -> None:
    for name in sensitive_fields:
        if name not in payload:
            continue
        value = payload[name]
        if not isinstance(value, str) or not _REF_RE.match(value):
            raise InlinePIIError(
                f"sensitive field {name!r} must be a 'blob_'/'doc_' reference, not inline content (§9)"
            )
