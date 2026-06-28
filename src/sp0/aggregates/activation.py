from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional

from psycopg.types.json import Jsonb

from sp0.contracts import (
    Command, CommandResult, DbConn, Disposition, Handler, HandlerContext, HandlerResult,
    IdentityEnvelope, NewActivation, ProvenanceEnvelope,
)
from sp0.aggregates._append import append
from sp0.aggregates.ids import mint_id
from sp0.aggregates.feature_versions import load_governance_attributes, mint_feature_version
from sp0.governance.activation_policy import ActivationPolicy, evaluate_activation_guards


def _jsonable(value: Any) -> Any:
    """Coerce guard inputs (which may carry tuples / nested mappings) to JSON-safe values for the
    audited ACTIVATION_BLOCKED payload."""
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


@dataclass(frozen=True, slots=True)
class ActivationResult:
    activated: bool
    conflict: bool
    feature_version_id: str
    use_case: str
    event_id: str


@dataclass(frozen=True, slots=True)
class SagaStep1Result:
    feature_version_id: str
    activation_message_id: str


def _schedule_expiry_timer(conn: DbConn, feature_id: str, feature_version_id: str,
                           use_case: str, expires_at: datetime) -> None:
    conn.execute(
        "INSERT INTO timers (timer_id, idempotency_key, aggregate, aggregate_id, kind, "
        "fire_at, payload) VALUES (%s,%s,'feature',%s,'experiment_expiry',%s,%s) "
        "ON CONFLICT (idempotency_key) DO NOTHING",
        (mint_id("tmr"), f"expiry:{feature_version_id}:{use_case}", feature_id, expires_at,
         Jsonb({"handler": "deactivate_expired_version", "feature_id": feature_id,
                "feature_version_id": feature_version_id, "use_case": use_case})),
    )


def _cas_claim_slot(
    conn: DbConn, *, feature_id: str, use_case: str, new_fv: str,
    base: Optional[str], state: str, activated_seq: int,
) -> bool:
    """Atomic CAS on the (feature_id, use_case) active-map slot. Returns True iff this caller
    won the slot. The DB write IS the gate (no read-then-write window):
      - null base  -> INSERT ... ON CONFLICT DO NOTHING: only the first concurrent first-
                      activation inserts; the loser conflicts and returns False (no overwrite).
      - non-null base -> conditional UPDATE guarded by `feature_version_id = base`: succeeds only
                      while the slot still holds the run's base version.
    This closes the null-base race where two concurrent first-activations could both pass a
    stale `current == base (None)` precheck and the later one silently overwrite the first."""
    if base is None:
        row = conn.execute(
            "INSERT INTO feature_active_versions "
            "(feature_id, use_case, feature_version_id, activation_state, activated_seq) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (feature_id, use_case) DO NOTHING "
            "RETURNING feature_version_id",
            (feature_id, use_case, new_fv, state, activated_seq),
        ).fetchone()
        return row is not None
    row = conn.execute(
        "UPDATE feature_active_versions SET feature_version_id=%s, activation_state=%s, "
        "activated_seq=%s, activated_at=now() "
        "WHERE feature_id=%s AND use_case=%s AND feature_version_id=%s "
        "RETURNING feature_version_id",
        (new_fv, state, activated_seq, feature_id, use_case, base),
    ).fetchone()
    return row is not None


