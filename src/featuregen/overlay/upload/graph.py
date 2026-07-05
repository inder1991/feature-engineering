from __future__ import annotations

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
            "data_type, definition, is_grain, is_as_of, concept, domain, sensitivity, search_doc) "
            f"VALUES (%s, %s, 'column', %s, %s, %s, %s, %s, %s, %s, %s, %s, {_SEARCH_DOC})",
            (catalog_source, c_ref, r.table, r.column, r.type, definition,
             r.is_grain, r.as_of, concept, domain, r.sensitivity or None,
             r.column, definition or "", r.table, humanize(concept) if concept else "",
             domain or ""))
        conn.execute(
            "INSERT INTO graph_edge (catalog_source, kind, from_ref, to_ref) "
            "VALUES (%s, 'contains', %s, %s) ON CONFLICT DO NOTHING",
            (catalog_source, _table_ref(r.table), c_ref))
