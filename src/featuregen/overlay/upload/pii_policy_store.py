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

**A POINTER CANNOT LICENSE ACROSS CONCEPTS.** The pointer is keyed on a concept and names a
revision; nothing in "revision_id is a foreign key" says the revision has to be a revision OF THAT
CONCEPT. A pointer for a revoked concept aimed at another concept's ACTIVE revision used to license
the revoked one, because the gate's bulk reader joined on ``revision_id`` and then keyed its answer
off the REVISION's concept. Three layers refuse it now and each is deliberate: the DB's composite
``(concept_name, revision_id)`` foreign key (migration 1056) makes the shape unwritable,
:func:`set_current_pii_use_policy` refuses it as a validation error before any write, and every
reader keys off the POINTER's concept and raises on a mismatch rather than answering.

**THE APPROVER RECORD IS TAMPER-EVIDENT.** ``approved_by`` / ``approved_at`` stay OUT of the content
hash — that is what makes re-approval resolve back to the original revision id — so they carry their
OWN ``attestation_hash``, written at insert and re-derived on every read path. Same for the pointer's
``declared_by``. Under single-approver (D14) that one name IS the control, and a control that a plain
``UPDATE`` can rewrite is not one. Identity is untouched; only forgery becomes visible.

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
from datetime import UTC, datetime

from psycopg.types.json import Jsonb

from featuregen.contracts import DbConn
from featuregen.materialize.canonical import materialize_hash
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


def revision_attestation_hash(*, revision_id: str, approved_by: str,
                              approved_at: datetime, provenance_payload: object) -> str:
    """The tamper-evidence seal over the revision's PROVENANCE — never over its content.

    The fields the §6.2 split keeps out of identity, and no domain tag: the two attestation
    payloads in this module have disjoint key sets, so neither can ever be mistaken for
    the other. ``approved_at`` is normalized to UTC before it is rendered, because a timestamptz
    read back under a different session timezone is the SAME instant written differently, and an
    attestation that broke on a session setting would be a false alarm rather than a control.
    ``provenance_payload`` (the stored provenance JSON, which duplicates the actor as
    ``producer_ref``) is sealed too — review residual R2: a second, unsealed copy of the actor
    could otherwise silently disagree with the sealed ``approved_by``."""
    return materialize_hash({
        "revision_id": revision_id,
        "approved_by": approved_by,
        "approved_at": approved_at.astimezone(UTC).isoformat(),
        "provenance": provenance_payload,
    })


def pointer_attestation_hash(*, concept_name: str, revision_id: str, declared_by: str,
                             pointer_version: int) -> str:
    """The same seal over WHO made a revision current, at WHICH pointer version.

    ``pointer_version`` is in it so a forger cannot replay an older, legitimately-attested
    ``declared_by`` onto a later pointer state. ``updated_at`` is deliberately out: it is a clock
    reading, not a decision."""
    return materialize_hash({
        "concept_name": concept_name,
        "revision_id": revision_id,
        "declared_by": declared_by,
        "pointer_version": pointer_version,
    })


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
    """Append ``revision`` if absent (idempotent on identical content) and return its id.

    ``approved_at`` is supplied by the CALLER rather than left to the column default, because the
    attestation covers it: the seal has to be computed over the instant that lands, and
    ``ON CONFLICT DO NOTHING`` gives no ``RETURNING`` row to read one back from. The column keeps
    its ``DEFAULT now()`` for any other writer."""
    if not approved_by or not approved_by.strip():
        raise PolicyValidationError("a data-use policy revision must record who approved it")
    approver = approved_by.strip()
    approved_at = datetime.now(UTC)
    conn.execute(
        "INSERT INTO pii_use_policy_revision ("
        "  revision_id, concept_name, purpose, status, provenance, content_hash, approved_by, "
        "  approved_at, attestation_hash) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (revision_id) DO NOTHING",
        (revision.revision_id, revision.concept_name, revision.purpose, revision.status.value,
         Jsonb(revision.provenance.payload()), revision.content_hash, approver, approved_at,
         revision_attestation_hash(revision_id=revision.revision_id, approved_by=approver,
                                   provenance_payload=revision.provenance.payload(),
                                   approved_at=approved_at)))
    # READ-BACK VERIFY (the house rule): the loader recomputes BOTH hashes, so this proves the row
    # that landed is the row we meant, not merely that INSERT returned.
    if load_pii_use_policy_revision(conn, revision.revision_id) is None:
        raise PolicyStoreConflict(
            f"data-use policy revision {revision.revision_id} did not persist")
    return revision.revision_id


