"""The Critique service's `CONTRACT_REVIEW` mode (spec §6.4) — the CHALLENGER that reviews the Draft
for contradictions / ambiguity / scope problems and emits structured critique findings that FEED the
Doubt Router (Task 5.2). SP-2 owns THIS one mode only; the reusable multi-mode Critique Service is
SP-8 — no other mode is built here.

The pass runs over the PII-free STRUCTURED Draft semantics via the P3 auditable-LLM envelope
(`call_llm` → `drive_structured_call`), validating the response against a registered structured-output
schema. It is a challenger, never a gate: it may only RAISE doubts (force a field to must-ask / add an
open question); it never confirms, lowers a doubt, or rewrites the contract. A critique-LLM failure
FAILS CLOSED per the §9.2 taxonomy (STATUS_FAILED → "failed_into_clarification") — it never fabricates
a clean verdict."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from featuregen.contracts import DbConn, IdentityEnvelope
from featuregen.intake.events import CONTRACT_CRITIQUED
from featuregen.intake.llm import (
    STATUS_OK,
    STATUS_REPAIRED,
    STATUS_RETRIED,
    LLMRequest,
    call_llm,
)
from featuregen.intake.redaction import (
    INPUT_KEY_CATALOG,
    INPUT_KEY_CLASSIFICATION,
    EgressViolation,
    _first_pii,
)
from featuregen.intake.store import append_feature_contract_event as append_fc_event

# The CONTRACT_REVIEW structured-output schema id/version call_llm resolves + validates against.
CONTRACT_REVIEW_SCHEMA_ID = "contract_review"
CONTRACT_REVIEW_SCHEMA_VERSION = 1
CRITIQUE_SCHEMA_OWNER = "featuregen-intake"

# Pinned generation settings for the challenger pass (part of the llm_call idempotency key, §9.3).
_REVIEW_SETTINGS = {"provider": "fake", "model": "fake-structured", "max_tokens": 2048}

# The envelope dispositions that carry a USABLE validated critique (§9.2). Anything else is a
# fail-closed disposition (STATUS_FAILED / "failed_into_clarification") — the challenger then fails
# into clarification with NO findings rather than fabricating a clean verdict.
_USABLE_STATUSES = frozenset({STATUS_OK, STATUS_REPAIRED, STATUS_RETRIED})

# CONTRACT_REVIEW output-schema (§6.4). A malformed critique is a doubt, not a value: call_llm drives
# the §9.2 repair/retry taxonomy against this and ultimately fails closed rather than passing an
# unvalidated body downstream. additionalProperties stays open (additive-friendly, mirrors events.py).
_FINDING_JSON_SCHEMA = {
    "type": "object",
    "required": ["severity", "category", "evidence", "recommendation", "blocks_progress"],
    "properties": {
        "severity": {"type": "string"},        # HIGH | MEDIUM | LOW
        "category": {"type": "string"},         # AMBIGUOUS_DEFINITION | CONTRADICTION | SCOPE | ...
        "field": {"type": ["string", "null"]},  # the field a blocking finding forces to must-ask
        "evidence": {"type": "string"},
        "recommendation": {"type": "string"},
        "blocks_progress": {"type": "boolean"},
    },
    "additionalProperties": True,
}
CONTRACT_REVIEW_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["review_type", "status", "findings"],
    "properties": {
        "review_type": {"type": "string"},
        "status": {"type": "string"},           # OK | NEEDS_REVIEW (the challenger's verdict)
        "findings": {"type": "array", "items": _FINDING_JSON_SCHEMA},
    },
    "additionalProperties": True,
}


def register_critique_schemas(registry) -> None:
    """Register the CONTRACT_REVIEW structured-output schema in SP-0's document registry so call_llm
    can validate the challenger's response (§9.1). Idempotent (register_schema upserts)."""
    registry.register_schema(
        CONTRACT_REVIEW_SCHEMA_ID,
        CONTRACT_REVIEW_SCHEMA_VERSION,
        CONTRACT_REVIEW_OUTPUT_SCHEMA,
        CRITIQUE_SCHEMA_OWNER,
    )


@dataclass(frozen=True, slots=True)
class CritiqueFinding:
    severity: str        # HIGH | MEDIUM | LOW
    category: str        # e.g. AMBIGUOUS_DEFINITION | CONTRADICTION | SCOPE
    evidence: str
    recommendation: str
    blocks_progress: bool
    field: str | None = None  # the field a blocking finding forces to must-ask (§6.4)


