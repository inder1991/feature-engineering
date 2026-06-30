from __future__ import annotations

from featuregen.contracts.db import DbConn
from featuregen.contracts.gates import GateTaskSpec
from featuregen.gates.tasks import open_task


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
    (pin 19) — the SAME per-side plan the initial proposal used (Phase 4 Task 4.2): a
    single-authority fact yields one task, an `approved_join` with two distinct owners yields one
    task per side (an unknown side routes to the platform-admin/governance queue). The gate is
    shared (`authority.gate` — `OVERLAY_DATA_OWNER` for data facts, `OVERLAY_COMPLIANCE` for
    policy_tag). prior_value is NOT stored on human_tasks (it has no such column) — it is surfaced
    through the overlay_proposal read model, which the projection sets to the prior value on
    EXPIRED/STALED, and read back via get_task_proposal (Phase 4.6). Returns the opened task ids."""
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
