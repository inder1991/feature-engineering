from __future__ import annotations

from datetime import datetime, timezone

from psycopg.types.json import Json

from featuregen.authz.sod import gate_sod_reason
from featuregen.contracts.db import DbConn
from featuregen.contracts.gates import GateTaskSpec, SignalResult
from featuregen.contracts.identity import IdentityEnvelope
from featuregen.gates.duration import parse_duration
from featuregen.identity.build import validate_identity
from featuregen.idgen import mint_id


class GateError(Exception):
    """Raised on malformed/unknown human-gate task operations (§7)."""


def _task_aggregate(run_id, feature_id) -> tuple[str, str]:
    if run_id:
        return "run", run_id
    return "feature", feature_id


def open_task(conn: DbConn, spec: GateTaskSpec, actor: IdentityEnvelope) -> str:
    task_id = mint_id("task")
    conn.execute(
        """
        INSERT INTO human_tasks
            (task_id, task_version, run_id, feature_id, gate, required_inputs,
             eligible_assignees, allowed_responses, quorum_required, quorum_of_role,
             delegation_allowed, sla, status)
        VALUES (%s,1,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'open')
        """,
        (
            task_id, spec.run_id, spec.feature_id, spec.gate,
            list(spec.required_inputs), Json(dict(spec.eligible_assignees)),
            list(spec.allowed_responses), spec.quorum_required, spec.quorum_of_role,
            spec.delegation_allowed, spec.sla,
        ),
    )
    if spec.sla:
        base = datetime.now(timezone.utc)
        sla = parse_duration(spec.sla)
        agg, agg_id = _task_aggregate(spec.run_id, spec.feature_id)
        ladder = {
            "reminder": base + sla / 2,
            "sla": base + sla,
            "escalation": base + sla + sla / 2,
            "auto_park": base + sla * 2,
        }
        for kind, fire_at in ladder.items():
            conn.execute(
                """
                INSERT INTO timers
                    (timer_id, idempotency_key, aggregate, aggregate_id, task_id, kind,
                     fire_at, status, cas_task_version)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'scheduled',1)
                """,
                (mint_id("tmr"), f"{task_id}:{kind}", agg, agg_id, task_id, kind, fire_at),
            )
    return task_id


def bump_task_version(conn: DbConn, task_id: str) -> int:
    row = conn.execute(
        "UPDATE human_tasks SET task_version = task_version + 1, updated_at=now() "
        "WHERE task_id=%s RETURNING task_version",
        (task_id,),
    ).fetchone()
    if row is None:
        raise GateError(f"unknown task {task_id}")
    return row[0]


def cancel_task(
    conn: DbConn,
    task_id: str,
    *,
    reason: str,
    new_status: str = "cancelled",
) -> None:
    if new_status not in ("cancelled", "superseded"):
        raise GateError(f"invalid cancel status {new_status!r}")
    conn.execute(
        "UPDATE human_tasks SET status=%s, updated_at=now() WHERE task_id=%s AND status='open'",
        (new_status, task_id),
    )
    conn.execute(
        "UPDATE timers SET status='cancelled' WHERE task_id=%s AND status='scheduled'",
        (task_id,),
    )


def cancel_tasks_on_run_advance(
    conn: DbConn,
    run_id: str,
    *,
    reason: str = "run advanced past gate",
    new_status: str = "cancelled",
) -> int:
    """Cancel every OPEN gate task (and its scheduled timers) for a run when the run advances
    past their gate — the §7 "cancellation on run advance" clause, made concrete.

    PHASE BOUNDARY: the advancing event / transition is emitted by the Phase 06 lifecycle
    command (or the Phase 03 state machine); that owner CALLS this Phase-07 mechanism inside the
    same §5.1 atomic step transaction. Phase 07 owns the cancellation effect; the trigger is
    upstream. Returns the number of tasks cancelled."""
    open_ids = conn.execute(
        "SELECT task_id FROM human_tasks WHERE run_id=%s AND status='open'",
        (run_id,),
    ).fetchall()
    for (task_id,) in open_ids:
        cancel_task(conn, task_id, reason=reason, new_status=new_status)
    return len(open_ids)


class IneligibleResponderError(GateError):
    """Responder is not eligible (role/scope/quorum-role/delegation) for this gate (§7)."""


class ResponseNotAllowedError(GateError):
    """Response is not in the task's allowed_responses (§7)."""


class SoDViolationError(GateError):
    """The answer violates segregation-of-duties for the gate (author/validator/approver, §6.3).
    Enforced HERE so a DIRECT submit_human_signal caller gets the same SoD as the command path —
    matching the shared-contract docstring ('Enforces eligibility + SoD (§6.3) + quorum')."""


