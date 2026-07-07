from featuregen.intake.llm import (
    PROVIDER_NON_RETRYABLE,
    PROVIDER_OK,
    FakeLLM,
    FakeResponse,
    LLMResult,
)
from featuregen.intake.redaction import INPUT_KEY_CATALOG, INPUT_KEY_INTENT
from featuregen.overlay.upload.enrich_llm import audited_enrich_call, register_enrichment_schemas

_META = {"table": "accounts", "column": "balance", "type": "numeric"}


def _call(db, client):
    register_enrichment_schemas(db)
    return audited_enrich_call(
        db, client, task="overlay.enrich.concept", prompt_id="overlay_concept_v1",
        schema_id="overlay_concept", catalog_metadata=_META, out_key="concept",
        instruction="Classify the concept of this column.")


def test_audited_call_returns_output_and_records(db):
    out = _call(db, FakeLLM(script={"overlay.enrich.concept":
                                    FakeResponse(output={"concept": "monetary_amount"})}))
    assert out == "monetary_amount"
    # exactly one immutable llm_call record was written under the overlay-enrichment run bucket
    n = db.execute(
        "SELECT count(*) FROM llm_call WHERE run_id = 'overlay-enrichment'").fetchone()[0]
    assert n == 1


def test_request_carries_schema_and_reserved_keys(db):
    captured = {}

    class _Capture:
        def call(self, request):
            captured["schema"] = request.output_schema
            captured["inputs"] = dict(request.inputs)
            return LLMResult(output={"concept": "monetary_amount"}, self_reported_scores={},
                             call_ref="", status=PROVIDER_OK)

    _call(db, _Capture())
    assert captured["schema"] is not None                 # M2: schema attached
    assert INPUT_KEY_INTENT in captured["inputs"]         # reserved keys, not bare
    assert INPUT_KEY_CATALOG in captured["inputs"]
    assert captured["inputs"][INPUT_KEY_CATALOG] == _META
    assert "definition" not in captured["inputs"]         # no free-text egress


def test_provider_that_fails_without_schema_now_succeeds(db):
    """A ClaudeLLM-shaped client fails closed with no output_schema; the audited call attaches one."""
    class _RealShaped:
        def call(self, request):
            if not request.output_schema:
                return LLMResult(output={}, self_reported_scores={}, call_ref="",
                                 status=PROVIDER_NON_RETRYABLE)
            return LLMResult(output={"concept": "monetary_amount"}, self_reported_scores={},
                             call_ref="", status=PROVIDER_OK)

    assert _call(db, _RealShaped()) == "monetary_amount"
