from __future__ import annotations

from dataclasses import dataclass

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.concepts import humanize
from featuregen.overlay.upload.enrich import content_hash

_SCHEMA = "public"

# Weighted tsvector: column name (A) > definition (B) > table/concept/domain (C).
_SEARCH_DOC = (
    "setweight(to_tsvector('english', coalesce(%s, '')), 'A') || "   # column name
    "setweight(to_tsvector('english', coalesce(%s, '')), 'B') || "   # definition
    "setweight(to_tsvector('english', coalesce(%s, '')), 'C') || "   # table name
    "setweight(to_tsvector('english', coalesce(%s, '')), 'C') || "   # concept
    "setweight(to_tsvector('english', coalesce(%s, '')), 'C')"       # domain
)


def _table_ref(table: str) -> str:
    return f"{_SCHEMA}.{table}"


def _column_ref(table: str, column: str) -> str:
    return f"{_SCHEMA}.{table}.{column}"


def build_graph(conn, catalog_source: str, rows: list[CanonicalRow],
                concepts: dict[str, str] | None = None,
                definitions: dict[str, str] | None = None,
                domains: dict[str, str] | None = None) -> None:
    concepts = concepts or {}
    definitions = definitions or {}   # {content_hash: drafted_definition} (blank columns only)
    domains = domains or {}           # {table_name: domain}
    conn.execute("DELETE FROM graph_edge WHERE catalog_source = %s", (catalog_source,))
    conn.execute("DELETE FROM graph_node WHERE catalog_source = %s", (catalog_source,))

    for table in {r.table for r in rows}:
        t_ref = _table_ref(table)
        domain = domains.get(table)
        conn.execute(
            "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
            "data_type, definition, is_grain, is_as_of, concept, domain, search_doc) "
            f"VALUES (%s, %s, 'table', %s, NULL, NULL, NULL, false, false, NULL, %s, {_SEARCH_DOC})",
            (catalog_source, t_ref, table, domain, table, "", table, "", domain or ""))

    for r in rows:
        c_ref = _column_ref(r.table, r.column)
        concept = concepts.get(content_hash(r))
        domain = domains.get(r.table)
        # Declared definition wins; a drafted one fills a blank (R3 — never overwrite a human's).
        definition = r.definition or definitions.get(content_hash(r)) or None
        conn.execute(
            "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
            "data_type, definition, is_grain, is_as_of, concept, domain, sensitivity, "
            "additivity, unit, currency, entity, search_doc) "
            f"VALUES (%s, %s, 'column', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, {_SEARCH_DOC})",
            (catalog_source, c_ref, r.table, r.column, r.type, definition,
             r.is_grain, r.as_of, concept, domain, r.sensitivity or None,
             r.additivity or None, r.unit or None, r.currency or None, r.entity or None,
             r.column, definition or "", r.table, humanize(concept) if concept else "",
             (domain or "") + " " + (r.entity or "")))
        conn.execute(
            "INSERT INTO graph_edge (catalog_source, kind, from_ref, to_ref) "
            "VALUES (%s, 'contains', %s, %s) ON CONFLICT DO NOTHING",
            (catalog_source, _table_ref(r.table), c_ref))
        if r.joins_to:
            # Single-column join: this column -> target "table.column" (may be not-yet-loaded).
            to_ref = f"{_SCHEMA}.{r.joins_to}" if r.joins_to.count(".") == 1 else r.joins_to
            conn.execute(
                "INSERT INTO graph_edge (catalog_source, kind, from_ref, to_ref, cardinality) "
                "VALUES (%s, 'joins', %s, %s, %s) ON CONFLICT DO NOTHING",
                (catalog_source, c_ref, to_ref, r.cardinality or None))


@dataclass(frozen=True, slots=True)
class JoinEdge:
    from_ref: str
    to_ref: str
    cardinality: str | None
    resolved: bool   # whether to_ref is a known node (a pending/cross-source target is unresolved)


def column_joins(conn, catalog_source: str, object_ref: str) -> list[JoinEdge]:
    """The join edges out of a column — including ones whose target isn't loaded yet (pending)."""
    rows = conn.execute(
        "SELECT e.from_ref, e.to_ref, e.cardinality, "
        # M5: scope by catalog — a cross-source target present in ANOTHER catalog is NOT resolved here.
        "  EXISTS(SELECT 1 FROM graph_node n WHERE n.object_ref = e.to_ref "
        "         AND n.catalog_source = e.catalog_source) AS resolved "
        "FROM graph_edge e "
        "WHERE e.catalog_source = %s AND e.kind = 'joins' AND e.from_ref = %s "
        "ORDER BY e.to_ref",
        (catalog_source, object_ref)).fetchall()
    return [JoinEdge(from_ref=r[0], to_ref=r[1], cardinality=r[2], resolved=r[3]) for r in rows]
