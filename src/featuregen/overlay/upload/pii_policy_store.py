"""PII allow-policy persistence (D14; migration 1056).

Immutable ``pii_use_policy_revision`` rows + a CAS ``pii_use_policy_current`` pointer, keyed on the
CONCEPT name. Same discipline as :mod:`featuregen.overlay.upload.temporal_policy_store` and
:mod:`featuregen.overlay.upload.serving_policy_store` — read either module for the CAS template, the
read-back verification and the body-carried ``expected_pointer_version`` convention. Two things are
specific to this store:

**SINGLE-APPROVER (D14).** :func:`approve_pii_use_policy` and :func:`revoke_pii_use_policy` are ONE
action each. There is no propose/confirm split and no confirmer column; the route's platform-admin
gate plus the immutable who/when/purpose record is the whole control. This is a deliberate
deviation from the four-eyes convention, made by explicit user decision.

**REVOCATION IS A NEW REVISION.** ``status`` is revision CONTENT, so revoking mints a
content-distinct revision (same concept, same purpose, ``status='revoked'``) and advances the
pointer to it. Nothing is deleted and nothing is updated in place. Re-approving with the identical
purpose therefore resolves back to the ORIGINAL revision id — content-addressed identity working as
designed — and WHO made it current is recorded on the pointer row, which is exactly why the pointer
carries ``declared_by``/``updated_at`` and identity does not.

**Exceptions are the house pair.** ``PolicyStoreConflict`` (409: a CAS miss or store corruption) and
``PolicyValidationError`` (400: a request no retry can fix) are reused verbatim from
:mod:`source_selection`. Its docstring says "ONE exception for both policy stores: the two are the
same mechanism over different keys"; this is a third store over a third key and the same mechanism,
so a third pair would be the duplication that note exists to prevent, and would give one route a
different status code for the same race.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from psycopg.types.json import Jsonb

from featuregen.contracts import DbConn
from featuregen.overlay.upload.pii_policy import (
    PiiUsePolicyRevisionV1,
    PiiUsePolicyStatus,
    policy_eligible_concepts,
    validate_policy_concept,
)
from featuregen.overlay.upload.source_selection import (
    PolicyStoreConflict,
    PolicyValidationError,
    assert_policy_write_valid,
    human_declaration_provenance,
    provenance_from_payload,
)


@dataclass(frozen=True, slots=True)
class PiiUsePolicyCurrentV1:
    """The CAS current pointer for one concept's data-use policy."""

    concept_name: str
    revision_id: str
    pointer_version: int
    declared_by: str
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PiiUsePolicyStateV1:
    """One pii-classed concept as a SURFACE sees it — the shape the governance panel renders.

    ``revision`` is ``None`` when nobody has declared anything, which is NOT a gap and NOT a
    failure: it is a concept nobody has had to decide about yet. The three states a reader must be
    able to tell apart are (no policy) / (active, with its purpose and approver) / (revoked, which
    records that somebody withdrew a specific declaration). Collapsing the first and the third into
    one "not allowed" would erase the only one of the two that anyone decided."""

    concept_name: str
    revision: PiiUsePolicyRevisionV1 | None = None
    pointer: PiiUsePolicyCurrentV1 | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None

    @property
    def status(self) -> str:
        """``none`` | ``active`` | ``revoked`` — the closed vocabulary the surfaces render."""
        if self.revision is None:
            return "none"
        return self.revision.status.value

    @property
    def pointer_version(self) -> int:
        """0 when nothing was ever declared — the version a first approve must carry."""
        return 0 if self.pointer is None else self.pointer.pointer_version


def record_pii_use_policy_revision(conn: DbConn, revision: PiiUsePolicyRevisionV1, *,
                                   approved_by: str) -> str:
    """Append ``revision`` if absent (idempotent on identical content) and return its id."""
    if not approved_by or not approved_by.strip():
        raise PolicyValidationError("a data-use policy revision must record who approved it")
    conn.execute(
        "INSERT INTO pii_use_policy_revision ("
        "  revision_id, concept_name, purpose, status, provenance, content_hash, approved_by) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (revision_id) DO NOTHING",
        (revision.revision_id, revision.concept_name, revision.purpose, revision.status.value,
         Jsonb(revision.provenance.payload()), revision.content_hash, approved_by.strip()))
    # READ-BACK VERIFY (the house rule): the loader recomputes the content hash, so this proves the
    # row that landed is the row we meant, not merely that INSERT returned.
    if load_pii_use_policy_revision(conn, revision.revision_id) is None:
        raise PolicyStoreConflict(
            f"data-use policy revision {revision.revision_id} did not persist")
    return revision.revision_id


