"""Draft formula, asynchronously: two identities, one state machine, and no paid answer bought twice.

**Async is not an optimisation here, it is the shape.** A draft is two provider calls plus validation
plus admission. Doing that inside an HTTP request holds ``get_conn``'s single transaction open across
both LLM calls — the failure ``materialization_runs.py`` refuses for compiles, for the same reasons.
It is also what makes the stage progression visible: a caller polls a row that MOVES, rather than
waiting on a socket that says nothing until it says everything.

**The two identities are the reuse rule.**

* :func:`formula_identity` answers *"would asking the model again produce the same thing?"* — the
  candidate, the planning request, the frozen catalog snapshot, the authoring configuration and
  model contract, and the user's own revision of the definition. Each part is something that, if it
  moved, means the answer could legitimately differ.
* :func:`admission_identity` answers *"is that formula admissible HERE, NOW?"* — the formula's hash,
  the admission policy version, and the engine's capability-set hash.

Folded together, an engine gaining an FX operator would invalidate the formula and buy the same
answer from the model a second time. Kept apart, the capability hash moves, a new admission row is
written against the SAME formula, and nothing is spent.

**Double-clicking must not buy two answers.** The uniqueness is on the formula IDENTITY, not on a
caller-supplied idempotency key: a client that minted a fresh key per click would defeat a key-based
guard, and what is being protected is money.

**Every transition commits on its own.** :func:`advance` moves one step and returns; the worker calls
it between stages. No transaction spans a provider call, so a crash mid-draft leaves the row saying
exactly how far it got. There is deliberately no ``in_flight`` boolean — ``state`` IS that
information, and a second field would let the two disagree.

**BLOCKED is a product result.** A formula can be valid and still name an operator this engine does
not advertise. Recording that as FAILED would send an operator to investigate an outage when the
remedy is an engine capability nobody has proved yet — a different person, a different fix.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum

from featuregen.canonical import jcs_sha256
from featuregen.contracts.db import DbConn
from featuregen.formula.control import FormulaControlFlow

__all__ = [
    "ADMISSION_POLICY_VERSION",
    "DraftStateV1",
    "FormulaDraftV1",
    "InvalidTransition",
    "admission_identity",
    "advance",
    "capability_set_hash",
    "existing_admission",
    "formula_identity",
    "read_draft",
    "record_admission",
    "request_draft",
]

#: The admission RULE SET a decision was made under. Part of the admission identity, so a change to
#: the checks re-decides existing formulas without re-authoring them.
ADMISSION_POLICY_VERSION = 1


class DraftStateV1(StrEnum):
    """Where a draft has got to. Six working states and three terminals."""

    REQUESTED = "REQUESTED"
    AUTHORING = "AUTHORING"
    CRITIC_REVIEW = "CRITIC_REVIEW"
    VALIDATING = "VALIDATING"
    ADMISSION = "ADMISSION"
    READY = "READY"
    #: Valid formula, unmet engine capability or governed requirement. A PRODUCT result.
    BLOCKED = "BLOCKED"
    #: The run could not complete — a provider refusal, a crash. An OPERATIONAL result.
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in (DraftStateV1.READY, DraftStateV1.BLOCKED,
                        DraftStateV1.FAILED, DraftStateV1.CANCELLED)


#: The ONE legal path forward, plus the terminals reachable from any working state. Written as a
#: table rather than as `if` chains so "can a draft go from VALIDATING back to AUTHORING" has one
#: answer — no, and re-authoring is a new request against a new identity.
_NEXT: Mapping[DraftStateV1, frozenset[DraftStateV1]] = {
    DraftStateV1.REQUESTED: frozenset({DraftStateV1.AUTHORING}),
    DraftStateV1.AUTHORING: frozenset({DraftStateV1.CRITIC_REVIEW}),
    DraftStateV1.CRITIC_REVIEW: frozenset({DraftStateV1.VALIDATING}),
    DraftStateV1.VALIDATING: frozenset({DraftStateV1.ADMISSION}),
    DraftStateV1.ADMISSION: frozenset({DraftStateV1.READY}),
    DraftStateV1.READY: frozenset(),
    DraftStateV1.BLOCKED: frozenset(),
    DraftStateV1.FAILED: frozenset(),
    DraftStateV1.CANCELLED: frozenset(),
}

#: Reachable from any NON-terminal state. A provider can refuse at any point and a user may cancel
#: at any point — and BLOCKED belongs with them rather than only after ADMISSION, because a product
#: refusal can be KNOWN before a model is ever asked. A candidate set generated with no pinned
#: catalog snapshot is blocked at REQUESTED: there is nothing to author against, and that is a real
#: answer for a user. Forcing it through AUTHORING → CRITIC_REVIEW → VALIDATING to reach a legal
#: BLOCKED would write four stages that never happened, which is a worse lie than a short path.
#:
#: READY is deliberately NOT here, and that asymmetry is the point: a refusal may be discovered
#: early, but a formula may only be declared ready after the run that produced it. READY stays
#: reachable from ADMISSION alone, so a READY draft whose trace records no critic stays impossible.
_FROM_ANYWHERE = frozenset({DraftStateV1.BLOCKED, DraftStateV1.FAILED, DraftStateV1.CANCELLED})


class InvalidTransition(ValueError):
    """A state move the machine does not permit.

    Raised rather than ignored: a worker that silently skipped a stage would produce a READY draft
    whose trace never records a critic, and nothing downstream could tell.
    """


@dataclass(frozen=True, slots=True)
class RetirementV1:
    """A draft's retirement, as stored (migration 1096)."""

    reason: str
    detail: str
    replacement_draft_id: str | None
    retired_by: str
    retired_at: str


