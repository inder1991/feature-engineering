import pytest

from featuregen.intake.llm import (
    PROVIDER_OK,
    PROVIDER_REFUSAL,
    FakeLLM,
    FakeResponse,
    LLMRequest,
    LLMResult,
    compute_input_hash,
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