def set_current_pii_use_policy(conn: DbConn, *, concept_name: str, revision_id: str,
                               expected_pointer_version: int, declared_by: str) -> int:
    """Advance the CAS current pointer and return the NEW pointer version.

    A POINTER MAY ONLY NAME A REVISION OF ITS OWN CONCEPT. The revision's concept is read first and
    a mismatch is a ``PolicyValidationError`` — a request no retry can fix, not a race — because a
    pointer for a revoked concept aimed at another concept's ACTIVE revision would license the
    revoked one. The composite FK in migration 1056 refuses the same shape at the database, and the
    readers refuse to answer over it; this layer exists so the refusal names WHAT is wrong instead
    of surfacing as a driver-level constraint error. A revision_id that names NOTHING is left to the
    FK and the CAS, which is what already reports it."""
    if expected_pointer_version < 0:
        raise PolicyValidationError("expected_pointer_version cannot be negative")
    if not declared_by or not declared_by.strip():
        raise PolicyValidationError("advancing a policy pointer must record who declared it")
    owner = conn.execute(
        "SELECT concept_name FROM pii_use_policy_revision WHERE revision_id = %s",
        (revision_id,)).fetchone()
    if owner is not None and owner[0] != concept_name:
        raise PolicyValidationError(
            f"revision {revision_id} declares a policy for {owner[0]!r}, so it cannot become the "
            f"current policy for {concept_name!r}. A data-use policy licenses ONE concept; a "
            f"pointer that crossed concepts would license a meaning nobody approved")
    declarer = declared_by.strip()
    version = expected_pointer_version + 1
    attestation = pointer_attestation_hash(
        concept_name=concept_name, revision_id=revision_id, declared_by=declarer,
        pointer_version=version)
    if expected_pointer_version == 0:
        changed = conn.execute(
            "INSERT INTO pii_use_policy_current "
            "  (concept_name, revision_id, pointer_version, declared_by, attestation_hash) "
            "VALUES (%s,%s,1,%s,%s) ON CONFLICT (concept_name) DO NOTHING",
            (concept_name, revision_id, declarer, attestation)).rowcount
    else:
        changed = conn.execute(
            "UPDATE pii_use_policy_current SET revision_id = %s, "
            "  pointer_version = pointer_version + 1, declared_by = %s, attestation_hash = %s, "
            "  updated_at = now() "
            "WHERE concept_name = %s AND pointer_version = %s",
            (revision_id, declarer, attestation, concept_name,
             expected_pointer_version)).rowcount
    if changed != 1:
        raise PolicyStoreConflict(
            f"the data-use policy for {concept_name!r} advanced past version "
            f"{expected_pointer_version}")
    return version


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


#: Every revision column a verified read needs, in the order :func:`_verified` takes them. Named
#: once so the three read paths cannot drift into verifying different things.
_REVISION_FIELDS = ("concept_name, purpose, status, provenance, content_hash, revision_id, "
                    "approved_by, approved_at, attestation_hash")


def load_pii_use_policy_revision(conn: DbConn,
                                 revision_id: str) -> PiiUsePolicyRevisionV1 | None:
    """Load ONE revision, content- and attestation-verified; corruption raises, never serves.

    ``.active`` ON THE RESULT IS THE REVISION'S OWN STATUS, NOT CURRENCY. A revision can be
    ``active`` and long superseded — approve/revoke/re-approve resolves back to the ORIGINAL
    revision id, so the very first approval stays ``active`` forever while the pointer moves on
    without it. An auditor resolving the revision ids a governed contract recorded must ask
    :func:`current_pii_use_policy` (or, for a whole contract's worth at once,
    :func:`resolve_policy_provenance`) which of them is the concept's current answer TODAY. Reading
    ``.active`` as "still in force" is the one misreading this loader can invite."""
    row = conn.execute(
        f"SELECT {_REVISION_FIELDS} FROM pii_use_policy_revision WHERE revision_id = %s",
        (revision_id,)).fetchone()
    if row is None:
        return None
    return _verified(*row)


