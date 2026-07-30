"""The proposal entry-point command (SP-1 design §6).

Houses `propose_fact` — the proposal handler that validates a fact value, enforces replacement
semantics, mints evidence atomically, appends `OVERLAY_FACT_PROPOSED`, and opens
one human-gate task per resolved authority side. Lifted out of `commands.py`; `commands` re-exports
it (and references it from `_OVERLAY_CATALOG`) so existing `featuregen.overlay.commands` imports keep
resolving.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict
from typing import Any, TypedDict, cast

from featuregen.contracts import Command, CommandResult, DbConn
from featuregen.contracts.gates import GateTaskSpec
from featuregen.contracts.identity import identity_to_jsonb
from featuregen.gates.tasks import open_task
from featuregen.overlay._lifecycle import _NON_TERMINAL, _latest_proposed
from featuregen.overlay._types import FactType
from featuregen.overlay.authority import resolve_authority
from featuregen.overlay.catalog import current_catalog_adapter
from featuregen.overlay.evidence import (
    AssertionStrength,
    EvidenceLifecycle,
    EvidenceProducer,
    write_evidence,
)
from featuregen.overlay.facts import FactValidationError, validate_fact_value
from featuregen.overlay.identity import (
    display_object_ref,
    fact_key,
    join_write_error,
    proposal_fingerprint,
)
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import append_overlay_event, load_fact

_RAW_EVIDENCE_REQUIRED = frozenset({
    "table_snapshot_at",
    "row_count",
    "sample_size",
    "profile_version",
    "thresholds",
    "metric_values",
})
_RAW_EVIDENCE_OPTIONAL = frozenset({
    "producer",
    "strength",
    "lifecycle",
    "producer_configuration_hash",
    "producer_item_ref",
    "evidence_spans",
})


class _RawEvidenceArgs(TypedDict):
    table_snapshot_at: object
    row_count: int
    sample_size: int
    profile_version: str
    thresholds_used: Mapping[str, Any]
    metric_values: Mapping[str, Any]
    producer: EvidenceProducer
    strength: AssertionStrength
    lifecycle: EvidenceLifecycle
    producer_configuration_hash: str | None
    producer_item_ref: str | None
    evidence_spans: tuple[str, ...]


def _raw_evidence_args(payload: object) -> _RawEvidenceArgs:
    """Validate the closed raw-evidence payload before minting an immutable record."""
    if not isinstance(payload, Mapping):
        raise ValueError("raw evidence must be an object")
    keys = set(payload)
    missing = _RAW_EVIDENCE_REQUIRED - keys
    unknown = keys - _RAW_EVIDENCE_REQUIRED - _RAW_EVIDENCE_OPTIONAL
    if missing:
        raise ValueError(f"raw evidence missing fields {sorted(missing)}")
    if unknown:
        raise ValueError(f"raw evidence has unknown fields {sorted(unknown)}")
    thresholds = payload["thresholds"]
    metrics = payload["metric_values"]
    if not isinstance(thresholds, Mapping) or not isinstance(metrics, Mapping):
        raise ValueError("raw evidence thresholds and metric_values must be objects")
    spans = payload.get("evidence_spans", ())
    if (
        isinstance(spans, (str, bytes))
        or not isinstance(spans, Sequence)
        or not all(isinstance(span, str) and span.strip() for span in spans)
    ):
        raise ValueError("raw evidence evidence_spans must be a sequence of non-blank strings")
    try:
        producer = EvidenceProducer(
            payload.get("producer", EvidenceProducer.PROFILER.value))
        strength = AssertionStrength(
            payload.get("strength", AssertionStrength.SUPPORTED.value))
        lifecycle = EvidenceLifecycle(
            payload.get("lifecycle", EvidenceLifecycle.ACTIVE.value))
    except ValueError as exc:
        raise ValueError(f"raw evidence has an invalid provenance axis: {exc}") from exc
    for count_name in ("row_count", "sample_size"):
        count = payload[count_name]
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError(f"raw evidence {count_name} must be a non-negative integer")
    profile_version = payload["profile_version"]
    if not isinstance(profile_version, str) or not profile_version.strip():
        raise ValueError("raw evidence profile_version must not be blank")
    return _RawEvidenceArgs(
        table_snapshot_at=payload["table_snapshot_at"],
        row_count=payload["row_count"],
        sample_size=payload["sample_size"],
        profile_version=profile_version,
        thresholds_used=thresholds,
        metric_values=metrics,
        producer=producer,
        strength=strength,
        lifecycle=lifecycle,
        producer_configuration_hash=payload.get("producer_configuration_hash"),
        producer_item_ref=payload.get("producer_item_ref"),
        evidence_spans=tuple(spans),
    )


def propose_fact(conn: DbConn, cmd: Command) -> CommandResult:
    """Validate and record a proposed fact, then open a human-gate task per authority side.

    Replacement semantics: denied whenever a non-terminal fact already exists for the
    `fact_key`; only an empty stream or a REJECTED terminal admits a new proposal, and a previously
    rejected `proposal_fingerprint` stays sticky-denied.
    """
    adapter = current_catalog_adapter()
    args = cmd.args
    ref = args["ref"]
    fact_type: FactType = args["fact_type"]
    use_case = args.get("use_case")
    proposed_value = args["proposed_value"]
    evidence_ref = args.get("evidence_ref")
    # a caller (the profiler) may hand propose_fact the raw evidence metric payload instead of
    # a pre-minted evidence_ref. propose_fact mints the immutable evidence row ITSELF, after every
    # replacement-semantics deny path has returned, so a denied proposal never orphans an evidence
    # row. Legacy callers passing an explicit evidence_ref keep their existing behavior.
    evidence_payload = args.get("evidence")
    try:
        validate_fact_value(fact_type, proposed_value, use_case=use_case)
    except FactValidationError as exc:
        return CommandResult(
            accepted=False, aggregate_id="", denied_reason=f"invalid fact value: {exc}"
        )
    # SP-1.5 review fix: reject a cross-catalog approved_join (F4) or one whose proposed_value
    # describes a different join than `ref` (authority/key derive from ref; the value is what
    # consumers read — a mismatch lets the wrong owners attest a join over other tables).
    join_err = join_write_error(ref, fact_type, proposed_value, use_case)
    if join_err is not None:
        return CommandResult(accepted=False, aggregate_id="", denied_reason=join_err)
    if fact_type == "entity_bridge":
        # The pure identity gate above proves ref/value consistency. Endpoint existence and current
        # identifier classification require the catalog connection, so they live in this second,
        # state-aware gate. LLM-classified identifiers remain admissible; human review is not a
        # precondition.
        from featuregen.overlay.upload.bridge_candidates import bridge_catalog_write_error

        bridge_err = bridge_catalog_write_error(conn, ref)
        if bridge_err is not None:
            return CommandResult(
                accepted=False, aggregate_id="", denied_reason=bridge_err)
    key = fact_key(ref, fact_type, use_case)
    fp = proposal_fingerprint(
        proposed_value,
        profile_version=args.get("profile_version"),
        thresholds=args.get("thresholds"),
    )
    existing = load_fact(conn, key)
    state = fold_overlay_state(existing)
    if state.status in _NON_TERMINAL:
        latest = _latest_proposed(existing)
        if latest is not None and latest.payload.get("proposal_fingerprint") == fp:
            return CommandResult(
                accepted=False,
                aggregate_id=key,
                denied_reason="duplicate of a pending proposal (same fingerprint)",
            )
        return CommandResult(
            accepted=False,
            aggregate_id=key,
            denied_reason=(
                f"a non-terminal fact already exists (status={state.status}); cannot re-propose"
            ),
        )
    if state.status == "REJECTED":
        rejected_fps = {
            e.payload.get("retired_fingerprint")
            for e in existing
            if e.type == "OVERLAY_FACT_REJECTED"
        }
        if fp in rejected_fps:
            return CommandResult(
                accepted=False,
                aggregate_id=key,
                denied_reason=(
                    "fingerprint previously rejected (sticky); change the proposal to re-submit"
                ),
            )
    # Mint evidence atomically with the accepted append. Every deny path above returns before
    # this point, so no evidence row is written for a denied proposal. If a concurrent tx commits a
    # non-terminal fact for this key between the load_fact above and the append below, append_event's
    # OCC raises ConcurrencyError and this INSERT rolls back with the rest of the transaction —
    # either way there is no orphan evidence.
    if evidence_ref is None and evidence_payload is not None:
        try:
            raw_evidence_args = _raw_evidence_args(evidence_payload)
        except ValueError as exc:
            return CommandResult(
                accepted=False,
                aggregate_id=key,
                denied_reason=f"invalid raw evidence: {exc}",
            )
        evidence_ref = write_evidence(
            conn,
            fact_key=key,
            created_by=identity_to_jsonb(cmd.actor),  # a dict, never a raw IdentityEnvelope
            **raw_evidence_args,
        )
    authority = resolve_authority(conn, adapter, ref, fact_type)
    payload: dict[str, object] = {
        "catalog_object_ref": asdict(ref),
        "object_ref": display_object_ref(ref),
        "fact_type": fact_type,
        "use_case": use_case,
        "proposed_value": proposed_value,
        "proposal_fingerprint": fp,
        "evidence_ref": evidence_ref,
        "proposed_by": cmd.actor.subject,
    }
    # SOURCE-provenance four-eyes (program-audit F2/F10): an ingest stage proposing an
    # uploader-authored value under the SERVICE actor records the uploading HUMAN principal here;
    # `confirm_fact` (via `uploader_ne_confirmer`) bars that principal from confirming their own
    # declared value. Absent for every other caller — the payload is byte-identical when unset.
    if args.get("source_uploader"):
        payload["source_uploader"] = args["source_uploader"]
    draft = append_overlay_event(
        conn,
        fact_key=key,
        type="OVERLAY_FACT_PROPOSED",
        payload=payload,
        actor=cmd.actor,
        # Pin OCC to the observed head: a fresh propose expects an empty stream (0); the only
        # non-fresh propose that proceeds is a re-propose after REJECTED — pin it to the rejected
        # head so a concurrent re-propose collides cleanly instead of appending a duplicate DRAFT.
        expected_version=0 if not existing else existing[-1].stream_version,
    )
    # One task per resolved side: a known side -> the data owner; an unknown side ->
    # the platform-admin/governance queue. `task_assignees` dedupes same-owner / both-unknown.
    for eligible in authority.task_assignees:
        # dict(eligible) infers dict[str, object] (EligibleAssignee is a TypedDict); every value is a
        # str Literal (role/subject/side), so narrowing to the Mapping[str, str] the spec wants is
        # sound — a pure annotation, no runtime change.
        assignees = cast("dict[str, str]", dict(eligible))
        open_task(
            conn,
            GateTaskSpec(
                gate=authority.gate,
                required_inputs=("proposed_value",),
                eligible_assignees=assignees,
                allowed_responses=("confirm", "reject"),
                fact_key=key,
                draft_event_id=draft.event_id,
                target_event_id=draft.event_id,
                evidence_ref=evidence_ref,
            ),
            cmd.actor,
        )
    return CommandResult(accepted=True, aggregate_id=key, produced_event_ids=(draft.event_id,))
