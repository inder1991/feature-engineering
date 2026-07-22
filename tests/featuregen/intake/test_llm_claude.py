import pytest

from featuregen.intake.llm import (
    PROVIDER_AUTH_ERROR,
    PROVIDER_MAX_TOKENS,
    PROVIDER_NON_RETRYABLE,
    PROVIDER_OK,
    PROVIDER_REFUSAL,
    PROVIDER_TRANSIENT,
)
from featuregen.intake.llm_claude import (
    ClaudeConfig,
    ClaudeLLM,
    LLMAdapterUnavailable,
    _map_stop_reason,
)


def test_importing_adapter_does_not_import_anthropic():
    # The real SDK must never load at import time — CI never depends on `anthropic` (D5, §15).
    import featuregen.intake.llm_claude as mod
    # the module holds no module-level `anthropic` symbol (it is imported lazily inside .call)
    assert not hasattr(mod, "anthropic")


def _bare_request():
    from featuregen.intake.llm import LLMRequest

    return LLMRequest(
        task="structure_intent", prompt_id="intake.v1", prompt_version=1,
        inputs={"redacted_intent": "x", "catalog_metadata": {}, "raw_input_classification": "clean"},
        output_schema_id="S", output_schema_version=1,
        generation_settings={"provider": "anthropic", "model": "claude-opus-4-8"},
    )


def test_disabled_adapter_fails_closed_not_fallback():
    # An unconfigured/disabled adapter must fail closed — never silently return a FakeLLM result.
    adapter = ClaudeLLM(ClaudeConfig(enabled=False))
    with pytest.raises(LLMAdapterUnavailable):
        adapter.call(_bare_request())


def test_stop_reason_mapping_to_provider_taxonomy():
    assert _map_stop_reason("end_turn") == PROVIDER_OK
    assert _map_stop_reason("refusal") == PROVIDER_REFUSAL          # policy decline → clarify
    assert _map_stop_reason("max_tokens") == PROVIDER_MAX_TOKENS    # truncation → retry
    assert _map_stop_reason("tool_use") == PROVIDER_OK
    # N11 — an UNKNOWN/unexpected stop_reason fails CLOSED, never maps to OK (was a fail-open default)
    assert _map_stop_reason("some_future_reason") == PROVIDER_NON_RETRYABLE


# ---- Phase-1 hardening: wire-schema projection + safe 400 diagnostic (SDK-FREE, run in CI) ------


def test_wire_output_config_projects_schema():
    """The wire `output_config` must carry a PROJECTED (Anthropic-compatible) schema — `maxLength`/
    `maxItems` stripped and the nullable-enum normalized — while the request's pinned effort wins.
    Pure + SDK-free so the projection is proven every CI build without importing the SDK."""
    from featuregen.intake.llm import LLMRequest
    from featuregen.intake.llm_claude import ClaudeConfig, _wire_output_config
    from featuregen.intake.schema_projection import provider_incompatibilities

    canonical = {"type": "object", "properties": {
        "results": {"type": "array", "items": {"type": "object", "properties": {
            "ref": {"type": "string", "maxLength": 128},
            "basis": {"type": ["string", "null"], "enum": ["event", "snapshot", None]},
        }}, "maxItems": 40}}}
    req = LLMRequest(task="t", prompt_id="p", prompt_version=1, inputs={"x": 1},
                     output_schema_id="s", output_schema_version=1,
                     generation_settings={"effort": "low"}, output_schema=canonical)
    wire = _wire_output_config(req, ClaudeConfig(enabled=True, effort="high"))
    assert wire["format"]["type"] == "json_schema"
    assert provider_incompatibilities(wire["format"]["schema"]) == []
    assert wire["effort"] == "low"  # request's PINNED effort wins over the config default


def test_rejected_schema_keyword_is_token_only():
    """`_rejected_schema_keyword` returns a bare JSON-Schema keyword TOKEN (or None) — never the
    provider message body — so a schema-rejection 400 can be diagnosed without logging content."""
    from featuregen.intake.llm_claude import _SCHEMA_KEYWORDS, _rejected_schema_keyword

    msg = "output_config.format.schema: 'maxLength' is not supported for this endpoint"
    assert _rejected_schema_keyword(msg) == "maxLength"
    # returns only a fixed token or None — never echoes the message body
    assert _rejected_schema_keyword(msg) in _SCHEMA_KEYWORDS
    assert _rejected_schema_keyword("a benign message with no schema keyword") is None


# ---- finding #24: the adapter surfaces provider usage and applies the pinned settings ----------


def _stub_adapter(monkeypatch, create):
    """A ClaudeLLM whose SDK surface is stubbed: `anthropic` in sys.modules is a bare module with
    the two exception types `.call` references, and the constructed client is a create-capturing
    fake — no SDK, no network."""
    import sys
    import types

    stub = types.ModuleType("anthropic")
    stub.APIStatusError = type("APIStatusError", (Exception,), {})
    stub.APIConnectionError = type("APIConnectionError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "anthropic", stub)
    adapter = ClaudeLLM(ClaudeConfig(enabled=True))
    adapter._client = types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
    return adapter


def _schema_request(generation_settings):
    from dataclasses import replace

    return replace(_bare_request(), output_schema={"type": "object"},
                   generation_settings=generation_settings)


def test_claude_adapter_surfaces_provider_usage_and_pinned_settings(monkeypatch):
    """#24: resp.usage token counts must ride out on LLMResult.cost_metadata (they were discarded),
    and the request's PINNED generation settings (max_tokens/thinking/effort) are what the adapter
    actually applies — so the audited settings are the applied settings."""
    from types import SimpleNamespace

    captured = {}

    def _create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text='{"concept": "monetary_amount"}')],
            usage=SimpleNamespace(input_tokens=321, output_tokens=87),
        )

    adapter = _stub_adapter(monkeypatch, _create)
    out = adapter.call(_schema_request(
        {"provider": "anthropic", "model": "claude-opus-4-8",
         "max_tokens": 1024, "thinking": "adaptive", "effort": "low"}))
    assert out.status == PROVIDER_OK
    assert out.output == {"concept": "monetary_amount"}
    assert out.cost_metadata["input_tokens"] == 321
    assert out.cost_metadata["output_tokens"] == 87
    assert captured["max_tokens"] == 1024
    assert captured["thinking"] == {"type": "adaptive"}
    assert captured["output_config"]["effort"] == "low"    # pinned setting wins over config default


