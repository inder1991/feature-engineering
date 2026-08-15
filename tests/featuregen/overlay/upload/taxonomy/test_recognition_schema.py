"""Task 1 of the 2026-08-15 recognition repair seam — the FROZEN recognition output contracts.

The live incident's second candidate was ``{"use_case_id": "x", ..., "rationale": "placeholder"}``.
Under v1 that body is *structurally valid* — ``use_case_id`` is a bare string — so it passed the
seam's schema gate, was never put to the model as repair, and only failed the post-call semantic
pass, which is all-or-nothing and threw away the CORRECT sibling with it.

These tests hold two lines:

* **v1 never changes.** Legacy ``llm_call``/recognition rows were produced under it; editing its body
  would relabel history under a version number that already means something else.
* **v2 is frozen, reviewed, committed bytes.** ``DocumentSchemaRegistry.register_schema`` UPSERTS, so
  a body derived at import time would let one version number mean two different contracts on two
  deployments. The enum is pinned by digest, and taxonomy growth requires a v3 — not an edit.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from featuregen.contracts import SchemaValidationError
from featuregen.documents.registry import DocumentSchemaRegistry
from featuregen.overlay.upload.enrich_llm import (
    canonical_output_schema,
    register_enrichment_schemas,
)
from featuregen.overlay.upload.taxonomy.recognition import _STATUS_VALUES, RecognitionStatus
from featuregen.overlay.upload.taxonomy.recognition_schema import (
    PROVIDER_STATUS_VALUES,
    V2_SCHEMA_PATH,
    V2_SCHEMA_SHA256,
    frozen_leaf_ids,
    render_use_case_recognition_v2_schema,
)
from featuregen.overlay.upload.taxonomy.use_cases import selectable_leaves

CHURN = "customer.relationship_attrition.churn"

# The canonical JSON of ("use_case_recognition", 1), digested. Recorded 2026-08-15 from the body that
# has been in production since Phase-1A — the contract every stored recognition answer was produced
# under.
_V1_CANONICAL_SHA256 = "b6653f6fb8588b74f13cd6ae7492ae80ba1fcb50fb9743a6c46af6c53d819081"


def _canonical_bytes(schema: dict) -> bytes:
    return json.dumps(schema, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _body(use_case_id: str, *, status: str = "classified") -> dict:
    return {
        "status": status,
        "candidates": [{
            "use_case_id": use_case_id,
            "relationship": "primary",
            "confidence": "high",
            "evidence_spans": ["predict churn in the next 90 days"],
            "rationale": "the request is about customers leaving",
        }],
        "modelling_contexts": [],
        "target_entity": "customer",
        "ambiguity_note": None,
    }


def test_v1_is_byte_frozen() -> None:
    """v1 is not merely still registered — it is the SAME BODY. A legacy row's audit trail names
    ``(use_case_recognition, 1)``; if that pair's meaning drifts, every stored answer is relabelled
    under a contract it was never produced against."""
    v1 = canonical_output_schema("use_case_recognition", 1)
    assert hashlib.sha256(_canonical_bytes(v1)).hexdigest() == _V1_CANONICAL_SHA256, (
        "the v1 recognition output schema changed. v1 is the contract every legacy llm_call and "
        "recognition row was produced under — it is FROZEN. Author a new version instead.")
    # And what v1 permitted, spelled out: a bare-string id, and a model-returnable technical_failure.
    assert v1["properties"]["candidates"]["items"]["properties"]["use_case_id"] == {
        "type": "string"}
    assert "technical_failure" in v1["properties"]["status"]["enum"]


def test_v2_bytes_match_their_pinned_hash() -> None:
    """The committed file IS the contract. An edit that skips the pin fails here — and, because the
    loader checks the same digest at import, would fail the whole build rather than quietly ship a
    different meaning under the same version number."""
    raw = V2_SCHEMA_PATH.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == V2_SCHEMA_SHA256, (
        f"{V2_SCHEMA_PATH.name} no longer matches V2_SCHEMA_SHA256. The recognition output contract "
        "is frozen per version: taxonomy growth requires a NEW version, not an edit to v2.")


def test_the_leaf_list_equals_the_registry_at_generation_time() -> None:
    """The drift alarm. A leaf added to (or removed from) the taxonomy without a new schema version
    means the prompt offers ids the schema refuses — or the schema admits ids the applicability layer
    cannot scope. Either way the operator must decide, so this fails LOUDLY rather than letting the
    enum be silently regenerated into the same version number."""
    live = sorted(selectable_leaves())
    frozen = list(frozen_leaf_ids())
    added = sorted(set(live) - set(frozen))
    removed = sorted(set(frozen) - set(live))
    assert frozen == live, (
        "the selectable taxonomy has moved since use_case_recognition v2 was frozen "
        f"(added: {added or 'none'}; removed: {removed or 'none'}). Do NOT regenerate v2 in place — "
        "one version number must mean one contract. Author v3: run `python -m "
        "featuregen.overlay.upload.taxonomy.recognition_schema` against a NEW file, register the new "
        "pair in enrich_llm._SCHEMAS, review the diff, and bump _OUTPUT_SCHEMA_VERSION.")
    # The generator reproduces the committed bytes exactly — regeneration is reviewable as a diff.
    assert render_use_case_recognition_v2_schema(live) == V2_SCHEMA_PATH.read_text(encoding="utf-8")
    assert len(frozen) == 88


def test_an_invalid_id_fails_at_the_schema_layer(db) -> None:
    """The live incident's literal value. Under v1 it validated (and the taxonomy post-pass then
    discarded the whole body, correct sibling included); under v2 it is refused by the SCHEMA — which
    is what puts it inside the audited seam's repair loop in Task 2."""
    register_enrichment_schemas(db)
    reg = DocumentSchemaRegistry(db)

    reg.validate("use_case_recognition", 1, _body("x"))          # v1: structurally valid. The defect.

    with pytest.raises(SchemaValidationError) as invalid:
        reg.validate("use_case_recognition", 2, _body("x"))
    # It fails on the ENUM keyword at the id's own path — which is what makes the seam's repair
    # complaint both actionable and value-free (`intake.llm._safe_reason` carries the pointer and the
    # keyword, never the raw message, which embeds `'x'`).
    cause = invalid.value.__cause__
    assert cause is not None and cause.validator == "enum"
    assert cause.json_path == "$.candidates[0].use_case_id"
    reg.validate("use_case_recognition", 2, _body(CHURN))        # a real leaf still passes


