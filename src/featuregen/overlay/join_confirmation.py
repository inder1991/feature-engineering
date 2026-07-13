"""Dual-owner approved_join confirmation (SP-1 design §6.4).

Houses `_confirm_approved_join` — the two-step PARTIALLY_CONFIRMED -> VERIFIED flow a dual-owner
`approved_join` takes (two distinct confirmations, one per side) — plus its join-only helpers
`_join_side`/`_join_confirmers`. Lifted out of `commands.py`; `commands` re-exports
`_confirm_approved_join` (and dispatches to it from `confirm_fact`) so existing
`featuregen.overlay.commands` imports keep resolving.
"""
from __future__ import annotations

from datetime import UTC, datetime

from featuregen.contracts import CommandResult
from featuregen.overlay._lifecycle import (
    _cas_target,
    _close_fact_tasks,
    _deny_audited,
    _latest_proposed,
    resolve_ttl,
)
from featuregen.overlay._types import JoinSide
from featuregen.overlay.authority import proposer_ne_confirmer
from featuregen.overlay.catalog import current_catalog_adapter
from featuregen.overlay.config import current_overlay_config
from featuregen.overlay.expiry import schedule_expiry
from featuregen.overlay.facts import FactValidationError, validate_fact_value
from featuregen.overlay.identity import _ref_from_payload
from featuregen.overlay.store import append_overlay_event


def _join_side(authority, subject) -> JoinSide:
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
    # for the governance side; otherwise only the resolved owners may confirm.
    is_owner = actor.subject in owners
    is_governance = authority.governance_queue and "platform-admin" in actor.role_claims
    if not (is_owner or is_governance):
        return _deny_audited(conn, cmd, key, "actor is not an owner of either side of the join")
    if not proposer_ne_confirmer(stream, actor):
        return _deny_audited(conn, cmd, key, "proposer may not confirm (four-eyes, §6.5)")
    # Decide first-vs-second from the CURRENT cycle's partials only: the fold resets
    # state.partial_confirmers = [] on every OVERLAY_FACT_CONFIRMED, and EXPIRED/STALED leave it
    # empty, so a re-verify cycle starts with no partials. Scanning the raw stream would treat a
    # PRIOR cycle's PARTIALLY_CONFIRMED as this cycle's first confirm and bypass two-party SoD on
    # every re-verification (single re-confirm -> VERIFIED, or the cycle-1 first owner wrongly denied).
    partial = state.partial_confirmers
    proposed = _latest_proposed(stream)
    # SP-1.5 Task 7 (review fix): a drift-STALEd dual join must not be re-confirmed while a referent
    # is missing — gate BOTH the first-partial and the second (VERIFIED) step. Config-gated.
    if state.status == "STALE":
        try:
            current_overlay_config()
        except RuntimeError:
            pass  # no OverlayConfig sealed -> hardening off (backward-compat)
        else:
            check_value = (
                state.prior_value
                if state.prior_value is not None
                else (proposed.payload["proposed_value"] if proposed else None)
            )
            ref = _ref_from_payload(stream[0].payload["catalog_object_ref"])
            # Task 0 (sealed-runtime fix, whole-branch review FIX 1): the SAME dispatcher as the
            # second-confirm gate below and the single-path STALE/REVERIFY gate — graph_node
            # answers existence under the sentinel UploadContextAdapter (whose empty fingerprint
            # fail-closed EVERY real join: a drift-STALEd join could never re-verify in a sealed
            # deployment), referent_gap for a real adapter. Lazy import: overlay -> overlay/upload
            # at module load would cycle (mirrors expiry.py's passc import).
            from featuregen.overlay.upload.join_referents import check_referents_exist

            gap = check_referents_exist(
                conn, current_catalog_adapter(), ref, "approved_join", check_value
            )
            if gap is not None:
                return _deny_audited(conn, cmd, key, f"stale re-confirm blocked: {gap}")
    if not partial:
        evt = append_overlay_event(
            conn,
            fact_key=key,
            type="OVERLAY_FACT_PARTIALLY_CONFIRMED",
            payload={
                "by_owner": actor.subject,
                "role": f"data_owner_{_join_side(authority, actor.subject)}",
                "draft_event_id": state.draft_event_id,
                "note": cmd.args.get("note"),
            },
            actor=actor,
            caused_by=state.draft_event_id,
            # Pin OCC to the folded head: otherwise both owners can load DRAFT, both take this
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
    # Side coverage: when one side has a KNOWN owner and the other routes to the
    # governance queue, the two confirmations must be one owner + one platform-admin. Two
    # platform-admins must NOT verify a join that has a known owner (that bypasses the owner's side).
    if authority.governance_queue and owners:
        if first not in owners and actor.subject not in owners:
            return _deny_audited(
                conn, cmd, key, "a known owner must confirm their side of the join"
            )
    # Validate the FINAL value before the second-owner CONFIRMED append (the join confirm
    # path validates too, even though approved_join takes no override). On a re-verify, re-affirm
    # the last verified value (symmetry with confirm_fact); benign today since approved_join
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
    # SP-1.5 re-review #2: the referent gate must run AGAIN here, at the VERIFY-producing second
    # confirm — the first-partial gate above only fires when the folded status is STALE, but the
    # first partial flips status to PARTIALLY_CONFIRMED, so a referent that VANISHES between the two
    # owners' confirms (a days-long window) would otherwise reach VERIFIED un-checked. Config-gated.
    try:
        current_overlay_config()
    except RuntimeError:
        pass  # no OverlayConfig sealed -> hardening off (backward-compat)
    else:
        # Task 0 (sealed-runtime fix): route existence to the mode's authoritative structural
        # source — graph_node under the sentinel UploadContextAdapter (whose empty fingerprint
        # fail-closed EVERY real join in a sealed deployment), referent_gap otherwise. Lazy import:
        # overlay -> overlay/upload at module load would cycle (mirrors expiry.py's passc import).
        from featuregen.overlay.upload.join_referents import check_referents_exist

        gap = check_referents_exist(
            conn,
            current_catalog_adapter(),
            _ref_from_payload(stream[0].payload["catalog_object_ref"]),
            "approved_join",
            value,
        )
        if gap is not None:
            return _deny_audited(conn, cmd, key, f"join re-confirm blocked: {gap}")
    expires_at = datetime.now(UTC) + resolve_ttl("approved_join", key)  # SP-1.5 Task 6.3 (was 180d)
    # Thread the cycle-stable head: _cas_target at PARTIALLY_CONFIRMED returns
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
            "note": cmd.args.get("note"),
        },
        actor=actor,
        caused_by=confirms_event_id,
        expected_version=stream[-1].stream_version,  # pin OCC to the folded head
    )
    schedule_expiry(conn, key, confirmed.event_id, expires_at)  # arm overlay_expiry
    _close_fact_tasks(conn, key, reason="join fully confirmed")
    return CommandResult(accepted=True, aggregate_id=key, produced_event_ids=(confirmed.event_id,))
