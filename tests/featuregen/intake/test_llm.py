import pytest

from featuregen.contracts import SchemaValidationError
from featuregen.intake.llm import (
    PROVIDER_OK,
    PROVIDER_REFUSAL,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_REPAIRED,
    STATUS_RETRIED,
    FakeLLM,
    FakeResponse,
    LLMRequest,
    LLMResult,
    compute_input_hash,
    drive_structured_call,
)


def _req(inputs=None, task="structure_intent", prompt_id="intake.v1"):
    return LLMRequest(
        task=task, prompt_id=prompt_id, prompt_version=1,
        inputs=inputs if inputs is not None else {"redacted_intent": "x", "catalog_metadata": {}},
        output_schema_id="TEST_STRUCT", output_schema_version=1,
        generation_settings={"provider": "fake", "model": "fake-1", "max_tokens": 1024},
    )


def test_compute_input_hash_ignores_transient_underscore_keys():
    base = {"redacted_intent": "count", "catalog_metadata": {"o": ["t"]}}
    h1 = compute_input_hash(base)
    # a transient repair annotation must NOT change the identity hash (stable across repairs)
    h2 = compute_input_hash({**base, "_repair_errors": ["missing entity"]})
    assert h1 == h2
    # a change to model-facing content DOES change the hash
    assert compute_input_hash({**base, "redacted_intent": "different"}) != h1


def test_fakellm_returns_scripted_provider_result():
    fake = FakeLLM()
    fake.script(
        task="structure_intent", prompt_id="intake.v1",
        responses=[FakeResponse(output={"entity": "customer"},
                                self_reported_scores={"entity": {"ambiguity": 0.05, "confidence": 0.97}})],
    )
    out = fake.call(_req())
    assert isinstance(out, LLMResult)
    assert out.output == {"entity": "customer"}
    assert out.self_reported_scores["entity"]["confidence"] == 0.97
    assert out.status == PROVIDER_OK
    assert out.call_ref == ""  # single-shot: call_llm stamps the real ref


def test_fakellm_consumes_sequence_across_calls():
    fake = FakeLLM()
    fake.script(
        task="structure_intent", prompt_id="intake.v1",
        responses=[FakeResponse(output={}, provider_status="invalid"),
                   FakeResponse(output={"entity": "customer"})],
    )
    r = _req()
    assert fake.call(r).status == "invalid"   # attempt 0
    assert fake.call(r).status == PROVIDER_OK  # attempt 1 (repair-driven re-call would land here)
    assert fake.call(r).status == PROVIDER_OK  # exhausted sequence repeats the last


def test_fakellm_scriptable_to_refusal():
    fake = FakeLLM()
    fake.script(task="structure_intent", prompt_id="intake.v1",
                responses=[FakeResponse(output={}, provider_status=PROVIDER_REFUSAL)])
    assert fake.call(_req()).status == PROVIDER_REFUSAL


def test_fakellm_raises_on_unscripted_key():
    with pytest.raises(KeyError):
        FakeLLM().call(_req())


def test_fakellm_task_key_constructor_form_with_fallback():
    # R19 canonical construction: task-keyed script + task-key fallback (P9's `_wire` uses EXACTLY
    # this). A request for the task resolves regardless of prompt_id / inputs.
    fake = FakeLLM(script={"structure_intent": FakeResponse(output={"entity": "customer"})})
    out = fake.call(_req(prompt_id="whatever.v9", inputs={"redacted_intent": "z"}))
    assert isinstance(out, LLMResult)
    assert out.output == {"entity": "customer"}
    assert out.status == PROVIDER_OK
    assert out.call_ref == ""


def test_fakellm_constructor_accepts_sequence_value():
    # A task-key value may be a SEQUENCE consumed in order (drives repair/retry paths in the E2E).
    fake = FakeLLM(script={"structure_intent": [FakeResponse(output={}, provider_status="invalid"),
                                                FakeResponse(output={"entity": "customer"})]})
    r = _req()
    assert fake.call(r).status == "invalid"
    assert fake.call(r).status == PROVIDER_OK


def test_llm_client_seam_registers_and_fails_closed_when_unset():
    # R10 module-global DI seam: current_ fails closed until register_ is called; then round-trips.
    from featuregen.intake import llm as _lmod
    from featuregen.intake.llm import current_llm_client, register_llm_client

    _lmod._LLM_CLIENT = None  # ensure unset for a deterministic fail-closed assertion
    with pytest.raises(RuntimeError):
        current_llm_client()
    fake = FakeLLM(script={"structure_intent": FakeResponse(output={"entity": "customer"})})
    register_llm_client(fake)
    assert current_llm_client() is fake


# ---- structured-output taxonomy (§9.2) ------------------------------------------------------


