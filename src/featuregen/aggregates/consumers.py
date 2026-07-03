from __future__ import annotations

from datetime import UTC, datetime, timedelta

from psycopg.types.json import Jsonb

from featuregen.aggregates._append import append, identity_dict
from featuregen.aggregates.activation import _jsonable as _jsonable_inputs
from featuregen.aggregates.feature_versions import load_governance_attributes
from featuregen.aggregates.ids import mint_id, new_consumer_id
from featuregen.contracts import Command, CommandResult, DbConn
from featuregen.governance.activation_policy import evaluate_activation_guards

_DEFAULT_GRACE_SECONDS = 7 * 24 * 3600


def register_consumer_command(conn: DbConn, cmd: Command) -> CommandResult:
    feature_id = cmd.aggregate_id
    args = cmd.args
    consumer_id = conn.execute(
        "INSERT INTO consumers (consumer_id, feature_id, feature_version_id, consumer_kind, "
        "consumer_ref, registered_by) VALUES (%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (feature_id, consumer_kind, consumer_ref) DO UPDATE SET "
        "edge_status='active', deregistered_at=NULL RETURNING consumer_id",
        (
            new_consumer_id(),
            feature_id,
            args.get("feature_version_id"),
            args["consumer_kind"],
            args["consumer_ref"],
            Jsonb(identity_dict(cmd.actor)),
        ),
    ).fetchone()[0]
    evt = append(
        conn,
        aggregate="feature",
        aggregate_id=feature_id,
        type="CONSUMER_REGISTERED",
        payload={
            "feature_id": feature_id,
            "consumer_id": consumer_id,
            "consumer_kind": args["consumer_kind"],
            "consumer_ref": args["consumer_ref"],
        },
        actor=cmd.actor,
        feature_id=feature_id,
    )
    return CommandResult(accepted=True, aggregate_id=feature_id, produced_event_ids=(evt.event_id,))


def deregister_consumer_command(conn: DbConn, cmd: Command) -> CommandResult:
    feature_id = cmd.aggregate_id
    args = cmd.args
    row = conn.execute(
        "UPDATE consumers SET edge_status='deregistered', deregistered_at=now() "
        "WHERE feature_id=%s AND consumer_kind=%s AND consumer_ref=%s RETURNING consumer_id",
        (feature_id, args["consumer_kind"], args["consumer_ref"]),
    ).fetchone()
    consumer_id = row[0] if row else None
    evt = append(
        conn,
        aggregate="feature",
        aggregate_id=feature_id,
        type="CONSUMER_DEREGISTERED",
        payload={
            "feature_id": feature_id,
            "consumer_id": consumer_id,
            "consumer_kind": args["consumer_kind"],
            "consumer_ref": args["consumer_ref"],
        },
        actor=cmd.actor,
        feature_id=feature_id,
    )
    return CommandResult(accepted=True, aggregate_id=feature_id, produced_event_ids=(evt.event_id,))


def supersede_command(conn: DbConn, cmd: Command) -> CommandResult:
    feature_id = cmd.aggregate_id
    args = cmd.args
    use_case = args["use_case"]
    new_fv = args["feature_version_id"]
    # MAJOR #14: supersede must compare-and-swap on expected_prior, exactly like activate
    # (activation.py:105-111). Omitting it must be DENIED — never an unconditional overwrite of the
    # production-active slot — so the no-silent-clobber property (§5.8/§12) holds on this path too.
    expected_prior = args.get("expected_prior")
    if not expected_prior:
        return CommandResult(
            accepted=False,
            aggregate_id=feature_id,
            denied_reason="supersede requires expected_prior (CAS)",
        )
    row = conn.execute(
        "SELECT feature_version_id FROM feature_active_versions "
        "WHERE feature_id=%s AND use_case=%s FOR UPDATE",
        (feature_id, use_case),
    ).fetchone()
    prior = row[0] if row else None
    # §3.8 governance guards — a supersession promotes new_fv into the use-case slot, so it must
    # pass the same activation guards (intrinsic use_case_not_blocked + policy-parameterized) as a
    # fresh activation. On failure emit an audited ACTIVATION_BLOCKED event and do NOT promote.
    attrs = load_governance_attributes(conn, new_fv)
    failure = evaluate_activation_guards(attrs, use_case=use_case, approval_type="PRODUCTION")
    if failure is not None:
        evt = append(
            conn,
            aggregate="feature",
            aggregate_id=feature_id,
            type="ACTIVATION_BLOCKED",
            payload={
                "feature_id": feature_id,
                "feature_version_id": new_fv,
                "use_case": use_case,
                "approval_type": "PRODUCTION",
                "guard": failure.guard,
                "guard_inputs": _jsonable_inputs(failure.inputs),
                "guard_result": failure.result,
            },
            actor=cmd.actor,
            feature_id=feature_id,
        )
        return CommandResult(
            accepted=True, aggregate_id=feature_id, produced_event_ids=(evt.event_id,)
        )
    # CAS-claim the slot FIRST (transient seq), guarded by expected_prior — mirror activate's
    # _cas_claim_slot non-null-base branch (activation.py:105-111). 0 rows updated means the slot no
    # longer holds expected_prior: CAS conflict, no clobber, and (crucially) NO VERSION_SUPERSEDED
    # event — the event append below runs ONLY on the CAS-success path.
    updated = conn.execute(
        "UPDATE feature_active_versions SET feature_version_id=%s, activation_state='PRODUCTION', "
        "activated_seq=%s, activated_at=now() "
        "WHERE feature_id=%s AND use_case=%s AND feature_version_id=%s",
        (new_fv, 0, feature_id, use_case, expected_prior),
    ).rowcount
    if updated == 0:
        return CommandResult(
            accepted=False,
            aggregate_id=feature_id,
            denied_reason="supersede CAS conflict",
        )
    evt = append(
        conn,
        aggregate="feature",
        aggregate_id=feature_id,
        type="VERSION_SUPERSEDED",
        payload={
            "feature_id": feature_id,
            "feature_version_id": new_fv,
            "superseded_version_id": prior,
            "use_case": use_case,
        },
        actor=cmd.actor,
        feature_id=feature_id,
    )
    conn.execute(
        "UPDATE feature_active_versions SET activated_seq=%s WHERE feature_id=%s AND use_case=%s",
        (evt.global_seq, feature_id, use_case),
    )
    return CommandResult(accepted=True, aggregate_id=feature_id, produced_event_ids=(evt.event_id,))


