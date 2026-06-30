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
from datetime import UTC, datetime

from featuregen.commands.registry import get_command, register_command
from featuregen.contracts import Command, CommandResult, DbConn
from featuregen.overlay._lifecycle import (
    _AWAITING_CONFIRMATION,
    _DEFAULT_TTL,
    _cas_target,
    _close_fact_tasks,
    _deny_audited,
    _latest_proposed,
)
from featuregen.overlay._lifecycle import OverlayCommandError as OverlayCommandError
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
from featuregen.overlay.profiler_command import (
    _existing_proposal_fingerprint as _existing_proposal_fingerprint,
)
from featuregen.overlay.profiler_command import (
    _run_profiler as _run_profiler,
)
from featuregen.overlay.proposal_commands import propose_fact as propose_fact
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import append_overlay_event, load_fact
from featuregen.overlay.task_read import get_task_proposal as get_task_proposal


def confirm_fact(conn: DbConn, cmd: Command) -> CommandResult:
    """Confirm a proposed fact → VERIFIED (§6.3). A **human** authority only; CAS on
    `target_event_id`; fine-grained authority (owner-of-object / Compliance / governance
    platform-admin); four-eyes (proposer ≠ confirmer); the FINAL value (override or original) is
    validated BEFORE OVERLAY_FACT_CONFIRMED is appended (pin 17). On success it arms the
    overlay_expiry timer (decision 5) and closes the open task."""
    if cmd.actor.actor_kind != "human":
        return _deny_audited(
            conn, cmd, cmd.aggregate_id or "", "confirm_fact requires a human authority"
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
    # Dual-owner approved_join (two distinct confirmations required) follows the two-step
    # PARTIALLY_CONFIRMED -> VERIFIED flow (Task 4.5, §6.4). Same-owner-both-sides has
    # `authority.dual` False and falls through to the single path below.
    if fact_type == "approved_join" and authority.dual:
        return _confirm_approved_join(conn, cmd, key, stream, state, authority)
    if not _actor_is_authority(authority, cmd.actor):
        return _deny_audited(
            conn, cmd, key, "actor is not the resolved authority for this fact"
        )
    if not proposer_ne_confirmer(stream, cmd.actor):
        return _deny_audited(
            conn, cmd, key, "four-eyes: a proposer may not confirm the same fact"
        )
    proposed = _latest_proposed(stream)
    # The confirmer may override the value on a REVERIFY/STALE correction. Validate the FINAL value
    # (override or original) BEFORE appending OVERLAY_FACT_CONFIRMED (pin 17) so a malformed
    # correction can never be persisted as a confirmed fact. With NO override, a re-verify
    # re-affirms the LAST VERIFIED value (state.prior_value) — defaulting to the cycle-1 proposed
    # value would silently revert a prior human correction (P1b). A fresh DRAFT has no prior value,
    # so it defaults to the proposed value.
    default_value = (
        state.prior_value
        if state.status in ("REVERIFY", "STALE")
        else proposed.payload["proposed_value"]
    )
    value = args.get("value", default_value)
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
        # Pin OCC to the head this handler folded against (C2): the target_event_id compare is only
        # a pre-append read; re-asserting the version atomically stops two concurrent authorities
        # from both landing a CONFIRMED (lost-update). Mirrors freshness.py _apply_expiry/_stale_one.
        expected_version=stream[-1].stream_version,
    )
    # Arm the SP-0 overlay_expiry timer on this fact-key stream (decision 5; freshness.py is
    # created in this phase, Task 4.3, pin 10).
    from featuregen.overlay.freshness import (
        schedule_expiry,  # local import: freshness.py is created in Task 4.3
    )

    schedule_expiry(conn, key, confirmed.event_id, expires_at)
    _close_fact_tasks(conn, key, reason="fact confirmed")
    return CommandResult(accepted=True, aggregate_id=key, produced_event_ids=(confirmed.event_id,))


def _join_side(authority, subject) -> str:
    """The join side (`from`/`to`) a confirmer covers, derived from the side-ordered
    `authority.subjects` (None marks an unknown/governance side) — NOT confirmation order."""
    subs = list(authority.subjects)  # ordered (from, to)
    if subs and subs[0] == subject:
        return "from"
    if len(subs) > 1 and subs[1] == subject:
        return "to"
    if subs and subs[0] is None:        # a platform-admin covers the unknown side
        return "from"
    if len(subs) > 1 and subs[1] is None:
        return "to"
    return "from"


