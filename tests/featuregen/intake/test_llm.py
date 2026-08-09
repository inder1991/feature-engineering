import pytest

from featuregen.contracts import AttestedSchemaValidationError, SchemaValidationError
from featuregen.intake.llm import (
    DEFAULT_RETRY_BUDGET,
    MAX_ATTESTED_REASON_CHARS,
    PROVIDER_MAX_TOKENS,
    PROVIDER_NON_RETRYABLE,
    PROVIDER_OK,
    PROVIDER_REFUSAL,
    PROVIDER_SCHEMA_FAULT,
    PROVIDER_TRANSIENT,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_REPAIRED,
    STATUS_RETRIED,
    FakeLLM,
    FakeResponse,
    LLMRequest,
    LLMResult,
    _safe_reason,
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


def test_repair_reasons_never_carry_the_offending_value():
    """jsonschema messages embed the instance value; the repair channel must not."""
    import jsonschema

    with pytest.raises(jsonschema.ValidationError) as raised:
        jsonschema.validate(instance={"ref": "Acme Corporation Ltd"},
                            schema={"type": "object",
                                    "properties": {"ref": {"type": "integer"}}})
    exc = SchemaValidationError(f"t@v1: {raised.value.message}")
    exc.__cause__ = raised.value

    reason = _safe_reason(exc)
    assert "Acme Corporation Ltd" not in reason
    assert "ref" in reason and "type" in reason


def test_a_model_chosen_key_never_rides_out_inside_the_json_pointer():
    """The pointer is built from INSTANCE property names, so it is not automatically schema text.

    Under an open object (`additionalProperties: <subschema>`) jsonschema descends with the key the
    MODEL chose, and that key lands in `json_path` verbatim — the same leak the raw message is
    rejected for, arriving through the channel that replaced it. Every schema reaching
    `validate_output` today is closed, so this is latent rather than live; the guarantee has to hold
    HERE, not in the schemas.
    """
    import jsonschema

    with pytest.raises(jsonschema.ValidationError) as raised:
        jsonschema.validate(
            instance={"totals": {"Acme Corporation Ltd": "not a number"}},
            schema={"type": "object",
                    "properties": {"totals": {"type": "object",
                                              "additionalProperties": {"type": "integer"}}}})
    assert "Acme Corporation Ltd" in raised.value.json_path   # the leak being guarded against
    exc = SchemaValidationError(f"t@v1: {raised.value.message}")
    exc.__cause__ = raised.value

    assert _safe_reason(exc) == "the structure did not match the required schema"


def test_a_pointer_of_declared_names_and_indices_still_reaches_the_model():
    """The guard must not over-fire: a pointer built only from schema-declared names and array
    indices is precisely what makes a repair actionable, and it survives intact. Without this, a
    guard that always fell back would satisfy the leak test above and quietly gut the feature."""
    import jsonschema

    with pytest.raises(jsonschema.ValidationError) as raised:
        jsonschema.validate(
            instance={"items": [{"ref": 1}, {"ref": "Acme Corporation Ltd"}]},
            schema={"type": "object",
                    "properties": {"items": {"type": "array", "items": {
                        "type": "object",
                        "properties": {"ref": {"type": "integer"}}}}}})
    exc = SchemaValidationError(f"t@v1: {raised.value.message}")
    exc.__cause__ = raised.value

    assert _safe_reason(exc) == "$.items[1].ref: failed 'type'"


def test_a_repair_recall_carries_the_value_free_reason_to_the_provider():
    """What the repair re-call actually SENDS is the thing under test, not the helper in isolation.

    The reason reaches the provider twice over: in `_repair_errors` (rendered into the repair turn)
    and in the audited `repair_attempts` ledger (stored verbatim in `llm_dispatch.redacted_input`).
    Neither may carry the instance value, which never went through the §9.4 egress guard."""
    import jsonschema

    schema = {"type": "object", "properties": {"ref": {"type": "integer"}}}

    def _validate(output):
        try:
            jsonschema.validate(instance=dict(output), schema=schema)
        except jsonschema.ValidationError as exc:
            raise SchemaValidationError(f"t@v1: {exc.message}") from exc

    seen: list[dict] = []

    class _Offending:
        def call(self, request):
            seen.append(dict(request.inputs))
            return LLMResult(output={"ref": "Acme Corporation Ltd"}, self_reported_scores={},
                             call_ref="", status=PROVIDER_OK)

    outcome = drive_structured_call(_Offending(), _req(), _validate, repair_budget=1)

    assert outcome.status == STATUS_FAILED
    assert "_repair_errors" not in seen[0]                     # nothing to repair on a first attempt
    assert seen[1]["_repair_errors"] == ["$.ref: failed 'type'"]
    # and the ledger the audit chain replays says the same, without moving the ceiling (a repair
    # entry consumes its slot; only a truncation retry carries a raised max_tokens).
    (entry,) = outcome.repair_attempts
    assert entry["reason"] == "$.ref: failed 'type'"
    assert "max_tokens" not in entry


# ── the author-attested exemption (Task 2b) ─────────────────────────────────────────────────────

def test_an_attested_reason_reaches_the_repair_channel():
    """A hand-authored validator has no jsonschema `__cause__`, so the structural rebuild has
    nothing to work from and every such failure collapses to one generic constant. The attested
    subclass is the ONE way past that: the author states, in the type, that this string is
    value-free."""
    exc = AttestedSchemaValidationError(
        "entity_ref 'ftr::x.Ahmed Al-Mansouri' is not offered. Choose from: a, b, c",
        llm_safe_reason="entity_ref: not one of the offered column_refs")

    assert _safe_reason(exc) == "entity_ref: not one of the offered column_refs"
    assert "Ahmed Al-Mansouri" not in _safe_reason(exc)


def test_an_attested_reason_wins_over_a_structural_cause():
    """Both available: the ATTESTED one is preferred. The author wrote it precisely because it says
    something the pointer cannot — which field, and what the field wanted."""
    import jsonschema

    with pytest.raises(jsonschema.ValidationError) as raised:
        jsonschema.validate(instance={"ref": "Acme Corporation Ltd"},
                            schema={"type": "object",
                                    "properties": {"ref": {"type": "integer"}}})
    exc = AttestedSchemaValidationError("t@v1: boom", llm_safe_reason="entity_ref: wants a column")
    exc.__cause__ = raised.value

    assert _safe_reason(exc) == "entity_ref: wants a column"


def test_the_default_is_unchanged_by_the_attestation_seam():
    """THE binding constraint. An error that did not attest must behave EXACTLY as before — no
    cause is still the generic constant, and a value-bearing message is still discarded."""
    plain = SchemaValidationError(
        "entity_ref 'ftr::x.Ahmed Al-Mansouri' is not one of the columns offered")
    assert _safe_reason(plain) == "the structure did not match the required schema"


def test_only_the_declared_type_may_claim_the_exemption():
    """A duck-typed attribute is NOT an attestation. The exemption is a TYPE so that `grep
    AttestedSchemaValidationError` enumerates every site that ever claimed it; an attribute set on
    a plain error somewhere far away would be an invisible one."""
    impostor = SchemaValidationError("t@v1: boom")
    impostor.llm_safe_reason = "Ahmed Al-Mansouri: not offered"   # noqa: B010 — deliberate

    assert _safe_reason(impostor) == "the structure did not match the required schema"


def test_an_attestation_must_actually_say_something():
    """An empty or whitespace attestation falls back to the sanitised default rather than sending
    an empty complaint the model cannot act on. Constructing one without a reason at all is a
    TypeError — you cannot have the type without the attestation."""
    assert _safe_reason(AttestedSchemaValidationError("boom", llm_safe_reason="   ")) == (
        "the structure did not match the required schema")
    with pytest.raises(TypeError):
        AttestedSchemaValidationError("boom")            # type: ignore[call-arg]


def test_an_attested_reason_is_bounded_in_the_LEDGER_not_only_on_the_wire():
    """`_wire_prompt` truncates the JOINED errors at 2000 chars, but `repair_attempts[].reason` is
    stored un-truncated in `llm_call.repair_attempts` and (audit-wrapped) in
    `llm_dispatch.redacted_input`. So the bound has to exist HERE, where the reason is minted."""
    exc = AttestedSchemaValidationError("boom", llm_safe_reason="x" * (MAX_ATTESTED_REASON_CHARS * 3))
    assert len(_safe_reason(exc)) == MAX_ATTESTED_REASON_CHARS


def test_an_attested_reason_reaches_the_provider_and_the_ledger():
    """The same two consumers Task 2 proved for the structural reason, re-proved for the attested
    one: the repair TURN and the audited ledger entry."""
    seen: list[dict] = []

    def _validate(output):
        raise AttestedSchemaValidationError(
            f"entity_ref {output.get('ref')!r} is not offered",
            llm_safe_reason="entity_ref: not one of the offered column_refs")

    class _Offending:
        def call(self, request):
            seen.append(dict(request.inputs))
            return LLMResult(output={"ref": "Ahmed Al-Mansouri"}, self_reported_scores={},
                             call_ref="", status=PROVIDER_OK)

    outcome = drive_structured_call(_Offending(), _req(), _validate, repair_budget=1)

    assert outcome.status == STATUS_FAILED
    assert "_repair_errors" not in seen[0]
    assert seen[1]["_repair_errors"] == ["entity_ref: not one of the offered column_refs"]
    (entry,) = outcome.repair_attempts
    assert entry["reason"] == "entity_ref: not one of the offered column_refs"
    # and the model's own text never rode along on either channel
    assert "Ahmed Al-Mansouri" not in str(seen[1]) and "Ahmed Al-Mansouri" not in str(entry)


def test_the_attested_reason_stays_out_of_the_identity_hash():
    """A repair must keep its parent's identity or it double-charges and forks dedup. The attested
    reason rides the same `_`-prefixed transient key, so this holds for it too."""
    base = {"redacted_intent": "count", "catalog_metadata": {}}
    assert compute_input_hash(base) == compute_input_hash(
        {**base, "_repair_errors": ["entity_ref: not one of the offered column_refs"]})


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


def test_a_truncation_retry_raises_max_tokens():
    """A max_tokens retry must DIFFER from the attempt it follows, or it cannot succeed."""
    seen: list[int] = []

    class _Truncating:
        def call(self, request):
            seen.append(request.generation_settings["max_tokens"])
            return LLMResult(output={}, self_reported_scores={}, call_ref="",
                             status=PROVIDER_MAX_TOKENS)

    request = LLMRequest(
        task="t", prompt_id="p", prompt_version=1, inputs={},
        output_schema_id="s", output_schema_version=1,
        generation_settings={"model": "m", "max_tokens": 4096},
        output_schema={"type": "object"})
    outcome = drive_structured_call(_Truncating(), request, lambda _out: None)

    assert seen == [4096, 8192, 16384], "each retry must raise the ceiling"
    assert outcome.status == STATUS_FAILED
    assert outcome.repair_attempts[0]["max_tokens"] == 8192


def test_a_truncation_retry_raises_the_wall_clock_with_the_ceiling():
    """A raised ceiling the attempt has no TIME to fill is not a different attempt.

    Generating 8-16K output tokens takes materially longer than the 60s default per-attempt clock,
    and adaptive thinking spends more of that budget too. Leaving the clock pinned converts the
    truncation into an APITimeoutError -> PROVIDER_NON_RETRYABLE: the escalation is thrown away and
    the call ends FAILED on the very attempt the raised ceiling was supposed to rescue.
    """
    seen: list[tuple[int, float]] = []

    class _Truncating:
        def call(self, request):
            seen.append((request.generation_settings["max_tokens"], request.timeout_scale))
            return LLMResult(output={}, self_reported_scores={}, call_ref="",
                             status=PROVIDER_MAX_TOKENS)

    request = LLMRequest(
        task="t", prompt_id="p", prompt_version=1, inputs={},
        output_schema_id="s", output_schema_version=1,
        generation_settings={"model": "m", "max_tokens": 4096},
        output_schema={"type": "object"})
    drive_structured_call(_Truncating(), request, lambda _out: None)

    # ONE decision, two consequences: 4x the tokens is also 4x the clock. The first attempt is the
    # un-escalated baseline — whatever FEATUREGEN_LLM_TIMEOUT is configured to, it is unchanged.
    assert seen == [(4096, 1.0), (8192, 2.0), (16384, 4.0)]


def test_a_transient_retry_is_replayed_unchanged():
    """Only truncation escalates — a transient fault is genuinely re-attemptable as-is."""
    seen: list[tuple[int, float]] = []

    class _Transient:
        def call(self, request):
            seen.append((request.generation_settings["max_tokens"], request.timeout_scale))
            return LLMResult(output={}, self_reported_scores={}, call_ref="",
                             status=PROVIDER_TRANSIENT)

    request = LLMRequest(
        task="t", prompt_id="p", prompt_version=1, inputs={},
        output_schema_id="s", output_schema_version=1,
        generation_settings={"model": "m", "max_tokens": 4096},
        output_schema={"type": "object"})
    drive_structured_call(_Transient(), request, lambda _out: None)

    # neither the ceiling NOR the clock moves — a transient retry must stay byte-identical
    assert seen == [(4096, 1.0), (4096, 1.0), (4096, 1.0)]


# ---- bounded backoff on the transient class (Task 4 review, Important 1) -----------------------


def _driven(status, *, sleep):
    """Drive a chain that returns `status` forever, recording every wait."""
    calls: list[int] = []

    class _Failing:
        def call(self, request):
            calls.append(1)
            return LLMResult(output={}, self_reported_scores={}, call_ref="", status=status)

    request = LLMRequest(
        task="t", prompt_id="p", prompt_version=1, inputs={},
        output_schema_id="s", output_schema_version=1,
        generation_settings={"model": "m", "max_tokens": 4096},
        output_schema={"type": "object"})
    outcome = drive_structured_call(_Failing(), request, lambda _out: None, sleep=sleep)
    return outcome, len(calls)


@pytest.mark.real_backoff
def test_a_transient_retry_WAITS_before_re_calling():
    """The gap `_SDK_MAX_RETRIES = 0` opened.

    This driver was the only retry layer with no wait: it re-called in the same tick, so three
    attempts against a rate-limited or overloaded provider finished inside a few milliseconds and
    were three guaranteed failures. That was survivable only while the Anthropic SDK absorbed the
    class underneath us with its own backoff, which Task 4 removed to bound the wall clock.

    Injects its own `sleep`, so the suite-wide zeroing fixture cannot make this pass vacuously.
    """
    from featuregen.intake.llm import transient_backoff_s

    waits: list[float] = []
    _outcome, calls = _driven(PROVIDER_TRANSIENT, sleep=waits.append)

    assert calls == 3                            # initial + 2 bounded retries
    assert waits == [1.0, 2.0]                   # doubling, one wait per re-call — never before the first
    assert waits == [transient_backoff_s(1), transient_backoff_s(2)]


@pytest.mark.parametrize("status", [PROVIDER_MAX_TOKENS, PROVIDER_SCHEMA_FAULT])
def test_a_DETERMINISTIC_retry_never_waits(status):
    """Truncation and schema-fault share the `_RETRYABLE` arm but are deterministic: a truncation
    does not un-truncate because we waited. Sleeping there would add latency to the COMMON
    escalation path and buy nothing, so the wait is keyed to the class, not to the arm."""
    waits: list[float] = []
    _outcome, calls = _driven(status, sleep=waits.append)

    assert calls == 3                            # the retries still happen
    assert waits == []                           # they just do not wait


@pytest.mark.real_backoff
def test_the_backoff_schedule_is_BOUNDED():
    """Doubling from 1s, capped at 5s per wait — so a widened `retry_budget` cannot turn the
    backoff into an unbounded contributor to the source advisory lock hold. At the shipped budget
    of 2 the whole schedule costs 3s against a 2702s worst-case chain."""
    from featuregen.intake.llm import _TRANSIENT_BACKOFF_CAP_S, transient_backoff_s

    schedule = [transient_backoff_s(n) for n in range(1, 8)]

    assert schedule == [1.0, 2.0, 4.0, 5.0, 5.0, 5.0, 5.0]
    assert max(schedule) == _TRANSIENT_BACKOFF_CAP_S
    assert sum(schedule[:DEFAULT_RETRY_BUDGET]) == 3.0


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


def test_cost_metadata_is_REPORTED_on_a_failed_outcome():
    """The exact argument `provider_calls` already makes, applied to tokens: the requests were made,
    so the spend was incurred. `_failed` hard-coded `cost_metadata={}`, so a run's token spend
    under-counted by EXACTLY its failures — and a first live run is most likely to produce failures,
    which is the reading this instrumentation exists to support."""
    fake = FakeLLM()
    fake.script(task="structure_intent", prompt_id="intake.v1",
                responses=[FakeResponse(output={"wrong": 1}, cost_metadata={"input_tokens": 10,
                                                                            "output_tokens": 3}),
                           FakeResponse(output={"wrong": 2}, cost_metadata={"input_tokens": 12,
                                                                            "output_tokens": 4}),
                           FakeResponse(output={"wrong": 3}, cost_metadata={"input_tokens": 14,
                                                                            "output_tokens": 5})])
    out = drive_structured_call(fake, _req(), _needs_entity, repair_budget=2)
    assert out.status == STATUS_FAILED
    assert out.provider_calls == 3
    assert out.cost_metadata == {"input_tokens": 36, "output_tokens": 12}


def test_cost_metadata_ACCUMULATES_across_every_physical_attempt():
    """The second half, and it under-counts the SUCCESS path too: both arms captured only the LAST
    response's usage, so a call that repaired twice before validating reported one attempt's tokens
    and silently dropped the two it had already paid for."""
    fake = FakeLLM()
    fake.script(task="structure_intent", prompt_id="intake.v1",
                responses=[FakeResponse(output={"wrong": 1}, cost_metadata={"input_tokens": 10,
                                                                            "output_tokens": 3}),
                           FakeResponse(output={"entity": "customer"},
                                        cost_metadata={"input_tokens": 20, "output_tokens": 7})])
    out = drive_structured_call(fake, _req(), _needs_entity)
    assert out.status == STATUS_REPAIRED
    assert out.provider_calls == 2
    assert out.cost_metadata == {"input_tokens": 30, "output_tokens": 10}


def test_accumulated_cost_tolerates_attempts_that_report_no_usage():
    """A provider ERROR yields `_fail(...)` with no usage at all, so the accumulator must treat a
    missing key as zero rather than propagating None into the sum or dropping the attempts that DID
    report. Partial usage is explicitly allowed by `_usage_cost`'s contract."""
    fake = FakeLLM()
    fake.script(task="structure_intent", prompt_id="intake.v1",
                responses=[FakeResponse(output={}, provider_status="max_tokens",
                                        cost_metadata={"input_tokens": 9}),
                           FakeResponse(output={}, provider_status="max_tokens"),
                           FakeResponse(output={"entity": "customer"},
                                        cost_metadata={"output_tokens": 4})])
    out = drive_structured_call(fake, _req(), _needs_entity, retry_budget=2)
    assert out.status == STATUS_RETRIED
    assert out.cost_metadata == {"input_tokens": 9, "output_tokens": 4}


def test_a_non_retryable_outcome_is_not_re_attempted():
    """The other half of "a timeout is not retried": once the adapter NAMES a timeout
    `non_retryable`, the driver must issue exactly ONE physical call and stop.

    Asserted on `provider_calls` rather than on `_RETRYABLE` membership, so adding the token back
    to the retryable tuple fails here — the point is the spend, not the tuple. At the 300s ceiling
    a later task configures, the difference this pins is 300s versus 900s per doomed call.
    """
    fake = FakeLLM()
    fake.script(task="structure_intent", prompt_id="intake.v1",
                responses=[FakeResponse(output={}, provider_status=PROVIDER_NON_RETRYABLE)])
    out = drive_structured_call(fake, _req(), _needs_entity, retry_budget=2)
    assert out.status == STATUS_FAILED
    assert out.provider_calls == 1
    assert out.repair_attempts == ()      # nothing was re-called, so nothing is on the ledger


def test_auth_error_fails_closed_and_flags_security_audit():
    fake = FakeLLM()
    fake.script(task="structure_intent", prompt_id="intake.v1",
                responses=[FakeResponse(output={}, provider_status="auth_error")])
    out = drive_structured_call(fake, _req(), _needs_entity)
    assert out.status == STATUS_FAILED
    assert out.security_audit_reason is not None
