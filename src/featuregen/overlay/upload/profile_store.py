"""Catalog-narrative persistence (profile plan Task 2; migration 1047).

Immutable ``catalog_profile_revision`` rows + a CAS ``catalog_profile_current`` pointer, following
the strictest existing template (``bridge_store``'s pointer_version CAS): the writer names the
exact ``expected_pointer_version`` it read; ANY drift raises :class:`CatalogProfileConflict`
(the routes map it to 409) — a pointer is never advanced over an unseen concurrent write.

Read-back verification: revision ids are DETERMINISTIC from canonical content
(``catalog_profiles.build_catalog_profile_revision``), so re-recording identical content is an
idempotent ``ON CONFLICT DO NOTHING``; the write then READS BACK the stored row and re-derives its
identity — a stored row that cannot reproduce the id it is filed under is store corruption and
raises rather than being silently served (the ``load_current_bridge_realizations`` posture).

Read scope: a catalog's narrative is readable ONLY under the same DERIVED visibility as the
catalog listing — the caller can see >= 1 column of the catalog (``overlay/upload/catalogs.py``
shape; review D "orphan existence oracle": a narrative pointer must never reveal a catalog whose
every column is hidden). :func:`catalog_visible` is that predicate; the routes 404 on it.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable

from psycopg.types.json import Jsonb

from featuregen.contracts import DbConn
from featuregen.materialize.canonical import materialize_hash
from featuregen.overlay.upload.catalog_profiles import (
    CatalogProfileCurrentV1,
    CatalogProfileRevisionV1,
    catalog_narrative_block,
    narrative_content_payload,
)
from featuregen.overlay.upload.read_scope import allowed_classes

logger = logging.getLogger(__name__)


class CatalogProfileConflict(Exception):
    """CAS miss (the pointer advanced past the expected version) or store corruption."""


def record_catalog_profile_revision(conn: DbConn, revision: CatalogProfileRevisionV1) -> str:
    """Append ``revision`` if absent (idempotent on identical content) and return its id.

    Read-back-verify: after the insert the stored row is re-hashed; a row filed under
    ``revision.revision_id`` whose content does not reproduce that id raises — corruption is
    surfaced, never served."""
    conn.execute(
        "INSERT INTO catalog_profile_revision ("
        "  revision_id, catalog_source, display_name, description, business_context,"
        "  business_domains, producer, strength, lifecycle, producer_ref, ingestion_run_id,"
        "  content_hash) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (revision_id) DO NOTHING",
        (revision.revision_id, revision.catalog_source, revision.display_name,
         revision.description, revision.business_context, Jsonb(list(revision.business_domains)),
         revision.producer, revision.strength, revision.lifecycle, revision.producer_ref,
         revision.ingestion_run_id, revision.content_hash))
    stored = _load_revision(conn, revision.revision_id)
    if stored is None:   # unreachable outside a torn store; fail loudly, not silently
        raise CatalogProfileConflict(
            f"catalog profile revision {revision.revision_id} did not persist")
    _verify(stored)
    return revision.revision_id


def set_current_catalog_profile(
    conn: DbConn, *, catalog_source: str, revision_id: str, expected_pointer_version: int,
) -> int:
    """Advance the CAS current pointer to ``revision_id`` and return the NEW pointer version.

    ``expected_pointer_version=0`` claims first-write (INSERT); ``>=1`` names the exact version
    the caller read (UPDATE ... WHERE pointer_version = expected). Exactly the
    ``bridge_store.py`` pattern: a rowcount other than 1 raises :class:`CatalogProfileConflict`."""
    if expected_pointer_version == 0:
        changed = conn.execute(
            "INSERT INTO catalog_profile_current (catalog_source, revision_id, pointer_version) "
            "VALUES (%s, %s, 1) ON CONFLICT (catalog_source) DO NOTHING",
            (catalog_source, revision_id)).rowcount
    else:
        changed = conn.execute(
            "UPDATE catalog_profile_current SET revision_id = %s, "
            "pointer_version = pointer_version + 1, updated_at = now() "
            "WHERE catalog_source = %s AND pointer_version = %s",
            (revision_id, catalog_source, expected_pointer_version)).rowcount
    if changed != 1:
        raise CatalogProfileConflict(
            f"catalog profile pointer for {catalog_source!r} advanced past version "
            f"{expected_pointer_version}")
    return expected_pointer_version + 1


def upsert_current_catalog_profile(conn: DbConn, *, catalog_source: str, revision_id: str) -> int:
    """Serialized pointer advance for the INGEST path and return the new version.

    The uploader has no pointer view to CAS against (the narrative rides the multipart upload),
    and the ingest transaction must not fail an otherwise-good upload over a narrative race — so
    this takes the pointer ROW lock (``FOR UPDATE``) and advances unconditionally under it. The
    API PUT keeps the strict body-carried ``expected_pointer_version`` CAS
    (:func:`set_current_catalog_profile`); this helper exists ONLY for the in-ingest write, which
    already holds the source advisory lock against sibling ingests."""
    row = conn.execute(
        "SELECT pointer_version FROM catalog_profile_current WHERE catalog_source = %s "
        "FOR UPDATE", (catalog_source,)).fetchone()
    if row is None:
        changed = conn.execute(
            "INSERT INTO catalog_profile_current (catalog_source, revision_id, pointer_version) "
            "VALUES (%s, %s, 1) ON CONFLICT (catalog_source) DO NOTHING",
            (catalog_source, revision_id)).rowcount
        if changed == 1:
            return 1
        # A concurrent first-write committed while we waited: lock the now-visible row.
        row = conn.execute(
            "SELECT pointer_version FROM catalog_profile_current WHERE catalog_source = %s "
            "FOR UPDATE", (catalog_source,)).fetchone()
        if row is None:   # unreachable outside a torn store
            raise CatalogProfileConflict(
                f"catalog profile pointer for {catalog_source!r} could not be initialized")
    conn.execute(
        "UPDATE catalog_profile_current SET revision_id = %s, "
        "pointer_version = pointer_version + 1, updated_at = now() WHERE catalog_source = %s",
        (revision_id, catalog_source))
    return row[0] + 1


def current_catalog_profile(
    conn: DbConn, catalog_source: str,
) -> tuple[CatalogProfileRevisionV1, CatalogProfileCurrentV1] | None:
    """Resolve the CURRENT narrative (revision + pointer) for a catalog, content-verified.

    ``None`` when no narrative exists. A current row whose revision cannot reproduce its own
    identity raises (store integrity, never a silent skip)."""
    row = conn.execute(
        "SELECT c.revision_id, c.pointer_version FROM catalog_profile_current c "
        "WHERE c.catalog_source = %s", (catalog_source,)).fetchone()
    if row is None:
        return None
    revision = _load_revision(conn, row[0])
    if revision is None:
        raise CatalogProfileConflict(
            f"catalog_profile_current for {catalog_source!r} points at missing revision {row[0]}")
    _verify(revision)
    return revision, CatalogProfileCurrentV1(
        catalog_source=catalog_source, revision_id=row[0], pointer_version=row[1])


def current_catalog_profile_revision_id(conn: DbConn, catalog_source: str) -> str | None:
    """The current narrative revision id (the D12.10 input to ``dataset_profile_hash``), or
    ``None`` when the catalog has no narrative."""
    row = conn.execute(
        "SELECT revision_id FROM catalog_profile_current WHERE catalog_source = %s",
        (catalog_source,)).fetchone()
    return row[0] if row is not None else None


def current_catalog_narrative_block(conn: DbConn, catalog_source: str) -> dict:
    """The current narrative as MODEL CONTEXT (Task 7b), or ``{}``.

    ``{}`` — never ``None`` — because every caller merges it into a block it is already building,
    and "this catalog has no authored narrative" must add no key at all rather than a key saying
    nothing. `semantic_context.MISSING_CONTEXT_CODES` already carries `catalog_profile_absent` for
    the honest statement of that state; this function must not manufacture a second, blank one.

    FLAG-GATED on `FEATUREGEN_DATASET_PROFILES`, the single Release-A gate. That is not caution, it
    is the flag's contract: with it off the upload part is ignored with a warning and the PUT route
    404s, so NO supported write path can author a narrative — and every existing payload stays
    byte-identical. Reading one anyway would surface state the deployment does not admit exists.

    FAIL-SOFT. A narrative is advisory context; an unavailable or torn narrative store degrades this
    call to "no narrative" and is logged, exactly as `ingest`'s existing catalog-context probe
    does. Failing an upload or a feature-generation request over descriptive prose would be a worse
    outcome than sending the payload without it.
    """
    from featuregen.overlay.upload.profile_vocab import dataset_profiles_enabled

    if not dataset_profiles_enabled():
        return {}
    try:
        current = current_catalog_profile(conn, catalog_source)
    except Exception:  # noqa: BLE001 — advisory: a narrative never fails its caller
        logger.warning("advisory catalog-narrative read failed for %r", catalog_source,
                       exc_info=True)
        return {}
    if current is None:
        return {}
    return catalog_narrative_block(current[0])


def catalog_visible(conn: DbConn, catalog_source: str, *, roles: Iterable[str] = ()) -> bool:
    """The DERIVED catalog visibility (D11 / catalogs.py shape): visible iff the caller can see at
    least one COLUMN in it. The narrative routes 404 when this is false, so a narrative pointer
    can never outlive (or reveal) a catalog the caller may not see."""
    row = conn.execute(
        "SELECT 1 FROM graph_node WHERE catalog_source = %s AND kind = 'column' "
        "AND COALESCE(visible_requires, '{}') <@ %s LIMIT 1",
        (catalog_source, allowed_classes(roles))).fetchone()
    return row is not None


def _load_revision(conn: DbConn, revision_id: str) -> CatalogProfileRevisionV1 | None:
    row = conn.execute(
        "SELECT catalog_source, display_name, description, business_context, business_domains, "
        "producer, strength, lifecycle, producer_ref, ingestion_run_id, content_hash "
        "FROM catalog_profile_revision WHERE revision_id = %s", (revision_id,)).fetchone()
    if row is None:
        return None
    return CatalogProfileRevisionV1(
        catalog_source=row[0], display_name=row[1], description=row[2], business_context=row[3],
        business_domains=tuple(row[4]), producer=row[5], strength=row[6], lifecycle=row[7],
        producer_ref=row[8], ingestion_run_id=row[9], content_hash=row[10],
        revision_id=revision_id)


def _verify(revision: CatalogProfileRevisionV1) -> None:
    """Re-derive the stored row's identity from its stored content; a mismatch is corruption."""
    recomputed = materialize_hash(narrative_content_payload(
        catalog_source=revision.catalog_source, display_name=revision.display_name,
        description=revision.description, business_context=revision.business_context,
        business_domains=revision.business_domains, producer=revision.producer,
        strength=revision.strength, lifecycle=revision.lifecycle))
    if recomputed != revision.content_hash or revision.revision_id != f"cpr_{recomputed}":
        raise CatalogProfileConflict(
            f"catalog profile revision {revision.revision_id} fails content verification")
