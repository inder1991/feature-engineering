"""Crosswalk definition persistence (Release C Task 10; migration 1050).

Immutable ``crosswalk_definition_revision`` rows + a CAS ``crosswalk_definition_current`` pointer
keyed on ``definition_id``. The mechanism is the tree's strictest template
(``bridge_store.py:610-630``) rather than the loosest: every write is verified by READING THE ROW
BACK and re-deriving its identity from the stored content, so a revision that did not persist, or
persisted mangled, is a raised error and never a crosswalk that quietly vanished.

**Nothing here can execute anything.** There is no execution table, no safety column and no
availability column. A stored row means "this crosswalk is proposed and visible"; execution safety
is a separate axis that Task 11 MEASURES (§5.8). ``FEATUREGEN_CROSSWALK_EXECUTION`` gates the
execution a later task builds — discovery is deliberately not behind it.

**Review status lives on the pointer** and its vocabulary is ``LinkReviewStatus`` verbatim — no
third review vocabulary. Confirming a crosswalk moves that column and nothing else: it does not
make one visible (it already is), and it cannot make one executable (nothing on this tree can).

**Read scope is WHOLE-PAYLOAD** (the Release-B discipline). A crosswalk names three datasets and
four-or-more columns; a caller who cannot see any ONE of them does not get a redacted crosswalk,
they do not get the crosswalk. :func:`visible_crosswalk_definitions` filters, because a listing
must omit rather than raise; :func:`publish_crosswalk_definition` raises, because an author who
cannot see what they are writing about is a request error.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from psycopg.types.json import Jsonb

from featuregen.contracts import DbConn
from featuregen.materialize.canonical import materialize_hash
from featuregen.overlay.upload.bridge_assessment import (
    EvidenceKind,
    EvidenceRefV1,
    IdentifierColumnMemberV1,
    IdentifierEndpointV1,
    LinkReviewStatus,
    TypeBasis,
)
from featuregen.overlay.upload.crosswalk import (
    CROSSWALK_WRITE_INVALID,
    CrosswalkContractError,
    CrosswalkDefinitionRevisionV1,
    LogicalMappingPairV1,
)
from featuregen.overlay.upload.object_ref import normalize_ref, parse_ref

#: Storage bounds, enforced HERE before the CHECKs in migration 1050 ever see the row. The CHECK is
#: the backstop for a hand-written row; this is where a discovery bug is caught as a discovery bug.
MAX_PAIRS_PER_LEG = 16
MAX_EVIDENCE_REFS = 64
MAX_REF_LENGTH = 512
MAX_ACTOR_LENGTH = 256


class CrosswalkStoreConflict(Exception):
    """A CAS miss (the pointer advanced past the expected version) or store corruption.

    Distinct from :class:`CrosswalkContractError`: a conflict means "try again with what you now
    know" (409); a contract refusal means "this can never be written as given" (400)."""


@dataclass(frozen=True, slots=True)
class CrosswalkDefinitionCurrentV1:
    """The CAS current pointer for one crosswalk definition."""

    definition_id: str
    revision_id: str
    review_status: LinkReviewStatus
    pointer_version: int
    declared_by: str


@dataclass(frozen=True, slots=True)
class CurrentCrosswalkV1:
    """One CURRENT crosswalk: its definition revision joined to its pointer.

    Deliberately carries no safety, no tier and no executability. There is nothing true to put in
    such a field on this tree, and a nullable one would be read as an answer."""

    revision: CrosswalkDefinitionRevisionV1
    current: CrosswalkDefinitionCurrentV1

    @property
    def review_status(self) -> LinkReviewStatus:
        return self.current.review_status


# ── validation ──────────────────────────────────────────────────────────────────────────────────

def _assert_writable(revision: CrosswalkDefinitionRevisionV1, *, actor: str) -> None:
    if not actor or not actor.strip():
        raise CrosswalkContractError(
            CROSSWALK_WRITE_INVALID, "a crosswalk revision must record who authored it")
    if len(actor.strip()) > MAX_ACTOR_LENGTH:
        raise CrosswalkContractError(
            CROSSWALK_WRITE_INVALID, f"actor exceeds {MAX_ACTOR_LENGTH} characters")
    for leg, pairs in (("source_to_mapping", revision.source_to_mapping_pairs),
                       ("mapping_to_target", revision.mapping_to_target_pairs)):
        if len(pairs) > MAX_PAIRS_PER_LEG:
            raise CrosswalkContractError(
                CROSSWALK_WRITE_INVALID,
                f"the {leg} leg carries {len(pairs)} pairs, over the {MAX_PAIRS_PER_LEG} bound. A "
                "leg that wide is a discovery bug arriving as a storage problem")
    if len(revision.evidence_refs) > MAX_EVIDENCE_REFS:
        raise CrosswalkContractError(
            CROSSWALK_WRITE_INVALID,
            f"{len(revision.evidence_refs)} evidence refs exceeds the {MAX_EVIDENCE_REFS} bound")
    for ref in (*revision.dataset_refs(), *revision.column_refs()):
        if len(ref) > MAX_REF_LENGTH:
            raise CrosswalkContractError(
                CROSSWALK_WRITE_INVALID, f"logical ref {ref!r} exceeds {MAX_REF_LENGTH} characters")