class RetirementDisagreement(RuntimeError):
    """A second retirement or replacement that does not AGREE with the recorded one.

    Distinct from idempotency, which is the same act repeated and is silently fine. This is two
    operators deciding differently about one draft, and `ON CONFLICT DO NOTHING` reported it as
    success — so the second decision vanished and whoever made it believed it had taken effect.
    """


class DraftRetired(FormulaControlFlow):
    """This draft is retired: it is no longer the current answer and must not be advanced, restored
    or compiled.

    ▲ **A CONTROL SIGNAL, NOT A SEMANTIC RESULT — and it has to be typed as one.** While this was a
    bare `RuntimeError`, `_sync_from_trace`'s deliberate re-raise reached the replay orchestrator's
    `except Exception` and was folded into `TECHNICAL_FAILURE (author:DraftRetired)`. Two things
    went wrong there. The spend still stopped, so the harm was not money; the harm was that the
    worker's `except DraftRetired` arm — the one that reports `retired`, leaves the draft's state
    alone and completes the queue item — became UNREACHABLE from the authoring loop, and that a
    withdrawal was recorded permanently, in a write-once run row, as though the software had
    crashed. "Somebody withdrew this" and "this run failed" are different facts and the evidence
    must not confuse them.

    `FormulaControlFlow` is exactly this category ("must never be folded into a semantic result"),
    every one of the orchestrator's handlers already re-raises it, and its nearest sibling
    `LeaseFenceLost` says the same kind of thing: this worker no longer holds the right to spend on
    this run. It remains a `RuntimeError` by inheritance, so no existing caller's `except` changes
    meaning.
    """


@dataclass(frozen=True, slots=True)
class FormulaDraftV1:
    """One draft as stored — what was asked, how far it got, and what it produced."""

    formula_draft_id: str
    considered_revision_id: str
    option_id: str
    formula_identity_hash: str
    state: DraftStateV1
    authoring_run_id: str | None
    formula_content_hash: str | None
    formula_json: Mapping[str, object] | None
    blockers: tuple[Mapping[str, str], ...]
    failure_reason: str | None
    #: WHY this draft is no longer the current answer, or `None`. On the object rather than a second
    #: query away: a reader that has to ask separately is a reader that can forget to, and a retired
    #: draft rendered from `state` alone still reads "Formula ready".
    retirement: RetirementV1 | None = None

    @property
    def is_retired(self) -> bool:
        return self.retirement is not None

    @property
    def stage_label(self) -> str:
        """The words a candidate card shows while this is in flight.

        Owned here rather than in the client, so the API and the screen cannot describe one state
        with two different sentences.
        """
        return {
            DraftStateV1.REQUESTED: "Queued",
            DraftStateV1.AUTHORING: "Authoring formula…",
            DraftStateV1.CRITIC_REVIEW: "Critic review…",
            DraftStateV1.VALIDATING: "Validating…",
            DraftStateV1.ADMISSION: "Checking execution support…",
            DraftStateV1.READY: "Formula ready",
            DraftStateV1.BLOCKED: "Blocked",
            DraftStateV1.FAILED: "Failed",
            DraftStateV1.CANCELLED: "Cancelled",
        }[self.state]


