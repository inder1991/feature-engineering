from __future__ import annotations

from featuregen.contracts.db import DbConn
from featuregen.contracts.gates import GateTaskSpec
from featuregen.gates.tasks import open_task


def open_fact_review_task(conn: DbConn, *, fact_key: str, reason: str, actor) -> str:
    """Open ONE review task on a VERIFIED fact WITHOUT staling it (richness Task 5, Step 5).

    The re-verify machinery above is tied to expiry/stale/drift events — every existing caller
    changes the fact's status first. This is the missing "question a live fact" command: the fact
    stays VERIFIED and servable (NO overlay event is appended, so the status CANNOT move); the
    opened `human_tasks` row is CAS-bound to the fact's CURRENT `confirmed_event_id` and carries
    ``reason`` (e.g. ``basis_review`` — the CIB availability basis question) in its
    `required_inputs`, so a later confirm/reject through the normal gate flow still CAS-checks
    against the event the reviewer actually looked at. Idempotent: an already-open task for the
    fact is returned instead of duplicated. Raises `OverlayCommandError` when the fact stream is
    empty or the fact is not currently VERIFIED (only a confirmed fact has a current confirmed
    event to review)."""
    from featuregen.overlay._lifecycle import OverlayCommandError
    from featuregen.overlay.authority import resolve_authority
    from featuregen.overlay.catalog import current_catalog_adapter
    from featuregen.overlay.identity import _ref_from_payload
    from featuregen.overlay.state import fold_overlay_state
    from featuregen.overlay.store import load_fact

    existing = conn.execute(
        "SELECT task_id FROM human_tasks WHERE fact_key=%s AND status='open' "
        "ORDER BY created_at DESC LIMIT 1", (fact_key,)).fetchone()
    if existing is not None:
        return existing[0]
    stream = load_fact(conn, fact_key)
    if not stream:
        raise OverlayCommandError(f"no fact stream for {fact_key!r}")
    state = fold_overlay_state(stream)
    if state.status != "VERIFIED" or not state.confirmed_event_id:
        raise OverlayCommandError(
            f"fact {fact_key!r} is {state.status}, not VERIFIED — a review-without-staling task "
            "binds to a current confirmed event")
    ref = _ref_from_payload(stream[0].payload["catalog_object_ref"])
    authority = resolve_authority(conn, current_catalog_adapter(), ref, state.fact_type)
    eligible = authority.task_assignees[0]   # ONE row by contract; table facts have one side
    spec = GateTaskSpec(
        gate=authority.gate,
        required_inputs=(reason, "target_confirmed_event_id"),
        eligible_assignees=dict(eligible),
        allowed_responses=("confirm", "reject"),
        fact_key=fact_key,
        target_event_id=state.confirmed_event_id,
    )
    return open_task(conn, spec, actor)


def open_reverify_task(
    conn: DbConn,
    *,
    fact_key: str,
    fact_type: str,
    target_confirmed_event_id: str,
    authority,
    actor,
) -> tuple[str, ...]:
    """Reopen the §6 re-verification gate, CAS-bound to the fact's current confirmed_event_id
    (stored as each task's target_event_id; a later confirm/reject is rejected if the fact has
    since advanced). Opens one task **per resolved side** by iterating `authority.task_assignees`
    — the SAME per-side plan the initial proposal used: a
    single-authority fact yields one task, an `approved_join` with two distinct owners yields one
    task per side (an unknown side routes to the platform-admin/governance queue). The gate is
    shared (`authority.gate` — `OVERLAY_DATA_OWNER` for data facts, `OVERLAY_COMPLIANCE` for
    policy_tag). prior_value is NOT stored on human_tasks (it has no such column) — it is surfaced
    through the overlay_proposal read model, which the projection sets to the prior value on
    EXPIRED/STALED, and read back via get_task_proposal. Returns the opened task ids."""
    del fact_type  # gate is taken from the resolved authority; kept for caller symmetry
    task_ids: list[str] = []
    for eligible in authority.task_assignees:
        spec = GateTaskSpec(
            gate=authority.gate,
            required_inputs=("prior_value", "target_confirmed_event_id"),
            eligible_assignees=dict(eligible),
            allowed_responses=("confirm", "reject"),
            fact_key=fact_key,
            target_event_id=target_confirmed_event_id,
        )
        task_ids.append(open_task(conn, spec, actor))
    return tuple(task_ids)