def apply_activation(
    conn: DbConn, *, feature_id: str, feature_version_id: str, use_case: str,
    base_feature_version_id: Optional[str], approval_type: str, actor: IdentityEnvelope,
    expires_at: Optional[datetime] = None, provenance: Optional[ProvenanceEnvelope] = None,
    policy: Optional[ActivationPolicy] = None,
) -> ActivationResult:
    row = conn.execute(
        "SELECT feature_version_id FROM feature_active_versions "
        "WHERE feature_id=%s AND use_case=%s FOR UPDATE",
        (feature_id, use_case),
    ).fetchone()
    current = row[0] if row else None
    if current == feature_version_id:
        return ActivationResult(True, False, feature_version_id, use_case, "")  # idempotent
    # §3.8 governance guards — evaluated against the frozen version attributes BEFORE claiming the
    # slot. The intrinsic use_case_not_blocked guard plus the injected policy-parameterized guards
    # (verification stamp / risk ceiling / required artifacts) gate the activation. On failure we
    # emit an audited ACTIVATION_BLOCKED event and return WITHOUT activating (no CAS, no slot).
    attrs = load_governance_attributes(conn, feature_version_id)
    # P1 cross-feature integrity: a feature must NOT be able to activate another feature's
    # version. The version's frozen feature_id must equal the activation's feature_id — and the
    # base (expected current active version), if supplied, must belong to the same feature too.
    # Reject loudly BEFORE any slot claim or event so no cross-feature state is ever written.
    # (Backstopped by the composite FK feature_active_versions(feature_id, feature_version_id).)
    if attrs.feature_id != feature_id:
        raise ValueError(
            f"cross-feature activation: feature_version_id={feature_version_id!r} belongs to "
            f"feature_id={attrs.feature_id!r}, not {feature_id!r}"
        )
    if base_feature_version_id is not None:
        base_attrs = load_governance_attributes(conn, base_feature_version_id)
        if base_attrs.feature_id != feature_id:
            raise ValueError(
                f"cross-feature base: base_feature_version_id={base_feature_version_id!r} "
                f"belongs to feature_id={base_attrs.feature_id!r}, not {feature_id!r}"
            )
    failure = evaluate_activation_guards(
        attrs, use_case=use_case, approval_type=approval_type, policy=policy,
    )
    if failure is not None:
        evt = append(
            conn, aggregate="feature", aggregate_id=feature_id, type="ACTIVATION_BLOCKED",
            payload={"feature_id": feature_id, "feature_version_id": feature_version_id,
                     "use_case": use_case, "base_feature_version_id": base_feature_version_id,
                     "approval_type": approval_type, "guard": failure.guard,
                     "guard_inputs": _jsonable(failure.inputs), "guard_result": failure.result},
            actor=actor, feature_id=feature_id,
        )
        return ActivationResult(False, False, feature_version_id, use_case, evt.event_id)
    if current != base_feature_version_id:
        evt = append(
            conn, aggregate="feature", aggregate_id=feature_id, type="ACTIVATION_CONFLICT",
            payload={"feature_id": feature_id, "feature_version_id": feature_version_id,
                     "use_case": use_case, "base_feature_version_id": base_feature_version_id,
                     "current_active_version_id": current},
            actor=actor, feature_id=feature_id,
        )
        return ActivationResult(False, True, feature_version_id, use_case, evt.event_id)
    activation_state = "ACTIVE_EXPERIMENTAL" if approval_type == "EXPERIMENTAL" else "PRODUCTION"
    # CAS-claim the slot FIRST (with a transient seq); only the winner appends VERSION_ACTIVATED.
    won = _cas_claim_slot(
        conn, feature_id=feature_id, use_case=use_case, new_fv=feature_version_id,
        base=base_feature_version_id, state=activation_state, activated_seq=0,
    )
    if not won:
        evt = append(
            conn, aggregate="feature", aggregate_id=feature_id, type="ACTIVATION_CONFLICT",
            payload={"feature_id": feature_id, "feature_version_id": feature_version_id,
                     "use_case": use_case, "base_feature_version_id": base_feature_version_id,
                     "current_active_version_id": None, "reason": "lost_cas_race"},
            actor=actor, feature_id=feature_id,
        )
        return ActivationResult(False, True, feature_version_id, use_case, evt.event_id)
    evt = append(
        conn, aggregate="feature", aggregate_id=feature_id, type="VERSION_ACTIVATED",
        payload={"feature_id": feature_id, "feature_version_id": feature_version_id,
                 "use_case": use_case, "base_feature_version_id": base_feature_version_id,
                 "activation_state": activation_state},
        actor=actor, provenance=provenance, feature_id=feature_id,
    )
    conn.execute(
        "UPDATE feature_active_versions SET activated_seq=%s "
        "WHERE feature_id=%s AND use_case=%s",
        (evt.global_seq, feature_id, use_case),
    )
    if activation_state == "ACTIVE_EXPERIMENTAL" and expires_at is not None:
        _schedule_expiry_timer(conn, feature_id, feature_version_id, use_case, expires_at)
    return ActivationResult(True, False, feature_version_id, use_case, evt.event_id)