#: Serializes a draft's TRANSITIONS against its RETIREMENT. `pg_advisory_xact_lock` releases on
#: commit or rollback, so nothing has to unlock it, and the key is the draft.
#:
#: `hashtext` is 32-bit, so two unrelated draft ids CAN collide and block each other. That is
#: conservative contention, never incorrect state — the pair simply serializes when it did not have
#: to — and at this cardinality it is not worth a wider key to avoid. Saying "two different drafts
#: never contend" would have been wrong.
#:
#: ▲ WHY A LOCK AND NOT JUST `WHERE NOT EXISTS`. That predicate is evaluated under the statement's
#: SNAPSHOT, so an UNCOMMITTED concurrent retirement is invisible to it — and the FK lock the
#: retirement takes on `formula_draft` is compatible with a non-key update of the same row. An
#: advance overlapping an in-flight retirement could therefore still commit. The predicate remains
#: (it is what refuses an already-visible retirement); the lock is what makes "overlapping" mean
#: "one of them goes first".
_DRAFT_LOCK_NAMESPACE = 0x0FD1


@contextmanager
def _draft_locked(conn: DbConn, formula_draft_id: str):
    """Hold the draft's lock across the statements that depend on it.

    ▲ A TRANSACTION IS PART OF THE LOCK, not an assumption about the caller.
    `pg_advisory_xact_lock` releases on commit — so on an AUTOCOMMIT connection the lock statement
    is its own transaction and the lock is gone before the next statement runs, which reopens
    exactly the race it was taken to close. The production worker happens to call inside a
    transaction; the cleanup runbook's operator connection does not, and neither did the fixture I
    wrote for it.

    `conn.transaction()` starts an explicit one when there is none and nests harmlessly when there
    is, so the guarantee stops depending on how the caller happened to open the connection.
    """
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                     (_DRAFT_LOCK_NAMESPACE, formula_draft_id))
        yield


def formula_identity(
    *,
    considered_revision_id: str,
    option_id: str,
    planning_request_hash: str,
    catalog_snapshot_hash: str,
    authoring_config_hash: str,
    definition_revision: str,
) -> str:
    """*Would asking the model again produce the same thing?*

    Every field is something whose movement legitimately changes the answer. The engine's
    capabilities are deliberately NOT here — they decide whether a formula may be USED, not what the
    model would write, and folding them in would buy the same answer again every time an engine
    gained an operator.
    """
    return jcs_sha256({
        "considered_revision_id": considered_revision_id,
        "option_id": option_id,
        "planning_request_hash": planning_request_hash,
        "catalog_snapshot_hash": catalog_snapshot_hash,
        "authoring_config_hash": authoring_config_hash,
        "definition_revision": definition_revision,
    })


def capability_set_hash(advertised: Sequence[str] | Sequence[tuple[str, str]]) -> str:
    """The identity of an ADVERTISED SET, sorted and de-duplicated.

    The set rather than the engine id: an engine that gains an operator is the same engine, and it
    is the set that moved. Sorted so the order a reader happened to receive them in is not part of
    the identity.

    Members are typed SIGNATURES — ``("aggregate", "sum")`` — since capability became per-variant:
    an engine that gains `avg` while keeping `sum` has genuinely moved, and a hash over bare kinds
    could not see that. Normalised to strings so the two spellings cannot produce two identities for
    the same set, which would re-buy an admission decision that had already been made.
    """
    members = sorted(
        {f"{item[0]}:{item[1]}" if isinstance(item, tuple) else str(item) for item in advertised})
    return jcs_sha256({"advertised": members})