def _join_confirmers(authority, first_subject, second_subject) -> list:
    first_side = _join_side(authority, first_subject)
    second_side = _join_side(authority, second_subject)
    if first_side == second_side:       # both governance/unknown — disambiguate by order
        second_side = "to" if first_side == "from" else "from"
    return [
        {"subject": first_subject, "role": f"data_owner_{first_side}"},
        {"subject": second_subject, "role": f"data_owner_{second_side}"},
    ]


def _confirm_approved_join(conn, cmd, key, stream, state, authority):
    """Dual-owner approved_join confirmation (§6.4). The FIRST authorized confirmer appends
    OVERLAY_FACT_PARTIALLY_CONFIRMED (recording their side) and closes their task; the SECOND
    (a DISTINCT subject covering the OTHER side) validates the final value and appends
    OVERLAY_FACT_CONFIRMED recording BOTH confirmers, arms expiry, and closes the remaining task."""
    actor = cmd.actor
    owners = {s for s in authority.subjects if s}
    # A mixed join (one known owner + one unknown/governance side) requires a platform-admin to act
    # for the governance side; otherwise only the resolved owners may confirm (decision 7).
    is_owner = actor.subject in owners
    is_governance = authority.governance_queue and "platform-admin" in actor.role_claims
    if not (is_owner or is_governance):
        return _deny_audited(conn, cmd, key, "actor is not an owner of either side of the join")
    if not proposer_ne_confirmer(stream, actor):
        return _deny_audited(conn, cmd, key, "proposer may not confirm (four-eyes, §6.5)")
    # Decide first-vs-second from the CURRENT cycle's partials only (C1): the fold resets
    # state.partial_confirmers = [] on every OVERLAY_FACT_CONFIRMED, and EXPIRED/STALED leave it
    # empty, so a re-verify cycle starts with no partials. Scanning the raw stream would treat a
    # PRIOR cycle's PARTIALLY_CONFIRMED as this cycle's first confirm and bypass two-party SoD on
    # every re-verification (single re-confirm -> VERIFIED, or the cycle-1 first owner wrongly denied).
    partial = state.partial_confirmers
    proposed = _latest_proposed(stream)
    if not partial:
        evt = append_overlay_event(
            conn,
            fact_key=key,
            type="OVERLAY_FACT_PARTIALLY_CONFIRMED",
            payload={
                "by_owner": actor.subject,
                "role": f"data_owner_{_join_side(authority, actor.subject)}",
                "draft_event_id": state.draft_event_id,
            },
            actor=actor,
            caused_by=state.draft_event_id,
            # Pin OCC to the folded head (C2): otherwise both owners can load DRAFT, both take this
            # first-confirmer branch and both append PARTIALLY_CONFIRMED, permanently stranding the
            # join (no CONFIRMED, no open task). The loser collides and re-loads into the second path.
            expected_version=stream[-1].stream_version,
        )
        _close_fact_tasks(conn, key, subject=actor.subject, reason="first owner confirmed (partial)")
        return CommandResult(accepted=True, aggregate_id=key, produced_event_ids=(evt.event_id,))
    first = partial[-1]["subject"]
    if actor.subject == first:
        return _deny_audited(
            conn, cmd, key, "this owner already confirmed; awaiting the other owner"
        )
    # Side coverage (finding 3): when one side has a KNOWN owner and the other routes to the
    # governance queue, the two confirmations must be one owner + one platform-admin. Two
    # platform-admins must NOT verify a join that has a known owner (that bypasses the owner's side).
    if authority.governance_queue and owners:
        if first not in owners and actor.subject not in owners:
            return _deny_audited(
                conn, cmd, key, "a known owner must confirm their side of the join"
            )
    # Validate the FINAL value before the second-owner CONFIRMED append (pin 17 — the join confirm
    # path validates too, even though approved_join takes no override). On a re-verify, re-affirm
    # the last verified value (symmetry with confirm_fact, P1b); benign today since approved_join
    # takes no override (prior_value == proposed_value across cycles), but future-proof.
    value = (
        state.prior_value
        if state.status in ("REVERIFY", "STALE")
        else proposed.payload["proposed_value"]
    )
    try:
        validate_fact_value("approved_join", value)
    except FactValidationError as exc:
        return CommandResult(
            accepted=False, aggregate_id=key, denied_reason=f"invalid confirmed value: {exc}"
        )
    expires_at = datetime.now(UTC) + _DEFAULT_TTL
    # Thread the cycle-stable head (F7): _cas_target at PARTIALLY_CONFIRMED returns
    # `confirmed_event_id or draft_event_id` — cycle 1 yields the draft (unchanged), a re-verify
    # cycle yields the prior confirmed_event_id (the confirmation actually being re-verified), so the
    # recorded causality (confirms_event_id + caused_by) matches single-fact confirm_fact.
    confirms_event_id = _cas_target(state)
    confirmed = append_overlay_event(
        conn,
        fact_key=key,
        type="OVERLAY_FACT_CONFIRMED",
        payload={
            "value": value,
            "confirmers": _join_confirmers(authority, first, actor.subject),
            "expires_at": expires_at.isoformat(),
            "confirms_event_id": confirms_event_id,
        },
        actor=actor,
        caused_by=confirms_event_id,
        expected_version=stream[-1].stream_version,  # pin OCC to the folded head (C2)
    )
    # local import: freshness.py is created in Task 4.3 (avoids a top-level forward dependency)
    from featuregen.overlay.freshness import schedule_expiry

    schedule_expiry(conn, key, confirmed.event_id, expires_at)  # arm overlay_expiry (decision 5)
    _close_fact_tasks(conn, key, reason="join fully confirmed")
    return CommandResult(accepted=True, aggregate_id=key, produced_event_ids=(confirmed.event_id,))