def set_current_pii_use_policy(conn: DbConn, *, concept_name: str, revision_id: str,
                               expected_pointer_version: int, declared_by: str) -> int:
    """Advance the CAS current pointer and return the NEW pointer version."""
    if expected_pointer_version < 0:
        raise PolicyValidationError("expected_pointer_version cannot be negative")
    if not declared_by or not declared_by.strip():
        raise PolicyValidationError("advancing a policy pointer must record who declared it")
    if expected_pointer_version == 0:
        changed = conn.execute(
            "INSERT INTO pii_use_policy_current "
            "  (concept_name, revision_id, pointer_version, declared_by) "
            "VALUES (%s,%s,1,%s) ON CONFLICT (concept_name) DO NOTHING",
            (concept_name, revision_id, declared_by.strip())).rowcount
    else:
        changed = conn.execute(
            "UPDATE pii_use_policy_current SET revision_id = %s, "
            "  pointer_version = pointer_version + 1, declared_by = %s, updated_at = now() "
            "WHERE concept_name = %s AND pointer_version = %s",
            (revision_id, declared_by.strip(), concept_name, expected_pointer_version)).rowcount
    if changed != 1:
        raise PolicyStoreConflict(
            f"the data-use policy for {concept_name!r} advanced past version "
            f"{expected_pointer_version}")
    return expected_pointer_version + 1


def _publish(conn: DbConn, revision: PiiUsePolicyRevisionV1, *, expected_pointer_version: int,
             actor: str) -> tuple[str, int]:
    """Append the revision and advance the pointer — one transaction, validation first."""
    assert_policy_write_valid(expected_pointer_version=expected_pointer_version, actor=actor)
    revision_id = record_pii_use_policy_revision(conn, revision, approved_by=actor)
    version = set_current_pii_use_policy(
        conn, concept_name=revision.concept_name, revision_id=revision_id,
        expected_pointer_version=expected_pointer_version, declared_by=actor)
    return revision_id, version


def approve_pii_use_policy(conn: DbConn, *, concept_name: str, purpose: str,
                           expected_pointer_version: int, actor: str) -> tuple[str, int]:
    """Declare that ``concept_name`` may be a model input, for ``purpose``. ONE action (D14).

    The concept is validated against the registry first: a protected characteristic is refused with
    wording that says no policy can lift it, and a non-pii concept is refused as needing no policy
    at all. Provenance is ``human/confirmed/active`` — the person IS the authority here, which is
    exactly what the single-approver decision means."""
    revision = PiiUsePolicyRevisionV1(
        concept_name=concept_name, purpose=purpose, status=PiiUsePolicyStatus.ACTIVE,
        provenance=human_declaration_provenance(producer_ref=actor))
    return _publish(conn, revision, expected_pointer_version=expected_pointer_version, actor=actor)


def revoke_pii_use_policy(conn: DbConn, *, concept_name: str, expected_pointer_version: int,
                          actor: str) -> tuple[str, int]:
    """Withdraw the ACTIVE policy for ``concept_name`` — as a NEW revision, never a delete.

    The revoked revision carries the SAME purpose as the one it withdraws, because that is what was
    withdrawn; inventing a new purpose here would record a declaration nobody made. Refuses (400)
    when there is nothing current to revoke, and when the current revision is already revoked —
    both are requests no retry can fix, and a second revocation would advance the pointer for no
    decision."""
    name = validate_policy_concept(concept_name)
    current = current_pii_use_policy(conn, name)
    if current is None:
        raise PolicyValidationError(
            f"there is no data-use policy for {name!r} to revoke. Nothing licenses it today, so "
            f"the feature use gate already refuses it")
    revision, _pointer = current
    if not revision.active:
        raise PolicyValidationError(
            f"the data-use policy for {name!r} is already revoked; there is nothing to withdraw")
    revoked = PiiUsePolicyRevisionV1(
        concept_name=name, purpose=revision.purpose, status=PiiUsePolicyStatus.REVOKED,
        provenance=human_declaration_provenance(producer_ref=actor))
    return _publish(conn, revoked, expected_pointer_version=expected_pointer_version, actor=actor)


def load_pii_use_policy_revision(conn: DbConn,
                                 revision_id: str) -> PiiUsePolicyRevisionV1 | None:
    """Load and CONTENT-VERIFY one revision; corruption raises rather than being served."""
    row = conn.execute(
        "SELECT concept_name, purpose, status, provenance, content_hash "
        "FROM pii_use_policy_revision WHERE revision_id = %s", (revision_id,)).fetchone()
    if row is None:
        return None
    return _verified(row[0], row[1], row[2], row[3], row[4], revision_id)


def _verified(concept_name: str, purpose: str, status: str, provenance: object,
              content_hash: str, revision_id: str) -> PiiUsePolicyRevisionV1:
    """Rebuild + verify one revision row. A row whose stored hash does not match its own content
    is NEVER served: for this store that would mean a purpose or a status somebody edited under the
    decision that references it, and a silently-flipped ``revoked``->``active`` is the exact shape
    of tamper this hash exists to catch."""
    revision = PiiUsePolicyRevisionV1(
        concept_name=concept_name, purpose=purpose, status=status,
        provenance=provenance_from_payload(provenance))
    if revision.content_hash != content_hash or revision.revision_id != revision_id:
        raise PolicyStoreConflict(
            f"data-use policy revision {revision_id} fails content verification")
    return revision


