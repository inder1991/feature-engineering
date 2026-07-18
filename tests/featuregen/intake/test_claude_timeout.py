"""MF-4: the real Claude adapter must bound each provider call with a wall-clock timeout so a hung
`messages.create` cannot hold the source advisory lock indefinitely. The `ClaudeConfig.timeout`
default/env test is SDK-FREE (constructs a dataclass only) and runs in CI; the call-path test drives
`llm.call(...)`, which does `import anthropic`, so it is SDK-GATED via `pytest.importorskip`."""
from featuregen.intake.llm_claude import ClaudeConfig


def test_timeout_default_and_env(monkeypatch):
    # Default 60s; env FEATUREGEN_LLM_TIMEOUT overrides. SDK-free — constructs the dataclass only.
    assert ClaudeConfig().timeout == 60.0
    monkeypatch.setenv("FEATUREGEN_LLM_TIMEOUT", "12.5")
    assert ClaudeConfig.from_env().timeout == 12.5


def test_messages_create_receives_timeout(monkeypatch):
    # SDK-GATED (resolution #1): `ClaudeLLM.call` does `import anthropic` after `_ensure_client`, so
    # this only runs where the SDK is installed. It proves the configured timeout is forwarded to
    # `messages.create` as the `timeout=` kwarg. `create` raises to short-circuit after capture (we
    # do not need a valid provider response), and RuntimeError is NOT in the adapter's caught set, so
    # it propagates — we assert the capture happened regardless.
    import pytest

    pytest.importorskip("anthropic")
    from featuregen.intake.llm import LLMRequest
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
