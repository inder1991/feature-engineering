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
    _MAX_REPAIR_FEEDBACK_CHARS,
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
from featuregen.intake.redaction import (
    INPUT_KEY_CATALOG,
    INPUT_KEY_INTENT,
    INPUT_KEY_REPAIR_ERRORS,
)
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
    # source advisory lock indefinitely and could fail the whole catalog ingest. This bounds each
    # PHYSICAL attempt — and because `_SDK_MAX_RETRIES` is 0 it is also the entire wall clock of one
    # `messages.create`, so the ceiling an operator configures is the ceiling that elapses. The
    # driver's bounded repair/retry budget (§9.2) is the only retry layer above it.
    # Default 60s (env FEATUREGEN_LLM_TIMEOUT); the kind deployment sets 300.
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
    ``request.inputs`` (what the egress guard, audit record, and idempotency hash read) is untouched.

    A REPAIR re-call additionally appends the driver's value-free validation complaint to the user
    turn (see the ``INPUT_KEY_REPAIR_ERRORS`` block below); a first attempt carries no such key and
    renders exactly as before."""
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
    # A repair re-call carries the errors that refuted the previous answer. Without this the
    # repair sends byte-identical bytes and the budget buys nothing. The key is `_`-prefixed so it
    # stays OUT of `compute_input_hash` — the repair keeps its parent's identity while differing
    # on the wire, which is exactly the intent. The values are already value-free (`_safe_reason`).
    # It is the SHARED `INPUT_KEY_REPAIR_ERRORS` constant, never a literal: the writer is in
    # `llm.drive_structured_call` and a rename on one side alone would silently revert every repair
    # to a byte-identical re-call.
    errors = request.inputs.get(INPUT_KEY_REPAIR_ERRORS)
    if errors:
        rendered = "; ".join(str(e) for e in errors)[:_MAX_REPAIR_FEEDBACK_CHARS]
        user_content += (
            "\n\nYour previous answer did not validate against the required output schema. "
            f"Correct exactly these problems and return the fixed structure: {rendered}"
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


def _effective_timeout(request: LLMRequest, config: ClaudeConfig) -> float:
    """The per-attempt wall clock (MF-4), SCALED by any truncation escalation the driver applied.

    `config.timeout` stays the baseline for every ordinary call — the deployment layer owns that
    value via FEATUREGEN_LLM_TIMEOUT, and this multiplies it rather than replacing it, so raising
    the configured value raises the escalated ceilings with it. `request.timeout_scale` is 1.0
    unless `_escalated` raised `max_tokens`, in which case it carries the SAME ratio: an attempt
    allowed 4x the output tokens is allowed 4x the time to generate them. Without this the
    escalated retry is cut off mid-generation and the truncation is merely relabelled as an
    APITimeoutError -> PROVIDER_NON_RETRYABLE, which now ends the call outright. Pure + SDK-free so
    a unit test can prove the coupling without importing the SDK."""
    return config.timeout * request.timeout_scale


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


#: `max_retries` for the Anthropic client. ZERO, deliberately.
#:
#: The SDK default is 2, and it retries `APITimeoutError` internally — its own documentation states
#: "wall-clock can reach timeout x (max_retries+1)". So the SDK silently does, one layer down, the
#: exact thing the `APITimeoutError` arm below refuses to do: re-issue a request identically after
#: it needed longer than the clock it was given. Left at the default, a 300s configured ceiling
#: costs up to 900s per physical attempt, and the per-attempt bound MF-4 exists to enforce (see
#: `ClaudeConfig.timeout`) is multiplied by 3 while the source advisory lock is held.
#:
#: With this at 0, `drive_structured_call`'s bounded repair/retry budget is the ONE retry authority
#: (spec §9.2), the elapsed wall time equals `_effective_timeout`, and the warning that arm logs is
#: honest rather than a third of the truth.
#:
#: WHAT THIS GIVES UP. Three classes, not one — the SDK was absorbing more than self-inflicted
#: rate-limit bursts, and each is listed with where it now lands:
#:
#: 1. CONNECTION-LEVEL TIMEOUTS, and this is the sharp edge. The SDK raises `APITimeoutError` from
#:    ANY `httpx.TimeoutException` — connect, pool and write, not only "this generation needed more
#:    clock". All of them reach the `APITimeoutError` arm below, which returns
#:    PROVIDER_NON_RETRYABLE, which `llm.py` treats as terminal. So a sub-second TCP connect blip
#:    now kills the chunk with no retry at ANY layer. NOT recovered here and NOT recovered by the
#:    driver's backoff (that arm is never reached): the fix is to stop classifying every
#:    `APITimeoutError` as a generation overrun, which is a change to the arm's own contract rather
#:    than to this constant. Tracked as DEFERRED-WORK A.49.
#: 2. PROVIDER-SIDE CAPACITY — 529 `overloaded_error`, and org-wide or shared-key ITPM/OTPM limits.
#:    These are independent of OUR concurrency: one sequential caller issuing max_tokens=32000
#:    requests is exactly the shape that meets an output-token-per-minute ceiling. They map to
#:    PROVIDER_TRANSIENT and are RECOVERED, by the bounded backoff `llm._TRANSIENT_BACKOFF_BASE_S`
#:    adds to the driver's own retry arm — at the layer that can see the disposition, and costing
#:    ≤3s against a 2700s worst-case chain.
#: 3. A LONG `retry-after`. Bounded backoff does not clear a 30-60s per-minute quota, and the
#:    PROVIDER_* taxonomy carries no metadata channel to pass the provider's header into the
#:    decision. Partially given up, deliberately, and tracked as DEFERRED-WORK A.49.
#:
#: What is NOT given up is the self-inflicted burst: `run_batched` issues chunks strictly
#: sequentially from a single-replica backend, so there is no concurrency of ours to rate-limit.
#: And the SDK's own sleeps were themselves an unbounded addition to the lock hold — a
#: `retry-after: 60` sleeps outside the `timeout` budget entirely — which the driver's capped
#: backoff is not.
_SDK_MAX_RETRIES = 0


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
                self._client = anthropic.Anthropic(max_retries=_SDK_MAX_RETRIES)
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
                # MF-4 — bound each attempt (retries bounded at 2), scaled by any truncation
                # escalation so a retry granted more tokens is granted the time to generate them.
                "timeout": _effective_timeout(request, self._config),
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
        except anthropic.APITimeoutError:
            # NOT transient, and ordered BEFORE APIConnectionError because it is a SUBCLASS of it —
            # today this exception reaches that arm by inheritance, never by intent.
            #
            # The CASE THIS ARM IS FOR is a generation that needed longer than the ceiling it was
            # given: deterministic, so re-attempting it identically spends the budget proving it.
            # Fail closed; the stage reports it and the operator raises FEATUREGEN_LLM_TIMEOUT or
            # switches this adapter to streaming.
            #
            # THE CASE IT ALSO CATCHES, and should not: the SDK raises APITimeoutError from ANY
            # `httpx.TimeoutException`, so a connect/pool/write timeout — a transient network blip,
            # not a long generation — takes this same terminal path. That used to be masked because
            # the SDK retried those internally; `_SDK_MAX_RETRIES = 0` removed the mask without
            # narrowing this arm, so the blip now kills the chunk with no retry at any layer.
            # KNOWN AND TRACKED (DEFERRED-WORK A.49) rather than silently narrowed here: splitting
            # the two needs a discriminator this arm does not currently have, and widening what
            # counts as retryable is a change to the §9.2 taxonomy's contract, not a comment fix.
            #
            # The logged clock is the EFFECTIVE one (an escalated truncation retry runs at a
            # multiple of the configured value), or the operator reads a ceiling that was never
            # applied — and with no SDK retry layer beneath it, that effective clock is also the
            # real elapsed wall time rather than a third of it.
            logger.warning("anthropic call timed out after %.0fs (task=%s) — non-retryable; "
                           "raise FEATUREGEN_LLM_TIMEOUT or stream",
                           _effective_timeout(request, self._config), request.task)
            return _fail(PROVIDER_NON_RETRYABLE)
        except anthropic.APIConnectionError:
            return _fail(PROVIDER_TRANSIENT)        # network → bounded retry (NOT a timeout: above)

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