def activate_command(conn: DbConn, cmd: Command) -> CommandResult:
    """Synchronous lifecycle command `activate` (§4.4) — a separate entrypoint from the async
    `activate_version` saga handler; both delegate to apply_activation."""
    args = cmd.args
    res = apply_activation(
        conn, feature_id=cmd.aggregate_id, feature_version_id=args["feature_version_id"],
        use_case=args["use_case"], base_feature_version_id=args.get("base_feature_version_id"),
        approval_type=args["approval_type"], actor=cmd.actor, expires_at=args.get("expires_at"),
    )
    event_ids = (res.event_id,) if res.event_id else ()
    return CommandResult(accepted=True, aggregate_id=cmd.aggregate_id, produced_event_ids=event_ids)


def request_activation(
    conn: DbConn, *, feature_id: str, feature_version_id: str, use_case: str,
    base_feature_version_id: Optional[str], approval_type: str, produced_by_run: str,
    actor: IdentityEnvelope, expires_at: Optional[datetime] = None,
) -> str:
    """§5.8 saga step 1b (in the run's tx): record ACTIVATION_REQUESTED on the RUN stream
    (carrying every arg the feature-side handler needs, since the Phase-04 worker passes the
    handler only a HandlerContext built from this run-stream event), then enqueue a
    feature-partitioned `activate_version` queue row referencing it."""
    req = append(
        conn, aggregate="run", aggregate_id=produced_by_run, type="ACTIVATION_REQUESTED",
        payload={"run_id": produced_by_run, "feature_id": feature_id,
                 "feature_version_id": feature_version_id, "use_case": use_case,
                 "base_feature_version_id": base_feature_version_id,
                 "approval_type": approval_type,
                 "expires_at": expires_at.isoformat() if expires_at else None},
        actor=actor, run_id=produced_by_run, feature_id=feature_id,
    )
    message_id = f"activate:{feature_version_id}:{use_case}"
    conn.execute(
        "INSERT INTO queue (message_id, partition_key, handler, payload) "
        "VALUES (%s, %s, 'activate_version', %s) ON CONFLICT (message_id) DO NOTHING",
        (message_id, f"feature:{feature_id}",
         Jsonb({"run_id": produced_by_run, "event_id": req.event_id})),
    )
    return message_id


def on_run_approved(
    conn: DbConn, *, feature_id: str, produced_by_run: str, use_case: str, approval_type: str,
    actor: IdentityEnvelope, provenance: ProvenanceEnvelope, verification_stamp: str,
    risk_tier: str, approved_use_cases, blocked_use_cases, required_artifact_refs: Mapping[str, Any],
    content_hash: str, base_feature_version_id: Optional[str] = None,
    dsl_operation_catalog_version: Optional[str] = None,
    approval: Optional[Mapping[str, Any]] = None, expires_at: Optional[datetime] = None,
) -> SagaStep1Result:
    """§5.8 saga step 1, ALL in the run's own transaction: mint the frozen feature_version
    (Task 10) and emit the activation request (request_activation). The run is now terminal; the
    version exists but is not yet active — feature-side activation runs async via the worker."""
    fv_id = mint_feature_version(
        conn, feature_id=feature_id, produced_by_run=produced_by_run,
        verification_stamp=verification_stamp, risk_tier=risk_tier, approval_type=approval_type,
        approved_use_cases=approved_use_cases, blocked_use_cases=blocked_use_cases,
        required_artifact_refs=required_artifact_refs, content_hash=content_hash, actor=actor,
        provenance=provenance, base_feature_version_id=base_feature_version_id,
        dsl_operation_catalog_version=dsl_operation_catalog_version, approval=approval,
        expires_at=expires_at,
    )
    message_id = request_activation(
        conn, feature_id=feature_id, feature_version_id=fv_id, use_case=use_case,
        base_feature_version_id=base_feature_version_id, approval_type=approval_type,
        produced_by_run=produced_by_run, actor=actor, expires_at=expires_at,
    )
    return SagaStep1Result(feature_version_id=fv_id, activation_message_id=message_id)


