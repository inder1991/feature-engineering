"""MF-4: the real Claude adapter must bound each provider call with a wall-clock timeout so a hung
`messages.create` cannot hold the source advisory lock indefinitely. The `ClaudeConfig.timeout`
default/env test is SDK-FREE (constructs a dataclass only) and runs in CI; the call-path test drives
`llm.call(...)`, which does `import anthropic`, so it is SDK-GATED via `pytest.importorskip`."""
from dataclasses import replace

from featuregen.intake.llm import LLMRequest
from featuregen.intake.llm_claude import ClaudeConfig, _effective_timeout


def test_timeout_default_and_env(monkeypatch):
    # Default 60s; env FEATUREGEN_LLM_TIMEOUT overrides. SDK-free — constructs the dataclass only.
    assert ClaudeConfig().timeout == 60.0
    monkeypatch.setenv("FEATUREGEN_LLM_TIMEOUT", "12.5")
    assert ClaudeConfig.from_env().timeout == 12.5


def _req(timeout_scale=1.0):
    return LLMRequest(task="t", prompt_id="p", prompt_version=1, inputs={},
                      output_schema_id="s", output_schema_version=1,
                      generation_settings={}, output_schema={"type": "object"},
                      timeout_scale=timeout_scale)


def test_effective_timeout_scales_the_configured_clock_with_the_escalated_ceiling():
    # A truncation retry raises max_tokens; the wall clock must follow, or the retry is cut off
    # before it can spend the ceiling it was given. SDK-free (the `_wire_output_config` idiom):
    # `_effective_timeout` is pure, so the ceiling/clock coupling is provable without the SDK.
    #
    # It SCALES the configured value rather than replacing it — a later task sets
    # FEATUREGEN_LLM_TIMEOUT deliberately at the deployment layer, and this must ride whatever
    # that value is. Asserted against a NON-default config so a hard-coded 60 could not pass.
    config = ClaudeConfig(timeout=300.0)
    assert _effective_timeout(_req(), config) == 300.0            # un-escalated: exactly as configured
    assert _effective_timeout(_req(2.0), config) == 600.0         # 2x the ceiling, 2x the clock
    assert _effective_timeout(_req(4.0), config) == 1200.0


def test_effective_timeout_leaves_the_baseline_call_untouched():
    # The un-escalated path must keep EXACTLY today's timeout — this change raises the clock only
    # for a retry that was granted more tokens, never the baseline for every call.
    assert _effective_timeout(_req(), ClaudeConfig()) == ClaudeConfig().timeout == 60.0


def test_messages_create_receives_timeout(monkeypatch):
    # SDK-GATED (resolution #1): `ClaudeLLM.call` does `import anthropic` after `_ensure_client`, so
    # this only runs where the SDK is installed. It proves the configured timeout is forwarded to
    # `messages.create` as the `timeout=` kwarg. `create` raises to short-circuit after capture (we
    # do not need a valid provider response), and RuntimeError is NOT in the adapter's caught set, so
    # it propagates — we assert the capture happened regardless.
    import pytest

    pytest.importorskip("anthropic")
    from featuregen.intake.llm_claude import ClaudeLLM

    captured = {}

    class FakeMessages:
        def create(self, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            raise RuntimeError("stop")

    class FakeClient:
        messages = FakeMessages()

    llm = ClaudeLLM(ClaudeConfig(enabled=True, timeout=7.0))
    monkeypatch.setattr(llm, "_ensure_client", lambda: FakeClient())
    req = LLMRequest(task="t", prompt_id="p", prompt_version=1, inputs={"x": 1},
                     output_schema_id="s", output_schema_version=1, generation_settings={},
                     output_schema={"type": "object", "properties": {}})
    with pytest.raises(RuntimeError):
        llm.call(req)
    assert captured["timeout"] == 7.0
    # ...and an ESCALATED request (a truncation retry granted 4x the tokens) carries 4x the clock
    # all the way to `messages.create` — the end-to-end wiring the pure helper cannot prove alone.
    with pytest.raises(RuntimeError):
        llm.call(replace(req, timeout_scale=4.0))
    assert captured["timeout"] == 28.0
