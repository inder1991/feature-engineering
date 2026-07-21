"""Config-gated real Claude adapter (spec §9.5, Decision D12). Ships but is NEVER required in CI:
`anthropic` is imported LAZILY inside `.call`, never at module scope. Default model
`claude-sonnet-5` (overridable via FEATUREGEN_LLM_MODEL), adaptive thinking, structured outputs
via output_config.format. Maps each
provider outcome to the §9.2 PROVIDER_* taxonomy. NO production fallback to FakeLLM — an
enabled-but-unavailable adapter fails closed (LLMAdapterUnavailable) into the clarification/manual
path. The output-schema carries NO PHI/PII (server-compiled, cross-call-cached, §9.1).

See the Adapter Appendix in docs/plans/2026-07-01-sp2-03-llm-envelope.md for the full SDK call.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from featuregen.intake.llm import (
    DEFAULT_LLM_MODEL,
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
from featuregen.intake.schema_projection import project_for_anthropic

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClaudeConfig:
    enabled: bool = False
    model: str = DEFAULT_LLM_MODEL       # config-driven; never hard-coded at a call site
    max_tokens: int = 4096
    thinking: str = "adaptive"           # adaptive thinking (§9.5); budget_tokens is a 400 on 4.8
    effort: str = "high"
    # MF-4 — per-call wall-clock ceiling (seconds). A hung provider call would otherwise hold the
    # source advisory lock indefinitely and could fail the whole catalog ingest. Retries stay bounded
    # (2, unchanged); this bounds each attempt. Default 60s (env FEATUREGEN_LLM_TIMEOUT).
    timeout: float = 60.0

    @classmethod
    def from_env(cls) -> ClaudeConfig:
        return cls(
            enabled=os.environ.get("FEATUREGEN_LLM_PROVIDER") == "anthropic",
            model=os.environ.get("FEATUREGEN_LLM_MODEL", DEFAULT_LLM_MODEL),
            max_tokens=int(os.environ.get("FEATUREGEN_LLM_MAX_TOKENS", "4096")),
            thinking=os.environ.get("FEATUREGEN_LLM_THINKING", "adaptive"),
            effort=os.environ.get("FEATUREGEN_LLM_EFFORT", "high"),
            timeout=float(os.environ.get("FEATUREGEN_LLM_TIMEOUT", "60")),
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
    # An UNKNOWN/unexpected stop_reason must NOT be treated as OK (fail-open) — a new provider outcome
    # the driver doesn't recognize fails CLOSED into the manual path rather than passing a bad result (N11).
    return _STOP_REASON_MAP.get(stop_reason, PROVIDER_NON_RETRYABLE)


def _wire_prompt(request: LLMRequest) -> tuple[list[dict] | None, str]:
    """Split the outbound content into an optional CACHED ``system`` block + the user turn.

    Only the redacted, LLM-safe content reaches the model (§9.4). When the request marks metadata
    keys CACHEABLE (``cacheable_metadata_keys`` — a large STATIC prefix shared byte-for-byte across
    every chunk of a wide-table batch, i.e. the ~276-concept classification vocabulary), that block
    is lifted into a ``system`` text block carrying an ephemeral ``cache_control`` breakpoint.
    Anthropic prompt caching is a PREFIX match rendered ``tools`` -> ``system`` -> ``messages``, so the
    vocabulary is sent (and billed at full rate) ONCE; chunks 2..N of the same table reuse the cached
    prefix at ~0.1x cost and a fraction of the latency, instead of re-sending ~23K input tokens per
    chunk and blowing the 240s stage deadline. The vocabulary still egresses in full — the metadata a
    given item classifies is unchanged, so per-item classification is unchanged.

    With no cacheable keys present the ``system`` block is None and the entire payload rides a single
    user message — byte-for-byte today's rendering (definition/domain batches, single-mode calls, and
    every non-enrichment caller are unaffected). Pure + SDK-free so the split is unit-testable
    without importing the provider SDK. Operates on a COPY of the catalog metadata, so
    ``request.inputs`` (what the egress guard, audit record, and idempotency hash read) is untouched."""
    intent = request.inputs.get(INPUT_KEY_INTENT, "")
    catalog = dict(request.inputs.get(INPUT_KEY_CATALOG, {}) or {})
    cache_keys = [k for k in request.cacheable_metadata_keys if k in catalog]
    system: list[dict] | None = None
    if cache_keys:
        cached = {k: catalog.pop(k) for k in cache_keys}
        system = [{"type": "text",
                   "text": f"Shared classification context (names/types/grain only): {cached}",
                   "cache_control": {"type": "ephemeral"}}]
    user_content = (
        f"Structure the following intent for task '{request.task}'.\n"
        f"Intent (redacted, LLM-safe): {intent}\n"
        f"Catalog metadata (names/types/grain only): {catalog}"
    )
    return system, user_content


def _wire_output_config(request: LLMRequest, config: ClaudeConfig) -> dict:
    """Build the `output_config` sent to Anthropic. The canonical strict schema stays the source of
    truth for validating the RESPONSE (the driver's `reg.validate`, unchanged); here we PROJECT it to
    the provider-compatible subset for the WIRE ONLY (`project_for_anthropic`). Pure + SDK-free so a
    unit test can prove the outbound schema is clean without importing the SDK. The request's PINNED
    generation_settings win over the config default (#24) — the audited settings are the applied ones."""
    # `call()` fails closed on a missing schema before reaching here; `or {}` keeps the pure helper
    # type-safe (project_for_anthropic wants a dict) without changing that behavior.
    return {
        "effort": request.generation_settings.get("effort", config.effort),
        "format": {"type": "json_schema",
                   "schema": project_for_anthropic(request.output_schema or {})},
    }


# JSON-Schema keywords a provider 400 might name. Length/array-size/numeric bounds are stripped by the
# wire projection; `enum`/`type` round out the recognizable tokens. Order = extraction priority.
_SCHEMA_KEYWORDS = ("maxLength", "maxItems", "minItems", "minimum", "maximum",
                    "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "enum", "type")


def _rejected_schema_keyword(message: str) -> str | None:
    """Best-effort extraction of the rejected JSON-Schema keyword from a provider 400 message.
    Returns only a keyword token — never the message body — so nothing content-bearing is logged."""
    for kw in _SCHEMA_KEYWORDS:
        if kw in message:
            return kw
    return None


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
        # referenced structurally; it carries no PHI/PII (§9.1). See the Adapter Appendix. A large
        # STATIC shared prefix (the concept vocabulary) rides a cached `system` block; the volatile
        # per-item metadata rides the user turn — see `_wire_prompt` for the caching rationale.
        system, user_content = _wire_prompt(request)
        try:
            # N11 — ENFORCE structured output: attach the registered structural schema (resolved by
            # call_llm from output_schema_id/version onto request.output_schema) via output_config.format.
            # The schema is structural only — it carries no PHI/PII (§9.1). Fail closed if it is missing.
            if not request.output_schema:
                return _fail(PROVIDER_NON_RETRYABLE)
            # #24 — the request's PINNED generation_settings win (config is the fallback), so the
            # settings the audit records are the settings the provider actually ran with. The schema
            # is PROJECTED to the Anthropic-compatible subset for the wire (canonical stays the
            # response source of truth); the build is a pure, SDK-free, unit-tested helper.
            output_config = _wire_output_config(request, self._config)
            create_kwargs = {
                "model": model,
                "max_tokens": request.generation_settings.get("max_tokens", self._config.max_tokens),
                "thinking": {
                    "type": request.generation_settings.get("thinking", self._config.thinking)},
                "output_config": output_config,
                "messages": [{"role": "user", "content": user_content}],
                "timeout": self._config.timeout,   # MF-4 — bound each attempt (retries bounded at 2)
            }
            if system is not None:                 # vocab-caching: a cached shared-prefix system block
                create_kwargs["system"] = system   # (omitted entirely when there is no static prefix)
            resp = client.messages.create(**create_kwargs)
        except anthropic.APIStatusError as exc:  # map transport/status failures to the taxonomy
            status = getattr(exc, "status_code", 0)
            if status == 400:
                # A schema-rejection 400 (the provider refusing a structured-output schema) is
                # logged as HTTP status + a single JSON-Schema keyword TOKEN only — never the
                # request/response body or any PII. It still falls through to the taxonomy below.
                keyword = _rejected_schema_keyword(str(getattr(exc, "message", exc)))
                logger.warning("anthropic rejected structured-output schema (HTTP 400, keyword=%s)",
                               keyword or "unknown")
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
            output=output, self_reported_scores=scores, call_ref="", status=provider_status,
            cost_metadata=_usage_cost(resp),  # #24 — provider usage rides out, never discarded
        )


def _fail(provider_status: str) -> LLMResult:
    return LLMResult(output={}, self_reported_scores={}, call_ref="", status=provider_status)


def _usage_cost(resp) -> dict:
    """#24/N9 — lift the provider-reported token usage (`resp.usage`) onto LLMResult.cost_metadata
    so it lands on the immutable llm_call record instead of being discarded. Usage is OPTIONAL
    (a FakeLLM-shaped client has none): absent/partial usage yields an empty/partial dict."""
    usage = getattr(resp, "usage", None)
    if usage is None:
        return {}
    out: dict = {}
    for key in ("input_tokens", "output_tokens",
                "cache_creation_input_tokens", "cache_read_input_tokens"):
        val = getattr(usage, key, None)
        if isinstance(val, int):
            out[key] = val
    return out


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
