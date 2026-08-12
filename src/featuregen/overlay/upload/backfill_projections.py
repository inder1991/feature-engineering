"""Re-derive the REBUILDABLE display projections of an already-uploaded catalog.

Migration 1052 added two things a live catalog cannot grow on its own:

* ``graph_node.data_role`` — the derived display role the search FACET reads. The facet mechanism
  reads literal ``graph_node`` columns and nothing else, so until the column is filled the facet is
  empty for every table already in the catalog;
* the TABLE search-document slots for ``definition`` and ``business_context``. Rows written by an
  older build of ``graph._SEARCH_DOC`` keep their old document until something touches them, so a
  catalog uploaded before the change stays unfindable by its own table prose — and for a TECHNICAL
  catalog that prose is the ONLY text a table has at all.

Both are produced by code that runs only during an upload. ``rebuild_search_docs`` was written as
the named backfill seam and had NO production caller; the ``data_role`` projection had none either.
So on a live deployment the new surfaces stayed empty until somebody happened to re-upload — which
is the same inert-mechanism failure this programme keeps finding, one layer down: the mechanism
exists, is correct, and nothing reaches it.

**This is a projection rebuild, never a re-derivation of authority.** It reads the evidence and
decisions that are already there and re-runs the SAME ``resolve_and_project`` an upload runs; it
records no evidence, confirms nothing, and cannot change what any field MEANS. A display column it
fills is display — operational reads still go through the decision log.

**Idempotent in what it PROJECTS.** Run twice, the flat columns and the search documents are
byte-identical. The append-only field-decision log does gain one ``RESOLVED`` event per resolved
field per run — exactly as every re-upload does, because a decision log is append-only by
construction — so the command is safe to repeat but is not free.

**Fail-soft per ref, honest in aggregate.** One table's fault is contained by a savepoint and the
remaining refs still project; the report carries the failure count so the CLI can exit non-zero. A
backfill that swallowed a partial failure and exited 0 would tell an operator the catalog is
consistent when it is not.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass

from featuregen.contracts import DbConn
from featuregen.overlay.upload.field_resolution import resolve_and_project
from featuregen.overlay.upload.graph import rebuild_search_docs
from featuregen.overlay.upload.object_ref import parse_ref

logger = logging.getLogger(__name__)


class UnknownCatalogSource(ValueError):
    """An explicitly-named ``--source`` holds no catalog nodes.

    Raised rather than treated as an empty success: ``rebuild_search_docs`` is correctly a no-op on
    an unknown source, but an OPERATOR who typed a source name and got "0 rows, exit 0" would read a
    typo as a completed backfill."""


@dataclass(frozen=True, slots=True)
class SourceBackfillReportV1:
    """What one catalog's rebuild did, and what it could not do."""

    catalog_source: str
    table_refs_projected: int = 0
    table_refs_failed: int = 0
    search_docs_rebuilt: int = 0

    @property
    def ok(self) -> bool:
        return self.table_refs_failed == 0

    def as_dict(self) -> dict:
        return {"catalog_source": self.catalog_source,
                "table_refs_projected": self.table_refs_projected,
                "table_refs_failed": self.table_refs_failed,
                "search_docs_rebuilt": self.search_docs_rebuilt}


def catalog_sources(conn: DbConn) -> tuple[str, ...]:
    """Every catalog source with graph nodes, in a deterministic order."""
    return tuple(r[0] for r in conn.execute(
        "SELECT DISTINCT catalog_source FROM graph_node ORDER BY catalog_source").fetchall())


def evidence_bearing_table_refs(conn: DbConn, source: str) -> tuple[str, ...]:
    """This source's TABLE logical refs that still carry ACTIVE field evidence.

    Read from ``field_evidence`` rather than from the graph, because the graph is exactly what is
    stale: a table whose ``data_role`` column is empty is indistinguishable from one that has no
    role. The evidence is the thing that survived, and ``resolve_and_project`` iterates only fields
    with active evidence anyway — so anything else would be a second, divergent notion of what is
    projectable.

    The source is re-checked from the PARSED ref rather than trusted to the ``LIKE`` prefix: a
    catalog source containing ``_`` or ``%`` would otherwise over-match, and a backfill that
    projected a neighbouring catalog's refs under this source's name would corrupt the very
    projections it exists to repair.
    """
    rows = conn.execute(
        "SELECT DISTINCT logical_ref FROM field_evidence "
        "WHERE lifecycle = 'active' AND logical_ref LIKE %s ORDER BY logical_ref",
        (f"{source}::%",)).fetchall()
    out: list[str] = []
    for (ref,) in rows:
        try:
            ref_source, _schema, _table, column = parse_ref(ref)
        except ValueError:
            continue
        if column is None and ref_source == source:
            out.append(ref)
    return tuple(out)


def backfill_source(conn: DbConn, source: str) -> SourceBackfillReportV1:
    """Re-project ONE catalog's table display fields, then rebuild its whole search document set.

    Order matters: the search documents are rebuilt LAST so they carry the prose the projections
    just filled in. (``resolve_and_project`` already rebuilds the per-ref document, so this is
    belt-and-braces for the table refs — but it is the only thing that reaches a COLUMN node, or a
    table with no evidence, whose document was written by an older expression.)"""
    projected = failed = 0
    for table_ref in evidence_bearing_table_refs(conn, source):
        try:
            with conn.transaction():    # per-ref savepoint: one fault must not lose the rest
                resolve_and_project(conn, source=source, logical_refs=[table_ref])
            projected += 1
        except Exception:   # noqa: BLE001 — contained per ref; the count is reported, never hidden
            failed += 1
            logger.warning("backfill: table display projection failed for %r ref %r — evidence "
                           "intact, remaining refs still projected", source, table_ref,
                           exc_info=True)
    return SourceBackfillReportV1(
        catalog_source=source, table_refs_projected=projected, table_refs_failed=failed,
        search_docs_rebuilt=rebuild_search_docs(conn, source))


def backfill_projections(conn: DbConn, *, sources: Iterable[str] | None = None,
                         ) -> tuple[SourceBackfillReportV1, ...]:
    """Rebuild the 1052 display projections for ``sources`` (default: every catalog).

    Raises :class:`UnknownCatalogSource` when a NAMED source holds no catalog nodes. Nothing is
    committed here — the caller owns the transaction boundary."""
    known = catalog_sources(conn)
    if sources is None:
        selected: tuple[str, ...] = known
    else:
        selected = tuple(dict.fromkeys(s.strip().lower() for s in sources))
        missing = [s for s in selected if s not in known]
        if missing:
            raise UnknownCatalogSource(
                f"no catalog nodes for {', '.join(missing)!r} — known sources: "
                f"{', '.join(known) or '(none)'}")
    return tuple(backfill_source(conn, source) for source in selected)