def _needs_entity(output):
    if "entity" not in output:
        raise SchemaValidationError("missing required field: entity")


def test_ok_first_try():
    fake = FakeLLM()
    fake.script(task="structure_intent", prompt_id="intake.v1",
                responses=[FakeResponse(output={"entity": "customer"})])
    out = drive_structured_call(fake, _req(), _needs_entity)
    assert out.status == STATUS_OK
    assert out.output == {"entity": "customer"}
    assert out.repair_attempts == ()
    assert out.validation_result == {"result": STATUS_OK}


def test_provider_ok_but_schema_invalid_repairs_then_validates():
    fake = FakeLLM()
    fake.script(task="structure_intent", prompt_id="intake.v1",
                responses=[FakeResponse(output={"wrong": 1}),                 # ok token, invalid body
                           FakeResponse(output={"entity": "customer"})])       # repair validates
    out = drive_structured_call(fake, _req(), _needs_entity)
    assert out.status == STATUS_REPAIRED
    assert out.output == {"entity": "customer"}
    assert len(out.repair_attempts) == 1 and out.repair_attempts[0]["class"] == "repair"


def test_repair_budget_exhausted_fails_into_clarification():
    fake = FakeLLM()
    fake.script(task="structure_intent", prompt_id="intake.v1",
                responses=[FakeResponse(output={}, provider_status="invalid"),
                           FakeResponse(output={}, provider_status="invalid"),
                           FakeResponse(output={}, provider_status="invalid")])
    out = drive_structured_call(fake, _req(), _needs_entity, repair_budget=2)
    assert out.status == STATUS_FAILED
    assert len(out.repair_attempts) == 2  # N=2 repairs attempted, then fail closed
    assert out.validation_result["result"] == STATUS_FAILED


def test_refusal_fails_into_clarification_without_repair():
    fake = FakeLLM()
    fake.script(task="structure_intent", prompt_id="intake.v1",
                responses=[FakeResponse(output={}, provider_status="refusal"),
                           FakeResponse(output={"entity": "customer"})])  # must NOT be consumed
    out = drive_structured_call(fake, _req(), _needs_entity)
    assert out.status == STATUS_FAILED
    assert out.repair_attempts == ()  # a decline is not a malformed structure — no repair


def test_max_tokens_retries_then_validates():
    fake = FakeLLM()
    fake.script(task="structure_intent", prompt_id="intake.v1",
                responses=[FakeResponse(output={}, provider_status="max_tokens"),
                           FakeResponse(output={"entity": "customer"})])
    out = drive_structured_call(fake, _req(), _needs_entity)
    assert out.status == STATUS_RETRIED
    assert out.repair_attempts[0]["class"] == "retry"


def test_provider_calls_counted_single_request():
    # #21: the outcome must report how many provider requests were ACTUALLY issued.
    fake = FakeLLM()
    fake.script(task="structure_intent", prompt_id="intake.v1",
                responses=[FakeResponse(output={"entity": "customer"})])
    out = drive_structured_call(fake, _req(), _needs_entity)
    assert out.status == STATUS_OK
    assert out.provider_calls == 1


def test_provider_calls_counted_across_repairs():
    # #21: a repaired call issued TWO provider requests — both must be counted, or a
    # provider-call budget tallied from the outcome undercounts reality.
    fake = FakeLLM()
    fake.script(task="structure_intent", prompt_id="intake.v1",
                responses=[FakeResponse(output={"wrong": 1}),                 # ok token, invalid body
                           FakeResponse(output={"entity": "customer"})])       # repair validates
    out = drive_structured_call(fake, _req(), _needs_entity)
    assert out.status == STATUS_REPAIRED
    assert out.provider_calls == 2


def test_provider_calls_counted_when_retry_budget_exhausted():
    # #21: 1 initial + 2 retries = 3 provider requests; the FAILED outcome still reports them
    # (the requests were made — the budget was spent).
    fake = FakeLLM()
    fake.script(task="structure_intent", prompt_id="intake.v1",
                responses=[FakeResponse(output={}, provider_status="max_tokens"),
                           FakeResponse(output={}, provider_status="max_tokens"),
                           FakeResponse(output={}, provider_status="max_tokens")])
    out = drive_structured_call(fake, _req(), _needs_entity, retry_budget=2)
    assert out.status == STATUS_FAILED
    assert out.provider_calls == 3


def test_auth_error_fails_closed_and_flags_security_audit():
    fake = FakeLLM()
    fake.script(task="structure_intent", prompt_id="intake.v1",
                responses=[FakeResponse(output={}, provider_status="auth_error")])
    out = drive_structured_call(fake, _req(), _needs_entity)
    assert out.status == STATUS_FAILED
    assert out.security_audit_reason is not None
