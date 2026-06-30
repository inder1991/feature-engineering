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
from datetime import UTC, datetime, timedelta

from featuregen.commands.registry import get_command, register_command
from featuregen.contracts import Command, CommandResult, DbConn
from featuregen.contracts.gates import GateTaskSpec
from featuregen.gates.tasks import cancel_task, open_task
from featuregen.overlay.authority import (
    _actor_is_authority,
    proposer_ne_confirmer,
    resolve_authority,
)
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

# Statuses from which a fact is still awaiting a confirm/reject decision. VERIFIED is excluded
# (it is replaced via its own re-verify flow); REJECTED is terminal.
_AWAITING_CONFIRMATION = ("DRAFT", "PARTIALLY_CONFIRMED", "REVERIFY", "STALE")

# Default re-verify horizon stamped onto OVERLAY_FACT_CONFIRMED and armed as the overlay_expiry
# timer (decision 5; the design calls this a "configurable horizon"). 180 days = semi-annual.
_DEFAULT_TTL = timedelta(days=180)


class OverlayCommandError(Exception):
    """Raised on overlay command misconfiguration / unauthorized task reads."""


def _latest_proposed(stream):
    """The most recent `OVERLAY_FACT_PROPOSED` event in `stream`, or None."""
    for e in reversed(list(stream)):
        if e.type == "OVERLAY_FACT_PROPOSED":
            return e
    return None


def _cas_target(state) -> str | None:
    """The event id a confirm/reject must CAS against — the current head of the fact (§6.3).

    DRAFT/PARTIALLY_CONFIRMED bind to the open draft (`draft_event_id`); REVERIFY/STALE bind to
    the confirmed event being re-verified (`confirmed_event_id`). A `target_event_id` that does not
    match this id has been superseded by a newer draft/confirmation and is denied as stale."""
    if state.status in ("DRAFT", "PARTIALLY_CONFIRMED"):
        return state.draft_event_id
    return state.confirmed_event_id


def _close_fact_tasks(conn: DbConn, fact_key: str, *, reason: str) -> None:
    """Cancel every OPEN human-gate task for `fact_key` (and void its scheduled timers), called by
    the same confirm/reject command that resolves the fact so the task is never left dangling."""
    rows = conn.execute(
        "SELECT task_id FROM human_tasks WHERE fact_key=%s AND status='open'",
        (fact_key,),
    ).fetchall()
    for (task_id,) in rows:
        cancel_task(conn, task_id, reason=reason)


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


