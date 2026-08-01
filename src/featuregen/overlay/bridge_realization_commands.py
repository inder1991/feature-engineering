"""Separate decision commands for directional bridge execution safety and optional human review."""
from __future__ import annotations

from collections.abc import Mapping

from psycopg.types.json import Jsonb

from featuregen.contracts import Command, CommandResult, DbConn
from featuregen.contracts.identity import identity_to_jsonb
from featuregen.idgen import mint_id


def _update_axis(
    conn: DbConn,
    cmd: Command,
    *,
    axis: str,
    column: str,
    value: str,
) -> CommandResult:
    revision_id = str(cmd.args.get("realization_revision_id") or "")
    expected = cmd.args.get("expected_pointer_version")
    if not revision_id or not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
        return CommandResult(
            accepted=False,
            aggregate_id="",
            denied_reason="realization revision and positive expected_pointer_version are required",
        )
    evidence = cmd.args.get("evidence") or {}
    if not isinstance(evidence, Mapping):
        return CommandResult(
            accepted=False,
            aggregate_id=revision_id,
            denied_reason="decision evidence must be an object",
        )
    row = conn.execute(
        "SELECT realization_id FROM bridge_join_realization_current "
        "WHERE realization_revision_id=%s AND pointer_version=%s",
        (revision_id, expected),
    ).fetchone()
    if row is None:
        return CommandResult(
            accepted=False,
            aggregate_id=revision_id,
            denied_reason="stale realization decision or revision is not current",
        )
    realization_id = row[0]
    changed = conn.execute(
        f"UPDATE bridge_join_realization_current SET {column}=%s, "
        "pointer_version=pointer_version+1, updated_at=now() "
        "WHERE realization_id=%s AND realization_revision_id=%s AND pointer_version=%s",
        (value, realization_id, revision_id, expected),
    ).rowcount
    if changed != 1:
        return CommandResult(
            accepted=False,
            aggregate_id=realization_id,
            denied_reason="stale realization decision",
        )
    event_id = mint_id("brd")
    # occurred_at is written explicitly with clock_timestamp(): the column's DEFAULT now() is
    # TRANSACTION-start time, so two decisions in one transaction landed with identical
    # timestamps and every `ORDER BY occurred_at, decision_event_id` read ordered them by the
    # ULID's random tail — a coin flip within one millisecond. clock_timestamp() advances
    # per statement, so decision order is the order the decisions were actually taken.
    conn.execute(
        "INSERT INTO bridge_realization_decision_event "
        "(decision_event_id, realization_revision_id, decision_axis, decision_value, "
        " actor_json, evidence_json, occurred_at) VALUES (%s,%s,%s,%s,%s,%s, clock_timestamp())",
        (
            event_id,
            revision_id,
            axis,
            value,
            Jsonb(identity_to_jsonb(cmd.actor)),
            Jsonb(dict(evidence)),
        ),
    )
    return CommandResult(
        accepted=True,
        aggregate_id=realization_id,
        produced_event_ids=(event_id,),
    )


def decide_bridge_realization_safety(conn: DbConn, cmd: Command) -> CommandResult:
    """Record deterministic safety over one exact realization revision."""
    safe = cmd.args.get("safe")
    if not isinstance(safe, bool):
        return CommandResult(
            accepted=False,
            aggregate_id="",
            denied_reason="safe must be a boolean",
        )
    return _update_axis(
        conn,
        cmd,
        axis="deterministic_safety",
        column="safety_status",
        value="deterministically_validated" if safe else "unsafe",
    )


def review_bridge_realization(conn: DbConn, cmd: Command) -> CommandResult:
    """Record optional human endorsement without changing deterministic safety."""
    if cmd.actor.actor_kind != "human":
        return CommandResult(
            accepted=False,
            aggregate_id="",
            denied_reason="bridge realization review requires a human",
        )
    approved = cmd.args.get("approved")
    if not isinstance(approved, bool):
        return CommandResult(
            accepted=False,
            aggregate_id="",
            denied_reason="approved must be a boolean",
        )
    return _update_axis(
        conn,
        cmd,
        axis="human_review",
        column="review_status",
        value="human_verified" if approved else "unreviewed",
    )
