"""The version seam: a batch/scalar call pins the request's prompt/schema version; default is 1.

Foundational Phase-2 Slice-1 seam — a later task bumps Pass B to v2. Without threading, a versioned
call would silently egress under the v1 contract (wrong prompt_version / wrong output_schema_version /
validated against the v1 schema). These tests assert the versions the driver actually sees.
"""
from featuregen.documents.registry import DocumentSchemaRegistry
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload import enrich_llm
from featuregen.overlay.upload.enrich_batch import BatchItem

# A v2 output-schema registered test-only (only v1 exists in `_SCHEMAS`), same shape so a scripted
# v2-valid body validates cleanly through the driver — proving a CLEAN validated call under v2, not
# the dirty repair-exhausted STATUS_FAILED path (which would resolve schema=None and never assert v2).
_CONCEPT_BATCH_V2 = {
    "type": "object", "additionalProperties": False,
    "properties": {"results": {"type": "array",
        "items": {"type": "object", "additionalProperties": False,
                  "properties": {"ref": {"type": "string", "maxLength": 128},
                                 "concept": {"type": "string", "maxLength": 128}},
                  "required": ["ref", "concept"]}}},
    "required": ["results"]}


def _capture_versions(monkeypatch):
    seen = {}
    real = enrich_llm.drive_structured_call

    def spy(client, req, validate):
        seen["prompt_version"] = req.prompt_version
        seen["schema_version"] = req.output_schema_version
        return real(client, req, validate)
    monkeypatch.setattr(enrich_llm, "drive_structured_call", spy)
    return seen


def test_audited_batch_call_defaults_to_v1(db, monkeypatch):
    seen = _capture_versions(monkeypatch)
    # A valid item + a scripted `results` entry keyed on the task so the driver actually runs
    # (an empty `items` list returns early before drive_structured_call — the spy would never fire).
    client = FakeLLM(script={"overlay.enrich.concept": FakeResponse(
        output={"results": [{"ref": "t", "concept": "monetary_stock"}]})})
    enrich_llm.audited_batch_call(
        db, client, task="overlay.enrich.concept", prompt_id="overlay_concept_v1",
        schema_id="overlay_concept_batch", shared_metadata={},
        items=[BatchItem(ref="t", metadata={"table": "t"})], out_key="concept",
        instruction="x", accept=lambda raw, ref: (raw, "valid"), ref_aware=True)
    assert seen["prompt_version"] == 1 and seen["schema_version"] == 1


def test_audited_batch_call_honors_explicit_version(db, monkeypatch):
    seen = _capture_versions(monkeypatch)
    # Register a test-only v2 schema first (only v1 ships in `_SCHEMAS`), else schema_for(...,2)
    # resolves None and the call reaches STATUS_FAILED via the repair-exhausted path — the version
    # would be "honored" only by accident, not by a clean validated v2 call.
    DocumentSchemaRegistry(db).register_schema(
        "overlay_concept_batch", 2, _CONCEPT_BATCH_V2, "featuregen-overlay")
    client = FakeLLM(script={"overlay.enrich.concept": FakeResponse(
        output={"results": [{"ref": "t", "concept": "monetary_stock"}]})})
    enrich_llm.audited_batch_call(
        db, client, task="overlay.enrich.concept", prompt_id="overlay_concept_v1",
        schema_id="overlay_concept_batch", shared_metadata={},
        items=[BatchItem(ref="t", metadata={"table": "t"})], out_key="concept",
        instruction="x", accept=lambda raw, ref: (raw, "valid"), ref_aware=True,
        prompt_version=3, schema_version=2)
    assert seen["prompt_version"] == 3 and seen["schema_version"] == 2
