"""Overlay command handlers (SP-1 design §6).

Task 4.2 lands `propose_fact` — the proposal entry point that validates a fact value, enforces
replacement semantics (decision 6), appends `OVERLAY_FACT_PROPOSED`, and opens one human-gate
task per resolved authority side. The `confirm_fact`/`reject_fact`/`enter_fact` handlers (and the
`_cas_target`/`_actor_is_authority`/`_close_fact_tasks` helpers + `freshness.schedule_expiry`
they need) land in Task 4.3; Phase 6 appends `("run_profiler", _run_profiler)` to
`_OVERLAY_CATALOG`.
"""
from __future__ import annotations

from dataclasses import asdict

from featuregen.commands.registry import get_command, register_command
from featuregen.contracts import Command, CommandResult, DbConn
from featuregen.contracts.gates import GateTaskSpec
from featuregen.gates.tasks import open_task
from featuregen.overlay.authority import resolve_authority
from featuregen.overlay.catalog import current_catalog_adapter
from featuregen.overlay.facts import FactValidationError, validate_fact_value
from featuregen.overlay.identity import (
    display_object_ref,
    fact_key,
    proposal_fingerprint,
)
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import append_overlay_event, load_fact

# Non-terminal folded statuses: while a fact sits in any of these a fresh proposal is denied —
# a live VERIFIED fact stays usable until its OWN re-verify flow replaces it (no VERIFIED->DRAFT
# regression). Only an empty stream or a REJECTED terminal admits a new proposal (decision 6).
_NON_TERMINAL = ("DRAFT", "PARTIALLY_CONFIRMED", "VERIFIED", "REVERIFY", "STALE")


class OverlayCommandError(Exception):
    """Raised on overlay command misconfiguration / unauthorized task reads."""


def _latest_proposed(stream):
    """The most recent `OVERLAY_FACT_PROPOSED` event in `stream`, or None."""
    for e in reversed(list(stream)):
        if e.type == "OVERLAY_FACT_PROPOSED":
            return e
    return None


def propose_fact(conn: DbConn, cmd: Command) -> CommandResult:
    """Validate and record a proposed fact, then open a human-gate task per authority side.

    Replacement semantics (decision 6): denied whenever a non-terminal fact already exists for the
    `fact_key`; only an empty stream or a REJECTED terminal admits a new proposal, and a previously
    rejected `proposal_fingerprint` stays sticky-denied.
    """
    adapter = current_catalog_adapter()
    args = cmd.args
    ref = args["ref"]
    fact_type = args["fact_type"]
    use_case = args.get("use_case")
    proposed_value = args["proposed_value"]
    evidence_ref = args.get("evidence_ref")
    try:
        validate_fact_value(fact_type, proposed_value, use_case=use_case)
    except FactValidationError as exc:
        return CommandResult(
            accepted=False, aggregate_id="", denied_reason=f"invalid fact value: {exc}"
        )
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
        expected_version=0 if not existing else None,
    )
    # One task per resolved side (decision 7): a known side -> the data owner; an unknown side ->
    # the platform-admin/governance queue. `task_assignees` dedupes same-owner / both-unknown.
    for eligible in authority.task_assignees:
        open_task(
            conn,
            GateTaskSpec(
                gate=authority.gate,
                required_inputs=("proposed_value",),
                eligible_assignees=dict(eligible),
                allowed_responses=("confirm", "reject"),
                fact_key=key,
                draft_event_id=draft.event_id,
                target_event_id=draft.event_id,
                evidence_ref=evidence_ref,
            ),
            cmd.actor,
        )
    return CommandResult(accepted=True, aggregate_id=key, produced_event_ids=(draft.event_id,))


# `_OVERLAY_CATALOG` is a TUPLE of (action, handler) pairs (pin 12 — mirrors SP-0's `_CATALOG`),
# NOT a dict. Task 4.3 inserts confirm_fact/reject_fact; Phase 6 appends ("run_profiler", ...).
_OVERLAY_CATALOG = (("propose_fact", propose_fact),)


def register_overlay_commands() -> None:
    """Idempotent (decision 8): `register_command` raises on duplicate and the command registry
    persists across tests (the root harness resets only the event registry), so skip any action
    that is already registered instead of re-registering it."""
    for action, handler in _OVERLAY_CATALOG:
        try:
            get_command(action)
        except KeyError:
            register_command(action, handler)
