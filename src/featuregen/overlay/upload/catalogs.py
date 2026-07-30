"""The catalog-LISTING read model — the pick-list behind ``GET /catalogs``.

WHY IT EXISTS. Every governance / readiness / semantics surface is keyed by a catalog slug the caller
had to already know: the Governance Review screen opens on an empty "source" text input and renders
nothing until the operator guesses right. The live slugs are ``cib`` and ``ftr`` — neither
discoverable nor memorable. This module enumerates the slugs a caller is allowed to know about.

VISIBILITY IS DERIVED, because catalog-level visibility does not exist. The only mechanism in the
system is column-level: migration 1032's generated ``graph_node.visible_requires`` (folding the raw
file tag and the governed concept floor) tested with ``<@ allowed_classes(roles)`` — the identical
predicate asset_detail, search, readiness and the planner's scope resolver apply. So:

    a catalog is visible to a caller iff the caller can see AT LEAST ONE COLUMN in it.

That rule is not implemented as a filter over an independently-built catalog list; it falls out of a
single ``GROUP BY`` over the read-scoped column rows. A catalog whose every column is hidden produces
no group, so it is ABSENT — no name, no zero-count placeholder, no row. Fail-closed by construction:
there is no code path that can emit a catalog the caller cannot see a column in, and adding one would
mean deleting the ``GROUP BY``.

WHY ``graph_node`` IS THE UNIVERSE. Two other enumerations exist and both are wrong for this:

* ``governance_analytics.list_source_governance_summaries`` reads
  ``SELECT DISTINCT catalog_source FROM overlay_proposal``. That read model structurally never sees
  an entity bridge — ``overlay/projection.py`` skips bridge facts because a two-source fact cannot
  live in a single-``catalog_source`` row — and it carries no visibility column, so it could not be
  scoped even if it were complete.
* ``planner/scope.resolve_catalog_scope`` already derives the same read-scoped source list, but it
  additionally requires a drift watermark, truncates at a consideration bound, and returns a frozen
  planner contract. It answers "what may the planner join across", not "what may this operator open".

NO DISPLAY NAME. There is no display-name concept anywhere in this system: ``graph_node`` carries no
per-catalog label, and the upload surface takes the slug as the only identifier. This module returns
the slug and nothing that resembles a prose name. A UI wanting a readable form can upper-case it
(``cib`` -> ``CIB``); the API must not ship a name it cannot source.

NO PENDING COUNTS. The governance queue owns those (``/sources/{source}/governance/*``), which the
screen fetches anyway. A second count here would drift from the first.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from featuregen.overlay.upload.read_scope import allowed_classes, visibility_predicate

#: Column nodes only. A ``kind = 'table'`` node carries no sensitivity of its own (its
#: ``visible_requires`` is ``{}``, i.e. world-visible), so it can never be the basis of a scoped
#: count — the table universe is derived from the visible COLUMNS instead.
_LIST_CATALOGS_SQL = (
    "SELECT catalog_source, "
    # (schema_name, table_name), not table_name alone: a bare table name is not unique across
    # schemas (readiness._scoped_refs raises on exactly that ambiguity). Composite DISTINCT treats
    # NULLs as equal, so the common schema-less shape groups correctly.
    "       count(DISTINCT (schema_name, table_name)) AS tables, "
    "       count(*) AS columns "
    "FROM graph_node "
    f"WHERE kind = 'column' AND {visibility_predicate()} "
    "GROUP BY catalog_source "
    "ORDER BY catalog_source"
)


@dataclass(frozen=True)
class VisibleCatalog:
    """One catalog the caller may know about.

    ``source`` is the slug exactly as it keys ``graph_node`` (ingest lowercases it on the way in, see
    ``uploads.py``), so it is directly usable as the ``{source}`` path segment of every peer route.
    Both counts are over the caller's VISIBLE columns only — they are honest about scope rather than
    about the catalog, and can therefore never reveal the size of what is hidden.
    """
    source: str
    tables: int
    columns: int


def list_visible_catalogs(conn, *, roles: Iterable[str] = ()) -> tuple[VisibleCatalog, ...]:
    """The catalogs ``roles`` may see, slug-sorted, with read-scoped table/column counts.

    One query, one scan of the read-scope GIN index; no per-catalog follow-up. Empty when the caller
    can see nothing (including when there is no catalog at all) — never an error.
    """
    rows = conn.execute(_LIST_CATALOGS_SQL, (allowed_classes(roles),)).fetchall()
    return tuple(VisibleCatalog(source=r[0], tables=r[1], columns=r[2]) for r in rows)
