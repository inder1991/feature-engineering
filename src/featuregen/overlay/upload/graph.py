from __future__ import annotations

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.concepts import humanize
from featuregen.overlay.upload.enrich import content_hash

_SCHEMA = "public"

# Weighted tsvector: column name (A) > definition (B) > table (C) > concept (C).
_SEARCH_DOC = (
    "setweight(to_tsvector('english', coalesce(%s, '')), 'A') || "
    "setweight(to_tsvector('english', coalesce(%s, '')), 'B') || "
    "setweight(to_tsvector('english', coalesce(%s, '')), 'C') || "
    "setweight(to_tsvector('english', coalesce(%s, '')), 'C')"
)


def _table_ref(table: str) -> str:
    return f"{_SCHEMA}.{table}"


def _column_ref(table: str, column: str) -> str:
    return f"{_SCHEMA}.{table}.{column}"


def build_graph(conn, catalog_source: str, rows: list[CanonicalRow],
                concepts: dict[str, str] | None = None) -> None:
    concepts = concepts or {}
    conn.execute("DELETE FROM graph_edge WHERE catalog_source = %s", (catalog_source,))
    conn.execute("DELETE FROM graph_node WHERE catalog_source = %s", (catalog_source,))

    tables: set[str] = {r.table for r in rows}

    for table in tables:
        t_ref = _table_ref(table)
        conn.execute(
            "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
            "data_type, definition, is_grain, is_as_of, concept, search_doc) "
            f"VALUES (%s, %s, 'table', %s, NULL, NULL, NULL, false, false, NULL, {_SEARCH_DOC})",
            (catalog_source, t_ref, table, table, "", table, ""))

    for r in rows:
        c_ref = _column_ref(r.table, r.column)
        concept = concepts.get(content_hash(r))
        conn.execute(
            "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
            "data_type, definition, is_grain, is_as_of, concept, search_doc) "
            f"VALUES (%s, %s, 'column', %s, %s, %s, %s, %s, %s, %s, {_SEARCH_DOC})",
            (catalog_source, c_ref, r.table, r.column, r.type, r.definition or None,
             r.is_grain, r.as_of, concept,
             r.column, r.definition, r.table, humanize(concept) if concept else ""))
        conn.execute(
            "INSERT INTO graph_edge (catalog_source, kind, from_ref, to_ref) "
            "VALUES (%s, 'contains', %s, %s) ON CONFLICT DO NOTHING",
            (catalog_source, _table_ref(r.table), c_ref))