def admission_identity(
    *, formula_content_hash: str, engine_id: str, capability_hash: str,
    admission_policy_version: int = ADMISSION_POLICY_VERSION,
) -> str:
    """*Is that formula admissible here, now?*

    Separate from the formula identity so a capability change re-decides an EXISTING formula. This
    is the function that makes "re-run admission without LLM spend" possible at all.
    """
    return jcs_sha256({
        "formula_content_hash": formula_content_hash,
        "engine_id": engine_id,
        "capability_set_hash": capability_hash,
        "admission_policy_version": admission_policy_version,
    })


def request_draft(
    conn: DbConn,
    *,
    formula_draft_id: str,
    considered_revision_id: str,
    option_id: str,
    planning_request_hash: str,
    catalog_snapshot_hash: str,
    authoring_config_hash: str,
    definition_revision: str,
    requested_by: str,
    requested_at: str,
) -> tuple[str, bool]:
    """Create the draft request, or return the EXISTING one for this identity.

    Returns ``(formula_draft_id, created)``. ``created=False`` is the double-click answer: the same
    candidate, snapshot and configuration already has a draft, and asking again must not buy a second
    paid authoring run. The caller returns 202 either way — the client's question ("is a draft coming
    for this candidate?") has the same answer.

    A short transaction by construction: one INSERT, no provider call anywhere near it.
    """
    identity = formula_identity(
        considered_revision_id=considered_revision_id, option_id=option_id,
        planning_request_hash=planning_request_hash,
        catalog_snapshot_hash=catalog_snapshot_hash,
        authoring_config_hash=authoring_config_hash,
        definition_revision=definition_revision)

    inserted = conn.execute(
        "INSERT INTO formula_draft (formula_draft_id, considered_revision_id, option_id, "
        "planning_request_hash, catalog_snapshot_hash, authoring_config_hash, "
        "definition_revision, formula_identity_hash, state, requested_by, requested_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (formula_identity_hash) DO NOTHING RETURNING formula_draft_id",
        (formula_draft_id, considered_revision_id, option_id, planning_request_hash,
         catalog_snapshot_hash, authoring_config_hash, definition_revision, identity,
         DraftStateV1.REQUESTED.value, requested_by, requested_at)).fetchone()
    if inserted is not None:
        return inserted[0], True

    existing = conn.execute(
        "SELECT d.formula_draft_id, r.reason, r.replacement_draft_id "
        "  FROM formula_draft d "
        "  LEFT JOIN formula_draft_retirement r ON r.formula_draft_id = d.formula_draft_id "
        " WHERE d.formula_identity_hash = %s", (identity,)).fetchone()
    # ▲ A RETIRED IDENTITY IS NOT A USABLE DRAFT. The identity is UNIQUE, so a new draft id alone
    # does not defeat this conflict — the request lands on the retired row and, before this check,
    # was reported as "an identical draft already exists" with `created=False`. Whoever asked would
    # reasonably conclude they already had what they wanted.
    #
    # Raised rather than silently re-minted, because re-minting is not this function's decision:
    # something IDENTITY-BEARING must change (the corrected authoring configuration, the catalog
    # snapshot, the definition revision), and only the caller knows which. The refusal names the
    # replacement when one exists, so the answer is usually "use that one".
    if existing is not None and existing[1] is not None:
        raise DraftRetired(
            f"formula identity {identity} belongs to retired draft {existing[0]} "
            f"({existing[1]}){'' if existing[2] is None else f', replaced by {existing[2]}'}. "
            f"A new draft id does not change the identity — something identity-bearing must "
            f"differ, or this request is asking for the very thing that was retired")
    return existing[0], False


def advance(
    conn: DbConn,
    formula_draft_id: str,
    to: DraftStateV1,
    *,
    authoring_run_id: str | None = None,
    formula_content_hash: str | None = None,
    formula_json: Mapping[str, object] | None = None,
    blockers: Sequence[Mapping[str, str]] = (),
    failure_reason: str | None = None,
) -> DraftStateV1:
    """Move ONE step and return the new state.

    **WHO COMMITS depends on who opened the transaction**, and `_draft_locked` decides that rather
    than assuming: inside an existing transaction this nests via savepoint and the CALLER still
    commits, exactly as before; on a connection with none — including an autocommit one — it opens
    and commits its own, because the advisory lock it takes is released by that commit and a lock
    the caller never scoped would protect nothing.

    Raises:
        InvalidTransition: the move is not on the machine's path. A worker that silently skipped a
            stage would produce a READY draft whose trace never records a critic, and nothing
            downstream could tell.
    """
    with _draft_locked(conn, formula_draft_id):
        return _advance_locked(
            conn, formula_draft_id, to, authoring_run_id=authoring_run_id,
            formula_content_hash=formula_content_hash, formula_json=formula_json,
            blockers=blockers, failure_reason=failure_reason)


