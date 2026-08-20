"""The build set a person declared, and the attempts made to build it.

**Two objects, deliberately separate.** A BUILD SET is a declaration — *"build these features,
together, against this target"*. A GENERATION REQUEST is an attempt to act on one. A set may be
attempted more than once (after a provider outage, after an engine gains a capability), and each
attempt has its own status, its own refusals and its own artifact. Folding them together would make
a retry indistinguishable from a second, different build.

**The set is immutable and content-addressed.** Every artifact downstream — group plan, contract,
sealed project, published columns — is derived from its membership. If the set could be edited, a
sealed artifact could stop matching the request it came from while both still looked current.
Changing your mind mints a new set.

**All or nothing.** There is no partial success, and its absence is a decision. A group's identity IS
its membership: building four of five features silently delivers something nobody asked for, under a
contract nobody chose, and the person who asked would have to notice to know. A refusal naming the
feature that could not be built is actionable; a quiet four-fifths is not. This is the same rule
``admit_artifacts_v2`` already applies to a batch, and it applies with more force here.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from featuregen.canonical import jcs_sha256
from featuregen.contracts.db import DbConn

__all__ = [
    "BuildSetV1",
    "GenerationRequestV1",
    "GenerationStatusV1",
    "InvalidStatusMove",
    "build_set_identity",
    "advance_request",
    "read_build_set",
    "read_request",
    "record_build_set",
    "request_generation",
]


class GenerationStatusV1(StrEnum):
    """Where an attempt has got to.

    Explicit states rather than a pair of booleans, so "still running" is distinguishable from "the
    worker died" and from "nothing consumes this table" — the gap S11's verification attempts have
    and this deliberately does not.
    """

    REQUESTED = "REQUESTED"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    #: The build could not be made from what was asked for. A PRODUCT result, naming which member.
    REFUSED = "REFUSED"
    #: The platform could not finish. An OPERATIONAL result, and somebody's to fix.
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in (GenerationStatusV1.SUCCEEDED, GenerationStatusV1.REFUSED,
                        GenerationStatusV1.FAILED, GenerationStatusV1.CANCELLED)

    @property
    def is_live(self) -> bool:
        """Whether an attempt in this state blocks another for the same set and environment."""
        return self in (GenerationStatusV1.REQUESTED, GenerationStatusV1.CLAIMED,
                        GenerationStatusV1.RUNNING)


#: The one legal path forward, plus the terminals reachable from any live state. A table rather than
#: `if` chains so "can a request go from RUNNING back to CLAIMED" has one answer — no, and a retry is
#: a NEW attempt against the same set.
_NEXT: Mapping[GenerationStatusV1, frozenset[GenerationStatusV1]] = {
    GenerationStatusV1.REQUESTED: frozenset({GenerationStatusV1.CLAIMED}),
    GenerationStatusV1.CLAIMED: frozenset({GenerationStatusV1.RUNNING}),
    GenerationStatusV1.RUNNING: frozenset({GenerationStatusV1.SUCCEEDED}),
    GenerationStatusV1.SUCCEEDED: frozenset(),
    GenerationStatusV1.REFUSED: frozenset(),
    GenerationStatusV1.FAILED: frozenset(),
    GenerationStatusV1.CANCELLED: frozenset(),
}

#: Reachable from any LIVE state. A build can be refused at any point (a member with no formula, an
#: operator the renderer cannot emit), the platform can fail at any point, and a user may cancel.
_FROM_ANYWHERE = frozenset({GenerationStatusV1.REFUSED, GenerationStatusV1.FAILED,
                            GenerationStatusV1.CANCELLED})


class InvalidStatusMove(ValueError):
    """A status move the lifecycle does not permit.

    Raised rather than ignored: a worker that skipped RUNNING would produce a SUCCEEDED request
    whose history says it was never worked on, and nothing downstream could tell.
    """


@dataclass(frozen=True, slots=True)
class BuildSetV1:
    """A build set as stored: what was declared, in order."""

    revision_id: str
    target_reading_revision_id: str
    selection_revision_ids: tuple[str, ...]
    declaration: Mapping[str, object]
    content_hash: str


@dataclass(frozen=True, slots=True)
class GenerationRequestV1:
    """One attempt at one build set."""

    request_id: str
    build_set_revision_id: str
    environment_id: str
    #: WHICH approval permitted this work. Carried on the object, not left to a second query: a
    #: reader that has to fetch it separately is a reader that can forget to, and every downstream
    #: check about "was this authorized" would then be optional.
    generation_authorization_revision_id: str
    status: GenerationStatusV1
    sealed_artifact_id: str | None
    refusals: tuple[Mapping[str, str], ...]
    failure_reason: str | None

    @property
    def stage_label(self) -> str:
        """The words a screen shows. Server-owned, so the API and the UI cannot describe one status
        with two different sentences."""
        return {
            GenerationStatusV1.REQUESTED: "Queued",
            GenerationStatusV1.CLAIMED: "Starting…",
            GenerationStatusV1.RUNNING: "Preparing features…",
            GenerationStatusV1.SUCCEEDED: "Code ready",
            GenerationStatusV1.REFUSED: "Cannot be built",
            GenerationStatusV1.FAILED: "Could not finish",
            GenerationStatusV1.CANCELLED: "Cancelled",
        }[self.status]


def build_set_identity(
    *,
    target_reading_revision_id: str,
    selection_revision_ids: Sequence[str],
    declaration_hash: str,
) -> str:
    """*Is this the same build somebody already declared?*

    Content-addressed over the target, the ORDERED members and the declaration. Two people asking
    for the same build get the same set, which makes a re-request cheap rather than a duplicate.

    Order is inside the identity because the order a person chose features in is a fact about the
    build — the dataclass says so, and a hash over a set would quietly disagree with it.
    """
    return jcs_sha256({
        "target_reading_revision_id": target_reading_revision_id,
        "selection_revision_ids": list(selection_revision_ids),
        "declaration_hash": declaration_hash,
    })


def record_build_set(
    conn: DbConn,
    *,
    revision_id: str,
    target_reading_revision_id: str,
    selection_revision_ids: Sequence[str],
    declaration: Mapping[str, object],
    declared_by: str,
    declared_at: str,
) -> tuple[str, bool]:
    """Record a build set, or return the EXISTING one with the same content.

    Returns ``(revision_id, created)``. ``created=False`` means this exact build was already
    declared — by this person a moment ago, or by a colleague last week. Either way there is nothing
    to add, and minting a second identical set would split its attempts across two roots.
    """
    if not selection_revision_ids:
        raise ValueError("a build set with no selections builds nothing")
    if len(set(selection_revision_ids)) != len(selection_revision_ids):
        raise ValueError(
            f"the same selection appears twice: {list(selection_revision_ids)!r}. Order is "
            f"meaningful here, so a duplicate makes 'which position is this feature in' "
            f"unanswerable")

    declaration_hash = jcs_sha256(dict(declaration))
    content = build_set_identity(
        target_reading_revision_id=target_reading_revision_id,
        selection_revision_ids=selection_revision_ids,
        declaration_hash=declaration_hash)

    inserted = conn.execute(
        "INSERT INTO build_set_revision (revision_id, target_reading_revision_id, "
        "declaration_hash, declaration_json, content_hash, declared_by, declared_at) "
        "VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s) "
        "ON CONFLICT (content_hash) DO NOTHING RETURNING revision_id",
        (revision_id, target_reading_revision_id, declaration_hash,
         json.dumps(dict(declaration)), content, declared_by, declared_at)).fetchone()
    if inserted is None:
        existing = conn.execute(
            "SELECT revision_id FROM build_set_revision WHERE content_hash = %s",
            (content,)).fetchone()
        return existing[0], False

    for position, selection in enumerate(selection_revision_ids):
        conn.execute(
            "INSERT INTO build_set_member (revision_id, position, selection_revision_id) "
            "VALUES (%s, %s, %s)", (revision_id, position, selection))
    return revision_id, True


def read_build_set(conn: DbConn, revision_id: str) -> BuildSetV1 | None:
    """One build set with its members IN ORDER, or ``None``."""
    row = conn.execute(
        "SELECT target_reading_revision_id, declaration_json, content_hash "
        "FROM build_set_revision WHERE revision_id = %s", (revision_id,)).fetchone()
    if row is None:
        return None
    members = tuple(r[0] for r in conn.execute(
        "SELECT selection_revision_id FROM build_set_member WHERE revision_id = %s "
        "ORDER BY position", (revision_id,)).fetchall())
    return BuildSetV1(revision_id=revision_id, target_reading_revision_id=row[0],
                      selection_revision_ids=members,
                      declaration=row[1] if isinstance(row[1], dict) else {},
                      content_hash=row[2])


def request_generation(
    conn: DbConn,
    *,
    request_id: str,
    build_set_revision_id: str,
    environment_id: str,
    requested_by: str,
    requested_at: str,
    generation_authorization_revision_id: str,
) -> tuple[str, bool]:
    """Start an attempt, or return the LIVE one for this set and environment.

    Returns ``(request_id, created)``. ``created=False`` is the double-click answer: an attempt is
    already in flight and asking again must not start a second compile of the same thing.

    ``generation_authorization_revision_id`` names WHICH approval permits this work. It travels
    inside a COMPOSITE foreign key with the build set and the environment, so a request naming an
    authorization issued for a different set or cluster is not caught — it cannot be written. REQUIRED,
    with no default. A default of ``None`` would mean absence had two meanings — "predates the
    chain" and "a caller forgot" — and a column whose absence means two things cannot distinguish
    them.

    Idempotent on the WORK — the build set and environment — rather than on a caller-supplied key,
    because a client minting a fresh key per click would defeat a key-based guard and generation
    costs real compute. The partial index scopes this to LIVE attempts only, so a retry after a
    failure is allowed: the guard protects against double-clicks, not against recovery.
    """
    inserted = conn.execute(
        "INSERT INTO generation_request (request_id, build_set_revision_id, environment_id, "
        "status, requested_by, requested_at, generation_authorization_revision_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT DO NOTHING RETURNING request_id",
        (request_id, build_set_revision_id, environment_id,
         GenerationStatusV1.REQUESTED.value, requested_by, requested_at,
         generation_authorization_revision_id)).fetchone()
    if inserted is not None:
        return inserted[0], True

    live = conn.execute(
        "SELECT request_id FROM generation_request "
        "WHERE build_set_revision_id = %s AND environment_id = %s "
        "AND status IN ('REQUESTED','CLAIMED','RUNNING')",
        (build_set_revision_id, environment_id)).fetchone()
    if live is None:
        raise RuntimeError(
            f"generation request {request_id!r} was refused by the live-attempt guard but no live "
            f"attempt exists for {build_set_revision_id!r} in {environment_id!r}: the two readings "
            f"disagree, and proceeding would start work nobody could find")
    return live[0], False


def advance_request(
    conn: DbConn,
    request_id: str,
    to: GenerationStatusV1,
    *,
    sealed_artifact_id: str | None = None,
    refusals: Sequence[Mapping[str, str]] = (),
    failure_reason: str | None = None,
) -> GenerationStatusV1:
    """Move ONE step and return the new status. The caller commits.

    Raises:
        InvalidStatusMove: the move is not on the lifecycle's path. A worker that skipped RUNNING
            would produce a SUCCEEDED request whose history says it was never worked on.
    """
    row = conn.execute(
        "SELECT status FROM generation_request WHERE request_id = %s", (request_id,)).fetchone()
    if row is None:
        raise InvalidStatusMove(f"generation request {request_id} does not exist")

    current = GenerationStatusV1(row[0])
    permitted = _NEXT[current] | (frozenset() if current.is_terminal else _FROM_ANYWHERE)
    if to not in permitted:
        raise InvalidStatusMove(
            f"generation request {request_id} cannot move {current.value} → {to.value}. "
            f"Permitted: {sorted(s.value for s in permitted) or 'nothing — it is terminal'}. "
            f"A retry is a NEW attempt against the same build set, never a step backwards")

    conn.execute(
        "UPDATE generation_request SET status = %s, "
        "sealed_artifact_id = COALESCE(%s, sealed_artifact_id), "
        "refusals = CASE WHEN %s::jsonb IS NULL THEN refusals ELSE %s::jsonb END, "
        "failure_reason = %s, updated_at = now() WHERE request_id = %s",
        (to.value, sealed_artifact_id,
         None if not refusals else json.dumps([dict(r) for r in refusals]),
         None if not refusals else json.dumps([dict(r) for r in refusals]),
         failure_reason, request_id))
    return to


def read_request(conn: DbConn, request_id: str) -> GenerationRequestV1 | None:
    """One attempt as stored, or ``None``."""
    row = conn.execute(
        "SELECT build_set_revision_id, environment_id, status, sealed_artifact_id, refusals, "
        "failure_reason, generation_authorization_revision_id "
        "FROM generation_request WHERE request_id = %s", (request_id,)).fetchone()
    if row is None:
        return None
    return GenerationRequestV1(
        request_id=request_id, build_set_revision_id=row[0], environment_id=row[1],
        generation_authorization_revision_id=row[6],
        status=GenerationStatusV1(row[2]), sealed_artifact_id=row[3],
        refusals=tuple(row[4] or ()), failure_reason=row[5])