def confirm_fact(conn: DbConn, cmd: Command) -> CommandResult:
    """Confirm a proposed fact → VERIFIED (§6.3). A **human** authority only; CAS on
    `target_event_id`; fine-grained authority (owner-of-object / Compliance / governance
    platform-admin); four-eyes (proposer ≠ confirmer); the FINAL value (override or original) is
    validated BEFORE OVERLAY_FACT_CONFIRMED is appended (pin 17). On success it arms the
    overlay_expiry timer (decision 5) and closes the open task."""
    if cmd.actor.actor_kind != "human":
        return CommandResult(
            accepted=False,
            aggregate_id=cmd.aggregate_id or "",
            denied_reason="confirm_fact requires a human authority",
        )
    adapter = current_catalog_adapter()
    args = cmd.args
    ref = args["ref"]
    fact_type = args["fact_type"]
    use_case = args.get("use_case")
    key = fact_key(ref, fact_type, use_case)
    stream = load_fact(conn, key)
    if not stream:
        return CommandResult(accepted=False, aggregate_id=key, denied_reason="fact does not exist")
    state = fold_overlay_state(stream)
    if state.status not in _AWAITING_CONFIRMATION:
        return CommandResult(
            accepted=False,
            aggregate_id=key,
            denied_reason=f"fact not awaiting confirmation (status={state.status})",
        )
    if args.get("target_event_id") != _cas_target(state):
        return CommandResult(
            accepted=False,
            aggregate_id=key,
            denied_reason="stale confirmation: target_event_id has been superseded",
        )
    authority = resolve_authority(conn, adapter, ref, fact_type)
    if not _actor_is_authority(authority, cmd.actor):
        return CommandResult(
            accepted=False,
            aggregate_id=key,
            denied_reason="actor is not the resolved authority for this fact",
        )
    if not proposer_ne_confirmer(stream, cmd.actor):
        return CommandResult(
            accepted=False,
            aggregate_id=key,
            denied_reason="four-eyes: a proposer may not confirm the same fact",
        )
    proposed = _latest_proposed(stream)
    # The confirmer may override the value on a REVERIFY/STALE correction. Validate the FINAL value
    # (override or original) BEFORE appending OVERLAY_FACT_CONFIRMED (pin 17) so a malformed
    # correction can never be persisted as a confirmed fact.
    value = args.get("value", proposed.payload["proposed_value"])
    try:
        validate_fact_value(fact_type, value, use_case=use_case)
    except FactValidationError as exc:
        return CommandResult(
            accepted=False, aggregate_id=key, denied_reason=f"invalid confirmed value: {exc}"
        )
    if fact_type == "approved_join":
        # same-owner-both-sides reaches the single path (Authority.dual is False); record BOTH side
        # roles for the one principal so audit attribution matches a two-owner join (finding 4).
        # (The dual dispatch + _confirm_approved_join are Task 4.5.)
        confirmers = [
            {"subject": cmd.actor.subject, "role": "data_owner_from"},
            {"subject": cmd.actor.subject, "role": "data_owner_to"},
        ]
    else:
        role = "compliance" if fact_type == "policy_tag" else "data_owner"
        confirmers = [{"subject": cmd.actor.subject, "role": role}]
    expires_at = datetime.now(UTC) + _DEFAULT_TTL
    confirmed = append_overlay_event(
        conn,
        fact_key=key,
        type="OVERLAY_FACT_CONFIRMED",
        payload={
            "value": value,
            "confirmers": confirmers,
            "expires_at": expires_at.isoformat(),
            "confirms_event_id": args["target_event_id"],
        },
        actor=cmd.actor,
        caused_by=args["target_event_id"],
    )
    # Arm the SP-0 overlay_expiry timer on this fact-key stream (decision 5; freshness.py is
    # created in this phase, Task 4.3, pin 10).
    from featuregen.overlay.freshness import (
        schedule_expiry,  # local import: freshness.py is created in Task 4.3
    )

    schedule_expiry(conn, key, confirmed.event_id, expires_at)
    _close_fact_tasks(conn, key, reason="fact confirmed")
    return CommandResult(accepted=True, aggregate_id=key, produced_event_ids=(confirmed.event_id,))


def reject_fact(conn: DbConn, cmd: Command) -> CommandResult:
    """Reject a proposed fact → REJECTED (§6.3). A **human** authority only; CAS on
    `target_event_id`; fine-grained authority; records the rejected proposal's
    `retired_fingerprint` (sticky-denial fuel for propose_fact) and closes the open task."""
    if cmd.actor.actor_kind != "human":
        return CommandResult(
            accepted=False,
            aggregate_id=cmd.aggregate_id or "",
            denied_reason="reject_fact requires a human authority",
        )
    adapter = current_catalog_adapter()
    args = cmd.args
    ref = args["ref"]
    fact_type = args["fact_type"]
    use_case = args.get("use_case")
    key = fact_key(ref, fact_type, use_case)
    stream = load_fact(conn, key)
    if not stream:
        return CommandResult(accepted=False, aggregate_id=key, denied_reason="fact does not exist")
    state = fold_overlay_state(stream)
    if state.status not in _AWAITING_CONFIRMATION:
        return CommandResult(
            accepted=False,
            aggregate_id=key,
            denied_reason=f"fact not awaiting confirmation (status={state.status})",
        )
    if args.get("target_event_id") != _cas_target(state):
        return CommandResult(
            accepted=False,
            aggregate_id=key,
            denied_reason="stale rejection: target_event_id has been superseded",
        )
    authority = resolve_authority(conn, adapter, ref, fact_type)
    if not _actor_is_authority(authority, cmd.actor):
        return CommandResult(
            accepted=False,
            aggregate_id=key,
            denied_reason="actor is not the resolved authority for this fact",
        )
    proposed = _latest_proposed(stream)
    retired_fp = proposed.payload.get("proposal_fingerprint") if proposed else None
    rejected = append_overlay_event(
        conn,
        fact_key=key,
        type="OVERLAY_FACT_REJECTED",
        payload={
            "rejected_by": cmd.actor.subject,
            "reason": args.get("reason"),
            "target_event_id": args["target_event_id"],
            "retired_fingerprint": retired_fp,
        },
        actor=cmd.actor,
        caused_by=args["target_event_id"],
    )
    _close_fact_tasks(conn, key, reason="fact rejected")
    return CommandResult(accepted=True, aggregate_id=key, produced_event_ids=(rejected.event_id,))