def _advance_locked(
    conn: DbConn,
    formula_draft_id: str,
    to: DraftStateV1,
    *,
    authoring_run_id: str | None,
    formula_content_hash: str | None,
    formula_json: Mapping[str, object] | None,
    blockers: Sequence[Mapping[str, str]],
    failure_reason: str | None,
) -> DraftStateV1:
    """The transition itself, with the draft's lock already held."""
    row = conn.execute(
        "SELECT d.state, r.reason FROM formula_draft d "
        "LEFT JOIN formula_draft_retirement r ON r.formula_draft_id = d.formula_draft_id "
        "WHERE d.formula_draft_id = %s", (formula_draft_id,)).fetchone()
    if row is None:
        raise InvalidTransition(f"formula draft {formula_draft_id} does not exist")
    # ▲ THE FENCE. A draft retired WHILE IN FLIGHT kept authoring: more provider calls, more spend,
    # and eventually a READY draft that an operator had already withdrawn. Checked on every step
    # rather than once at claim time, because retirement can happen at any point during a run and
    # the whole reason to withdraw one is usually that it should stop NOW.
    if row[1] is not None:
        raise DraftRetired(
            f"formula draft {formula_draft_id} was retired ({row[1]}) and must not advance to "
            f"{to.value}: continuing would spend against a draft somebody already withdrew, and "
            f"could end in a READY formula nobody wants")

    current = DraftStateV1(row[0])
    permitted = _NEXT[current] | (frozenset() if current.is_terminal else _FROM_ANYWHERE)
    if to not in permitted:
        raise InvalidTransition(
            f"formula draft {formula_draft_id} cannot move {current.value} → {to.value}. "
            f"Permitted: {sorted(s.value for s in permitted) or 'nothing — it is terminal'}. "
            f"Re-authoring is a NEW request against a new identity, never a step backwards")

    # ▲ THE FENCE RIDES THE UPDATE, and the read above is only the cheap check.
    #
    # THE CONTRACT, stated exactly: a retirement and a transition for one draft cannot overlap,
    # because both take `_lock_draft` first — whichever arrives second sees the other's committed
    # result. What this does NOT do is abort a provider call already in flight: an authorized turn
    # finishes, and the retirement stops the NEXT one. That is the honest boundary of a fence made
    # of database state, and it is where the progress callback's own check earns its place.
    #
    # Reading retirement and then writing in a second statement leaves a window: a retirement that
    # commits between them is invisible to the read and unopposed by the write, so the very
    # transition this exists to stop goes through. `NOT EXISTS` inside the UPDATE's own WHERE makes
    # the guard and the write ONE statement — the row is written only if it is still unretired at
    # the instant of writing. Matching no row is the refusal.
    moved = conn.execute(
        "UPDATE formula_draft SET state = %s, "
        "authoring_run_id = COALESCE(%s, authoring_run_id), "
        "formula_content_hash = COALESCE(%s, formula_content_hash), "
        "formula_json = COALESCE(%s::jsonb, formula_json), "
        "blockers = CASE WHEN %s::jsonb IS NULL THEN blockers ELSE %s::jsonb END, "
        "failure_reason = %s, updated_at = now() "
        "WHERE formula_draft_id = %s "
        "  AND NOT EXISTS (SELECT 1 FROM formula_draft_retirement r "
        "                   WHERE r.formula_draft_id = formula_draft.formula_draft_id) "
        "RETURNING formula_draft_id",
        (to.value, authoring_run_id, formula_content_hash,
         None if formula_json is None else json.dumps(dict(formula_json)),
         None if not blockers else json.dumps([dict(b) for b in blockers]),
         None if not blockers else json.dumps([dict(b) for b in blockers]),
         failure_reason, formula_draft_id)).fetchone()
    if moved is None:
        raise DraftRetired(
            f"formula draft {formula_draft_id} was retired while this transition was being "
            f"validated and must not advance to {to.value}: the retirement landed between the "
            f"check and the write, which is exactly the window this guard closes")
    return to


