"""Shared lifecycle core for the overlay command handlers (SP-1 design §6).

Houses the cross-handler primitives the propose/confirm/reject/enter handlers (and the task-read
path) all build on: the `OverlayCommandError` raised on misconfiguration / unauthorized reads, the
folded-status constants (`_NON_TERMINAL`/`_AWAITING_CONFIRMATION`) plus the re-verify horizon
(`_DEFAULT_TTL`), and the shared helpers (`_deny_audited`/`_latest_proposed`/`_cas_target`/
`_close_fact_tasks`). `commands.py` re-exports these so existing `featuregen.overlay.commands`
imports keep resolving.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from featuregen.contracts import Command, CommandResult, DbConn
from featuregen.gates.tasks import cancel_task
from featuregen.security.audit import record_denial

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


def _deny_audited(conn: DbConn, cmd: Command, key: str, reason: str) -> CommandResult:
    """Emit a tamper-evident COMMAND_DENIED security_audit row for an AUTHORITY or four-eyes/SoD
    handler denial (F4), then return the denial. These fine-grained denials happen INSIDE the handler
    (the coarse PolicyAuthorizer only audits role/kind/scope + coarse SoD denials), so without this
    they leave zero audit trace — a detective-control gap in a regulator-retention security chain.
    Benign validation/duplicate/wrong-state/CAS-stale denials stay unaudited (plain CommandResult).
    The resolved fact_key is recorded as aggregate_id (overlay commands carry cmd.aggregate_id=None)."""
    record_denial(conn, replace(cmd, aggregate_id=key), reason)
    return CommandResult(accepted=False, aggregate_id=key, denied_reason=reason)


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
