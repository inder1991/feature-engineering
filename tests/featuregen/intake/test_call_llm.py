import pytest
from tests.featuregen.intake._helpers import service_actor

from featuregen.documents.registry import DocumentSchemaRegistry
from featuregen.events.registry import event_registry
from featuregen.events.store import load_stream
from featuregen.idgen import new_run_id
from featuregen.intake.events import register_sp2_event_types  # from P1 (sp2-01)
from featuregen.intake.llm import (
    STATUS_FAILED,
    STATUS_OK,
    FakeLLM,
    FakeResponse,
    LLMRequest,
    call_llm,
    read_llm_call,
)
from featuregen.intake.redaction import (
    DefaultIntentRedactor,
    EgressViolation,
    build_llm_inputs,
)

_OUT_SCHEMA = {
    "type": "object",
    "required": ["entity"],
    "properties": {"entity": {"type": "string"}},
    "additionalProperties": True,
}


def _setup(db):
    register_sp2_event_types(event_registry())  # LLM_CALL_RECORDED@v1 (P1)
    DocumentSchemaRegistry(db).register_schema("TEST_STRUCT", 1, _OUT_SCHEMA, owner="test")


def _req(gen=None, cls="clean"):
    red = DefaultIntentRedactor().redact("count declined auths per customer", cls)
    inputs = build_llm_inputs(
        red, catalog_metadata={"objects": ["card_authorizations"]}, raw_input_classification=cls
    )
    return LLMRequest(
        task="structure_intent", prompt_id="intake.v1", prompt_version=1, inputs=inputs,
        output_schema_id="TEST_STRUCT", output_schema_version=1,
        generation_settings=gen or {"provider": "fake", "model": "fake-1", "max_tokens": 1024},
    )


def _fake_ok(seq=None):
    fake = FakeLLM()
    fake.script(task="structure_intent", prompt_id="intake.v1",
                responses=seq or [FakeResponse(output={"entity": "customer"},
                                               self_reported_scores={"entity": {"ambiguity": 0.05}})])
    return fake


def test_ok_records_and_emits_event(db):
    _setup(db)
    run_id = new_run_id()
    res = call_llm(db, _fake_ok(), _req(), run_id=run_id, actor=service_actor())
    assert res.status == STATUS_OK
    assert res.output == {"entity": "customer"}
    assert res.call_ref.startswith("llmc_")
    rec = read_llm_call(db, res.call_ref)
    assert rec.run_id == run_id
    assert rec.redacted_input["redacted_intent"] == "count declined auths per customer"  # replayable
    stream = load_stream(db, "feature_contract", run_id)
    assert [e.type for e in stream] == ["LLM_CALL_RECORDED"]
    assert stream[0].payload["llm_call_ref"] == res.call_ref
    assert stream[0].payload["status"] == STATUS_OK


def test_provider_cost_metadata_is_captured_on_the_record(db):
    """N9: provider-reported usage/cost is captured onto the immutable llm_call record so per-call LLM
    cost is auditable. Before the fix, cost_metadata was always {} (usage was never captured)."""
    _setup(db)
    run_id = new_run_id()
    fake = FakeLLM()
    fake.script(task="structure_intent", prompt_id="intake.v1",
                responses=[FakeResponse(output={"entity": "customer"},
                                        cost_metadata={"input_tokens": 120, "output_tokens": 30, "usd": "0.0021"})])
    res = call_llm(db, fake, _req(), run_id=run_id, actor=service_actor())
    assert res.status == STATUS_OK
    rec = read_llm_call(db, res.call_ref)
    assert rec.cost_metadata == {"input_tokens": 120, "output_tokens": 30, "usd": "0.0021"}


def test_call_llm_attaches_resolved_output_schema_for_the_adapter(db):
    """N11: call_llm resolves the registered structural output-schema and attaches it to the request the
    provider client receives, so a real adapter can ENFORCE structured output (output_config.format)."""
    _setup(db)
    run_id = new_run_id()
    seen = {}

    class _SpyClient:
        def call(self, request):
            from featuregen.intake.llm import LLMResult
            seen["output_schema"] = request.output_schema
            return LLMResult(output={"entity": "customer"}, self_reported_scores={},
                             call_ref="", status="ok")

    res = call_llm(db, _SpyClient(), _req(), run_id=run_id, actor=service_actor())
    assert res.status == STATUS_OK
    assert seen["output_schema"] == _OUT_SCHEMA  # the registered TEST_STRUCT@1 schema was attached


