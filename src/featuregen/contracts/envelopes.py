from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from featuregen.contracts.db import DbConn
from featuregen.contracts.provenance import (
    ProvenanceEnvelope,
)  # single source of truth (Phase 08 authoritative)


@dataclass(frozen=True, slots=True)
class IdentityEnvelope:
    """Identity-at-time-of-action for humans and services (§6.1)."""

    subject: str
    actor_kind: str
    authenticated: bool
    auth_method: str
    role_claims: tuple[str, ...]
    groups: tuple[str, ...] = ()
    tenant: str | None = None
    on_behalf_of: str | None = None
    impersonation: str | None = None
    break_glass: bool = False
    source_of_authority: str | None = None
    attestation: str | None = None


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    """A persisted domain event (§3.2). `actor` is the identity field everywhere."""

    event_id: str
    global_seq: int
    aggregate: str
    aggregate_id: str
    stream_version: int
    type: str
    schema_version: int
    table_version: int
    actor: IdentityEnvelope
    payload: Mapping[str, Any]
    provenance: ProvenanceEnvelope
    occurred_at: datetime
    recorded_at: datetime
    request_id: str | None = None
    feature_id: str | None = None
    run_id: str | None = None
    overlay_fact_id: str | None = None
    feature_contract_id: str | None = None
    caused_by: str | None = None


@dataclass(frozen=True, slots=True)
class NewEvent:
    """A to-be-appended event; global_seq/event_id/stream_version are allocated on append."""

    aggregate: str
    aggregate_id: str
    type: str
    schema_version: int
    payload: Mapping[str, Any]
    actor: IdentityEnvelope
    provenance: ProvenanceEnvelope
    request_id: str | None = None
    feature_id: str | None = None
    run_id: str | None = None
    overlay_fact_id: str | None = None
    feature_contract_id: str | None = None
    caused_by: str | None = None
    occurred_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class NewDocument:
    """A frozen document a handler emits (§3.4). derived_from MUST reference committed docs.
    doc_id is caller-supplied via HandlerContext.new_doc_id(); append_document persists it
    (see Phase 02 / §3.4). Canonical shape lives in the overview (00) and Phase 02."""

    doc_id: str
    stage: str
    schema_version: int
    branch_role: str
    content_hash: str
    body_classification: str
    provenance: ProvenanceEnvelope
    body_ref: str | None = None
    derived_from: tuple[str, ...] = ()
    supersedes: tuple[str, ...] = ()
    reject_reason: str | None = None


class Disposition(str, Enum):
    OK = "ok"
    RETRYABLE = "retryable"
    PERMANENT = "permanent"


@dataclass(frozen=True, slots=True)
class NewExternalCommand:
    """An external side effect to record in the §5.1 transaction (§5.4)."""

    integration: str
    idempotency_key: str
    request_payload: Mapping[str, Any]
    expected_run_id: str | None = None
    expected_stream_version: int | None = None
    expected_task_id: str | None = None
    job_handle: str | None = None
    dedup_supported: bool = False


@dataclass(frozen=True, slots=True)
class NewTimer:
    """A durable timer to schedule in the §5.1 transaction (§5.5)."""

    kind: str
    fire_at: datetime
    idempotency_key: str
    task_id: str | None = None
    business_calendar: str | None = None
    cas_task_version: int | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NewActivation:
    """A cross-aggregate feature activation a handler requests (§5.8). Applied by commit_step
    via apply_activation() on the STEP-transaction conn (Phase 06) — never by the handler — so
    the CAS, VERSION_ACTIVATED/ACTIVATION_CONFLICT events, active-map update, and expiry timer
    are atomic with the rest of the step."""

    feature_id: str
    feature_version_id: str
    use_case: str
    base_feature_version_id: str | None
    approval_type: str
    expires_at: datetime | None = None
    provenance: ProvenanceEnvelope | None = None


@dataclass(frozen=True, slots=True)
class HandlerResult:
    """A handler's typed return. Retry/permanent is signalled HERE, never via exceptions.
    Handlers are PURE: ALL effects are declared here and applied atomically by commit_step."""

    disposition: Disposition
    new_events: tuple[NewEvent, ...] = ()
    document: NewDocument | None = None
    external_commands: tuple[NewExternalCommand, ...] = ()
    timers: tuple[NewTimer, ...] = ()
    activations: tuple[NewActivation, ...] = ()
    error: str | None = None


@dataclass(frozen=True, slots=True)
class HandlerContext:
    run_id: str
    triggering_event: EventEnvelope
    documents: Mapping[str, NewDocument]
    read_conn: DbConn  # READ-ONLY (autocommit): load stream/documents only; handlers MUST NOT write

    def new_doc_id(self) -> str:
        """Mint a 'doc_'-prefixed id so the handler can set NewDocument(doc_id=...) and reference
        that exact id in its emitted events; commit_step persists it via append_document.
        (requires: from uuid import uuid4)"""
        return f"doc_{uuid4().hex}"


@dataclass(frozen=True, slots=True)
class Command:
    action: str
    aggregate: str
    aggregate_id: str | None
    args: Mapping[str, Any]
    actor: IdentityEnvelope
    idempotency_key: str
    expected_version: int | None = None


@dataclass(frozen=True, slots=True)
class CommandResult:
    accepted: bool
    aggregate_id: str
    produced_event_ids: tuple[str, ...] = ()
    denied_reason: str | None = None


@dataclass(frozen=True, slots=True)
class GuardOutcome:
    passed: bool
    resolved_inputs: Mapping[str, Any]
    per_predicate: Mapping[str, bool]


@dataclass(frozen=True, slots=True)
class GateTaskSpec:
    gate: str
    required_inputs: tuple[str, ...]
    eligible_assignees: Mapping[str, str]
    allowed_responses: tuple[str, ...]
    run_id: str | None = None
    feature_id: str | None = None
    quorum_required: int = 1
    quorum_of_role: str | None = None
    delegation_allowed: bool = True
    sla: str | None = None
    fact_key: str | None = None
    draft_event_id: str | None = None
    target_event_id: str | None = None
    evidence_ref: str | None = None


@dataclass(frozen=True, slots=True)
class SignalResult:
    task_id: str
    status: str
    counted: bool
    quorum_met: bool