# ── write ───────────────────────────────────────────────────────────────────────────────────────

def record_crosswalk_definition_revision(
    conn: DbConn, revision: CrosswalkDefinitionRevisionV1, *, authored_by: str,
) -> str:
    """Append ``revision`` if absent (idempotent on identical content) and return its id.

    The read-back is not belt-and-braces: an ``ON CONFLICT DO NOTHING`` insert reports success for a
    row it did not write, so the only honest confirmation is re-reading and re-deriving."""
    _assert_writable(revision, actor=authored_by)
    conn.execute(
        "INSERT INTO crosswalk_definition_revision ("
        "  revision_id, definition_id, source_endpoint_ref, target_endpoint_ref,"
        "  mapping_dataset_ref, mapping_temporal_policy_revision_id, definition_json,"
        "  content_hash, authored_by) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (revision_id) DO NOTHING",
        (revision.revision_id, revision.definition_id,
         revision.source_endpoint.logical_table_ref, revision.target_endpoint.logical_table_ref,
         revision.mapping_dataset_ref, revision.mapping_temporal_policy_revision_id,
         Jsonb(revision.content_payload()), materialize_hash(revision.content_payload()),
         authored_by.strip()))
    if load_crosswalk_definition_revision(conn, revision.revision_id) is None:
        raise CrosswalkStoreConflict(
            f"crosswalk definition revision {revision.revision_id} did not persist")
    return revision.revision_id


def set_current_crosswalk_definition(
    conn: DbConn, *, definition_id: str, revision_id: str, expected_pointer_version: int,
    declared_by: str, review_status: LinkReviewStatus = LinkReviewStatus.UNREVIEWED,
) -> int:
    """Advance the CAS current pointer and return the NEW pointer version.

    ``expected_pointer_version=0`` is the CLAIM that no pointer existed; a STORED 0 would make that
    claim indistinguishable from a real version, which is why the CHECK forbids it."""
    if expected_pointer_version < 0:
        raise CrosswalkContractError(
            CROSSWALK_WRITE_INVALID, "expected_pointer_version cannot be negative")
    if not declared_by or not declared_by.strip():
        raise CrosswalkContractError(
            CROSSWALK_WRITE_INVALID, "advancing a crosswalk pointer must record who declared it")
    if expected_pointer_version == 0:
        changed = conn.execute(
            "INSERT INTO crosswalk_definition_current "
            "  (definition_id, revision_id, review_status, pointer_version, declared_by) "
            "VALUES (%s,%s,%s,1,%s) ON CONFLICT (definition_id) DO NOTHING",
            (definition_id, revision_id, review_status.value, declared_by.strip())).rowcount
    else:
        changed = conn.execute(
            "UPDATE crosswalk_definition_current SET revision_id = %s, review_status = %s, "
            "  pointer_version = pointer_version + 1, declared_by = %s, updated_at = now() "
            "WHERE definition_id = %s AND pointer_version = %s",
            (revision_id, review_status.value, declared_by.strip(), definition_id,
             expected_pointer_version)).rowcount
    if changed != 1:
        raise CrosswalkStoreConflict(
            f"crosswalk pointer for {definition_id!r} advanced past version "
            f"{expected_pointer_version}")
    return expected_pointer_version + 1


def set_crosswalk_review_status(
    conn: DbConn, *, definition_id: str, review_status: LinkReviewStatus,
    expected_pointer_version: int, declared_by: str,
) -> int:
    """Record a HUMAN review verdict against the CURRENT revision — accountability only.

    It moves ``review_status`` and the pointer version. It cannot change what the crosswalk IS
    (that is the revision), it does not change whether the crosswalk is visible (it already is),
    and nothing on this tree consults it to decide execution. Truth rule, tested."""
    row = conn.execute(
        "SELECT revision_id FROM crosswalk_definition_current WHERE definition_id = %s",
        (definition_id,)).fetchone()
    if row is None:
        raise CrosswalkStoreConflict(
            f"crosswalk {definition_id!r} has no current pointer to review")
    return set_current_crosswalk_definition(
        conn, definition_id=definition_id, revision_id=row[0],
        expected_pointer_version=expected_pointer_version, declared_by=declared_by,
        review_status=review_status)