def record_admission(
    conn: DbConn,
    *,
    formula_draft_id: str,
    formula_content_hash: str,
    engine_id: str,
    advertised: Sequence[str],
    admitted: bool,
    blockers: Sequence[Mapping[str, str]] = (),
) -> str:
    """Record an admission decision under its OWN identity. Returns that identity.

    Idempotent on the identity: deciding the same formula against the same capability set twice is
    one decision. A DIFFERENT capability set writes a NEW row against the same formula — which is
    the reuse this table exists for, and why it is append-only.
    """
    capability = capability_set_hash(advertised)
    identity = admission_identity(
        formula_content_hash=formula_content_hash, engine_id=engine_id,
        capability_hash=capability)
    conn.execute(
        "INSERT INTO formula_draft_admission (admission_identity_hash, formula_draft_id, "
        "formula_content_hash, admission_policy_version, engine_id, capability_set_hash, "
        "admitted, blockers) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb) "
        "ON CONFLICT (admission_identity_hash) DO NOTHING",
        (identity, formula_draft_id, formula_content_hash, ADMISSION_POLICY_VERSION,
         engine_id, capability, admitted, json.dumps([dict(b) for b in blockers])))
    return identity


def read_draft(conn: DbConn, formula_draft_id: str) -> FormulaDraftV1 | None:
    """One draft, or ``None``. The polling endpoint's whole implementation."""
    row = conn.execute(
        "SELECT d.considered_revision_id, d.option_id, d.formula_identity_hash, d.state, "
        "d.authoring_run_id, d.formula_content_hash, d.formula_json, d.blockers, "
        "d.failure_reason, r.reason, r.detail, r.replacement_draft_id, r.retired_by, r.retired_at "
        "FROM formula_draft d "
        # LEFT JOIN, in the ONE read every caller uses: retirement travels with the draft rather
        # than being a second query a caller can forget. Without it a retired draft still renders
        # as "Formula ready", which is the state lying to whoever reads it.
        "LEFT JOIN formula_draft_retirement r ON r.formula_draft_id = d.formula_draft_id "
        "WHERE d.formula_draft_id = %s", (formula_draft_id,)).fetchone()
    if row is None:
        return None
    return FormulaDraftV1(
        formula_draft_id=formula_draft_id, considered_revision_id=row[0], option_id=row[1],
        formula_identity_hash=row[2], state=DraftStateV1(row[3]), authoring_run_id=row[4],
        formula_content_hash=row[5], formula_json=row[6],
        blockers=tuple(row[7] or ()), failure_reason=row[8],
        retirement=(None if row[9] is None else RetirementV1(
            reason=row[9], detail=row[10], replacement_draft_id=row[11],
            retired_by=row[12], retired_at=str(row[13]))))


def existing_admission(
    conn: DbConn, *, formula_content_hash: str, engine_id: str, advertised: Sequence[str],
) -> tuple[bool, tuple[Mapping[str, str], ...]] | None:
    """A prior decision for this exact formula and capability set, or ``None``.

    What "re-run admission without LLM spend" reads first: if the capability set has NOT moved, the
    stored decision still holds and nothing needs re-deciding either.
    """
    identity = admission_identity(
        formula_content_hash=formula_content_hash, engine_id=engine_id,
        capability_hash=capability_set_hash(advertised))
    row = conn.execute(
        "SELECT admitted, blockers FROM formula_draft_admission "
        "WHERE admission_identity_hash = %s", (identity,)).fetchone()
    return None if row is None else (row[0], tuple(row[1] or ()))


# ── retirement (1096) ────────────────────────────────────────────────────────────────────────────
def retire_formula_draft(
    conn,
    formula_draft_id: str,
    *,
    reason: str,
    retired_by: str,
    detail: str = "",
    replacement_draft_id: str | None = None,
) -> None:
    """Mark a draft as no longer the current answer. It is NOT deleted — it cannot be.

    `formula_draft_guard` raises on every DELETE, and rightly: a draft is what a person was shown
    and what an authoring run was spent on. So retirement is an APPEND beside it, and readers
    exclude or label retired drafts rather than finding them absent — which keeps "why is this draft
    gone?" answerable instead of a gap.

    Idempotent on the draft: retiring twice is not two facts.
    """
    with _draft_locked(conn, formula_draft_id):
        _retire_locked(conn, formula_draft_id, reason=reason, retired_by=retired_by,
                       detail=detail, replacement_draft_id=replacement_draft_id)