def enter_fact(conn: DbConn, cmd: Command) -> CommandResult:
    """Direct/proactive entry (§3.4): a HUMAN resolved authority self-confirms an owner-known fact.
    An audited exception to four-eyes — never available to a service/profiler proposal, and never to
    a dual-owner approved_join (which must use the two-task flow, §6.4)."""
    adapter = current_catalog_adapter()
    args = cmd.args
    if cmd.actor.actor_kind != "human":
        return CommandResult(
            accepted=False,
            aggregate_id="",
            denied_reason="self-confirm (enter_fact) requires a human authority",
        )
    ref = args["ref"]
    fact_type = args["fact_type"]
    use_case = args.get("use_case")
    proposed_value = args["proposed_value"]
    try:
        validate_fact_value(fact_type, proposed_value, use_case=use_case)
    except FactValidationError as exc:
        return CommandResult(
            accepted=False, aggregate_id="", denied_reason=f"invalid fact value: {exc}"
        )
    key = fact_key(ref, fact_type, use_case)
    authority = resolve_authority(conn, adapter, ref, fact_type)
    if authority.dual:
        return CommandResult(
            accepted=False,
            aggregate_id=key,
            denied_reason="dual-owner approved_join cannot be self-confirmed; use the two-task flow",
        )
    if not _actor_is_authority(authority, cmd.actor):
        return CommandResult(
            accepted=False,
            aggregate_id=key,
            denied_reason="actor is not the resolved authority for this fact",
        )
    if load_fact(conn, key):
        return CommandResult(
            accepted=False, aggregate_id=key, denied_reason="fact already exists; use propose/confirm"
        )
    fp = proposal_fingerprint(proposed_value)
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
            "evidence_ref": None,
            "proposed_by": cmd.actor.subject,
        },
        actor=cmd.actor,
        expected_version=0,
    )
    if fact_type == "approved_join":
        # Same-owner-both-sides reaches this path (Authority.dual is False); record BOTH side roles
        # for the one principal so audit attribution matches a two-owner join (finding 4).
        confirmers = [
            {"subject": cmd.actor.subject, "role": "data_owner_from"},
            {"subject": cmd.actor.subject, "role": "data_owner_to"},
        ]
    else:
        role = "compliance" if fact_type == "policy_tag" else "data_owner"
        confirmers = [{"subject": cmd.actor.subject, "role": role}]
    expires_at = datetime.now(UTC) + _DEFAULT_TTL
    confirmed = append_overlay_event(
        conn,
        fact_key=key,
        type="OVERLAY_FACT_CONFIRMED",
        payload={
            "value": proposed_value,
            "confirmers": confirmers,
            "expires_at": expires_at.isoformat(),
            "confirms_event_id": draft.event_id,
        },
        actor=cmd.actor,
        caused_by=draft.event_id,
    )
    # A self-confirmed fact reaches VERIFIED too, so it also gets an overlay_expiry timer (decision 5).
    from featuregen.overlay.freshness import (
        schedule_expiry,  # local import: freshness.py is created in Task 4.3
    )

    schedule_expiry(conn, key, confirmed.event_id, expires_at)
    return CommandResult(
        accepted=True, aggregate_id=key, produced_event_ids=(draft.event_id, confirmed.event_id)
    )


# `_OVERLAY_CATALOG` is a TUPLE of (action, handler) pairs (pin 12 — mirrors SP-0's `_CATALOG`),
# NOT a dict. Phase 6 appends ("run_profiler", ...).
_OVERLAY_CATALOG = (
    ("propose_fact", propose_fact),
    ("confirm_fact", confirm_fact),
    ("reject_fact", reject_fact),
    ("enter_fact", enter_fact),
)


def register_overlay_commands() -> None:
    """Idempotent (decision 8): `register_command` raises on duplicate and the command registry
    persists across tests (the root harness resets only the event registry), so skip any action
    that is already registered instead of re-registering it."""
    for action, handler in _OVERLAY_CATALOG:
        try:
            get_command(action)
        except KeyError:
            register_command(action, handler)
