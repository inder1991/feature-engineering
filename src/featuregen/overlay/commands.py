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
from featuregen.contracts.identity import identity_to_jsonb
from featuregen.gates.tasks import cancel_task, open_task
from featuregen.overlay.authority import (
    _actor_is_authority,
    proposer_ne_confirmer,
    resolve_authority,
)
from featuregen.overlay.catalog import current_catalog_adapter
from featuregen.overlay.evidence import read_evidence, write_evidence
from featuregen.overlay.facts import FactValidationError, validate_fact_value
from featuregen.overlay.identity import (
    CatalogObjectRef,
    display_object_ref,
    fact_key,
    proposal_fingerprint,
)
from featuregen.overlay.profiler import ProfilerLimits, run_profiler_scan
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

    DRAFT binds to the open draft (`draft_event_id`); REVERIFY/STALE bind to the confirmed event
    being re-verified (`confirmed_event_id`). A `target_event_id` that does not match this id has
    been superseded by a newer draft/confirmation and is denied as stale."""
    if state.status == "DRAFT":
        return state.draft_event_id
    if state.status == "PARTIALLY_CONFIRMED":
        # Re-verify cycle carries the prior confirmed_event_id (the id the per-side re-verify tasks
        # are stamped with by freshness.open_reverify_task); keep it as the cycle-stable CAS target
        # so the SECOND re-confirmer's task-scoped target still matches after the first partial (P1a).
        # The initial cycle has no prior confirmation (cleared on PROPOSED) -> bind to the open draft.
        return state.confirmed_event_id or state.draft_event_id
    return state.confirmed_event_id


def _close_fact_tasks(
    conn: DbConn, fact_key: str, *, reason: str, subject: str | None = None
) -> None:
    """Cancel OPEN human-gate tasks for `fact_key` (and void their scheduled timers), called by
    the same confirm/reject command that resolves the fact so a task is never left dangling.

    With `subject` set, only that assignee's side task is closed (matched on the task's
    `eligible_assignees->>'subject'`) — used by the approved_join PARTIALLY_CONFIRMED step so the
    OTHER side's task stays open for the second owner (decision 7). Default closes every open task."""
    if subject is None:
        rows = conn.execute(
            "SELECT task_id FROM human_tasks WHERE fact_key=%s AND status='open'",
            (fact_key,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT task_id FROM human_tasks "
            "WHERE fact_key=%s AND status='open' AND eligible_assignees->>'subject'=%s",
            (fact_key, subject),
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
        # Pin OCC to the observed head (I4): a fresh propose expects an empty stream (0); the only
        # non-fresh propose that proceeds is a re-propose after REJECTED — pin it to the rejected
        # head so a concurrent re-propose collides cleanly instead of appending a duplicate DRAFT.
        expected_version=0 if not existing else existing[-1].stream_version,
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
    # Dual-owner approved_join (two distinct confirmations required) follows the two-step
    # PARTIALLY_CONFIRMED -> VERIFIED flow (Task 4.5, §6.4). Same-owner-both-sides has
    # `authority.dual` False and falls through to the single path below.
    if fact_type == "approved_join" and authority.dual:
        return _confirm_approved_join(conn, cmd, key, stream, state, authority)
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
        return CommandResult(
            accepted=False, aggregate_id=key, denied_reason="actor is not an owner of either side of the join"
        )
    if not proposer_ne_confirmer(stream, actor):
        return CommandResult(
            accepted=False, aggregate_id=key, denied_reason="proposer may not confirm (four-eyes, §6.5)"
        )
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
        return CommandResult(
            accepted=False,
            aggregate_id=key,
            denied_reason="this owner already confirmed; awaiting the other owner",
        )
    # Side coverage (finding 3): when one side has a KNOWN owner and the other routes to the
    # governance queue, the two confirmations must be one owner + one platform-admin. Two
    # platform-admins must NOT verify a join that has a known owner (that bypasses the owner's side).
    if authority.governance_queue and owners:
        if first not in owners and actor.subject not in owners:
            return CommandResult(
                accepted=False,
                aggregate_id=key,
                denied_reason="a known owner must confirm their side of the join",
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
    confirmed = append_overlay_event(
        conn,
        fact_key=key,
        type="OVERLAY_FACT_CONFIRMED",
        payload={
            "value": value,
            "confirmers": _join_confirmers(authority, first, actor.subject),
            "expires_at": expires_at.isoformat(),
            "confirms_event_id": state.draft_event_id,
        },
        actor=actor,
        caused_by=state.draft_event_id,
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
    # Direct self-confirm is restricted to OWNER-KNOWN facts (§3.4): there is no platform-admin
    # enter_fact authz row (only data_owner/compliance), so an unowned fact — which resolves to the
    # governance/platform-admin queue — must NOT be single-party self-asserted (I2). A principal
    # holding both data_owner and platform-admin would otherwise clear `_actor_is_authority`'s
    # governance branch and bypass the two-party propose->confirm path. Route it through propose.
    if authority.governance_queue:
        return CommandResult(
            accepted=False,
            aggregate_id=key,
            denied_reason="unowned (governance-queue) fact cannot be self-confirmed; use propose/confirm",
        )
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


def get_task_proposal(conn: DbConn, task_id: str, actor) -> dict:
    """Task-scoped proposal read (§7.2): returns what the assignee must see to confirm. Authorized
    to the task's assignee (eligible subject/role) or the governance role; denied to anyone else —
    distinct from the deferred end-user `resolve_fact` authz.

    NOT a registered command handler (no `_OVERLAY_CATALOG` entry) — a direct read function. The CAS
    target and prior value come from AUTHORITATIVE, synchronous sources (the `human_tasks` row and
    the event stream), NOT the asynchronous `overlay_proposal` projection (finding 1)."""
    row = conn.execute(
        "SELECT fact_key, eligible_assignees, evidence_ref, target_event_id, status "
        "FROM human_tasks WHERE task_id=%s",
        (task_id,),
    ).fetchone()
    if row is None:
        raise OverlayCommandError(f"unknown task {task_id}")
    key, eligible, evidence_ref, target_event_id, status = row
    if status != "open":
        raise OverlayCommandError(f"task {task_id} is not open (status={status})")
    eligible = eligible or {}
    role = eligible.get("role")
    subject = eligible.get("subject")
    # Subject-scoped authz (I3): when the task is bound to a specific subject (a known-owner data
    # fact's task is {"role":"data_owner","subject":<owner>}), ONLY that subject may read it — the
    # bare role must NOT also satisfy it, or any data_owner-role holder would read another team's
    # proposal + evidence, silently defeating the subject narrowing. The role branch survives only
    # for SUBJECT-LESS governance/compliance tasks. Mirrors the confirm handler's subject-scoping.
    if subject is not None:
        authorized = actor.subject == subject
    else:
        authorized = role is not None and role in actor.role_claims
    # A platform-admin reads a GOVERNANCE task via the role branch above (its eligible role is
    # "platform-admin"); it is NOT granted blanket read of every task's proposal (finding 4).
    if not authorized:
        raise OverlayCommandError("actor is not authorized to read this task proposal")
    stream = load_fact(conn, key)
    proposed = _latest_proposed(stream)
    if proposed is None:
        raise OverlayCommandError(f"task {task_id} has no proposal on its fact stream")
    p = proposed.payload
    # `target_event_id` is stamped on the task at open time (the draft id for a fresh DRAFT; the
    # confirmed id under re-verification); the prior verified value is folded from the stream.
    prior_value = fold_overlay_state(stream).prior_value
    return {
        "object_ref": p["object_ref"],
        "fact_type": p["fact_type"],
        "use_case": p.get("use_case"),
        "proposed_value": p["proposed_value"],
        "prior_value": prior_value,
        "target_event_id": target_event_id,
        "evidence": read_evidence(conn, evidence_ref) if evidence_ref else None,
    }


def _existing_proposal_fingerprint(conn: DbConn, fk: str) -> tuple[str | None, str | None]:
    """Return (folded status, latest-proposed fingerprint) for `fk` read from the AUTHORITATIVE
    event stream — NOT the asynchronous `overlay_proposal` projection (round-5 finding 2). Reading
    the stream means the profiler's preflight sees exactly what `propose_fact` will, so it never
    writes evidence for a proposal that `propose_fact` would then deny under projection lag."""
    stream = load_fact(conn, fk)
    if not stream:
        return None, None
    state = fold_overlay_state(stream)
    proposed = _latest_proposed(stream)
    fp = proposed.payload.get("proposal_fingerprint") if proposed else None
    return state.status, fp


def _run_profiler(conn: DbConn, cmd: Command) -> CommandResult:
    """Service command (§6.6): run the deterministic profiler over `cmd.args["ref"]` and, for each
    candidate, write evidence and issue a `propose_fact`. Runs inside `execute_command`'s
    transaction, so `run_profiler_scan`'s `SET LOCAL statement_timeout` applies to this scan.

    Stream-based preflight (round-5 finding 2): for each candidate the folded stream status decides
    BEFORE any evidence is written — a non-terminal fact (DRAFT/PARTIALLY_CONFIRMED/VERIFIED/
    REVERIFY/STALE) blocks any new proposal (decision 6), and a REJECTED fact with the SAME
    `proposal_fingerprint` is sticky-skipped (fresh evidence alone never revives it). Skipping first
    guarantees no orphan evidence is left for a candidate `propose_fact` would deny."""
    ref = CatalogObjectRef(**dict(cmd.args["ref"]))
    limits = ProfilerLimits(allowed_schemas=frozenset(cmd.args.get("allowed_schemas", ())))
    adapter = current_catalog_adapter()
    proposals = run_profiler_scan(conn, adapter, ref, limits=limits)

    propose = get_command("propose_fact")
    produced: list[str] = []
    for proposal in proposals:
        fk = fact_key(proposal.ref, proposal.fact_type, proposal.use_case)
        metrics = proposal.evidence_metrics
        # Compute the fingerprint the SAME way propose_fact will (proposed_value + profile_version +
        # thresholds), so the dedup below matches the fingerprint that would be appended.
        fingerprint = proposal_fingerprint(
            proposal.proposed_value,
            profile_version=metrics["profile_version"],
            thresholds=metrics["thresholds"],
        )
        status, existing_fp = _existing_proposal_fingerprint(conn, fk)
        # Preflight matching propose_fact's replacement semantics (decision 6) — skip BEFORE writing
        # evidence so the profiler never creates orphan evidence for a denied proposal.
        if status in _NON_TERMINAL:
            continue  # a non-terminal fact exists; propose_fact would deny ANY new proposal
        if status == "REJECTED" and existing_fp == fingerprint:
            continue  # sticky dedup: a rejected candidate is not re-proposed on identical value

        evidence_ref = write_evidence(
            conn,
            fact_key=fk,
            table_snapshot_at=metrics["table_snapshot_at"],
            row_count=metrics["row_count"],
            sample_size=metrics["sample_size"],
            profile_version=metrics["profile_version"],
            thresholds_used=metrics["thresholds"],
            metric_values=metrics["metric_values"],
            created_by=identity_to_jsonb(cmd.actor),  # pin 14: a dict, never a raw IdentityEnvelope
        )
        # Issue propose_fact using ITS exact arg contract (Phase 4): a live CatalogObjectRef under
        # "ref"; propose_fact derives the proposal_fingerprint itself from proposed_value +
        # profile_version + thresholds (matching `fingerprint` above).
        propose_cmd = Command(
            action="propose_fact",
            aggregate="overlay_fact",
            aggregate_id=fk,
            args={
                "ref": proposal.ref,
                "fact_type": proposal.fact_type,
                "use_case": proposal.use_case,
                "proposed_value": dict(proposal.proposed_value),
                "evidence_ref": evidence_ref,
                "profile_version": metrics["profile_version"],
                "thresholds": metrics["thresholds"],
            },
            actor=cmd.actor,
            idempotency_key=f"profiler:{fk}:{fingerprint}",
        )
        result = propose(conn, propose_cmd)
        if not result.accepted:
            continue  # defensive: a concurrent change made the fact non-terminal — do not count it
        produced.extend(result.produced_event_ids)

    return CommandResult(
        accepted=True,
        aggregate_id=display_object_ref(ref),
        produced_event_ids=tuple(produced),
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
