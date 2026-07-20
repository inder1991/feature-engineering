"""The confirm / reject / direct-entry command handlers (SP-1 design §6.3, §3.4).

Houses `confirm_fact` (proposed -> VERIFIED, dispatching a dual-owner approved_join to
`join_confirmation._confirm_approved_join`), `reject_fact` (proposed -> REJECTED, recording the
sticky `retired_fingerprint`), and `enter_fact` (a human authority's direct self-confirm of an
owner-known fact, §3.4). Lifted out of `commands.py`; `commands` re-exports the three handlers (and
references them from `_OVERLAY_CATALOG`) so existing `featuregen.overlay.commands` imports keep
resolving.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime

from featuregen.contracts import Command, CommandResult, DbConn
from featuregen.overlay._lifecycle import (
    _AWAITING_CONFIRMATION,
    _cas_target,
    _close_fact_tasks,
    _deny_audited,
    _latest_proposed,
    resolve_ttl,
    within_renewal_grace,
)
from featuregen.overlay._types import Confirmer, FactType, Role
from featuregen.overlay.authority import (
    _actor_is_authority,
    proposer_ne_confirmer,
    resolve_authority,
)
from featuregen.overlay.catalog import current_catalog_adapter
from featuregen.overlay.config import current_overlay_config
from featuregen.overlay.expiry import demote_projected_join_edges, schedule_expiry
from featuregen.overlay.facts import FactValidationError, validate_fact_value
from featuregen.overlay.identity import (
    display_object_ref,
    fact_key,
    join_write_error,
    proposal_fingerprint,
)
from featuregen.overlay.join_confirmation import _confirm_approved_join
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import append_overlay_event, load_fact


def confirm_fact(conn: DbConn, cmd: Command) -> CommandResult:
    """Confirm a proposed fact → VERIFIED (§6.3). A **human** authority only; CAS on
    `target_event_id`; fine-grained authority (owner-of-object / Compliance / governance
    platform-admin); four-eyes (proposer ≠ confirmer); the FINAL value (override or original) is
    validated BEFORE OVERLAY_FACT_CONFIRMED is appended. On success it arms the
    overlay_expiry timer and closes the open task."""
    if cmd.actor.actor_kind != "human":
        return _deny_audited(
            conn, cmd, cmd.aggregate_id or "", "confirm_fact requires a human authority"
        )
    adapter = current_catalog_adapter()
    args = cmd.args
    ref = args["ref"]
    fact_type: FactType = args["fact_type"]
    use_case = args.get("use_case")
    key = fact_key(ref, fact_type, use_case)
    stream = load_fact(conn, key)
    if not stream:
        return CommandResult(accepted=False, aggregate_id=key, denied_reason="fact does not exist")
    state = fold_overlay_state(stream)
    # A VERIFIED fact inside its pre-expiry renewal grace window IS re-confirmable (SP-1.5 Task 6) —
    # this is what prevents the recurring expiry outage. Authority + four-eyes + CAS below are
    # UNCHANGED, so renewal preserves every guard (F8: a self-entered fact still needs a 2nd signer).
    renewing = within_renewal_grace(state, datetime.now(UTC))
    if state.status not in _AWAITING_CONFIRMATION and not renewing:
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
    # PARTIALLY_CONFIRMED -> VERIFIED flow (§6.4). Same-owner-both-sides has
    # `authority.dual` False and falls through to the single path below.
    if fact_type == "approved_join" and authority.dual:
        # SP-1.5 review fix: a still-VERIFIED dual join must NOT renew in place. The two-step join
        # confirm would regress VERIFIED -> PARTIALLY_CONFIRMED for a live fact, opening a
        # drift-laundering + expiry-skip window. Dual joins re-verify via the normal expiry/reverify
        # flow (from a non-live STALE/REVERIFY state), where the two-step is safe.
        if renewing and state.status == "VERIFIED":
            return CommandResult(
                accepted=False,
                aggregate_id=key,
                denied_reason=(
                    "dual-owner approved_join cannot renew in place; it re-verifies via the "
                    "expiry/reverify flow"
                ),
            )
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
    # (override or original) BEFORE appending OVERLAY_FACT_CONFIRMED so a malformed
    # correction can never be persisted as a confirmed fact. With NO override, a re-verify
    # re-affirms the LAST VERIFIED value (state.prior_value) — defaulting to the cycle-1 proposed
    # value would silently revert a prior human correction. A fresh DRAFT has no prior value,
    # so it defaults to the proposed value.
    if state.status == "VERIFIED":
        default_value = state.value  # renewal within grace: re-affirm the CURRENT confirmed value
    elif state.status in ("REVERIFY", "STALE"):
        default_value = state.prior_value
    else:
        default_value = proposed.payload["proposed_value"]
    value = args.get("value", default_value)
    try:
        validate_fact_value(fact_type, value, use_case=use_case)
    except FactValidationError as exc:
        return CommandResult(
            accepted=False, aggregate_id=key, denied_reason=f"invalid confirmed value: {exc}"
        )
    # SP-1.5 review fix #1: a confirmer may OVERRIDE the value (args["value"]), so the FINAL value —
    # not just the proposal — must pass the same F4 + ref/value-consistency gate propose_fact and
    # enter_fact apply. Without this a legitimate owner of the ref's tables could VERIFY a
    # cross-catalog or different-tables approved_join value under the ref's fact_key.
    join_err = join_write_error(ref, fact_type, value, use_case)
    if join_err is not None:
        return _deny_audited(conn, cmd, key, join_err)
    # SP-1.5 Task 7 (+ review #5b): re-confirming a drift-STALEd OR expiry-REVERIFY fact must not
    # re-affirm a value whose object/column the catalog no longer has. Config-gated: full SP-1.5
    # hardening is active only when a deployment has sealed an OverlayConfig.
    if state.status in ("STALE", "REVERIFY"):
        try:
            current_overlay_config()
        except RuntimeError:
            pass  # no OverlayConfig sealed -> hardening off (backward-compat)
        else:
            # Task 0 (sealed-runtime fix): same dispatcher as the dual-join second confirm — the
            # graph answers existence under the sentinel UploadContextAdapter, referent_gap for a
            # real adapter (byte-for-byte the prior behavior). Lazy import: overlay ->
            # overlay/upload at module load would cycle (mirrors expiry.py's passc import).
            from featuregen.overlay.upload.join_referents import check_referents_exist

            gap = check_referents_exist(conn, adapter, ref, fact_type, value)
            if gap is not None:
                return _deny_audited(conn, cmd, key, f"stale re-confirm blocked: {gap}")
    confirmers: list[Confirmer]
    if fact_type == "approved_join":
        # same-owner-both-sides reaches the single path (Authority.dual is False); record BOTH side
        # roles for the one principal so audit attribution matches a two-owner join.
        confirmers = [
            {"subject": cmd.actor.subject, "role": "data_owner_from"},
            {"subject": cmd.actor.subject, "role": "data_owner_to"},
        ]
    else:
        role: Role = "compliance" if fact_type == "policy_tag" else "data_owner"
        confirmers = [{"subject": cmd.actor.subject, "role": role}]
    expires_at = datetime.now(UTC) + resolve_ttl(fact_type, key)
    confirmed = append_overlay_event(
        conn,
        fact_key=key,
        type="OVERLAY_FACT_CONFIRMED",
        payload={
            "value": value,
            "confirmers": confirmers,
            "expires_at": expires_at.isoformat(),
            "confirms_event_id": args["target_event_id"],
            "note": args.get("note"),
        },
        actor=cmd.actor,
        caused_by=args["target_event_id"],
        # Pin OCC to the head this handler folded against: the target_event_id compare is only
        # a pre-append read; re-asserting the version atomically stops two concurrent authorities
        # from both landing a CONFIRMED (lost-update). Mirrors freshness.py _apply_expiry/_stale_one.
        expected_version=stream[-1].stream_version,
    )
    # Arm the SP-0 overlay_expiry timer on this fact-key stream.
    schedule_expiry(conn, key, confirmed.event_id, expires_at)
    _close_fact_tasks(conn, key, reason="fact confirmed")
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
    fact_type: FactType = args["fact_type"]
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
    # Retire the fingerprint of the value actually under review (mirrors confirm_fact).
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
            "category": args.get("category"),
            "target_event_id": args["target_event_id"],
            "retired_fingerprint": retired_fp,
        },
        actor=cmd.actor,
        caused_by=args["target_event_id"],
        # Pin OCC to the folded head: stops a confirm-vs-reject lost-update on the same head.
        expected_version=stream[-1].stream_version,
    )
    if fact_type == "approved_join":
        # Async demotion hook (Phase 3A Task 8): a pre-VERIFIED reject has no projected edge (the
        # UPDATE matches nothing); a REVERIFY/STALE reject re-stamps the demoted edge REJECTED.
        demote_projected_join_edges(conn, key, "REJECTED")
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
    fact_type: FactType = args["fact_type"]
    use_case = args.get("use_case")
    proposed_value = args["proposed_value"]
    try:
        validate_fact_value(fact_type, proposed_value, use_case=use_case)
    except FactValidationError as exc:
        return CommandResult(
            accepted=False, aggregate_id="", denied_reason=f"invalid fact value: {exc}"
        )
    join_err = join_write_error(ref, fact_type, proposed_value, use_case)  # SP-1.5 F4 + consistency
    if join_err is not None:
        return CommandResult(accepted=False, aggregate_id="", denied_reason=join_err)
    key = fact_key(ref, fact_type, use_case)
    # Delivery E four-eyes (E1): a governed semantic fact may NOT be single-party self-confirmed —
    # one principal must not both propose AND approve the same value. enter_fact is the audited
    # single-party exception to four-eyes; deny it here so these types always take the two-party
    # propose->confirm flow (service/LLM proposer + a DISTINCT human confirmer).
    if fact_type in ("entity_assignment", "currency_binding"):
        return _deny_audited(
            conn, cmd, key,
            "governed semantic fact requires the two-party propose/confirm flow (four-eyes)",
        )
    authority = resolve_authority(conn, adapter, ref, fact_type)
    # Direct self-confirm is restricted to OWNER-KNOWN facts (§3.4): there is no platform-admin
    # enter_fact authz row (only data_owner/compliance), so an unowned fact — which resolves to the
    # governance/platform-admin queue — must NOT be single-party self-asserted. A principal
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
    confirmers: list[Confirmer]
    if fact_type == "approved_join":
        # Same-owner-both-sides reaches this path (Authority.dual is False); record BOTH side roles
        # for the one principal so audit attribution matches a two-owner join.
        confirmers = [
            {"subject": cmd.actor.subject, "role": "data_owner_from"},
            {"subject": cmd.actor.subject, "role": "data_owner_to"},
        ]
    else:
        role: Role = "compliance" if fact_type == "policy_tag" else "data_owner"
        confirmers = [{"subject": cmd.actor.subject, "role": role}]
    expires_at = datetime.now(UTC) + resolve_ttl(fact_type, key)
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
    # A self-confirmed fact reaches VERIFIED too, so it also gets an overlay_expiry timer.
    schedule_expiry(conn, key, confirmed.event_id, expires_at)
    return CommandResult(
        accepted=True, aggregate_id=key, produced_event_ids=(draft.event_id, confirmed.event_id)
    )