def publish_crosswalk_definition(
    conn: DbConn, revision: CrosswalkDefinitionRevisionV1, *, expected_pointer_version: int,
    actor: str, roles: Iterable[str] = (),
    review_status: LinkReviewStatus = LinkReviewStatus.UNREVIEWED,
) -> tuple[str, int]:
    """Validate + read-scope + append the revision + advance the pointer, in that order.

    Nothing is written when a validation refuses. The scope check is the AUTHOR's: a crosswalk
    naming a dataset or column its author cannot see is refused whole, never trimmed."""
    from featuregen.overlay.upload.source_selection import (
        assert_column_refs_readable,
        assert_dataset_refs_readable,
    )

    _assert_writable(revision, actor=actor)
    assert_dataset_refs_readable(conn, revision.dataset_refs(), roles=roles)
    assert_column_refs_readable(conn, revision.column_refs(), roles=roles)
    revision_id = record_crosswalk_definition_revision(conn, revision, authored_by=actor)
    version = set_current_crosswalk_definition(
        conn, definition_id=revision.definition_id, revision_id=revision_id,
        expected_pointer_version=expected_pointer_version, declared_by=actor,
        review_status=review_status)
    return revision_id, version


# ── read ────────────────────────────────────────────────────────────────────────────────────────

def _endpoint_from_payload(payload: dict) -> IdentifierEndpointV1:
    """Rebuild the LOGICAL endpoint from its identity payload.

    Only the logical identity is stored, deliberately: type family, key role and concept authority
    are ASSESSMENT conclusions that belong to the bridge assessment, and duplicating them here would
    create a second place for them to disagree. The rebuilt members carry the neutral
    ``TypeBasis.DECLARED`` placeholder, which never enters a crosswalk identity."""
    return IdentifierEndpointV1(
        logical_table_ref=payload["logical_table_ref"],
        members=tuple(
            IdentifierColumnMemberV1(
                member["logical_column_ref"], "unknown", TypeBasis.DECLARED)
            for member in payload["members"]),
    )


def _revision_from_row(row) -> CrosswalkDefinitionRevisionV1:
    payload = row
    return CrosswalkDefinitionRevisionV1(
        source_endpoint=_endpoint_from_payload(payload["source_endpoint"]),
        mapping_dataset_ref=payload["mapping_dataset_ref"],
        source_to_mapping_pairs=tuple(
            LogicalMappingPairV1(**pair) for pair in payload["source_to_mapping_pairs"]),
        mapping_to_target_pairs=tuple(
            LogicalMappingPairV1(**pair) for pair in payload["mapping_to_target_pairs"]),
        target_endpoint=_endpoint_from_payload(payload["target_endpoint"]),
        mapping_temporal_policy_revision_id=payload["mapping_temporal_policy_revision_id"],
        evidence_refs=tuple(
            EvidenceRefV1(evidence_id=ref["evidence_id"], kind=EvidenceKind(ref["kind"]),
                          producer=ref["producer"], content_hash=ref["content_hash"])
            for ref in payload["evidence_refs"]),
    )


def load_crosswalk_definition_revision(
    conn: DbConn, revision_id: str,
) -> CrosswalkDefinitionRevisionV1 | None:
    """Load and CONTENT-VERIFY one revision; corruption raises rather than being served."""
    row = conn.execute(
        "SELECT definition_json, content_hash, definition_id FROM crosswalk_definition_revision "
        "WHERE revision_id = %s", (revision_id,)).fetchone()
    if row is None:
        return None
    try:
        revision = _revision_from_row(row[0])
    except (CrosswalkContractError, KeyError, TypeError, ValueError) as exc:
        # A STORED payload that no longer satisfies the contract is store corruption, not a caller
        # error — it must surface as the integrity failure it is, not as a 400 blaming whoever read.
        raise CrosswalkStoreConflict(
            f"crosswalk definition revision {revision_id} is not reconstructible: {exc}") from exc
    if (materialize_hash(revision.content_payload()) != row[1]
            or revision.revision_id != revision_id
            or revision.definition_id != row[2]):
        raise CrosswalkStoreConflict(
            f"crosswalk definition revision {revision_id} fails content verification")
    return revision