def _deprecate_now(conn: DbConn, cmd: Command, *, via: str) -> CommandResult:
    feature_id = cmd.aggregate_id
    args = cmd.args
    use_case = args["use_case"]
    fv = args["feature_version_id"]
    evt = append(
        conn,
        aggregate="feature",
        aggregate_id=feature_id,
        type="VERSION_DEPRECATED",
        payload={
            "feature_id": feature_id,
            "feature_version_id": fv,
            "use_case": use_case,
            "reason": args.get("reason"),
            "via": via,
        },
        actor=cmd.actor,
        feature_id=feature_id,
    )
    conn.execute(
        "UPDATE feature_active_versions SET activation_state='DEPRECATED' "
        "WHERE feature_id=%s AND use_case=%s AND feature_version_id=%s",
        (feature_id, use_case, fv),
    )
    return CommandResult(accepted=True, aggregate_id=feature_id, produced_event_ids=(evt.event_id,))


def deprecate_command(conn: DbConn, cmd: Command) -> CommandResult:
    feature_id = cmd.aggregate_id
    args = cmd.args
    use_case = args["use_case"]
    fv = args["feature_version_id"]
    active_refs = [
        r[0]
        for r in conn.execute(
            "SELECT consumer_ref FROM consumers WHERE feature_id=%s AND edge_status='active'",
            (feature_id,),
        ).fetchall()
    ]
    if active_refs and not args.get("force_quiesce"):
        return CommandResult(
            accepted=False,
            aggregate_id=feature_id,
            denied_reason=f"deprecate blocked: {len(active_refs)} active consumer(s)",
        )
    if active_refs:  # forced: §4.4-note/§6.3 impact-analysis + quiesce/grace transition
        grace_seconds = int(args.get("grace_seconds", _DEFAULT_GRACE_SECONDS))
        quiesced = append(
            conn,
            aggregate="feature",
            aggregate_id=feature_id,
            type="VERSION_QUIESCED",
            payload={
                "feature_id": feature_id,
                "feature_version_id": fv,
                "use_case": use_case,
                "impacted_consumers": active_refs,
                "grace_seconds": grace_seconds,
                "reason": args.get("reason"),
            },
            actor=cmd.actor,
            feature_id=feature_id,
        )
        fire_at = datetime.now(UTC) + timedelta(seconds=grace_seconds)
        conn.execute(
            "INSERT INTO timers (timer_id, idempotency_key, aggregate, aggregate_id, kind, "
            "fire_at, payload) VALUES (%s,%s,'feature',%s,'business_repair',%s,%s) "
            "ON CONFLICT (idempotency_key) DO NOTHING",
            (
                mint_id("tmr"),
                f"quiesce:{fv}:{use_case}",
                feature_id,
                fire_at,
                Jsonb(
                    {
                        "handler": "finalize_deprecate",
                        "feature_id": feature_id,
                        "feature_version_id": fv,
                        "use_case": use_case,
                    }
                ),
            ),
        )
        # active version stays PRODUCTION during the grace window; finalize_deprecate completes it.
        return CommandResult(
            accepted=True, aggregate_id=feature_id, produced_event_ids=(quiesced.event_id,)
        )
    return _deprecate_now(conn, cmd, via="direct")


def finalize_deprecate_command(conn: DbConn, cmd: Command) -> CommandResult:
    """Complete a quiesced deprecation after the grace window (grace-timer or operator driven).
    Idempotent: a no-op once the slot is already DEPRECATED or gone."""
    feature_id = cmd.aggregate_id
    args = cmd.args
    use_case = args["use_case"]
    fv = args["feature_version_id"]
    row = conn.execute(
        "SELECT activation_state FROM feature_active_versions "
        "WHERE feature_id=%s AND use_case=%s AND feature_version_id=%s FOR UPDATE",
        (feature_id, use_case, fv),
    ).fetchone()
    if row is None or row[0] == "DEPRECATED":
        return CommandResult(accepted=True, aggregate_id=feature_id)
    return _deprecate_now(conn, cmd, via="quiesce")


def retier_command(conn: DbConn, cmd: Command) -> CommandResult:
    feature_id = cmd.aggregate_id
    args = cmd.args
    row = conn.execute(
        "SELECT risk_tier FROM feature_versions WHERE feature_version_id=%s",
        (args["feature_version_id"],),
    ).fetchone()
    old_tier = row[0] if row else None
    evt = append(
        conn,
        aggregate="feature",
        aggregate_id=feature_id,
        type="VERSION_RETIERED",
        payload={
            "feature_id": feature_id,
            "feature_version_id": args["feature_version_id"],
            "old_risk_tier": old_tier,
            "new_risk_tier": args["new_risk_tier"],
            "requested_by": args.get("requested_by"),
        },
        actor=cmd.actor,
        feature_id=feature_id,
    )
    return CommandResult(accepted=True, aggregate_id=feature_id, produced_event_ids=(evt.event_id,))
