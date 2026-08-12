"""MF-4: the real Claude adapter must bound each provider call with a wall-clock timeout so a hung
`messages.create` cannot hold the source advisory lock indefinitely — and must then treat blowing
that bound as the DETERMINISTIC outcome it is, not as a transient fault worth re-attempting.

The `ClaudeConfig.timeout` default/env and `_effective_timeout` tests are SDK-FREE (a dataclass and
a pure helper) and run in CI. Tests that drive `llm.call(...)` need the `import anthropic` inside it
to resolve: the classification tests stub that module (so CI, which installs only the `dev` extra,
actually guards them), while the older kwarg-forwarding test remains `pytest.importorskip`-gated."""
from dataclasses import replace

from featuregen.intake.llm import LLMRequest
from featuregen.intake.llm_claude import (
    DEFAULT_LLM_TIMEOUT_S,
    ClaudeConfig,
    _effective_timeout,
)


def test_timeout_default_and_env(monkeypatch):
    # The default is `DEFAULT_LLM_TIMEOUT_S`; env FEATUREGEN_LLM_TIMEOUT overrides. SDK-free —
    # constructs the dataclass only. DERIVED from the constant, never restated: this test pinned a
    # literal 60.0 and went red when the default was raised to the manifest's 300 (2026-08-09),
    # which is the drift `test_the_CODE_DEFAULT_timeout_is_the_one_the_manifest_ships` now guards.
    assert ClaudeConfig().timeout == DEFAULT_LLM_TIMEOUT_S
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
    # The un-escalated path must keep EXACTLY the CONFIGURED timeout — escalation raises the clock
    # only for a retry that was granted more tokens, never the baseline for every call. The
    # relationship is the assertion; the number is whatever the config carries.
    assert (_effective_timeout(_req(), ClaudeConfig())
            == ClaudeConfig().timeout == DEFAULT_LLM_TIMEOUT_S)


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


# ---- a timeout is NAMED, and is not retried ---------------------------------------------------


def _stub_sdk(monkeypatch):
    """Install a bare `anthropic` module carrying the three exception types `ClaudeLLM.call`
    references, WITH THE REAL SDK's INHERITANCE: `APITimeoutError` subclasses `APIConnectionError`.

    That subclassing is the whole subject of these tests — it is why an `except APIConnectionError`
    arm silently swallows timeouts, and why the timeout arm must be ordered ahead of it. Stubbing
    (rather than `importorskip`) is deliberate: CI installs only the `dev` extra, so an SDK-gated
    test would never run in the build that has to guard this. The premise the stub encodes is
    pinned against the real SDK by `test_the_real_sdk_still_subclasses_the_connection_error`."""
    import sys
    import types

    stub = types.ModuleType("anthropic")
    stub.APIStatusError = type("APIStatusError", (Exception,), {})
    stub.APIConnectionError = type("APIConnectionError", (Exception,), {})
    stub.APITimeoutError = type("APITimeoutError", (stub.APIConnectionError,), {})
    monkeypatch.setitem(sys.modules, "anthropic", stub)
    return stub


def _adapter_raising(monkeypatch, exc_name, config=None):
    """A ClaudeLLM whose `messages.create` raises the named stubbed SDK exception — no SDK, no
    network, no response to parse."""
    import types

    from featuregen.intake.llm_claude import ClaudeLLM

    stub = _stub_sdk(monkeypatch)

    def _create(**_kwargs):
        raise getattr(stub, exc_name)("boom")

    adapter = ClaudeLLM(config or ClaudeConfig(enabled=True))
    adapter._client = types.SimpleNamespace(messages=types.SimpleNamespace(create=_create))
    return adapter


def test_a_timeout_is_non_retryable_not_transient(monkeypatch):
    """A timeout under a fixed ceiling is deterministic — retrying it spends budget proving that.

    `APITimeoutError` reached `except APIConnectionError` by INHERITANCE, not by intent, and was
    classified transient, so the driver re-attempted it twice. Both halves of that are wrong: the
    SDK has already retried genuine network faults internally before raising, and a request that
    needs longer than the ceiling needs longer on every attempt. At a 300s ceiling this is the
    difference between one doomed call and three."""
    from featuregen.intake.llm import PROVIDER_NON_RETRYABLE

    adapter = _adapter_raising(monkeypatch, "APITimeoutError")
    assert adapter.call(_req()).status == PROVIDER_NON_RETRYABLE


def test_a_genuine_connection_error_is_still_transient(monkeypatch):
    """The parent arm must not be collateral damage: a non-timeout network fault is still a bounded
    retry. Ordering the subclass first is only safe if the superclass keeps its own disposition."""
    from featuregen.intake.llm import PROVIDER_TRANSIENT

    adapter = _adapter_raising(monkeypatch, "APIConnectionError")
    assert adapter.call(_req()).status == PROVIDER_TRANSIENT


def test_the_timeout_warning_reports_the_clock_the_attempt_actually_ran(monkeypatch, caplog):
    """The warning exists to tell an operator what to raise FEATUREGEN_LLM_TIMEOUT above, so it must
    name the EFFECTIVE clock, not the configured baseline. An escalated truncation retry runs at a
    multiple of the configured value (`timeout_scale`); logging 60s for an attempt that was given
    240s sends the operator to a ceiling that was never applied."""
    import logging

    adapter = _adapter_raising(monkeypatch, "APITimeoutError", ClaudeConfig(enabled=True,
                                                                           timeout=60.0))
    with caplog.at_level(logging.WARNING, logger="featuregen.intake.llm_claude"):
        adapter.call(_req(timeout_scale=4.0))
    assert "timed out after 240s" in caplog.text


def test_the_real_sdk_still_subclasses_the_connection_error():
    """Pins the premise `_stub_sdk` encodes. Skipped where the SDK is absent (CI), but it fires in
    any environment that HAS anthropic if a future release re-parents `APITimeoutError` — at which
    point the arm ordering above stops being load-bearing and this file's stub would be a fiction."""
    import pytest

    anthropic = pytest.importorskip("anthropic")
    assert issubclass(anthropic.APITimeoutError, anthropic.APIConnectionError)
    assert not issubclass(anthropic.APITimeoutError, anthropic.APIStatusError)
