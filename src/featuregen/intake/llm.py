"""SP-2 auditable-LLM envelope (spec §9): LLMClient seam + FakeLLM + the structured-output
bounded-repair/retry taxonomy + the event-sourced call wrapper + the append-only llm_call store.

All agent code depends on the LLMClient INTERFACE, never on a provider (Decision D5). The provider
reports a single-shot outcome via LLMResult.status using the PROVIDER_* vocabulary; call_llm maps
it to the final STATUS_* vocabulary, stamps the real call_ref, and records the call. This module
ships FakeLLM + the taxonomy + the store; the real Claude adapter lives in llm_claude.py.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, runtime_checkable

from featuregen.contracts import SchemaValidationError

# ---- shared-contract shapes (overview §9.1) -------------------------------------------------


@dataclass(frozen=True)
class LLMRequest:
    task: str
    prompt_id: str
    prompt_version: int
    inputs: dict                # reserved-keyed, redacted (redaction.py); NO data values (§9.4)
    output_schema_id: str
    output_schema_version: int
    generation_settings: dict   # provider/model + thinking/effort/max_tokens — pinned; idempotency key


@dataclass(frozen=True)
class LLMResult:
    output: dict
    self_reported_scores: dict
    call_ref: str               # "" from a provider single-shot; the real llmc_ ref from call_llm
    status: str                 # PROVIDER_* single-shot; STATUS_* from call_llm


@runtime_checkable
class LLMClient(Protocol):
    def call(self, request: LLMRequest) -> LLMResult: ...


# provider single-shot outcome tokens (what LLMClient.call reports)
PROVIDER_OK = "ok"
PROVIDER_INVALID = "invalid"
PROVIDER_REFUSAL = "refusal"
PROVIDER_MAX_TOKENS = "max_tokens"
PROVIDER_SCHEMA_FAULT = "schema_fault"
PROVIDER_TRANSIENT = "transient"
PROVIDER_NON_RETRYABLE = "non_retryable"
PROVIDER_AUTH_ERROR = "auth_error"

# final wrapper statuses (call_llm / drive_structured_call return these)
STATUS_OK = "ok"
STATUS_REPAIRED = "repaired"
STATUS_RETRIED = "retried"
STATUS_FAILED = "failed_into_clarification"


def compute_input_hash(inputs: Mapping[str, Any]) -> str:
    """sha256 of the exact redacted (LLM-safe) input — the dedup/identity component (§9.3).
    Transient driver keys (`_`-prefixed, e.g. `_repair_errors`) are excluded so a repair re-call
    keeps the SAME identity as its parent (no double-charge, stable FakeLLM keying)."""
    material = {k: v for k, v in inputs.items() if not str(k).startswith("_")}
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---- FakeLLM (deterministic CI default) -----------------------------------------------------


@dataclass(frozen=True)
class FakeResponse:
    output: dict
    self_reported_scores: dict = field(default_factory=dict)
    provider_status: str = PROVIDER_OK


class FakeLLM:
    """Deterministic LLMClient for CI (mirrors SP-1's FixtureCatalog). Hermetic: no network,
    required in CI (§15).

    R19 canonical construction form (owner P3; P9's `_wire` uses EXACTLY this): a task-keyed
    script passed to the constructor — `FakeLLM(script={task_key: FakeResponse(...)})` — where each
    value is a single FakeResponse or a Sequence[FakeResponse]. `.call` resolves a request in
    priority order: (1) the exact `(task, prompt_id, input_hash)` entry, (2) the
    `(task, prompt_id, None)` wildcard, then (3) the **task-key fallback** keyed on `request.task`
    alone (the constructor script). A per-key SEQUENCE is consumed in order across calls (so a
    script drives repair/retry paths), repeating the last response once the sequence is exhausted.
    The finer-grained `.script(...)` builder registers `(task, prompt_id, input_hash)` entries for
    unit tests; the constructor task-key form is the one P9 wires."""

    def __init__(
        self,
        script: Mapping[str, FakeResponse | Sequence[FakeResponse]] | None = None,
    ) -> None:
        self._scripts: dict[tuple[str, str, str | None], list[FakeResponse]] = {}
        # R19 task-key fallback: {request.task -> [FakeResponse, ...]}, matched on task alone.
        self._task_fallback: dict[str, list[FakeResponse]] = {}
        self._calls: dict[tuple[str, str, str], int] = {}
        for task_key, responses in (script or {}).items():
            self._task_fallback[task_key] = (
                [responses] if isinstance(responses, FakeResponse) else list(responses)
            )

    def script(
        self,
        *,
        task: str,
        prompt_id: str,
        responses: Sequence[FakeResponse],
        input_hash: str | None = None,
    ) -> None:
        self._scripts[(task, prompt_id, input_hash)] = list(responses)

    def call(self, request: LLMRequest) -> LLMResult:
        h = compute_input_hash(request.inputs)
        seq = (
            self._scripts.get((request.task, request.prompt_id, h))
            or self._scripts.get((request.task, request.prompt_id, None))
            or self._task_fallback.get(request.task)   # R19 task-key fallback
        )
        if not seq:
            raise KeyError(
                f"FakeLLM has no script for {(request.task, request.prompt_id, h)}"
            )
        call_key = (request.task, request.prompt_id, h)
        idx = self._calls.get(call_key, 0)
        self._calls[call_key] = idx + 1
        resp = seq[min(idx, len(seq) - 1)]
        return LLMResult(
            output=dict(resp.output),
            self_reported_scores=dict(resp.self_reported_scores),
            call_ref="",
            status=resp.provider_status,
        )


# ---- structured-output taxonomy (§9.2): bounded repair / bounded retry / fail-closed ---------

DEFAULT_REPAIR_BUDGET = 2   # config-gated malformed-structure repairs (Decision D5)
DEFAULT_RETRY_BUDGET = 2    # config-gated truncation/schema-fault/transient retries

_RETRYABLE = (PROVIDER_MAX_TOKENS, PROVIDER_SCHEMA_FAULT, PROVIDER_TRANSIENT)


@dataclass(frozen=True)
class StructuredCallOutcome:
    output: dict
    self_reported_scores: dict
    status: str                 # STATUS_*
    validation_result: dict     # {"result": status, "reason"?: str}
    repair_attempts: tuple      # ({attempt, class, reason}, ...)
    cost_metadata: dict
    security_audit_reason: str | None


def _failed(resp: LLMResult, attempts: list, reason: str, *, security_audit: bool = False) -> StructuredCallOutcome:
    return StructuredCallOutcome(
        output=dict(resp.output),
        self_reported_scores=dict(resp.self_reported_scores),
        status=STATUS_FAILED,
        validation_result={"result": STATUS_FAILED, "reason": reason},
        repair_attempts=tuple(attempts),
        cost_metadata={},
        security_audit_reason=reason if security_audit else None,
    )


def drive_structured_call(
    client: LLMClient,
    request: LLMRequest,
    validate_output: Callable[[Mapping[str, Any]], None],
    *,
    repair_budget: int = DEFAULT_REPAIR_BUDGET,
    retry_budget: int = DEFAULT_RETRY_BUDGET,
) -> StructuredCallOutcome:
    """Drive one structured LLM call to a fail-closed disposition (§9.2). Provider-agnostic:
    re-invokes `client.call` for repairs/retries. `validate_output(output)` raises
    SchemaValidationError on an invalid structure. A `PROVIDER_OK` whose body fails validation is
    malformed structure → bounded repair. Refusal → fail into clarification directly (no repair).
    Truncation/schema-fault/transient → bounded retry. Auth → fail closed + security-audit signal.
    Nothing proceeds on an unresolved outcome; an invalid structure is a doubt, not a value."""
    attempts: list[dict] = []
    repairs_used = 0
    retries_used = 0
    errors: list[str] = []
    resp = client.call(request)
    while True:
        ps = resp.status
        if ps == PROVIDER_OK:
            try:
                validate_output(resp.output)
            except SchemaValidationError as exc:
                ps = PROVIDER_INVALID
                errors.append(str(exc))
            else:
                status = (
                    STATUS_REPAIRED if repairs_used
                    else STATUS_RETRIED if retries_used
                    else STATUS_OK
                )
                return StructuredCallOutcome(
                    output=dict(resp.output),
                    self_reported_scores=dict(resp.self_reported_scores),
                    status=status,
                    validation_result={"result": status},
                    repair_attempts=tuple(attempts),
                    cost_metadata={},
                    security_audit_reason=None,
                )
        if ps == PROVIDER_INVALID:
            if repairs_used < repair_budget:
                repairs_used += 1
                reason = errors[-1] if errors else "structure did not validate"
                attempts.append({"attempt": repairs_used, "class": "repair", "reason": reason})
                # re-prompt with the accumulated validation error, via a transient (`_`-prefixed)
                # key EXCLUDED from the identity hash so the repair keeps its parent's identity.
                request = replace(request, inputs={**request.inputs, "_repair_errors": list(errors)})
                resp = client.call(request)
                continue
            return _failed(resp, attempts, "repair budget exhausted (malformed structure)")
        if ps == PROVIDER_REFUSAL:
            return _failed(resp, attempts, "provider refusal (policy decline)")
        if ps in _RETRYABLE:
            if retries_used < retry_budget:
                retries_used += 1
                attempts.append({"attempt": retries_used, "class": "retry", "reason": ps})
                resp = client.call(request)
                continue
            return _failed(resp, attempts, f"{ps} retry budget exhausted")
        if ps == PROVIDER_AUTH_ERROR:
            return _failed(resp, attempts, "provider auth failure", security_audit=True)
        # PROVIDER_NON_RETRYABLE and any unknown token → fail closed
        return _failed(resp, attempts, f"non-retryable provider outcome ({ps})")


# ---- R10 collaborator DI seam (module-global; mirrors overlay/catalog.py) --------------------
# The ONE holder for the active LLMClient. All SP-2 agent code depends on the INTERFACE, never a
# provider (Decision D5). P4 resolves the client via current_llm_client(); P9 registers the FakeLLM
# via register_llm_client(...). Fail-closed if unset — never a silent default provider.
_LLM_CLIENT: LLMClient | None = None


def register_llm_client(client: LLMClient) -> None:
    """Register the process-wide LLMClient (last writer wins). P9 wires the FakeLLM here."""
    global _LLM_CLIENT
    _LLM_CLIENT = client


def current_llm_client() -> LLMClient:
    """Return the registered LLMClient; fail closed (RuntimeError) if none is registered."""
    if _LLM_CLIENT is None:
        raise RuntimeError(
            "no LLMClient registered; call register_llm_client(...) "
            "(register_sp2()/_wire does this)"
        )
    return _LLM_CLIENT
