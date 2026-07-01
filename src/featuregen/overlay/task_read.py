"""Task-scoped proposal read for the overlay human-gate flow (SP-1 design §7.2).

`get_task_proposal` is a direct read function (NOT a registered `_OVERLAY_CATALOG` handler) that
returns what a task assignee must see to confirm/reject. `commands.py` re-exports it so existing
`featuregen.overlay.commands` imports keep resolving.
"""
from __future__ import annotations

from featuregen.contracts import DbConn
from featuregen.overlay._lifecycle import OverlayCommandError, _latest_proposed
from featuregen.overlay.evidence import read_evidence
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact


def get_task_proposal(conn: DbConn, task_id: str, actor) -> dict:
    """Task-scoped proposal read (§7.2): returns what the assignee must see to confirm. Authorized
    to the task's assignee (eligible subject/role) or the governance role; denied to anyone else —
    distinct from the deferred end-user `resolve_fact` authz.

    NOT a registered command handler (no `_OVERLAY_CATALOG` entry) — a direct read function. The CAS
    target and prior value come from AUTHORITATIVE, synchronous sources (the `human_tasks` row and
    the event stream), NOT the asynchronous `overlay_proposal` projection."""
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
    # Subject-scoped authz: when the task is bound to a specific subject (a known-owner data
    # fact's task is {"role":"data_owner","subject":<owner>}), ONLY that subject may read it — the
    # bare role must NOT also satisfy it, or any data_owner-role holder would read another team's
    # proposal + evidence, silently defeating the subject narrowing. The role branch survives only
    # for SUBJECT-LESS governance/compliance tasks. Mirrors the confirm handler's subject-scoping.
    if subject is not None:
        authorized = actor.subject == subject
    else:
        authorized = role is not None and role in actor.role_claims
    # A platform-admin reads a GOVERNANCE task via the role branch above (its eligible role is
    # "platform-admin"); it is NOT granted blanket read of every task's proposal.
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
