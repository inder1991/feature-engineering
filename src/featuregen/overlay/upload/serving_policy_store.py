"""Serving-policy persistence (profile plan Task 7; migration 1048).

Immutable ``dataset_serving_policy_revision`` rows + a CAS ``dataset_serving_policy_current``
pointer, keyed on ``entity_id + need_role + serving_purpose``.

**The CAS template is the strictest one that exists** (``bridge_store``'s ``pointer_version``, via
``profile_store``): the writer names the exact ``expected_pointer_version`` it read, and ANY drift
raises :class:`~featuregen.overlay.upload.source_selection.PolicyStoreConflict` — the routes map it
to 409. ``expected_pointer_version`` rides in the request BODY (the ``governance.py`` convention),
never as an optional query gate that fails OPEN when omitted (the ``contract.py`` anti-pattern the
review names).

**Read-back verification.** Revision ids are deterministic from canonical content, so re-recording
identical content is an idempotent ``ON CONFLICT DO NOTHING``; the write then READS BACK the stored
row and re-derives its identity. A row that cannot reproduce the id it is filed under is store
corruption and raises rather than being silently served (the ``load_current_bridge_realizations``
posture).

**Authoring-time reference validation.** :func:`publish_serving_policy` refuses before any write
unless every eligible/preferred dataset ref names a table that exists and that the AUTHOR can see
(``source_selection.assert_dataset_refs_readable``, D11 derived anchor scope). A policy naming a
dataset its author cannot see would be an existence oracle; one naming a dataset nobody can see is
a policy that can never select.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from psycopg.types.json import Jsonb

from featuregen.contracts import DbConn
from featuregen.materialize.canonical import materialize_hash
from featuregen.overlay.upload.source_selection import (
    DatasetNeedRole,
    DatasetServingPolicyRevisionV1,
    PolicyStoreConflict,
    ServingPurpose,
    assert_dataset_refs_readable,
    provenance_from_payload,
)


@dataclass(frozen=True, slots=True)
class ServingPolicyCurrentV1:
    """The CAS current pointer for one ``(entity_id, need_role, serving_purpose)`` policy."""

    entity_id: str
    need_role: str
    serving_purpose: str
    revision_id: str
    pointer_version: int
    declared_by: str


def record_serving_policy_revision(conn: DbConn, revision: DatasetServingPolicyRevisionV1, *,
                                   authored_by: str) -> str:
    """Append ``revision`` if absent (idempotent on identical content) and return its id."""
    if not authored_by or not authored_by.strip():
        raise PolicyStoreConflict("a policy revision must record who authored it")
    conn.execute(
        "INSERT INTO dataset_serving_policy_revision ("
        "  revision_id, entity_id, need_role, serving_purpose, eligible_dataset_refs,"
        "  preferred_dataset_refs, provenance, content_hash, authored_by) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (revision_id) DO NOTHING",
        (revision.revision_id, revision.entity_id, revision.need_role.value,
         revision.serving_purpose.value, Jsonb(list(revision.eligible_dataset_refs)),
         Jsonb(list(revision.preferred_dataset_refs)), Jsonb(revision.provenance.payload()),
         revision.content_hash, authored_by.strip()))
    stored = load_serving_policy_revision(conn, revision.revision_id)
    if stored is None:   # unreachable outside a torn store; fail loudly, not silently
        raise PolicyStoreConflict(
            f"serving policy revision {revision.revision_id} did not persist")
    return revision.revision_id


def set_current_serving_policy(conn: DbConn, *, entity_id: str, need_role: str,
                               serving_purpose: str, revision_id: str,
                               expected_pointer_version: int, declared_by: str) -> int:
    """Advance the CAS current pointer and return the NEW pointer version.

    ``expected_pointer_version=0`` claims first-write (INSERT); ``>=1`` names the exact version the
    caller read (UPDATE ... WHERE pointer_version = expected). A rowcount other than 1 raises."""
    if expected_pointer_version < 0:
        raise PolicyStoreConflict("expected_pointer_version cannot be negative")
    if not declared_by or not declared_by.strip():
        raise PolicyStoreConflict("advancing a policy pointer must record who declared it")
    if expected_pointer_version == 0:
        changed = conn.execute(
            "INSERT INTO dataset_serving_policy_current "
            "  (entity_id, need_role, serving_purpose, revision_id, pointer_version, declared_by) "
            "VALUES (%s,%s,%s,%s,1,%s) "
            "ON CONFLICT (entity_id, need_role, serving_purpose) DO NOTHING",
            (entity_id, need_role, serving_purpose, revision_id, declared_by.strip())).rowcount
    else:
        changed = conn.execute(
            "UPDATE dataset_serving_policy_current SET revision_id = %s, "
            "  pointer_version = pointer_version + 1, declared_by = %s, updated_at = now() "
            "WHERE entity_id = %s AND need_role = %s AND serving_purpose = %s "
            "  AND pointer_version = %s",
            (revision_id, declared_by.strip(), entity_id, need_role, serving_purpose,
             expected_pointer_version)).rowcount
    if changed != 1:
        raise PolicyStoreConflict(
            f"serving policy pointer for ({entity_id!r}, {need_role!r}, {serving_purpose!r}) "
            f"advanced past version {expected_pointer_version}")
    return expected_pointer_version + 1


def publish_serving_policy(conn: DbConn, revision: DatasetServingPolicyRevisionV1, *,
                           expected_pointer_version: int, actor: str,
                           roles: Iterable[str] = ()) -> tuple[str, int]:
    """Validate refs, append the revision and advance the pointer — ONE transaction, in that order.

    Validation FIRST so a policy naming an unreadable dataset writes nothing at all; the CAS is the
    last step so a concurrent writer loses the pointer race rather than the whole authoring."""
    assert_dataset_refs_readable(
        conn, set(revision.eligible_dataset_refs) | set(revision.preferred_dataset_refs),
        roles=roles)
    revision_id = record_serving_policy_revision(conn, revision, authored_by=actor)
    version = set_current_serving_policy(
        conn, entity_id=revision.entity_id, need_role=revision.need_role.value,
        serving_purpose=revision.serving_purpose.value, revision_id=revision_id,
        expected_pointer_version=expected_pointer_version, declared_by=actor)
    return revision_id, version


def current_serving_policy(
    conn: DbConn, *, entity_id: str, need_role: str, serving_purpose: str,
) -> tuple[DatasetServingPolicyRevisionV1, ServingPolicyCurrentV1] | None:
    """Resolve the CURRENT policy (revision + pointer) for one key, content-verified.

    ``None`` when no policy exists — which is a normal state, not a failure: most needs are served
    by an explicit request or a single unambiguous candidate and never need a declaration."""
    row = conn.execute(
        "SELECT revision_id, pointer_version, declared_by FROM dataset_serving_policy_current "
        "WHERE entity_id = %s AND need_role = %s AND serving_purpose = %s",
        (entity_id, need_role, serving_purpose)).fetchone()
    if row is None:
        return None
    revision = load_serving_policy_revision(conn, row[0])
    if revision is None:
        raise PolicyStoreConflict(
            f"dataset_serving_policy_current for ({entity_id!r}, {need_role!r}, "
            f"{serving_purpose!r}) points at missing revision {row[0]}")
    return revision, ServingPolicyCurrentV1(
        entity_id=entity_id, need_role=need_role, serving_purpose=serving_purpose,
        revision_id=row[0], pointer_version=row[1], declared_by=row[2])


def load_serving_policy_revision(conn: DbConn,
                                 revision_id: str) -> DatasetServingPolicyRevisionV1 | None:
    """Load and CONTENT-VERIFY one revision. A stored row that cannot reproduce the id it is filed
    under raises: corruption is surfaced, never served."""
    row = conn.execute(
        "SELECT entity_id, need_role, serving_purpose, eligible_dataset_refs, "
        "       preferred_dataset_refs, provenance, content_hash "
        "FROM dataset_serving_policy_revision WHERE revision_id = %s", (revision_id,)).fetchone()
    if row is None:
        return None
    revision = DatasetServingPolicyRevisionV1(
        entity_id=row[0], need_role=DatasetNeedRole(row[1]),
        serving_purpose=ServingPurpose(row[2]),
        eligible_dataset_refs=tuple(row[3]), preferred_dataset_refs=tuple(row[4]),
        provenance=provenance_from_payload(row[5]))
    recomputed = materialize_hash(revision.content_payload())
    if recomputed != row[6] or revision.revision_id != revision_id:
        raise PolicyStoreConflict(
            f"serving policy revision {revision_id} fails content verification")
    return revision