def current_pii_use_policy(
    conn: DbConn, concept_name: str,
) -> tuple[PiiUsePolicyRevisionV1, PiiUsePolicyCurrentV1] | None:
    """Resolve the CURRENT policy (revision + pointer) for one concept, content-verified.

    Returns the revision whatever its status: "somebody revoked this" is a fact a surface must be
    able to render, and only the GATE collapses revoked into unlicensed."""
    row = conn.execute(
        "SELECT revision_id, pointer_version, declared_by, updated_at FROM pii_use_policy_current "
        "WHERE concept_name = %s", (concept_name,)).fetchone()
    if row is None:
        return None
    revision = load_pii_use_policy_revision(conn, row[0])
    if revision is None:
        raise PolicyStoreConflict(
            f"pii_use_policy_current for {concept_name!r} points at missing revision {row[0]}")
    return revision, PiiUsePolicyCurrentV1(
        concept_name=concept_name, revision_id=row[0], pointer_version=row[1],
        declared_by=row[2], updated_at=row[3])


def active_pii_use_policies(conn: DbConn, concept_names: Iterable[str]) -> dict[str, str]:
    """THE GATE'S READER: ``{concept_name: revision_id}`` for every named concept whose CURRENT
    policy is ACTIVE. ONE query, whatever the operand count.

    Shape decisions, each of which is a safety property rather than an optimisation:

    * **ONE bulk read per validation pass.** ``_use_gate`` runs on every candidate feature from
      every producer; a per-operand query would make a 157-recipe grounding pass issue a query
      storm for a question with at most a handful of distinct answers.
    * **ABSENCE IS A REFUSAL.** A concept with no row, a concept whose pointer is missing, and a
      concept whose current revision is ``revoked`` are all simply not in the returned mapping.
      There is no state in which this function invents a licence.
    * **REVOKED IS FILTERED IN SQL, and the status is read from the REVISION** (the immutable,
      hash-verified row) rather than from anything projected beside the pointer. The pointer says
      WHICH revision is current; the revision says what it declares.
    * **Corruption raises, it does not clear.** Every returned row is content-verified, so a policy
      whose stored content was edited under the decisions that reference it fails loudly instead of
      authorizing a personal-data operand.
    """
    names = sorted({n.strip().lower() for n in concept_names if isinstance(n, str) and n.strip()})
    if not names:
        return {}
    rows = conn.execute(
        "SELECT r.concept_name, r.purpose, r.status, r.provenance, r.content_hash, r.revision_id "
        "FROM pii_use_policy_current c "
        "JOIN pii_use_policy_revision r ON r.revision_id = c.revision_id "
        "WHERE c.concept_name = ANY(%s) AND r.status = %s",
        (names, PiiUsePolicyStatus.ACTIVE.value)).fetchall()
    out: dict[str, str] = {}
    for concept_name, purpose, status, provenance, content_hash, revision_id in rows:
        revision = _verified(concept_name, purpose, status, provenance, content_hash, revision_id)
        out[revision.concept_name] = revision.revision_id
    return out


def pii_use_policy_states(conn: DbConn) -> tuple[PiiUsePolicyStateV1, ...]:
    """EVERY pii-classed concept in the registry with its current policy state, concept-sorted.

    Registry-driven, not table-driven: the panel's job is to show the whole licensable surface —
    including the 20-odd concepts nobody has declared anything about — because a list of the rows
    that happen to exist would silently hide every concept a reviewer might need to approve."""
    eligible = policy_eligible_concepts()
    rows = conn.execute(
        "SELECT c.concept_name, c.revision_id, c.pointer_version, c.declared_by, c.updated_at, "
        "       r.purpose, r.status, r.provenance, r.content_hash, r.approved_by, r.approved_at "
        "FROM pii_use_policy_current c "
        "JOIN pii_use_policy_revision r ON r.revision_id = c.revision_id "
        "WHERE c.concept_name = ANY(%s)", (list(eligible),)).fetchall()
    by_concept = {}
    for (concept_name, revision_id, pointer_version, declared_by, updated_at,
         purpose, status, provenance, content_hash, approved_by, approved_at) in rows:
        revision = _verified(concept_name, purpose, status, provenance, content_hash, revision_id)
        by_concept[concept_name] = PiiUsePolicyStateV1(
            concept_name=concept_name, revision=revision,
            pointer=PiiUsePolicyCurrentV1(
                concept_name=concept_name, revision_id=revision_id,
                pointer_version=pointer_version, declared_by=declared_by, updated_at=updated_at),
            approved_by=approved_by, approved_at=approved_at)
    return tuple(by_concept.get(name, PiiUsePolicyStateV1(concept_name=name))
                 for name in eligible)
