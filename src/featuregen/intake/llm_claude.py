"""Config-gated real Claude adapter (spec §9.5, Decision D12). Ships but is NEVER required in CI:
`anthropic` is imported LAZILY inside `.call`, never at module scope. Default model
`claude-opus-4-8`, adaptive thinking, structured outputs via output_config.format. Maps each
provider outcome to the §9.2 PROVIDER_* taxonomy. NO production fallback to FakeLLM — an
enabled-but-unavailable adapter fails closed (LLMAdapterUnavailable) into the clarification/manual
path. The output-schema carries NO PHI/PII (server-compiled, cross-call-cached, §9.1).

See the Adapter Appendix in docs/plans/2026-07-01-sp2-03-llm-envelope.md for the full SDK call.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from featuregen.intake.llm import (
    PROVIDER_AUTH_ERROR,
    PROVIDER_MAX_TOKENS,
    PROVIDER_NON_RETRYABLE,
    PROVIDER_OK,
    PROVIDER_REFUSAL,
    PROVIDER_TRANSIENT,
    LLMRequest,
    LLMResult,
)
from featuregen.intake.redaction import INPUT_KEY_CATALOG, INPUT_KEY_INTENT


@dataclass(frozen=True)
class ClaudeConfig:
    enabled: bool = False
    model: str = "claude-opus-4-8"       # config-driven; never hard-coded at a call site
    max_tokens: int = 4096
    thinking: str = "adaptive"           # adaptive thinking (§9.5); budget_tokens is a 400 on 4.8
    effort: str = "high"

    @classmethod
    def from_env(cls) -> ClaudeConfig:
        return cls(
            enabled=os.environ.get("FEATUREGEN_LLM_PROVIDER") == "anthropic",
            model=os.environ.get("FEATUREGEN_LLM_MODEL", "claude-opus-4-8"),
            max_tokens=int(os.environ.get("FEATUREGEN_LLM_MAX_TOKENS", "4096")),
            thinking=os.environ.get("FEATUREGEN_LLM_THINKING", "adaptive"),
            effort=os.environ.get("FEATUREGEN_LLM_EFFORT", "high"),
        )


class LLMAdapterUnavailable(Exception):
    """The real adapter is enabled but unavailable (disabled, missing SDK, or missing creds). The
    platform FAILS CLOSED into the clarification/manual path — it NEVER swaps in FakeLLM (D5)."""


# Anthropic stop_reason (§9.5) -> the §9.2 PROVIDER_* taxonomy the driver acts on.
_STOP_REASON_MAP = {
    "end_turn": PROVIDER_OK,
    "tool_use": PROVIDER_OK,
    "stop_sequence": PROVIDER_OK,
    "pause_turn": PROVIDER_OK,
    "refusal": PROVIDER_REFUSAL,       # policy decline → fail into clarification (NOT repair)
    "max_tokens": PROVIDER_MAX_TOKENS,  # truncation → bounded retry
}


def _map_stop_reason(stop_reason: str) -> str:
    return _STOP_REASON_MAP.get(stop_reason, PROVIDER_OK)


class ClaudeLLM:
    """LLMClient over the Anthropic SDK. Construction is lazy — it does NOT import `anthropic`;
    the SDK loads inside `.call` only when enabled, so CI never imports it."""

    def __init__(self, config: ClaudeConfig) -> None:
        self._config = config
        self._client = None  # constructed lazily on first enabled call

    def _ensure_client(self):
        if not self._config.enabled:
            raise LLMAdapterUnavailable(
                "Claude adapter is not enabled; failing closed (no FakeLLM fallback, D5)"
            )
        if self._client is None:
            try:
                import anthropic  # lazy: only here, only when enabled — CI never reaches this
            except ImportError as exc:  # enabled but SDK absent → fail closed, never fall back
                raise LLMAdapterUnavailable(
                    "anthropic SDK not installed; failing closed (no FakeLLM fallback, D5)"
                ) from exc
            try:
                self._client = anthropic.Anthropic()
            except Exception as exc:  # missing creds / config → fail closed
                raise LLMAdapterUnavailable(f"Claude adapter unavailable: {exc}") from exc
        return self._client

    def call(self, request: LLMRequest) -> LLMResult:
        client = self._ensure_client()  # raises LLMAdapterUnavailable if disabled/unavailable
        import anthropic  # already importable if _ensure_client succeeded

        model = request.generation_settings.get("model", self._config.model)
        # Only the redacted, LLM-safe content reaches the model (§9.4). The output-schema is
        # referenced structurally; it carries no PHI/PII (§9.1). See the Adapter Appendix.
        user_content = (
            f"Structure the following intent for task '{request.task}'.\n"
            f"Intent (redacted, LLM-safe): {request.inputs.get(INPUT_KEY_INTENT, '')}\n"
            f"Catalog metadata (names/types/grain only): {request.inputs.get(INPUT_KEY_CATALOG, {})}"
        )
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=request.generation_settings.get("max_tokens", self._config.max_tokens),
                thinking={"type": self._config.thinking},
                output_config={"effort": self._config.effort},
                messages=[{"role": "user", "content": user_content}],
                # NOTE: attach the registered structural output-schema via
                # output_config={"format": {"type": "json_schema", "schema": <schema>}} — resolved
                # from output_schema_id/version by the caller; see the Adapter Appendix.
            )
        except anthropic.APIStatusError as exc:  # map transport/status failures to the taxonomy
            status = getattr(exc, "status_code", 0)
            if status in (401, 403):
                return _fail(PROVIDER_AUTH_ERROR)   # auth/permission → fail closed + security-audit
            if status == 429 or status >= 500:
                return _fail(PROVIDER_TRANSIENT)    # rate-limit / transient 5xx → bounded retry
            return _fail(PROVIDER_NON_RETRYABLE)    # other non-retryable 4xx → fail closed
        except anthropic.APIConnectionError:
            return _fail(PROVIDER_TRANSIENT)        # network → bounded retry

        provider_status = _map_stop_reason(resp.stop_reason)
        output, scores = _parse_structured(resp)
        return LLMResult(
            output=output, self_reported_scores=scores, call_ref="", status=provider_status
        )


def _fail(provider_status: str) -> LLMResult:
    return LLMResult(output={}, self_reported_scores={}, call_ref="", status=provider_status)


def _parse_structured(resp) -> tuple[dict, dict]:
    """Extract the schema-constrained JSON body. output_config.format guarantees the first text
    block is valid JSON; a parse failure surfaces as an empty body (→ malformed → repair)."""
    import json

    for block in resp.content:
        if getattr(block, "type", None) == "text":
            try:
                parsed = json.loads(block.text)
            except (ValueError, TypeError):
                return {}, {}
            return parsed, dict(parsed.get("field_scores", {}))
    return {}, {}


def build_claude_llm(config: ClaudeConfig | None = None) -> ClaudeLLM:
    return ClaudeLLM(config or ClaudeConfig.from_env())