def current_crosswalk_definition(
    conn: DbConn, definition_id: str,
) -> CurrentCrosswalkV1 | None:
    """Resolve the CURRENT crosswalk (revision + pointer) for one definition, content-verified."""
    row = conn.execute(
        "SELECT revision_id, review_status, pointer_version, declared_by "
        "FROM crosswalk_definition_current WHERE definition_id = %s", (definition_id,)).fetchone()
    if row is None:
        return None
    revision = load_crosswalk_definition_revision(conn, row[0])
    if revision is None:
        raise CrosswalkStoreConflict(
            f"crosswalk_definition_current for {definition_id!r} points at missing revision {row[0]}")
    return CurrentCrosswalkV1(
        revision=revision,
        current=CrosswalkDefinitionCurrentV1(
            definition_id=definition_id, revision_id=row[0],
            review_status=LinkReviewStatus(row[1]), pointer_version=row[2], declared_by=row[3]))


def _graph_key(logical_ref: str) -> tuple[str, str]:
    source, schema, table, column = parse_ref(logical_ref)
    return source, ".".join(part for part in (schema, table, column) if part)


def _visible_refs(conn: DbConn, refs: Sequence[str], *, kind: str,
                  roles: Iterable[str]) -> set[tuple[str, str]]:
    from featuregen.overlay.upload.read_scope import allowed_classes, anchor_visibility_predicate

    if not refs:
        return set()
    allowed = allowed_classes(roles)
    keys = [_graph_key(ref) for ref in refs]
    object_refs = sorted({key[1] for key in keys})
    if kind == "table":
        rows = conn.execute(
            "SELECT gn.catalog_source, lower(gn.object_ref) FROM graph_node gn "
            "WHERE gn.kind = 'table' AND lower(gn.object_ref) = ANY(%s) "
            f"AND {anchor_visibility_predicate('gn')}",
            (object_refs, allowed, allowed)).fetchall()
    else:
        rows = conn.execute(
            "SELECT catalog_source, lower(object_ref) FROM graph_node "
            "WHERE kind = 'column' AND lower(object_ref) = ANY(%s) "
            "AND COALESCE(visible_requires, '{}') <@ %s", (object_refs, allowed)).fetchall()
    return {(r[0], r[1]) for r in rows}


def _load_for_listing(conn: DbConn, revision_id: str) -> CrosswalkDefinitionRevisionV1 | None:
    """Load one revision for a LISTING, degrading a corrupt row to ``None`` instead of raising.

    Inside a SAVEPOINT, the ``context_graph._executable_realization_ids`` discipline: catching the
    exception does not un-abort a transaction a DATABASE fault aborted, and the next statement in
    the dossier's one repeatable-read transaction would then raise ``InFailedSqlTransaction``.
    Scoping the rollback is what makes "omit this one" a degraded answer rather than the first
    casualty of a dossier-wide failure.

    Returning ``None`` here is NOT the same answer as the point read's refusal — see
    :func:`visible_crosswalk_listing`. The caller counts what it omitted."""
    try:
        with conn.transaction():
            return load_crosswalk_definition_revision(conn, revision_id)
    except Exception:       # noqa: BLE001 — corruption must not end a listing
        return None


@dataclass(frozen=True, slots=True)
class CrosswalkListingV1:
    """What a listing found, and what it had to omit because it could not be read.

    The omission is a VALUE, not a log line: a corrupted crosswalk silently absent from a dossier
    is indistinguishable from a catalog that never had one, which is the failure mode the whole
    read-back-and-re-derive discipline exists to prevent."""

    crosswalks: tuple[CurrentCrosswalkV1, ...]
    #: The definition ids whose CURRENT revision would not reconstruct or failed verification.
    corrupt_definition_ids: tuple[str, ...] = ()


#: The truncation/omission key a context section reports an unreadable crosswalk under.
CROSSWALK_OMITTED_UNREADABLE = "crosswalk_unreadable"


