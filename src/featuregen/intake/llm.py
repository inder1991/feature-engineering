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
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, runtime_checkable

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from featuregen.contracts import IdentityEnvelope, SchemaValidationError
from featuregen.contracts.db import DbConn
from featuregen.contracts.identity import identity_to_jsonb
from featuregen.documents.registry import DocumentSchemaRegistry
from featuregen.idgen import mint_id
from featuregen.intake.events import LLM_CALL_RECORDED  # R17 — IMPORTED, never redeclared here
from featuregen.intake.redaction import (
    INPUT_KEY_REDACTION,
    INPUT_KEY_REDACTION_VERSION,
    EgressViolation,
    assert_llm_safe,
)
from featuregen.intake.store import (
    append_feature_contract_event,  # R1 — the ONE FC append seam (P1)
)
from featuregen.security.audit import record_security_event

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
    # N11 — the resolved structural JSON schema, attached by call_llm from the registry so the real
    # adapter can enforce structured output (output_config.format). NOT part of the idempotency key.
    output_schema: dict | None = None


@dataclass(frozen=True)
class LLMResult:
    output: dict
    self_reported_scores: dict
    call_ref: str               # "" from a provider single-shot; the real llmc_ ref from call_llm
    status: str                 # PROVIDER_* single-shot; STATUS_* from call_llm
    # N9 — provider-reported usage/cost (input/output tokens, $), captured onto the immutable llm_call so
    # per-call LLM cost is auditable. The real adapter fills this from the provider usage; FakeLLM {} by default.
    cost_metadata: dict = field(default_factory=dict)


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
# N7 — only a SUCCESSFUL call is idempotent-reusable. A FAILED (transient/refusal) record must NOT be
# replayed forever for the same identity; find_llm_call skips it so call_llm re-drives.
_REUSABLE_STATUSES = frozenset({STATUS_OK, STATUS_REPAIRED, STATUS_RETRIED})


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
    cost_metadata: dict = field(default_factory=dict)  # N9 — simulated provider usage/cost for tests


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
            cost_metadata=dict(resp.cost_metadata),
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
                    cost_metadata=dict(resp.cost_metadata),  # N9 — capture provider usage/cost
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


# ---- the append-only llm_call record store (§9.3) -------------------------------------------


@dataclass(frozen=True)
class LLMCallRecord:
    llm_call_ref: str
    run_id: str
    task: str
    provider: str
    model: str
    prompt_id: str
    prompt_version: int
    output_schema_id: str
    output_schema_version: int
    generation_settings: dict
    redaction_version: str
    input_hash: str
    redacted_input: dict
    input_redaction: dict
    raw_output: dict
    validation_result: dict
    repair_attempts: list
    latency_ms: int | None
    cost_metadata: dict | None
    created_at: object
    created_by: dict


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _record_from_row(row: Mapping[str, Any]) -> LLMCallRecord:
    return LLMCallRecord(
        llm_call_ref=row["llm_call_ref"], run_id=row["run_id"], task=row["task"],
        provider=row["provider"], model=row["model"], prompt_id=row["prompt_id"],
        prompt_version=row["prompt_version"], output_schema_id=row["output_schema_id"],
        output_schema_version=row["output_schema_version"],
        generation_settings=row["generation_settings"], redaction_version=row["redaction_version"],
        input_hash=row["input_hash"], redacted_input=row["redacted_input"],
        input_redaction=row["input_redaction"], raw_output=row["raw_output"],
        validation_result=row["validation_result"], repair_attempts=row["repair_attempts"],
        latency_ms=row["latency_ms"], cost_metadata=row["cost_metadata"],
        created_at=row["created_at"], created_by=row["created_by"],
    )


