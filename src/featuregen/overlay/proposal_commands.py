"""The proposal entry-point command (SP-1 design §6).

Houses `propose_fact` — the proposal handler that validates a fact value, enforces replacement
semantics, mints evidence atomically, appends `OVERLAY_FACT_PROPOSED`, and opens
one human-gate task per resolved authority side. Lifted out of `commands.py`; `commands` re-exports
it (and references it from `_OVERLAY_CATALOG`) so existing `featuregen.overlay.commands` imports keep
resolving.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import cast

from featuregen.contracts import Command, CommandResult, DbConn
from featuregen.contracts.gates import GateTaskSpec
from featuregen.contracts.identity import identity_to_jsonb
from featuregen.gates.tasks import open_task
from featuregen.overlay._lifecycle import _NON_TERMINAL, _latest_proposed
from featuregen.overlay._types import FactType
from featuregen.overlay.authority import resolve_authority
from featuregen.overlay.catalog import current_catalog_adapter
from featuregen.overlay.evidence import write_evidence
from featuregen.overlay.facts import FactValidationError, validate_fact_value
from featuregen.overlay.identity import (
    display_object_ref,
    fact_key,
    join_write_error,
    proposal_fingerprint,
)
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import append_overlay_event, load_fact


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
        evidence_ref = write_evidence(
            conn,
            fact_key=key,
            table_snapshot_at=evidence_payload["table_snapshot_at"],
            row_count=evidence_payload["row_count"],
            sample_size=evidence_payload["sample_size"],
            profile_version=evidence_payload["profile_version"],
            thresholds_used=evidence_payload["thresholds"],
            metric_values=evidence_payload["metric_values"],
            created_by=identity_to_jsonb(cmd.actor),  # a dict, never a raw IdentityEnvelope
        )
    authority = resolve_authority(conn, adapter, ref, fact_type)
    draft = append_overlay_event(
        conn,
        fact_key=key,
        type="OVERLAY_FACT_PROPOSED",
        payload={
            "catalog_object_ref": asdict(ref),
            "object_ref": display_object_ref(ref),
            "fact_type": fact_type,
            "use_case": use_case,
            "proposed_value": proposed_value,
            "proposal_fingerprint": fp,
            "evidence_ref": evidence_ref,
            "proposed_by": cmd.actor.subject,
        },
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