def _verified(concept_name: str, purpose: str, status: object, provenance: object,
              content_hash: str, revision_id: str, approved_by: str, approved_at: datetime,
              attestation_hash: str) -> PiiUsePolicyRevisionV1:
    """Rebuild + verify one revision row, on BOTH axes. A row that fails either is NEVER served.

    CONTENT: a purpose or a status somebody edited under the decision that references it; a
    silently-flipped ``revoked``->``active`` is the exact shape of tamper the content hash catches.

    ATTESTATION: ``approved_by`` / ``approved_at`` are outside content identity by design, which
    left the single-approver record itself forgeable by a plain ``UPDATE``. The attestation hash
    covers exactly those two plus the revision id, so a rewritten approver fails on read.

    The status is COERCED to the enum here rather than handed to the dataclass as text: a stored
    value outside the closed vocabulary is store corruption (the 1056 CHECK forbids it), and
    corruption on a READ path is a ``PolicyStoreConflict``, not the 400 a caller could act on."""
    try:
        coerced = PiiUsePolicyStatus(str(status).strip().lower())
    except ValueError as exc:
        raise PolicyStoreConflict(
            f"data-use policy revision {revision_id} carries the unknown status {status!r}; the "
            f"only safe reading of a status this store has no meaning for is to refuse") from exc
    revision = PiiUsePolicyRevisionV1(
        concept_name=concept_name, purpose=purpose, status=coerced,
        provenance=provenance_from_payload(provenance))
    if revision.content_hash != content_hash or revision.revision_id != revision_id:
        raise PolicyStoreConflict(
            f"data-use policy revision {revision_id} fails content verification")
    expected = revision_attestation_hash(
        revision_id=revision_id, approved_by=approved_by, approved_at=approved_at,
        provenance_payload=provenance)
    if expected != attestation_hash:
        raise PolicyStoreConflict(
            f"data-use policy revision {revision_id} fails APPROVER verification: the recorded "
            f"approver no longer matches the attestation written when it was declared. Under "
            f"single-approver (D14) that record is the control, so it is refused rather than served")
    return revision


def _verify_pointer(*, concept_name: str, revision_id: str, declared_by: str,
                    pointer_version: int, attestation_hash: str) -> None:
    """The pointer's half of the same seal — WHO made this revision current, at WHICH version."""
    expected = pointer_attestation_hash(
        concept_name=concept_name, revision_id=revision_id, declared_by=declared_by,
        pointer_version=pointer_version)
    if expected != attestation_hash:
        raise PolicyStoreConflict(
            f"the data-use policy pointer for {concept_name!r} fails DECLARER verification: the "
            f"recorded declarer no longer matches the attestation written when the pointer moved")


def current_pii_use_policy(
    conn: DbConn, concept_name: str,
) -> tuple[PiiUsePolicyRevisionV1, PiiUsePolicyCurrentV1] | None:
    """Resolve the CURRENT policy (revision + pointer) for one concept, content-verified.

    Returns the revision whatever its status: "somebody revoked this" is a fact a surface must be
    able to render, and only the GATE collapses revoked into unlicensed."""
    row = conn.execute(
        "SELECT revision_id, pointer_version, declared_by, updated_at, attestation_hash "
        "FROM pii_use_policy_current WHERE concept_name = %s", (concept_name,)).fetchone()
    if row is None:
        return None
    _verify_pointer(concept_name=concept_name, revision_id=row[0], declared_by=row[2],
                    pointer_version=row[1], attestation_hash=row[4])
    revision = load_pii_use_policy_revision(conn, row[0])
    if revision is None:
        raise PolicyStoreConflict(
            f"pii_use_policy_current for {concept_name!r} points at missing revision {row[0]}")
    if revision.concept_name != concept_name:
        raise PolicyStoreConflict(
            f"pii_use_policy_current for {concept_name!r} names revision {row[0]}, which declares "
            f"a policy for {revision.concept_name!r}")
    return revision, PiiUsePolicyCurrentV1(
        concept_name=concept_name, revision_id=row[0], pointer_version=row[1],
        declared_by=row[2], updated_at=row[3])


