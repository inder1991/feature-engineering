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
from dataclasses import dataclass
from enum import StrEnum

from featuregen.canonical import jcs_sha256
from featuregen.contracts.db import DbConn

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
        "SELECT formula_draft_id FROM formula_draft WHERE formula_identity_hash = %s",
        (identity,)).fetchone()
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
    """Move ONE step and return the new state. The caller commits.

    Raises:
        InvalidTransition: the move is not on the machine's path. A worker that silently skipped a
            stage would produce a READY draft whose trace never records a critic, and nothing
            downstream could tell.
    """
    row = conn.execute(
        "SELECT state FROM formula_draft WHERE formula_draft_id = %s",
        (formula_draft_id,)).fetchone()
    if row is None:
        raise InvalidTransition(f"formula draft {formula_draft_id} does not exist")

    current = DraftStateV1(row[0])
    permitted = _NEXT[current] | (frozenset() if current.is_terminal else _FROM_ANYWHERE)
    if to not in permitted:
        raise InvalidTransition(
            f"formula draft {formula_draft_id} cannot move {current.value} → {to.value}. "
            f"Permitted: {sorted(s.value for s in permitted) or 'nothing — it is terminal'}. "
            f"Re-authoring is a NEW request against a new identity, never a step backwards")

    conn.execute(
        "UPDATE formula_draft SET state = %s, "
        "authoring_run_id = COALESCE(%s, authoring_run_id), "
        "formula_content_hash = COALESCE(%s, formula_content_hash), "
        "formula_json = COALESCE(%s::jsonb, formula_json), "
        "blockers = CASE WHEN %s::jsonb IS NULL THEN blockers ELSE %s::jsonb END, "
        "failure_reason = %s, updated_at = now() WHERE formula_draft_id = %s",
        (to.value, authoring_run_id, formula_content_hash,
         None if formula_json is None else json.dumps(dict(formula_json)),
         None if not blockers else json.dumps([dict(b) for b in blockers]),
         None if not blockers else json.dumps([dict(b) for b in blockers]),
         failure_reason, formula_draft_id))
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
        "SELECT considered_revision_id, option_id, formula_identity_hash, state, "
        "authoring_run_id, formula_content_hash, formula_json, blockers, failure_reason "
        "FROM formula_draft WHERE formula_draft_id = %s", (formula_draft_id,)).fetchone()
    if row is None:
        return None
    return FormulaDraftV1(
        formula_draft_id=formula_draft_id, considered_revision_id=row[0], option_id=row[1],
        formula_identity_hash=row[2], state=DraftStateV1(row[3]), authoring_run_id=row[4],
        formula_content_hash=row[5], formula_json=row[6],
        blockers=tuple(row[7] or ()), failure_reason=row[8])


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