def test_idempotent_reuse_no_double_charge(db):
    _setup(db)
    run_id = new_run_id()
    fake = _fake_ok()  # scripted ONCE — a reuse must not call the provider again
    r1 = call_llm(db, fake, _req(), run_id=run_id, actor=service_actor())
    r2 = call_llm(db, fake, _req(), run_id=run_id, actor=service_actor())
    assert r1.call_ref == r2.call_ref
    assert db.execute("SELECT count(*) FROM llm_call WHERE run_id=%s", (run_id,)).fetchone()[0] == 1
    stream = load_stream(db, "feature_contract", run_id)
    assert len([e for e in stream if e.type == "LLM_CALL_RECORDED"]) == 1


def test_settings_change_forces_fresh_call(db):
    _setup(db)
    run_id = new_run_id()
    fake = _fake_ok(seq=[FakeResponse(output={"entity": "customer"}),
                         FakeResponse(output={"entity": "customer"})])
    r1 = call_llm(db, fake, _req(), run_id=run_id, actor=service_actor())
    r2 = call_llm(db, fake, _req(gen={"provider": "fake", "model": "fake-1", "max_tokens": 2048}),
                  run_id=run_id, actor=service_actor())
    assert r1.call_ref != r2.call_ref
    assert db.execute("SELECT count(*) FROM llm_call WHERE run_id=%s", (run_id,)).fetchone()[0] == 2


def test_refusal_fails_into_clarification_and_is_recorded(db):
    _setup(db)
    run_id = new_run_id()
    fake = FakeLLM()
    fake.script(task="structure_intent", prompt_id="intake.v1",
                responses=[FakeResponse(output={}, provider_status="refusal")])
    res = call_llm(db, fake, _req(), run_id=run_id, actor=service_actor())
    assert res.status == STATUS_FAILED
    rec = read_llm_call(db, res.call_ref)
    assert rec.validation_result["result"] == STATUS_FAILED   # the failure is audited, not swallowed
    assert load_stream(db, "feature_contract", run_id)[0].type == "LLM_CALL_RECORDED"


def test_failed_call_is_not_reused_and_retry_succeeds(db):
    """N7: a FAILED (transient/refusal) llm_call is NOT replayed forever for the same identity. An
    IDENTICAL retry re-drives (never served the cached failure) and, on success, records a fresh usable
    record that is reused thereafter. Before the fix, find_llm_call reused the failed record."""
    _setup(db)
    run_id = new_run_id()
    failing = FakeLLM()
    failing.script(task="structure_intent", prompt_id="intake.v1",
                   responses=[FakeResponse(output={}, provider_status="refusal")])
    r1 = call_llm(db, failing, _req(), run_id=run_id, actor=service_actor())
    assert r1.status == STATUS_FAILED
    # an identical retry is NOT served the cached failure — it re-drives and succeeds
    r2 = call_llm(db, _fake_ok(), _req(), run_id=run_id, actor=service_actor())
    assert r2.status == STATUS_OK, "a transient failure must be retryable, not replayed forever (N7)"
    assert r2.call_ref != r1.call_ref  # a fresh successful record, not the cached failure
    # a further identical call NOW reuses the SUCCESSFUL record (idempotent — no provider call)
    r3 = call_llm(db, _fake_ok(), _req(), run_id=run_id, actor=service_actor())
    assert r3.call_ref == r2.call_ref


def test_egress_violation_hard_fails_and_security_audits(db):
    _setup(db)
    run_id = new_run_id()
    bad = _req(cls="clean")
    bad.inputs["raw_input_classification"] = "unscanned"  # tamper past the redactor
    with pytest.raises(EgressViolation):
        call_llm(db, _fake_ok(), bad, run_id=run_id, actor=service_actor())
    # hard failure recorded in the security-audit stream; no llm_call, no domain event
    assert db.execute(
        "SELECT count(*) FROM security_audit WHERE event_type='LLM_EGRESS_BLOCKED'"
    ).fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM llm_call WHERE run_id=%s", (run_id,)).fetchone()[0] == 0
    assert load_stream(db, "feature_contract", run_id) == []