def active_pii_use_policies(conn: DbConn, concept_names: Iterable[str]) -> dict[str, str]:
    """THE GATE'S READER: ``{concept_name: revision_id}`` for every named concept whose CURRENT
    policy is ACTIVE. ONE query, whatever the operand count.

    Shape decisions, each of which is a safety property rather than an optimisation:

    * **ONE READ PER CANDIDATE, whatever the operand count** — not one per validation PASS, which
      is what an earlier version of this note claimed. ``_use_gate`` runs on every candidate
      feature from every producer and calls this once per candidate that binds personal data at
      all; the read is skipped entirely for the majority that bind none. What it rules out is the
      per-OPERAND query storm. TODO(cross-candidate batching): a 157-recipe grounding pass still
      asks this question once per pii-binding candidate for an answer set with a handful of
      distinct members. The seam is a caller-owned mapping passed in (the licensed set for the
      whole pass, resolved once), which is a change to ``feature_assist``'s pass structure rather
      than to this function — deliberately not plumbed now, because a stale cache here would
      license a revoked concept and the caching lifetime is the whole design question.
    * **THE POINTER'S CONCEPT IS THE ANSWER'S KEY**, and a revision that belongs to another concept
      is CORRUPTION, not a near-miss. The join is on ``(revision_id, concept_name)`` both, and a
      pointer that matches nothing under that composite raises instead of quietly dropping out —
      otherwise this reader and :func:`pii_use_policy_states` (whose content check fails on the
      same row) would disagree about the same corrupt record, one loudly and one silently.
    * **ABSENCE IS A REFUSAL.** A concept with no row, a concept whose pointer is missing, and a
      concept whose current revision is ``revoked`` are all simply not in the returned mapping.
      There is no state in which this function invents a licence.
    * **The status is read from the REVISION** (the immutable, hash-verified row) rather than from
      anything projected beside the pointer, and it is read off the VERIFIED reconstruction rather
      than off the raw column — the pointer says WHICH revision is current; the revision says what
      it declares, and only a revision that survives verification gets to say anything.
    * **Corruption raises, it does not clear.** Every row is content- AND attestation-verified, so
      a policy whose content, approver or declarer was edited under the decisions that reference it
      fails loudly instead of authorizing a personal-data operand.
    """
    names = sorted({n.strip().lower() for n in concept_names if isinstance(n, str) and n.strip()})
    if not names:
        return {}
    # LEFT JOIN on the COMPOSITE key: an unmatched right side is a pointer naming a revision that is
    # missing or belongs to another concept, and both are corruption this must report rather than
    # silently treat as "no policy".
    rows = conn.execute(
        "SELECT c.concept_name, c.revision_id, c.pointer_version, c.declared_by, "
        "       c.attestation_hash, "
        "       r.purpose, r.status, r.provenance, r.content_hash, r.approved_by, r.approved_at, "
        "       r.attestation_hash "
        "FROM pii_use_policy_current c "
        "LEFT JOIN pii_use_policy_revision r "
        "  ON r.revision_id = c.revision_id AND r.concept_name = c.concept_name "
        "WHERE c.concept_name = ANY(%s)", (names,)).fetchall()
    out: dict[str, str] = {}
    for (concept_name, revision_id, pointer_version, declared_by, pointer_attestation,
         purpose, status, provenance, content_hash, approved_by, approved_at,
         revision_attestation) in rows:
        if content_hash is None:
            raise PolicyStoreConflict(
                f"pii_use_policy_current for {concept_name!r} names revision {revision_id}, which "
                f"is not a revision of that concept. A pointer may only make ITS OWN concept's "
                f"revision current; nothing is licensed off a record in this state")
        _verify_pointer(concept_name=concept_name, revision_id=revision_id,
                        declared_by=declared_by, pointer_version=pointer_version,
                        attestation_hash=pointer_attestation)
        revision = _verified(concept_name, purpose, status, provenance, content_hash, revision_id,
                             approved_by, approved_at, revision_attestation)
        if revision.active:
            out[concept_name] = revision.revision_id
    return out


