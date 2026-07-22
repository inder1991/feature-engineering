"""ClaudeLLM must send Anthropic a PROJECTED schema, and record a schema-rejection 400 safely.

These END-TO-END tests drive ``ClaudeLLM.call()``, whose first act after ``_ensure_client`` is a
lazy ``import anthropic`` (line ~99). The SDK is a PRODUCTION-only dependency (CI uses FakeLLM), so
the whole module SKIPS when it is absent — these run in the deploy / live-canary path. SDK-free CI
coverage of the wiring lives in ``test_llm_claude.py`` (``_wire_output_config`` +
``_rejected_schema_keyword`` unit tests), so the projection is proven every build without the SDK.
"""
import logging

import pytest

pytest.importorskip("anthropic")

from featuregen.intake.llm import LLMRequest
from featuregen.intake.llm_claude import ClaudeConfig, ClaudeLLM
from featuregen.intake.schema_projection import provider_incompatibilities

CANONICAL = {"type": "object", "properties": {
    "results": {"type": "array", "items": {"type": "object", "properties": {
        "ref": {"type": "string", "maxLength": 128},
        "basis": {"type": ["string", "null"], "enum": ["event", "snapshot", None]},
    }}, "maxItems": 40}}}


class _Resp:
    """A tiny status-carrying stub for APIStatusError construction."""

    request = None
    headers: dict = {}   # anthropic>=0.117 APIStatusError.__init__ reads response.headers.get("request-id")

    def __init__(self, status_code):
        self.status_code = status_code


def _req():
    return LLMRequest(task="t", prompt_id="p", prompt_version=1, inputs={"x": 1},
                      output_schema_id="s", output_schema_version=1,
                      generation_settings={}, output_schema=CANONICAL)


def test_call_projects_schema_before_send(monkeypatch):
    captured = {}

    class FakeMessages:
        def create(self, **kwargs):
            captured["schema"] = kwargs["output_config"]["format"]["schema"]
            captured["timeout"] = kwargs.get("timeout")
            raise RuntimeError("stop-after-capture")  # we only need the outbound schema

    class FakeClient:
        messages = FakeMessages()

    llm = ClaudeLLM(ClaudeConfig(enabled=True))
    monkeypatch.setattr(llm, "_ensure_client", lambda: FakeClient())
    with pytest.raises(RuntimeError):  # the create-stub raises after capturing the wire schema
        llm.call(_req())
    assert provider_incompatibilities(captured["schema"]) == []


def test_schema_rejection_400_records_keyword(monkeypatch, caplog):
    import anthropic

    class FakeMessages:
        def create(self, **kwargs):
            raise anthropic.APIStatusError(
                message="output_config.format.schema: 'maxLength' is not supported",
                response=_Resp(400), body=None)

    class FakeClient:
        messages = FakeMessages()

    llm = ClaudeLLM(ClaudeConfig(enabled=True))
    monkeypatch.setattr(llm, "_ensure_client", lambda: FakeClient())
    with caplog.at_level(logging.WARNING):
        out = llm.call(_req())
    assert out.status  # a fail status, not a raise
    assert any("maxLength" in r.message and "400" in r.message for r in caplog.records)