def record_llm_call(
    conn: DbConn,
    *,
    run_id: str,
    request: LLMRequest,
    input_hash: str,
    redaction_version: str,
    input_redaction: Mapping[str, Any],
    raw_output: Mapping[str, Any],      # {"output": ..., "self_reported_scores": ...}
    validation_result: Mapping[str, Any],
    repair_attempts: list,
    latency_ms: int | None,
    cost_metadata: Mapping[str, Any] | None,
    created_by: Mapping[str, Any],      # identity_to_jsonb(actor)
) -> str:
    """Write ONE immutable llm_call record (§9.3) and return its `llm_call_ref`. Append-only: each
    call mints a fresh `llmc_` id and INSERTs — there is no update path. Stores the REDACTED input
    itself (`redacted_input`, replayable — never the raw intent, which stays in SP-0's encrypted
    raw_input_ref). `provider`/`model` are lifted from generation_settings into their own columns."""
    gs = dict(request.generation_settings)
    ref = mint_id("llmc")
    conn.execute(
        """
        INSERT INTO llm_call
            (llm_call_ref, run_id, task, provider, model, prompt_id, prompt_version,
             output_schema_id, output_schema_version, generation_settings, redaction_version,
             input_hash, redacted_input, input_redaction, raw_output, validation_result,
             repair_attempts, latency_ms, cost_metadata, created_by)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            ref, run_id, request.task, gs.get("provider"), gs.get("model"),
            request.prompt_id, request.prompt_version, request.output_schema_id,
            request.output_schema_version, Jsonb(gs), redaction_version, input_hash,
            Jsonb(dict(request.inputs)), Jsonb(dict(input_redaction)), Jsonb(dict(raw_output)),
            Jsonb(dict(validation_result)), Jsonb(list(repair_attempts)), latency_ms,
            Jsonb(dict(cost_metadata)) if cost_metadata is not None else None, Jsonb(dict(created_by)),
        ),
    )
    return ref


def read_llm_call(conn: DbConn, call_ref: str) -> LLMCallRecord:
    """Resolve an `llm_call_ref` to its immutable record. Raises KeyError if unknown."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM llm_call WHERE llm_call_ref = %s", (call_ref,))
        row = cur.fetchone()
    if row is None:
        raise KeyError(f"unknown llm_call_ref {call_ref!r}")
    return _record_from_row(row)


