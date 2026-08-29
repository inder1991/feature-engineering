"""The step-5 coordinator's durable state — child §3.5's aggregate, and nothing else.

Lifecycle rules, in one place because five tables have now learned them one incident at a time:

* **Idempotent on exact request content, scoped to LIVE jobs.** ``content_identity_hash`` folds the
  considered revision, the ORDERED selections, the declaration identity, the execution parameters
  and the environment/target — a second click lands on the live row; a retry after FAILED or
  CANCELLED is a NEW job (1107's money-guard lesson: a failed job is not an answer and must not
  hold the identity slot).
* **Claims are leases** (``FOR UPDATE SKIP LOCKED`` over due rows, fence rises per claim), so a
  crashed worker's job is re-claimable when the lease expires — the lane is its own lease
  reconciler, and §9.0.1's wedge shape cannot form.
* **Advances are compare-and-set.** Read-then-write is how `advance_request`'s race happened; every
  move here names the state it moves FROM and refuses if the row moved underneath.
* **BLOCKED is a product outcome, FAILED is a platform failure** — the same split every lane in
  this codebase enforces, because folding them makes an operator page for a governance refusal.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping, Sequence

from featuregen.canonical import jcs_sha256

__all__ = [
    "CodeGenJobV1",
    "CodeGenMemberV1",
    "InvalidJobMove",
    "JobMemberSpecV1",
    "JobMovedUnderneath",
    "JobStatusV1",
    "MemberStateV1",
    "advance_job",
    "claim_due_job",
    "create_job",
    "job_content_identity",
    "read_events",
    "read_job",
    "read_job_actions",
    "read_members",
    "record_event",
    "record_job_action",
    "release_job",
    "update_member",
]


class JobStatusV1(StrEnum):
    REQUESTED = "REQUESTED"
    PLANNING_FORMULAS = "PLANNING_FORMULAS"
    AUTHORING = "AUTHORING"
    READY_TO_BUILD = "READY_TO_BUILD"
    GENERATING_PREVIEW = "GENERATING_PREVIEW"
    #: Terminal BY DESIGN (child §3.5): this aggregate is a PREVIEW coordinator. Sandbox and
    #: production attempts link to it; it does not own them.
    PREVIEW_READY = "PREVIEW_READY"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in (JobStatusV1.PREVIEW_READY, JobStatusV1.BLOCKED,
                        JobStatusV1.FAILED, JobStatusV1.CANCELLED)


#: The one legal path forward, plus the terminals reachable from every live state.
_FORWARD: dict[JobStatusV1, frozenset[JobStatusV1]] = {
    JobStatusV1.REQUESTED: frozenset({JobStatusV1.PLANNING_FORMULAS}),
    JobStatusV1.PLANNING_FORMULAS: frozenset({JobStatusV1.AUTHORING}),
    JobStatusV1.AUTHORING: frozenset({JobStatusV1.READY_TO_BUILD}),
    JobStatusV1.READY_TO_BUILD: frozenset({JobStatusV1.GENERATING_PREVIEW}),
    JobStatusV1.GENERATING_PREVIEW: frozenset({JobStatusV1.PREVIEW_READY}),
    JobStatusV1.PREVIEW_READY: frozenset(),
    JobStatusV1.BLOCKED: frozenset(),
    JobStatusV1.FAILED: frozenset(),
    JobStatusV1.CANCELLED: frozenset(),
}
_TERMINALS_FROM_LIVE = frozenset({JobStatusV1.BLOCKED, JobStatusV1.FAILED, JobStatusV1.CANCELLED})


class MemberStateV1(StrEnum):
    SELECTED = "SELECTED"
    AUTHORING = "AUTHORING"
    FORMULA_READY = "FORMULA_READY"
    BOUND = "BOUND"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class InvalidJobMove(ValueError):
    """A move the state machine does not define — a programmer error, never a race."""


class JobMovedUnderneath(RuntimeError):
    """The row is not in the state the caller read — somebody else advanced it. Re-read."""


@dataclass(frozen=True, slots=True)
class JobMemberSpecV1:
    """One selected feature, as submitted — position is identity (column order)."""

    position: int
    selection_revision_id: str
    considered_revision_id: str
    option_id: str
    formula_strategy: str
    strategy_warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CodeGenJobV1:
    job_id: str
    content_identity_hash: str
    considered_revision_id: str
    target_reading_revision_id: str
    environment_id: str
    logical_group_name: str
    declaration: dict[str, Any]
    execution_parameters: dict[str, Any]
    status: JobStatusV1
    requested_by: str
    terminal_detail: dict[str, Any] | None
    build_set_revision_id: str | None
    generation_request_id: str | None
    lease_fence: int
    attempts: int


@dataclass(frozen=True, slots=True)
class CodeGenMemberV1:
    job_id: str
    position: int
    selection_revision_id: str
    considered_revision_id: str
    option_id: str
    formula_strategy: str
    strategy_warnings: tuple[str, ...]
    member_state: MemberStateV1
    formula_draft_id: str | None
    selection_formula_binding_id: str | None
    blockers: tuple[str, ...]


def job_content_identity(
    *, considered_revision_id: str, target_reading_revision_id: str, environment_id: str,
    logical_group_name: str, selection_revision_ids: Sequence[str],
    declaration_identity: Mapping[str, Any], execution_parameters: Mapping[str, Any],
    principal_scope_revision_id: str,
) -> str:
    """The EXACT request content, hashed — what "the same click" means.

    ``declaration_identity`` is the declaration's IDENTITY payload, not its stored provenance —
    the 55f7235a lesson: folding a clock read forks the identity per request.

    ▲ ``principal_scope_revision_id`` IS PART OF THE CONTENT (B0a, declared identity change). The
    caller's `roles` used to ride inside ``execution_parameters`` and were therefore inside this
    hash, so two people with different read scope asking for the same build were two jobs. Read
    scope is now server-derived and no longer travels in the parameters — and without the resolved
    scope here, those two requests would collapse onto ONE live job, whose recorded authority is
    whichever principal got there first. The server-resolved identity replaces the client's claim
    in the same place it used to sit.
    """
    return jcs_sha256({
        "considered_revision_id": considered_revision_id,
        "target_reading_revision_id": target_reading_revision_id,
        "environment_id": environment_id,
        "logical_group_name": logical_group_name,
        "selection_revision_ids": list(selection_revision_ids),   # ORDERED — order is identity
        "declaration_identity": dict(declaration_identity),
        "execution_parameters": dict(execution_parameters),
        "principal_scope_revision_id": principal_scope_revision_id,
    })


def create_job(
    conn, *, job_id: str, considered_revision_id: str, target_reading_revision_id: str,
    environment_id: str, logical_group_name: str, declaration: Mapping[str, Any],
    declaration_identity: Mapping[str, Any], execution_parameters: Mapping[str, Any],
    members: Sequence[JobMemberSpecV1], requested_by: str, requested_at: str,
    principal_scope_revision_id: str,
) -> tuple[str, bool]:
    """Record the job, its members and its PENDING action rows — or return the LIVE duplicate.

    Returns ``(job_id, created)``. Everything lands in the caller's transaction: a job with no
    members would be a build of nothing, and members without their job would be orphans.
    """
    if not members:
        raise ValueError("a code-generation job with no members is a build of nothing")
    selections = [m.selection_revision_id for m in members]
    if len(set(selections)) != len(selections):
        raise ValueError("a selection appears twice: naming a feature twice in one build is a "
                         "caller error, not two members")

    identity = job_content_identity(
        considered_revision_id=considered_revision_id,
        target_reading_revision_id=target_reading_revision_id, environment_id=environment_id,
        logical_group_name=logical_group_name, selection_revision_ids=selections,
        declaration_identity=declaration_identity, execution_parameters=execution_parameters,
        principal_scope_revision_id=principal_scope_revision_id)

    inserted = conn.execute(
        "INSERT INTO code_generation_job (job_id, content_identity_hash, considered_revision_id, "
        "target_reading_revision_id, environment_id, logical_group_name, declaration_json, "
        "execution_parameters_json, status, requested_by, requested_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s) "
        # ▲ The conflict target NAMES the partial predicate (the W4 lesson: without it Postgres
        # cannot match the partial unique index and refuses the ON CONFLICT outright).
        "ON CONFLICT (content_identity_hash) WHERE status NOT IN ('FAILED', 'CANCELLED') "
        "DO NOTHING RETURNING job_id",
        (job_id, identity, considered_revision_id, target_reading_revision_id, environment_id,
         logical_group_name, json.dumps(dict(declaration)),
         json.dumps(dict(execution_parameters)), JobStatusV1.REQUESTED.value, requested_by,
         requested_at)).fetchone()
    if inserted is None:
        live = conn.execute(
            "SELECT job_id FROM code_generation_job WHERE content_identity_hash = %s "
            "AND status NOT IN ('FAILED', 'CANCELLED')", (identity,)).fetchone()
        # The insert lost to a LIVE row by definition of the index; its id is the answer.
        return live[0], False

    for member in members:
        conn.execute(
            "INSERT INTO code_generation_job_member (job_id, position, selection_revision_id, "
            "considered_revision_id, option_id, formula_strategy, strategy_warnings_json) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)",
            (job_id, member.position, member.selection_revision_id,
             member.considered_revision_id, member.option_id, member.formula_strategy,
             json.dumps(list(member.strategy_warnings))))

    # ▲ One row PER ACTION the journey performs (parent §0.1.3): PENDING until its stage runs,
    # then carrying that stage's own authorization and decision. Recorded at creation so "which
    # acts does this job intend" is answerable before any of them happens.
    for action in ("AUTHOR_FORMULA", "GENERATE_PREVIEW"):
        conn.execute(
            "INSERT INTO code_generation_job_action (job_id, action) VALUES (%s, %s)",
            (job_id, action))

    record_event(conn, job_id, "REQUESTED", {"members": len(members)})
    return job_id, True


def read_job(conn, job_id: str) -> CodeGenJobV1 | None:
    row = conn.execute(
        "SELECT content_identity_hash, considered_revision_id, target_reading_revision_id, "
        "environment_id, logical_group_name, declaration_json, execution_parameters_json, "
        "status, requested_by, terminal_detail_json, build_set_revision_id, "
        "generation_request_id, lease_fence, attempts "
        "FROM code_generation_job WHERE job_id = %s", (job_id,)).fetchone()
    if row is None:
        return None
    return CodeGenJobV1(
        job_id=job_id, content_identity_hash=row[0], considered_revision_id=row[1],
        target_reading_revision_id=row[2], environment_id=row[3], logical_group_name=row[4],
        declaration=row[5] if isinstance(row[5], dict) else json.loads(row[5]),
        execution_parameters=row[6] if isinstance(row[6], dict) else json.loads(row[6]),
        status=JobStatusV1(row[7]), requested_by=row[8],
        terminal_detail=(row[9] if isinstance(row[9], dict) or row[9] is None
                         else json.loads(row[9])),
        build_set_revision_id=row[10], generation_request_id=row[11],
        lease_fence=row[12], attempts=row[13])


def read_members(conn, job_id: str) -> tuple[CodeGenMemberV1, ...]:
    rows = conn.execute(
        "SELECT position, selection_revision_id, considered_revision_id, option_id, "
        "formula_strategy, strategy_warnings_json, member_state, formula_draft_id, "
        "selection_formula_binding_id, blockers_json "
        "FROM code_generation_job_member WHERE job_id = %s ORDER BY position",
        (job_id,)).fetchall()
    return tuple(
        CodeGenMemberV1(
            job_id=job_id, position=r[0], selection_revision_id=r[1],
            considered_revision_id=r[2], option_id=r[3], formula_strategy=r[4],
            strategy_warnings=tuple(r[5] if isinstance(r[5], list) else json.loads(r[5])),
            member_state=MemberStateV1(r[6]), formula_draft_id=r[7],
            selection_formula_binding_id=r[8],
            blockers=tuple(r[9] if isinstance(r[9], list) else json.loads(r[9])))
        for r in rows)


def read_events(conn, job_id: str) -> tuple[dict[str, Any], ...]:
    rows = conn.execute(
        "SELECT event_seq, stage, detail_json, recorded_at FROM code_generation_job_event "
        "WHERE job_id = %s ORDER BY event_seq", (job_id,)).fetchall()
    return tuple(
        {"event_seq": r[0], "stage": r[1],
         "detail": r[2] if isinstance(r[2], dict) else json.loads(r[2]),
         "recorded_at": str(r[3])}
        for r in rows)


def read_job_actions(conn, job_id: str) -> tuple[dict[str, Any], ...]:
    rows = conn.execute(
        "SELECT action, resource_identity_hash, authorization_revision_id, "
        "decision_revision_id, state FROM code_generation_job_action WHERE job_id = %s "
        "ORDER BY action", (job_id,)).fetchall()
    return tuple(
        {"action": r[0], "resource_identity_hash": r[1], "authorization_revision_id": r[2],
         "decision_revision_id": r[3], "state": r[4]}
        for r in rows)


def record_event(conn, job_id: str, stage: str, detail: Mapping[str, Any]) -> None:
    conn.execute(
        "INSERT INTO code_generation_job_event (job_id, stage, detail_json) "
        "VALUES (%s, %s, %s::jsonb)", (job_id, stage, json.dumps(dict(detail))))


def record_job_action(
    conn, job_id: str, action: str, *, resource_identity_hash: str | None = None,
    authorization_revision_id: str | None = None, decision_revision_id: str | None = None,
    state: str,
) -> None:
    """Fill one action row as its stage runs. The composite FK enforces the authorization
    relationship exactly when both halves are named — a PENDING row claims nothing."""
    conn.execute(
        "UPDATE code_generation_job_action SET resource_identity_hash = COALESCE(%s, "
        "resource_identity_hash), authorization_revision_id = COALESCE(%s, "
        "authorization_revision_id), decision_revision_id = COALESCE(%s, decision_revision_id), "
        "state = %s WHERE job_id = %s AND action = %s",
        (resource_identity_hash, authorization_revision_id, decision_revision_id, state,
         job_id, action))


def claim_due_job(conn, *, worker_id: str, lease_seconds: int = 120) -> CodeGenJobV1 | None:
    """Claim ONE due live job — never leased, or lease expired — raising the fence.

    ``FOR UPDATE SKIP LOCKED`` so two workers never fight over a row; the fence identifies THIS
    claim so a zombie's late write is refusable. Runs in the caller's transaction.
    """
    row = conn.execute(
        "UPDATE code_generation_job SET lease_owner = %s, "
        "lease_expires_at = now() + make_interval(secs => %s), "
        "lease_fence = lease_fence + 1, attempts = attempts + 1 "
        "WHERE job_id = (SELECT job_id FROM code_generation_job "
        "  WHERE status IN ('REQUESTED', 'PLANNING_FORMULAS', 'AUTHORING', 'READY_TO_BUILD', "
        "                   'GENERATING_PREVIEW') "
        "    AND (lease_expires_at IS NULL OR lease_expires_at < now()) "
        "  ORDER BY requested_at LIMIT 1 FOR UPDATE SKIP LOCKED) "
        "RETURNING job_id", (worker_id, lease_seconds)).fetchone()
    return None if row is None else read_job(conn, row[0])


def release_job(conn, job_id: str) -> None:
    """Give the lease back without moving state — 'still waiting' is not an advance."""
    conn.execute(
        "UPDATE code_generation_job SET lease_owner = NULL, lease_expires_at = NULL "
        "WHERE job_id = %s", (job_id,))


def advance_job(
    conn, job_id: str, from_status: JobStatusV1, to_status: JobStatusV1, *,
    terminal_detail: Mapping[str, Any] | None = None,
) -> None:
    """Compare-and-set: move ``from_status`` → ``to_status`` or refuse by name."""
    forward = _FORWARD.get(from_status, frozenset())
    if to_status not in forward and not (
            not from_status.is_terminal and to_status in _TERMINALS_FROM_LIVE):
        raise InvalidJobMove(f"{from_status} -> {to_status} is not a move this lifecycle defines")
    if terminal_detail is not None and to_status not in _TERMINALS_FROM_LIVE:
        raise InvalidJobMove(f"terminal detail on a non-terminal move to {to_status}")

    moved = conn.execute(
        "UPDATE code_generation_job SET status = %s, terminal_detail_json = COALESCE(%s::jsonb, "
        "terminal_detail_json), lease_owner = CASE WHEN %s THEN NULL ELSE lease_owner END, "
        "lease_expires_at = CASE WHEN %s THEN NULL ELSE lease_expires_at END "
        "WHERE job_id = %s AND status = %s RETURNING job_id",
        (to_status.value,
         None if terminal_detail is None else json.dumps(dict(terminal_detail)),
         to_status.is_terminal, to_status.is_terminal, job_id, from_status.value)).fetchone()
    if moved is None:
        current = conn.execute(
            "SELECT status FROM code_generation_job WHERE job_id = %s", (job_id,)).fetchone()
        raise JobMovedUnderneath(
            f"job {job_id} is {'absent' if current is None else current[0]!r}, "
            f"not {from_status.value!r}: somebody else moved it — re-read before acting")
    record_event(conn, job_id, to_status.value, dict(terminal_detail or {}))


def update_member(
    conn, job_id: str, position: int, *, state: MemberStateV1,
    formula_draft_id: str | None = None, selection_formula_binding_id: str | None = None,
    blockers: Sequence[str] = (),
) -> None:
    """Advance one member. Blockers land exactly when the state is BLOCKED (the CHECK's rule)."""
    if (state is MemberStateV1.BLOCKED) != bool(blockers):
        raise InvalidJobMove("a BLOCKED member names its blockers, and only a BLOCKED member does")
    conn.execute(
        "UPDATE code_generation_job_member SET member_state = %s, "
        "formula_draft_id = COALESCE(%s, formula_draft_id), "
        "selection_formula_binding_id = COALESCE(%s, selection_formula_binding_id), "
        "blockers_json = %s::jsonb WHERE job_id = %s AND position = %s",
        (state.value, formula_draft_id, selection_formula_binding_id,
         json.dumps(list(blockers)), job_id, position))