def _retire_locked(
    conn: DbConn,
    formula_draft_id: str,
    *,
    reason: str,
    retired_by: str,
    detail: str,
    replacement_draft_id: str | None,
) -> None:
    """The retirement itself, with the draft's lock already held."""
    inserted = conn.execute(
        "INSERT INTO formula_draft_retirement (formula_draft_id, reason, detail, "
        "replacement_draft_id, retired_by) VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (formula_draft_id) DO NOTHING RETURNING formula_draft_id",
        (formula_draft_id, reason, detail, replacement_draft_id, retired_by)).fetchone()
    if inserted is not None:
        return

    # ▲ ALREADY RETIRED — but is this the SAME act repeated, or a SECOND operator deciding
    # differently? `ON CONFLICT DO NOTHING` alone reported both as success, so a colleague retiring
    # the same draft for a different reason had their decision silently discarded while being told
    # it took effect. Repeating an identical retirement stays free; disagreeing is loud.
    # The COMPLETE caller-controlled tuple, not just reason and actor. Comparing two of the four
    # let the other two disagree silently: the same person retiring for the same reason with a
    # DIFFERENT detail, or naming a different replacement, was reported as the identical act — and
    # the second detail or replacement was discarded while its author was told it had landed.
    recorded = conn.execute(
        "SELECT reason, detail, replacement_draft_id, retired_by FROM formula_draft_retirement "
        "WHERE formula_draft_id = %s", (formula_draft_id,)).fetchone()
    supplied = (reason, detail, replacement_draft_id, retired_by)
    if recorded is not None and tuple(recorded) != supplied:
        raise RetirementDisagreement(
            f"formula draft {formula_draft_id} is already retired as "
            f"(reason, detail, replacement, by) = {tuple(recorded)!r}; this call says "
            f"{supplied!r}. Retiring twice is not two facts — one of these decisions would be "
            f"lost, and the person who made it would be told it had taken effect")


def record_draft_replacement(conn, formula_draft_id: str, *, replacement_draft_id: str) -> None:
    """Name what replaced a retired draft — a SEPARATE, later act.

    Regeneration spends provider money and is somebody's decision, so a retirement is allowed to
    exist without a replacement. The trigger permits this field to be set once and never changed.
    """
    updated = conn.execute(
        "UPDATE formula_draft_retirement SET replacement_draft_id = %s "
        "WHERE formula_draft_id = %s AND replacement_draft_id IS NULL "
        "RETURNING formula_draft_id",
        (replacement_draft_id, formula_draft_id)).fetchone()
    if updated is not None:
        return

    # ▲ The UPDATE matched nothing, and the two reasons need different answers. Previously both
    # reported success: naming a replacement for a draft that was never retired did nothing at all,
    # and naming a SECOND replacement silently kept the first.
    recorded = conn.execute(
        "SELECT replacement_draft_id FROM formula_draft_retirement WHERE formula_draft_id = %s",
        (formula_draft_id,)).fetchone()
    if recorded is None:
        raise RetirementDisagreement(
            f"formula draft {formula_draft_id} has no retirement, so there is nothing for "
            f"{replacement_draft_id} to replace — a replacement recorded against a live draft "
            f"would claim a supersession nobody decided")
    if recorded[0] != replacement_draft_id:
        raise RetirementDisagreement(
            f"formula draft {formula_draft_id} was already replaced by {recorded[0]!r}; this call "
            f"names {replacement_draft_id!r}. 'What replaced this draft' must not have two answers")


def retired_draft_ids(conn) -> frozenset[str]:
    """Every retired draft. Readers filter with this rather than assuming a draft they can see is
    current."""
    return frozenset(
        row[0] for row in conn.execute(
            "SELECT formula_draft_id FROM formula_draft_retirement").fetchall())