def pii_use_policy_states(conn: DbConn) -> tuple[PiiUsePolicyStateV1, ...]:
    """EVERY pii-classed concept in the registry with its current policy state, concept-sorted.

    Registry-driven, not table-driven: the panel's job is to show the whole licensable surface —
    including the 20-odd concepts nobody has declared anything about — because a list of the rows
    that happen to exist would silently hide every concept a reviewer might need to approve."""
    eligible = policy_eligible_concepts()
    # The SAME composite LEFT JOIN the gate's reader uses (:func:`active_pii_use_policies`), for the
    # same reason: the two surfaces must give the same verdict on a corrupt record, or a reviewer
    # reading this listing and a feature flow reading the gate would be told different things about
    # one decision with no way to tell which was lying.
    rows = conn.execute(
        "SELECT c.concept_name, c.revision_id, c.pointer_version, c.declared_by, c.updated_at, "
        "       c.attestation_hash, "
        "       r.purpose, r.status, r.provenance, r.content_hash, r.approved_by, r.approved_at, "
        "       r.attestation_hash "
        "FROM pii_use_policy_current c "
        "LEFT JOIN pii_use_policy_revision r "
        "  ON r.revision_id = c.revision_id AND r.concept_name = c.concept_name "
        "WHERE c.concept_name = ANY(%s)", (list(eligible),)).fetchall()
    by_concept = {}
    for (concept_name, revision_id, pointer_version, declared_by, updated_at, pointer_attestation,
         purpose, status, provenance, content_hash, approved_by, approved_at,
         revision_attestation) in rows:
        if content_hash is None:
            raise PolicyStoreConflict(
                f"pii_use_policy_current for {concept_name!r} names revision {revision_id}, which "
                f"is not a revision of that concept")
        _verify_pointer(concept_name=concept_name, revision_id=revision_id,
                        declared_by=declared_by, pointer_version=pointer_version,
                        attestation_hash=pointer_attestation)
        revision = _verified(concept_name, purpose, status, provenance, content_hash, revision_id,
                             approved_by, approved_at, revision_attestation)
        by_concept[concept_name] = PiiUsePolicyStateV1(
            concept_name=concept_name, revision=revision,
            pointer=PiiUsePolicyCurrentV1(
                concept_name=concept_name, revision_id=revision_id,
                pointer_version=pointer_version, declared_by=declared_by, updated_at=updated_at),
            approved_by=approved_by, approved_at=approved_at)
    return tuple(by_concept.get(name, PiiUsePolicyStateV1(concept_name=name))
                 for name in eligible)


@dataclass(frozen=True, slots=True)
class PolicyRevisionProvenanceV1:
    """One resolved policy revision id, with the two facts an auditor actually needs.

    ``status`` is the REVISION's own declaration; ``is_current`` is whether the concept's pointer
    still names it TODAY. They are different questions and the pair exists because the first is
    routinely mistaken for the second: an ``active`` revision that is no longer current is the
    NORMAL outcome of approve -> revoke -> re-approve (content-addressed identity resolves the
    third action back to the first revision), so a contract can truthfully name an ``active``
    revision that licenses nothing right now."""

    revision_id: str
    concept_name: str
    purpose: str
    status: str
    is_current: bool
    current_revision_id: str | None
    approved_by: str | None = None
    approved_at: datetime | None = None


def resolve_policy_provenance(conn: DbConn,
                              revision_ids: Iterable[str]) -> tuple[PolicyRevisionProvenanceV1, ...]:
    """Resolve a governed contract's ``personal_data_policy_revision_ids`` for an AUDITOR.

    The convenience that stops ``.active`` being read as "still in force": every returned row
    carries BOTH the revision's own status and whether its concept's pointer still names it. Two
    reads, whatever the id count. A named id that does not exist RAISES — revisions are immutable
    and are never deleted, so an id a contract recorded and the store cannot produce is corruption,
    not an empty result."""
    ids = sorted({r.strip() for r in revision_ids if isinstance(r, str) and r.strip()})
    if not ids:
        return ()
    rows = conn.execute(
        f"SELECT {_REVISION_FIELDS} FROM pii_use_policy_revision WHERE revision_id = ANY(%s)",
        (ids,)).fetchall()
    revisions = {row[5]: (_verified(*row), row[6], row[7]) for row in rows}
    missing = [r for r in ids if r not in revisions]
    if missing:
        raise PolicyStoreConflict(
            f"data-use policy revisions named by a governed artifact are absent from the store: "
            f"{', '.join(missing)}. Revisions are immutable and are never deleted")
    concepts = sorted({revision.concept_name for revision, _by, _at in revisions.values()})
    pointers = dict(conn.execute(
        "SELECT concept_name, revision_id FROM pii_use_policy_current "
        "WHERE concept_name = ANY(%s)", (concepts,)).fetchall())
    out = []
    for revision_id in ids:
        revision, approved_by, approved_at = revisions[revision_id]
        current = pointers.get(revision.concept_name)
        out.append(PolicyRevisionProvenanceV1(
            revision_id=revision_id, concept_name=revision.concept_name,
            purpose=revision.purpose, status=revision.status.value,
            is_current=current == revision_id, current_revision_id=current,
            approved_by=approved_by, approved_at=approved_at))
    return tuple(out)