def reject_fact(conn: DbConn, cmd: Command) -> CommandResult:
    """Reject a proposed fact → REJECTED (§6.3). A **human** authority only; CAS on
    `target_event_id`; fine-grained authority; records the rejected proposal's
    `retired_fingerprint` (sticky-denial fuel for propose_fact) and closes the open task."""
    if cmd.actor.actor_kind != "human":
        return _deny_audited(
            conn, cmd, cmd.aggregate_id or "", "reject_fact requires a human authority"
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
        return _deny_audited(
            conn, cmd, key, "actor is not the resolved authority for this fact"
        )
    proposed = _latest_proposed(stream)
    proposed_value = proposed.payload.get("proposed_value") if proposed else None
    # Retire the fingerprint of the value actually under review (F8, mirrors confirm_fact's P1b).
    # On a REVERIFY/STALE reject AFTER a confirm-time override, state.prior_value is the corrected
    # value V' — retire ITS fingerprint so sticky-reject protects V' (not the discarded cycle-1 V0).
    # The value-equality guard keeps the no-override path on the STORED proposal fingerprint, which
    # also encodes profile_version+thresholds so a profiler re-propose of the same value still sticks.
    if state.status in ("REVERIFY", "STALE") and state.prior_value not in (None, proposed_value):
        retired_fp = proposal_fingerprint(state.prior_value)
    else:
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
        # Pin OCC to the folded head (C2): stops a confirm-vs-reject lost-update on the same head.
        expected_version=stream[-1].stream_version,
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
        return _deny_audited(
            conn, cmd, "", "self-confirm (enter_fact) requires a human authority"
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
    # Direct self-confirm is restricted to OWNER-KNOWN facts (§3.4): there is no platform-admin
    # enter_fact authz row (only data_owner/compliance), so an unowned fact — which resolves to the
    # governance/platform-admin queue — must NOT be single-party self-asserted (I2). A principal
    # holding both data_owner and platform-admin would otherwise clear `_actor_is_authority`'s
    # governance branch and bypass the two-party propose->confirm path. Route it through propose.
    if authority.governance_queue:
        return _deny_audited(
            conn,
            cmd,
            key,
            "unowned (governance-queue) fact cannot be self-confirmed; use propose/confirm",
        )
    if authority.dual:
        return _deny_audited(
            conn,
            cmd,
            key,
            "dual-owner approved_join cannot be self-confirmed; use the two-task flow",
        )
    if not _actor_is_authority(authority, cmd.actor):
        return _deny_audited(
            conn, cmd, key, "actor is not the resolved authority for this fact"
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
    ("run_profiler", _run_profiler),
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