def test_technical_failure_is_not_a_provider_status(db) -> None:
    """``technical_failure`` is the platform's own outcome for a provider failure, a refusal or an
    exhausted repair budget. Offering it on the wire invited the model to self-report a platform
    state it cannot observe (and the live incident's run recorded exactly that status). It is gone
    from the provider contract — and STILL a valid internal status, which is the converse this test
    also pins: the fail-open constructor must keep working."""
    register_enrichment_schemas(db)
    reg = DocumentSchemaRegistry(db)

    with pytest.raises(SchemaValidationError) as refused:
        reg.validate("use_case_recognition", 2, _body(CHURN, status="technical_failure"))
    # On the STATUS enum itself — not, say, an unregistered-version error that would pass this test
    # for the wrong reason.
    cause = refused.value.__cause__
    assert cause is not None and cause.validator == "enum" and cause.json_path == "$.status"
    assert "technical_failure" not in PROVIDER_STATUS_VALUES

    # The converse — the internal outcome is untouched.
    assert RecognitionStatus.TECHNICAL_FAILURE.value == "technical_failure"
    assert "technical_failure" in _STATUS_VALUES
    for status in PROVIDER_STATUS_VALUES:
        assert status in _STATUS_VALUES      # the wire vocabulary is a SUBSET of the internal one
