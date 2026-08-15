"""The FROZEN provider contract for use-case recognition — output schema v2.

v1 (still registered, byte-frozen, and the version every legacy row was produced under) declares
``use_case_id`` as a bare string: the 88 selectable objectives are prose in the prompt only, so an
id the taxonomy has never heard of — ``"x"``, the value the 2026-08-15 live incident actually
returned — is *structurally valid* and only fails a post-call semantic pass, outside the seam's
repair loop. v2 closes that hole: the closed vocabulary rides in the schema as an ``enum``, so an
invented id is a SCHEMA failure the audited seam can put to the model as bounded repair.

**Why the body is a committed FILE and not a comprehension over ``selectable_leaves()``.**
``DocumentSchemaRegistry.register_schema`` UPSERTS: registering ``("use_case_recognition", 2)`` a
second time with a different body silently overwrites it. If this enum were derived at import time,
the same version number would mean one thing on a deployment running today's taxonomy and another on
a deployment running next month's — and a stored recognition answer, an audit row, and a release-gate
certification would each be pinned to a version whose meaning had moved underneath them. So the
bytes are generated ONCE, reviewed in the diff, committed, and pinned by sha256 here. **Taxonomy
growth does not edit this file — it requires a v3.**

Regenerating (only ever to author a NEW version)::

    uv run python -m featuregen.overlay.upload.taxonomy.recognition_schema

writes the file from the live registry and prints the digest to paste into ``V2_SCHEMA_SHA256``.
Both halves of that are deliberate work: the file diff shows a reviewer exactly which objectives the
model may return, and the pin cannot be updated by accident.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

V2_SCHEMA_PATH = Path(__file__).with_name("use_case_recognition_v2.schema.json")

# sha256 of the committed bytes of that file, byte for byte. Checked at import (below): a build whose
# recognition contract is not the reviewed one must not dispatch under its version number.
V2_SCHEMA_SHA256 = "1c83e0b57eb5c83c17a7fca0b78f0bc9521bb71c80e973a64b560033bc821040"

# The statuses a MODEL may return. ``technical_failure`` is deliberately ABSENT: it is the
# platform's own outcome for a provider failure, a refusal, an exhausted repair budget or a mapping
# error (``recognition.unscoped_result(technical=True)``) — never a classification the LLM performs.
# v1 offered it on the wire, which invited the model to self-report a platform state. It remains a
# perfectly valid INTERNAL ``RecognitionStatus`` and ``validate_recognition_output`` still accepts it
# on a body the platform itself constructed; only the wire contract loses it.
PROVIDER_STATUS_VALUES: tuple[str, ...] = ("classified", "ambiguous", "unscoped")


def build_use_case_recognition_v2_schema(selectable_leaf_ids: Sequence[str]) -> dict[str, Any]:
    """The v2 body over an explicit leaf list. v1's structure exactly, with two changes: the
    ``use_case_id`` enum, and a ``status`` enum without ``technical_failure``.

    The enum is CANONICAL rather than ``x-wire-enum`` (the wire-only bargain
    ``schema_projection`` offers) on purpose: ``use_case_id`` is a value the platform TRUSTS — it
    scopes the whole recipe catalogue — so an off-taxonomy answer must be unreadable, not merely
    discouraged. Response validation therefore rejects it too, which is exactly what routes it into
    the seam's repair loop instead of into a silent technical failure."""
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {"type": "string", "enum": list(PROVIDER_STATUS_VALUES)},
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "use_case_id": {"type": "string",
                                        "enum": list(selectable_leaf_ids)},
                        "relationship": {"type": "string",
                                         "enum": ["primary", "secondary"]},
                        "confidence": {"type": "string",
                                       "enum": ["high", "medium", "low"]},
                        "evidence_spans": {"type": "array", "items": {"type": "string"}},
                        "rationale": {"type": "string"},
                    },
                    "required": ["use_case_id", "relationship", "confidence", "evidence_spans",
                                 "rationale"],
                },
            },
            "modelling_contexts": {"type": "array", "items": {"type": "string"}},
            "target_entity": {"type": ["string", "null"]},
            "ambiguity_note": {"type": ["string", "null"]},
        },
        "required": ["status", "candidates"],
    }


def render_use_case_recognition_v2_schema(selectable_leaf_ids: Sequence[str]) -> str:
    """The exact file text for a leaf list — deterministic (sorted keys, fixed indent, trailing
    newline) so the committed bytes are reproducible and the drift test can compare them directly."""
    body = build_use_case_recognition_v2_schema(selectable_leaf_ids)
    return json.dumps(body, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def frozen_leaf_ids() -> tuple[str, ...]:
    """The use-case ids the FROZEN v2 contract admits — read from the committed bytes, never from
    the live registry. A caller comparing this against ``selectable_leaves()`` is asking the drift
    question ("has the taxonomy moved since v2 was authored?"), which is the whole point."""
    enum = (USE_CASE_RECOGNITION_V2_SCHEMA["properties"]["candidates"]["items"]
            ["properties"]["use_case_id"]["enum"])
    return tuple(enum)


def _load_frozen_v2() -> dict[str, Any]:
    raw = V2_SCHEMA_PATH.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != V2_SCHEMA_SHA256:
        raise ValueError(
            f"{V2_SCHEMA_PATH.name} does not match its pinned digest (found {digest}, expected "
            f"{V2_SCHEMA_SHA256}). The recognition output contract is FROZEN per version: a "
            "taxonomy change requires a NEW schema version, not an edit to this one. If you are "
            "deliberately authoring a new version, regenerate with `python -m "
            "featuregen.overlay.upload.taxonomy.recognition_schema` and update the pin in the same "
            "reviewed commit.")
    schema: dict[str, Any] = json.loads(raw)
    return schema


USE_CASE_RECOGNITION_V2_SCHEMA: dict[str, Any] = _load_frozen_v2()


def main() -> None:  # pragma: no cover - a one-off authoring tool, not a runtime path
    """Regenerate the frozen bytes from the live taxonomy and print the digest to pin."""
    from featuregen.overlay.upload.taxonomy.use_cases import selectable_leaves

    text = render_use_case_recognition_v2_schema(sorted(selectable_leaves()))
    V2_SCHEMA_PATH.write_text(text, encoding="utf-8")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    print(f"wrote {V2_SCHEMA_PATH} ({len(text)} bytes)")
    print(f"pin V2_SCHEMA_SHA256 = {digest!r}")


if __name__ == "__main__":  # pragma: no cover
    main()
