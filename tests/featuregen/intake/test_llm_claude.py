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


def test_gen_settings_are_provider_derived_not_hardcoded_fake(monkeypatch):
    """N11: structure_intent generation settings derive from the configured provider — default fake in
    CI/local, anthropic when the adapter is enabled — never a hard-coded 'fake' baked into the prod path."""
    from featuregen.intake.commands import _gen_settings

    monkeypatch.delenv("FEATUREGEN_LLM_PROVIDER", raising=False)
    assert _gen_settings()["provider"] == "fake"
    monkeypatch.setenv("FEATUREGEN_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("FEATUREGEN_LLM_MODEL", "claude-opus-4-8")
    gs = _gen_settings()
    assert gs["provider"] == "anthropic" and gs["model"] == "claude-opus-4-8"


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