def test_claude_adapter_without_usage_still_returns_cleanly(monkeypatch):
    """#24: usage is OPTIONAL — a response without it yields empty cost_metadata, never a crash
    (FakeLLM-shaped clients carry no usage)."""
    from types import SimpleNamespace

    def _create(**kwargs):
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text='{"concept": "monetary_amount"}')],
        )

    adapter = _stub_adapter(monkeypatch, _create)
    out = adapter.call(_schema_request({"provider": "anthropic", "model": "claude-opus-4-8"}))
    assert out.status == PROVIDER_OK
    assert out.cost_metadata == {}


# ---- perf (vocab-caching): a large STATIC shared prefix rides a cached `system` block ------------


def _vocab_request(vocab):
    """A concept-batch-shaped request whose catalog carries the static vocabulary + volatile items,
    with the vocabulary marked as the shared cacheable prefix."""
    from dataclasses import replace

    return replace(
        _schema_request({"provider": "anthropic", "model": "claude-sonnet-5"}),
        inputs={"redacted_intent": "classify each column",
                "catalog_metadata": {"vocabulary": vocab,
                                     "items": [{"ref": "h1", "column": "balance"}]},
                "raw_input_classification": "clean"},
        cacheable_metadata_keys=("vocabulary",))


def test_wire_prompt_lifts_cacheable_vocab_into_a_cached_system_block():
    """SDK-FREE: the marked vocabulary is lifted into a `system` text block carrying an ephemeral
    `cache_control` breakpoint (so Anthropic caches the prefix and chunks 2..N read it cheaply),
    while the volatile per-item metadata rides the user turn and the vocab is NOT re-sent there."""
    from featuregen.intake.llm_claude import _wire_prompt

    vocab = [{"name": f"c{i}"} for i in range(50)]
    system, user = _wire_prompt(_vocab_request(vocab))
    assert system is not None and len(system) == 1
    block = system[0]
    assert block["cache_control"] == {"type": "ephemeral"}
    assert "c0" in block["text"] and "c49" in block["text"]   # the whole vocab rides the cached block
    assert "c0" not in user                                    # ...and is NOT re-sent in the user turn
    assert "balance" in user                                   # the volatile per-item metadata is


def test_wire_prompt_without_cacheable_keys_is_a_single_user_message():
    """No cacheable keys (definition/domain batch, single-mode, non-enrichment callers) → no system
    block; the whole payload rides one user message — byte-for-byte today's rendering."""
    from featuregen.intake.llm_claude import _wire_prompt

    system, user = _wire_prompt(_bare_request())              # cacheable_metadata_keys defaults to ()
    assert system is None
    assert user.startswith("Structure the following intent")


def test_claude_adapter_sends_vocab_as_a_cached_system_block(monkeypatch):
    """End-to-end at the adapter: a request that marks the vocab cacheable makes the SDK call carry a
    `system` block with `cache_control`, and the volatile user turn no longer re-sends the vocab."""
    from types import SimpleNamespace

    captured = {}

    def _create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text='{"results": []}')],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1))

    adapter = _stub_adapter(monkeypatch, _create)
    vocab = [{"name": f"c{i}"} for i in range(50)]
    adapter.call(_vocab_request(vocab))
    assert "system" in captured
    assert captured["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "c0" in captured["system"][0]["text"]
    assert "c0" not in captured["messages"][0]["content"]     # sent once, up front — not per chunk


def test_claude_adapter_without_cacheable_keys_sends_no_system(monkeypatch):
    """No regression: a request with no cacheable keys passes no `system` kwarg to the SDK — the
    outbound shape is byte-for-byte today's single-user-message call."""
    from types import SimpleNamespace

    captured = {}

    def _create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text='{"concept": "monetary_amount"}')])

    adapter = _stub_adapter(monkeypatch, _create)
    adapter.call(_schema_request({"provider": "anthropic", "model": "claude-sonnet-5"}))
    assert "system" not in captured


@pytest.mark.skipif(
    not __import__("os").environ.get("FEATUREGEN_LLM_SMOKE"),
    reason="config-gated live Claude smoke test; never gated in CI (D5, §15)",
)
def test_live_claude_structure_intent_smoke():  # pragma: no cover
    from featuregen.intake.llm import LLMRequest

    adapter = ClaudeLLM(ClaudeConfig.from_env())
    out = adapter.call(
        LLMRequest(
            task="structure_intent", prompt_id="intake.v1", prompt_version=1,
            inputs={"redacted_intent": "90-day rolling count of declined card authorizations",
                    "catalog_metadata": {"objects": ["card_authorizations"]},
                    "raw_input_classification": "clean"},
            output_schema_id="DRAFT_STRUCTURE", output_schema_version=1,
            generation_settings={"provider": "anthropic", "model": "claude-opus-4-8"},
        )
    )
    assert out.status in (PROVIDER_OK, PROVIDER_REFUSAL, PROVIDER_MAX_TOKENS,
                          PROVIDER_TRANSIENT, PROVIDER_AUTH_ERROR)