class ActivateVersionHandler:
    """§5.8 saga step 2 — the feature-side activation step the Phase-04 worker dispatches
    (keyed on `queue.handler == name`). The handler is PURE with respect to persistence: it
    reads the activation args from the run-stream ACTIVATION_REQUESTED triggering event and
    DECLARES the cross-aggregate effect as `HandlerResult.activations` — it performs NO writes
    via `ctx`. `commit_step` applies each NewActivation by calling `apply_activation` on the
    SINGLE step-transaction connection, so the active-map CAS, the feature-stream
    VERSION_ACTIVATED / ACTIVATION_CONFLICT event, and any experiment_expiry timer are atomic
    with the rest of the step (a failure rolls them ALL back — no orphan active-map row, event,
    or timer). The handler returns NO run-stream events. Idempotent: re-delivery re-declares the
    same effect and `apply_activation` no-ops when already active at this version. This is the
    sanctioned cross-aggregate saga executor; the general handler prohibition on feature-stream
    writes (§5.3) does not apply to its declared activation effect."""
    name = "activate_version"
    version = 1
    timeout_seconds = 30.0

    def handle(self, ctx: HandlerContext) -> HandlerResult:
        p = ctx.triggering_event.payload
        expires_at = p.get("expires_at")
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
        return HandlerResult(
            disposition=Disposition.OK,
            activations=(
                NewActivation(
                    feature_id=p["feature_id"],
                    feature_version_id=p["feature_version_id"],
                    use_case=p["use_case"],
                    base_feature_version_id=p.get("base_feature_version_id"),
                    approval_type=p["approval_type"],
                    expires_at=expires_at,
                ),
            ),
        )


ACTIVATE_VERSION_HANDLER: Handler = ActivateVersionHandler()


def register_phase06_handlers(registry) -> None:
    """Register Phase-06 saga handlers into Phase-04's HandlerRegistry (production wiring)."""
    registry.register(ACTIVATE_VERSION_HANDLER)


def deactivate_expired_version_command(conn: DbConn, cmd: Command) -> CommandResult:
    feature_id = cmd.aggregate_id
    feature_version_id = cmd.args["feature_version_id"]
    use_case = cmd.args["use_case"]
    row = conn.execute(
        "SELECT feature_version_id, activation_state FROM feature_active_versions "
        "WHERE feature_id=%s AND use_case=%s FOR UPDATE",
        (feature_id, use_case),
    ).fetchone()
    if row is None or row[0] != feature_version_id or row[1] != "ACTIVE_EXPERIMENTAL":
        return CommandResult(accepted=True, aggregate_id=feature_id)
    evt = append(
        conn, aggregate="feature", aggregate_id=feature_id, type="VERSION_EXPIRED",
        payload={"feature_id": feature_id, "feature_version_id": feature_version_id,
                 "use_case": use_case},
        actor=cmd.actor, feature_id=feature_id,
    )
    conn.execute(
        "DELETE FROM feature_active_versions WHERE feature_id=%s AND use_case=%s",
        (feature_id, use_case),
    )
    return CommandResult(accepted=True, aggregate_id=feature_id, produced_event_ids=(evt.event_id,))