def grant_task_delegation(
    conn: DbConn,
    task_id: str,
    *,
    principal: IdentityEnvelope,
    delegate_subject: str,
    granted_by: IdentityEnvelope,
) -> None:
    """Record a validated delegation grant (§7 'validly-delegated subjects'). The PRINCIPAL's
    eligibility (role + scope + quorum-role) is verified HERE against the principal's own
    IdentityEnvelope; submit_human_signal then trusts the recorded grant. Without this, a
    delegated answer's principal eligibility and the existence of a real delegation relationship
    would never be checked."""
    row = conn.execute(
        """
        SELECT eligible_assignees, quorum_of_role, delegation_allowed, status
          FROM human_tasks WHERE task_id=%s FOR UPDATE
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        raise GateError(f"unknown task {task_id}")
    eligible, quorum_of_role, delegation_allowed, status = row
    if status != "open":
        raise GateError(f"task {task_id} is not open (status={status})")
    if not delegation_allowed:
        raise IneligibleResponderError("delegation not allowed for this task")
    if principal.subject == delegate_subject:
        raise IneligibleResponderError("delegation principal must differ from delegate")
    required_role = eligible.get("role")
    required_scope = eligible.get("scope")
    if required_role is not None and required_role not in principal.role_claims:
        raise IneligibleResponderError(f"principal lacks role {required_role!r}")
    if required_scope is not None and required_scope not in principal.groups:
        raise IneligibleResponderError(f"principal lacks scope {required_scope!r}")
    if quorum_of_role is not None and quorum_of_role not in principal.role_claims:
        raise IneligibleResponderError(f"principal lacks quorum role {quorum_of_role!r}")
    conn.execute(
        """
        INSERT INTO task_delegations (task_id, principal, delegate, granted_by)
        VALUES (%s,%s,%s,%s)
        ON CONFLICT (task_id, principal, delegate) DO NOTHING
        """,
        (task_id, principal.subject, delegate_subject, granted_by.subject),
    )


def submit_human_signal(
    conn: DbConn,
    task_id: str,
    *,
    response: str,
    actor: IdentityEnvelope,
    expected_task_version: int,
    on_behalf_of: str | None = None,
) -> SignalResult:
    # §6.1 identity gate: even on the DIRECT call path (not routed through execute_command), an
    # unauthenticated / unattested actor can never answer a gate. Mirrors the command-path check
    # in authz.policy so the two paths cannot diverge (raises IdentityError on failure).
    validate_identity(actor)
    row = conn.execute(
        """
        SELECT task_version, run_id, feature_id, gate, eligible_assignees,
               allowed_responses, quorum_required, quorum_of_role, delegation_allowed, status
          FROM human_tasks WHERE task_id=%s FOR UPDATE
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        raise GateError(f"unknown task {task_id}")
    (task_version, run_id, feature_id, gate, eligible, allowed_responses,
     quorum_required, quorum_of_role, delegation_allowed, status) = row

    # late answer on a closed task is refused
    if status in ("answered", "conflict", "expired", "cancelled", "superseded"):
        return SignalResult(task_id, status, counted=False, quorum_met=(status == "answered"))

    # staleness keyed to the task's required_inputs/version, NOT the run's stream_version
    if expected_task_version != task_version:
        return SignalResult(task_id, status, counted=False, quorum_met=False)

    if response not in allowed_responses:
        raise ResponseNotAllowedError(f"{response!r} not in {allowed_responses}")

    # ── eligibility / delegation ──────────────────────────────────────────────
    # The EFFECTIVE authority of record is the principal when delegated, else the actor.
    if on_behalf_of is not None:
        if not delegation_allowed:
            raise IneligibleResponderError("delegation not allowed for this task")
        if on_behalf_of == actor.subject:
            raise IneligibleResponderError("delegation principal must differ from delegate")
        grant = conn.execute(
            "SELECT 1 FROM task_delegations WHERE task_id=%s AND principal=%s AND delegate=%s",
            (task_id, on_behalf_of, actor.subject),
        ).fetchone()
        if grant is None:
            raise IneligibleResponderError(
                "no valid delegation grant for this delegate acting for the principal"
            )
        authority = on_behalf_of           # principal eligibility was verified at grant time
    else:
        required_role = eligible.get("role")
        required_scope = eligible.get("scope")
        if required_role is not None and required_role not in actor.role_claims:
            raise IneligibleResponderError(f"actor lacks role {required_role!r}")
        if required_scope is not None and required_scope not in actor.groups:
            raise IneligibleResponderError(f"actor lacks scope {required_scope!r}")
        # quorum_of_role: only responders holding this role count toward the quorum (§7); it may
        # legitimately differ from eligible_assignees.role, so it is checked independently.
        if quorum_of_role is not None and quorum_of_role not in actor.role_claims:
            raise IneligibleResponderError(f"actor lacks quorum role {quorum_of_role!r}")
        authority = actor.subject

    # ── segregation of duties (same predicate as the command-authz path) ──────
    sod_reason = gate_sod_reason(
        conn, gate=gate, subject=authority, run_id=run_id, feature_id=feature_id
    )
    if sod_reason is not None:
        raise SoDViolationError(sod_reason)

    # ── idempotent insert, keyed to the EFFECTIVE authority ───────────────────
    # Distinctness/quorum/SoD key on the authority (coalesce(on_behalf_of, subject)) so a single
    # principal cannot be double-counted via two delegates, nor both self-answer and be delegated.
    # ON CONFLICT additionally no-ops an identical acting subject.
    already = conn.execute(
        """
        SELECT 1 FROM human_task_responses
         WHERE task_id=%s AND coalesce(on_behalf_of, subject)=%s
        """,
        (task_id, authority),
    ).fetchone()
    if already is not None:
        counted = False
    else:
        seq = conn.execute("SELECT nextval('global_seq_seq')").fetchone()[0]
        conn.execute(
            """
            INSERT INTO human_task_responses
                (task_id, subject, response, on_behalf_of, answered_seq)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (task_id, subject) DO NOTHING
            """,
            (task_id, actor.subject, response, on_behalf_of, seq),
        )
        counted = True

    # ── quorum of DISTINCT authorities with consistent answers ────────────────
    rows = conn.execute(
        "SELECT subject, on_behalf_of, response FROM human_task_responses WHERE task_id=%s",
        (task_id,),
    ).fetchall()
    authorities: dict[str, str] = {}
    for subj, obo, resp in rows:
        authorities[obo or subj] = resp

    new_status, quorum_met = status, False
    if len(authorities) >= quorum_required:
        if len(set(authorities.values())) == 1:
            new_status, quorum_met = "answered", True
            conn.execute(
                "UPDATE human_tasks SET status='answered', updated_at=now() WHERE task_id=%s",
                (task_id,),
            )
            conn.execute(
                "UPDATE timers SET status='cancelled' WHERE task_id=%s AND status='scheduled'",
                (task_id,),
            )
        else:
            new_status = "conflict"
            conn.execute(
                "UPDATE human_tasks SET status='conflict', updated_at=now() WHERE task_id=%s",
                (task_id,),
            )
            # Cancel the SLA ladder first, so a conflicted task does NOT double-escalate; the
            # single conflict-escalation timer below is the only one that should remain scheduled.
            conn.execute(
                "UPDATE timers SET status='cancelled' WHERE task_id=%s AND status='scheduled'",
                (task_id,),
            )
            agg, agg_id = _task_aggregate(run_id, feature_id)
            conn.execute(
                """
                INSERT INTO timers
                    (timer_id, idempotency_key, aggregate, aggregate_id, task_id, kind,
                     fire_at, status, cas_task_version)
                VALUES (%s,%s,%s,%s,%s,'escalation', now(), 'scheduled', %s)
                ON CONFLICT (idempotency_key) DO NOTHING
                """,
                (mint_id("tmr"), f"{task_id}:conflict-escalation", agg, agg_id, task_id,
                 task_version),
            )
    return SignalResult(task_id, new_status, counted=counted, quorum_met=quorum_met)


def submit_human_signal_command(conn: DbConn, cmd):
    """§4.4 command-catalog adapter for `submit_human_signal`. Registering it makes the gate-answer
    path flow through `execute_command`, so it inherits authz (§6.2), command-idempotency, identity
    validation and denial-routing instead of bypassing them. `cmd.args` carries `gate` (consulted by
    authz), `task_id`, `response`, `expected_task_version` and optional `on_behalf_of`."""
    from featuregen.contracts import CommandResult

    args = cmd.args
    result = submit_human_signal(
        conn,
        args["task_id"],
        response=args["response"],
        actor=cmd.actor,
        expected_task_version=args["expected_task_version"],
        on_behalf_of=args.get("on_behalf_of"),
    )
    return CommandResult(
        accepted=result.counted,
        aggregate_id=cmd.aggregate_id or "",
        produced_event_ids=(),
    )