def find_llm_call(
    conn: DbConn,
    *,
    run_id: str,
    task: str,
    input_hash: str,
    provider: str,
    model: str,
    prompt_id: str,
    prompt_version: int,
    output_schema_id: str,
    output_schema_version: int,
    redaction_version: str,
    generation_settings: Mapping[str, Any],
) -> LLMCallRecord | None:
    """Full-identity idempotency lookup (§9.3, Decision D16): reuse a record ONLY when EVERY
    identity component matches. Queries the (run_id, task, input_hash) candidate set (indexed) and
    compares the rest — including a canonicalized generation_settings — in Python."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM llm_call WHERE run_id=%s AND task=%s AND input_hash=%s "
            "ORDER BY created_at ASC",
            (run_id, task, input_hash),
        )
        rows = cur.fetchall()
    target_gs = _canonical(dict(generation_settings))
    for row in rows:
        if (
            row["provider"] == provider
            and row["model"] == model
            and row["prompt_id"] == prompt_id
            and row["prompt_version"] == prompt_version
            and row["output_schema_id"] == output_schema_id
            and row["output_schema_version"] == output_schema_version
            and row["redaction_version"] == redaction_version
            and _canonical(row["generation_settings"]) == target_gs
        ):
            rec = _record_from_row(row)
            # N7 — reuse ONLY a SUCCESSFUL record. A FAILED call is NOT replayed for the same identity;
            # skip it so call_llm re-drives (record_llm_call is append-only, so a later successful record
            # coexists and is reused thereafter). Rows are ordered created_at ASC — the first successful
            # match wins.
            if rec.validation_result.get("result") in _REUSABLE_STATUSES:
                return rec
    return None


def _result_from_record(rec: LLMCallRecord) -> LLMResult:
    """Rebuild the caller-facing LLMResult from a stored record (idempotent reuse — no new call)."""
    return LLMResult(
        output=dict(rec.raw_output.get("output", {})),
        self_reported_scores=dict(rec.raw_output.get("self_reported_scores", {})),
        call_ref=rec.llm_call_ref,
        status=rec.validation_result.get("result", STATUS_FAILED),
        cost_metadata=dict(rec.cost_metadata or {}),  # N9 — preserve captured cost on the reuse path
    )


# ---- the event-sourced wrapper (§9.1, §9.3) -------------------------------------------------
# NOTE (R17): LLM_CALL_RECORDED is IMPORTED from featuregen.intake.events (P1) above — it is the
# single source for the constant and is NEVER redeclared here.


def call_llm(
    conn: DbConn,
    client: LLMClient,
    request: LLMRequest,
    *,
    run_id: str,
    actor: IdentityEnvelope,
) -> LLMResult:
    """The auditable-LLM entry point every SP-2 agent uses (§9.1). Egress-guards (hard-fails a
    violation into the security-audit stream, no dispatch), dedups on the full call identity
    (reuse ⟹ no new call/record/event), drives the §9.2 taxonomy validating against the registered
    output-schema, records ONE immutable llm_call, and emits LLM_CALL_RECORDED on the
    feature_contract aggregate. Returns the final LLMResult (STATUS_*) with the real call_ref."""
    # 1. Egress hard-backstop (§9.4). A violation is a hard failure recorded in the security-audit
    #    stream — never a value, never a warning; no payload is dispatched.
    try:
        assert_llm_safe(request)
    except EgressViolation as exc:
        record_security_event(
            conn,
            event_type="LLM_EGRESS_BLOCKED",
            actor=actor,
            attempted_action="call_llm",
            decision="denied",
            reason=str(exc),
            aggregate="feature_contract",
            aggregate_id=run_id,
        )
        raise

    input_hash = compute_input_hash(request.inputs)
    redaction_version = request.inputs.get(INPUT_KEY_REDACTION_VERSION, "unversioned")
    input_redaction = request.inputs.get(INPUT_KEY_REDACTION, {})
    gs = request.generation_settings

    # 2. Idempotency: a truly identical retry reuses its record (no double-charge, §9.3).
    existing = find_llm_call(
        conn,
        run_id=run_id, task=request.task, input_hash=input_hash,
        provider=gs.get("provider"), model=gs.get("model"),
        prompt_id=request.prompt_id, prompt_version=request.prompt_version,
        output_schema_id=request.output_schema_id,
        output_schema_version=request.output_schema_version,
        redaction_version=redaction_version, generation_settings=gs,
    )
    if existing is not None:
        return _result_from_record(existing)

    # 3. Drive the structured call, validating the LLM output against the REGISTERED output-schema
    #    (structural-only; server-compiled/cross-call-cached in the real adapter, §9.1).
    doc_registry = DocumentSchemaRegistry(conn)

    def validate_output(output: Mapping[str, Any]) -> None:
        doc_registry.validate(request.output_schema_id, request.output_schema_version, output)

    # N11 — attach the resolved structural output-schema so a provider adapter can ENFORCE structured
    # output (output_config.format). Inputs-only identity/hash is unchanged; FakeLLM ignores it.
    request = replace(
        request,
        output_schema=doc_registry.schema_for(request.output_schema_id, request.output_schema_version),
    )
    t0 = time.monotonic()
    outcome = drive_structured_call(client, request, validate_output)
    latency_ms = int((time.monotonic() - t0) * 1000)

    # 4. Record the immutable, replayable llm_call (redacted input stored, not hash-only, §9.3).
    call_ref = record_llm_call(
        conn,
        run_id=run_id, request=request, input_hash=input_hash,
        redaction_version=redaction_version, input_redaction=input_redaction,
        raw_output={"output": outcome.output, "self_reported_scores": outcome.self_reported_scores},
        validation_result=outcome.validation_result, repair_attempts=list(outcome.repair_attempts),
        latency_ms=latency_ms, cost_metadata=outcome.cost_metadata,
        created_by=identity_to_jsonb(actor),
    )

    # 5. Auth failures are additionally security-audited (§9.2), never silently swallowed.
    if outcome.security_audit_reason:
        record_security_event(
            conn,
            event_type="LLM_PROVIDER_AUTH_FAILURE",
            actor=actor,
            attempted_action="call_llm",
            decision="denied",
            reason=outcome.security_audit_reason,
            aggregate="feature_contract",
            aggregate_id=run_id,
        )

    # 6. Emit LLM_CALL_RECORDED on the feature_contract aggregate via the R1 store seam.
    #    append_feature_contract_event sets aggregate="feature_contract",
    #    aggregate_id == feature_contract_id == run_id, and the run_id mirror column ALWAYS
    #    populated (= run_id, non-null, for correlation) — feature_id ALWAYS NULL, request_id
    #    optional (X3 one event-identity invariant, mirrors 0504's overlay branch). This is NEVER
    #    appended on the `run` aggregate; call_llm never touches the low-level
    #    featuregen.aggregates._append.append. The redacted body lives in the store (referenced by
    #    call_ref), never inlined in the event. Payload is SEMANTIC-only (R2 — no id fields;
    #    feature_contract_id/run_id ride the typed columns).
    #    X4: LLM_CALL_RECORDED is a NON-lifecycle audit event — fold_feature_contract_state ignores
    #    it and call_llm makes no fold-based decision here, so the append rides current head
    #    (expected_version=None is correct) and is NOT subject to the folded-head CAS rule (that
    #    rule governs the lifecycle-transition commands in P4/P5/P7/P8, not this audit append).
    append_feature_contract_event(
        conn,
        run_id=run_id,
        type=LLM_CALL_RECORDED,
        payload={
            "llm_call_ref": call_ref,
            "task": request.task,
            "status": outcome.status,
            "validation_result": outcome.validation_result.get("result"),
        },
        actor=actor,
    )

    return LLMResult(
        output=outcome.output,
        self_reported_scores=outcome.self_reported_scores,
        call_ref=call_ref,
        status=outcome.status,
    )