@dataclass(frozen=True, slots=True)
class CritiqueResult:
    review_type: str
    status: str
    findings: tuple[CritiqueFinding, ...]
    call_ref: str


def contract_review(
    conn: DbConn,
    client,
    draft_semantics: Mapping[str, Any],
    *,
    run_id: str,
    actor: IdentityEnvelope,
    catalog_metadata: Mapping[str, Any] | None = None,
    prompt_id: str = "contract_review",
    prompt_version: int = 1,
) -> CritiqueResult:
    """The Critique `CONTRACT_REVIEW` mode (spec §6.4). A single event-sourced LLM pass over the
    PII-free STRUCTURED Draft semantics (no raw intent text → no redaction needed; call_llm still
    egress-guards). It is a CHALLENGER, never a gate: it may only raise doubts / add open questions;
    it never confirms, lowers a doubt, or rewrites the contract. Emits a CONTRACT_CRITIQUED domain
    shadow on the feature_contract aggregate. A critique-LLM failure fails closed (§9.2): a non-OK,
    finding-free result — never a fabricated clean pass."""
    request = LLMRequest(
        task="contract_review",
        prompt_id=prompt_id,
        prompt_version=prompt_version,
        inputs={
            "draft_semantics": dict(draft_semantics),
            INPUT_KEY_CATALOG: dict(catalog_metadata or {}),
            # The structured Draft carries NO raw intent text → PII-free by construction ("clean");
            # call_llm still runs the egress hard-backstop over the whole payload before dispatch (§9.4).
            INPUT_KEY_CLASSIFICATION: "clean",
        },
        output_schema_id=CONTRACT_REVIEW_SCHEMA_ID,
        output_schema_version=CONTRACT_REVIEW_SCHEMA_VERSION,
        generation_settings=dict(_REVIEW_SETTINGS),
    )
    # Egress hard-backstop over the PRIMARY model-facing payload (§9.4). The Draft is "PII-free by
    # construction", but the global no-PII boundary demands a real scan on anything reaching the LLM:
    # assert_llm_safe only guards the reserved intent/catalog keys, not draft_semantics. Reuse
    # redaction's OWN scanner (recurses into nested str/dict/list) and FAIL CLOSED — residual PII in a
    # draft is an upstream invariant breach that must surface (EgressViolation), never be silently sent.
    hit = _first_pii(draft_semantics)
    if hit:
        raise EgressViolation(f"un-redacted {hit} detected in critique draft_semantics payload")
    result = call_llm(conn, client, request, run_id=run_id, actor=actor)

    if result.status in _USABLE_STATUSES:
        findings = tuple(
            CritiqueFinding(
                severity=str(f.get("severity", "LOW")),
                category=str(f.get("category", "")),
                evidence=str(f.get("evidence", "")),
                recommendation=str(f.get("recommendation", "")),
                blocks_progress=bool(f.get("blocks_progress", False)),
                field=f.get("field"),
            )
            for f in result.output.get("findings", [])
        )
        status = str(result.output.get("status", "NEEDS_REVIEW"))
    else:
        # Fail-closed (§9.2): the challenger did not return a usable structured verdict. It NEVER
        # fabricates a clean pass — it fails into clarification with no findings, and the failure
        # stays auditable (call_llm already recorded LLM_CALL_RECORDED with the failed disposition,
        # and the CONTRACT_CRITIQUED shadow below carries the fail-closed status).
        findings = ()
        status = result.status

    crit = CritiqueResult(
        review_type=str(result.output.get("review_type", "CONTRACT_REVIEW")),
        status=status,
        findings=findings,
        call_ref=result.call_ref,
    )
    append_fc_event(
        conn,
        run_id=run_id,
        type=CONTRACT_CRITIQUED,
        payload={
            "review_type": crit.review_type,
            "status": crit.status,
            "findings": [asdict(f) for f in findings],
            "critique_call_ref": crit.call_ref,  # the CONTRACT_CRITIQUED schema's canonical ref key
        },
        actor=actor,
    )
    return crit


def apply_critique(routing: dict[str, str], critique: CritiqueResult) -> dict[str, str]:
    """OR each `blocks_progress:true` finding into the routing: its field becomes must-ask (§6.4).
    A challenger can only RAISE a doubt — it never lowers a `human` back to `auto`, and a finding
    without a `field` never lowers anything."""
    out = dict(routing)
    for f in critique.findings:
        if f.blocks_progress and f.field:
            out[f.field] = "human"
    return out