def visible_crosswalk_listing(
    conn: DbConn, *, dataset_logical_ref: str | None = None, roles: Iterable[str] = (),
) -> CrosswalkListingV1:
    """Every CURRENT crosswalk this caller may see, optionally narrowed to one dataset.

    WHOLE-PAYLOAD read scope: a crosswalk is returned only when EVERY dataset it names (both
    endpoints AND the mapping dataset) and EVERY column it names are visible to ``roles``. A hidden
    mapping dataset withholds the whole crosswalk — a redacted crosswalk would still disclose that
    two identifier schemes are connected, and by what shape, which is the fact being protected.

    Fail-closed: a ref with no graph node is not PROVABLY visible, so its crosswalk is omitted.

    A LISTING OMITS, IT DOES NOT RAISE — including for a corrupt row. A crosswalk names three
    datasets, so one tampered record raising here takes out the dossier of every column of all
    three: a single corrupted row becomes a catalog-wide outage. The corrupt ones are counted in
    :attr:`CrosswalkListingV1.corrupt_definition_ids` so the omission stays visible, and
    :func:`load_crosswalk_definition_revision` keeps refusing on the point read, where "give me
    THIS crosswalk" deserves the integrity failure rather than a ``None`` that reads as absence."""
    where, params = "", []
    if dataset_logical_ref is not None:
        where = ("WHERE r.source_endpoint_ref = %s OR r.target_endpoint_ref = %s "
                 "   OR r.mapping_dataset_ref = %s")
        params = [dataset_logical_ref, dataset_logical_ref, dataset_logical_ref]
    rows = conn.execute(
        "SELECT c.definition_id, c.revision_id, c.review_status, c.pointer_version, c.declared_by "
        "FROM crosswalk_definition_current c "
        "JOIN crosswalk_definition_revision r ON r.revision_id = c.revision_id "
        f"{where} ORDER BY c.definition_id", params).fetchall()
    if not rows:
        return CrosswalkListingV1(crosswalks=())
    loaded = [(row, _load_for_listing(conn, row[1])) for row in rows]
    corrupt = tuple(row[0] for row, revision in loaded if revision is None)
    all_datasets = sorted({ref for _row, rev in loaded if rev for ref in rev.dataset_refs()})
    all_columns = sorted({ref for _row, rev in loaded if rev for ref in rev.column_refs()})
    visible_tables = _visible_refs(conn, all_datasets, kind="table", roles=roles)
    visible_columns = _visible_refs(conn, all_columns, kind="column", roles=roles)
    out: list[CurrentCrosswalkV1] = []
    for row, revision in loaded:
        if revision is None:
            continue
        needed_tables = {_graph_key(ref) for ref in revision.dataset_refs()}
        needed_columns = {_graph_key(ref) for ref in revision.column_refs()}
        if not (needed_tables <= visible_tables and needed_columns <= visible_columns):
            continue
        out.append(CurrentCrosswalkV1(
            revision=revision,
            current=CrosswalkDefinitionCurrentV1(
                definition_id=row[0], revision_id=row[1],
                review_status=LinkReviewStatus(row[2]), pointer_version=row[3],
                declared_by=row[4])))
    return CrosswalkListingV1(crosswalks=tuple(out), corrupt_definition_ids=corrupt)


def visible_crosswalk_definitions(
    conn: DbConn, *, dataset_logical_ref: str | None = None, roles: Iterable[str] = (),
) -> tuple[CurrentCrosswalkV1, ...]:
    """:func:`visible_crosswalk_listing` for a caller with nowhere to report an omission."""
    return visible_crosswalk_listing(
        conn, dataset_logical_ref=dataset_logical_ref, roles=roles).crosswalks


def crosswalks_for_column_listing(
    conn: DbConn, *, column_logical_ref: str, roles: Iterable[str] = (),
) -> CrosswalkListingV1:
    """The visible crosswalks whose SOURCE or TARGET endpoint contains ``column_logical_ref``.

    The mapping dataset's own columns are deliberately NOT an anchor: a mapping column takes part
    in the crosswalk's mechanism, not in the relationship it expresses. The corrupt count is the
    DATASET's, not the endpoint filter's — a crosswalk that could not be read cannot then be asked
    whether this column is one of its endpoints, and reporting it is the honest answer."""
    source, schema, table, column = parse_ref(column_logical_ref)
    if column is None:
        return CrosswalkListingV1(crosswalks=())
    dataset_ref = normalize_ref(source, schema, table, None)
    listing = visible_crosswalk_listing(conn, dataset_logical_ref=dataset_ref, roles=roles)
    return CrosswalkListingV1(
        crosswalks=tuple(
            crosswalk for crosswalk in listing.crosswalks
            if column_logical_ref in {
                member.logical_column_ref
                for endpoint in (crosswalk.revision.source_endpoint,
                                 crosswalk.revision.target_endpoint)
                for member in endpoint.members}),
        corrupt_definition_ids=listing.corrupt_definition_ids)


def crosswalks_for_column(
    conn: DbConn, *, column_logical_ref: str, roles: Iterable[str] = (),
) -> tuple[CurrentCrosswalkV1, ...]:
    """:func:`crosswalks_for_column_listing` for a caller with nowhere to report an omission."""
    return crosswalks_for_column_listing(
        conn, column_logical_ref=column_logical_ref, roles=roles).crosswalks
