from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass

from featuregen.overlay.identity import ApprovedJoinRef, CatalogObjectRef, ColumnPair
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.concepts import humanize
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.read_scope import allowed_sensitivities

_SCHEMA = "public"

logger = logging.getLogger(__name__)


def governed_joins_enabled() -> bool:
    """The governed `joins_to` seam (Task 7 / §12.1), default OFF. When ON, a declared join's raw
    'joins' edge is written DISPLAY-ONLY (authority='display_only') and routed into the governed
    approved_join path via propose_fact; feature-construction reads filter to authority='operational'
    so an ungoverned display edge is never used to build features.

    Pass C (OVERLAY_PASS_C, Phase 3A Task 10) IMPLIES governed mode: under Pass C every declared
    join must route through the governed approved_join path, so its env var is read DIRECTLY here —
    graph.py must NOT import ingest.pass_c_enabled (import cycle)."""
    return (os.environ.get("OVERLAY_GOVERNED_JOINS") == "1"
            or os.environ.get("OVERLAY_PASS_C") == "1")


def _join_edge_authority() -> str:
    """The `authority` a freshly-written 'joins' edge carries: 'display_only' under the governed seam
    (the governed approved_join fact is the operational source of truth), else 'operational' (today's
    behaviour — the raw edge IS the join feature-construction uses)."""
    return "display_only" if governed_joins_enabled() else "operational"


@dataclass(frozen=True, slots=True)
class ParsedJoinTarget:
    """The parse of a declared `joins_to` string. `ok=False` ALWAYS carries a `diagnostic` (a review
    reason) — a malformed join is surfaced, never silently dropped to a None."""
    ok: bool
    to_table: str | None
    to_col: str | None
    diagnostic: str | None


def parse_join_ref(joins_to: str) -> ParsedJoinTarget:
    """Parse a declared `joins_to` into its target (table, column).

    Supports `table.column` (2-part -> to_table, to_col) AND `schema.table.column` (3-part -> middle
    is the table, last is the column). Empty input, an empty table/column component, or an
    unparseable shape returns `ok=False` WITH a diagnostic (never a silent None), so the caller can
    raise a quarantine/review note rather than drop a declared relationship on the floor."""
    raw = (joins_to or "").strip()
    if not raw:
        return ParsedJoinTarget(False, None, None, "empty joins_to")
    parts = [p.strip() for p in raw.split(".")]
    if len(parts) == 2:
        to_table, to_col = parts[0], parts[1]
    elif len(parts) == 3:
        to_table, to_col = parts[1], parts[2]   # schema.table.column -> table is the middle segment
    else:
        return ParsedJoinTarget(
            False, None, None,
            f"unparseable joins_to {joins_to!r}: expected 'table.column' or 'schema.table.column'")
    if not to_table or not to_col:
        return ParsedJoinTarget(
            False, None, None, f"joins_to {joins_to!r} has an empty table or column component")
    return ParsedJoinTarget(True, to_table, to_col, None)


def governed_join_proposal(row: CanonicalRow) -> ApprovedJoinRef | None:
    """Build the governed `ApprovedJoinRef` a declared join maps to, or None when the row has no join
    or a malformed one (parse_join_ref not ok). Both endpoints are same-source column refs; the single
    declared column pair is (this column -> target column); cardinality defaults to 'N:1' (a child
    row referencing a parent — the safe-fan default) when the upload left it blank."""
    parsed = parse_join_ref(row.joins_to)
    if not parsed.ok:
        return None
    assert parsed.to_table is not None and parsed.to_col is not None   # ok=True guarantees both
    return ApprovedJoinRef(
        from_ref=CatalogObjectRef(row.source, "column", _SCHEMA, row.table, row.column),
        to_ref=CatalogObjectRef(row.source, "column", _SCHEMA, parsed.to_table, parsed.to_col),
        column_pairs=(ColumnPair(row.column, parsed.to_col),),
        cardinality=row.cardinality or "N:1")

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


def _validated_join_target(from_ref: str, joins_to: str) -> str | None:
    """The 'joins' edge target for a declared `joins_to`, as ``public.<table>.<column>`` — or None
    (logging the diagnostic) when the value is malformed. Gates the ungoverned graph write on the SAME
    parse_join_ref the governed proposal path uses, so a malformed join is skipped-loud rather than
    written as a raw operational edge to a garbage/phantom target (#5)."""
    parsed = parse_join_ref(joins_to)
    if not parsed.ok:
        logger.warning("skipping malformed joins_to on %s: %s", from_ref, parsed.diagnostic)
        return None
    assert parsed.to_table is not None and parsed.to_col is not None   # ok=True guarantees both
    return _column_ref(parsed.to_table, parsed.to_col)


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
            # Under the governed seam the raw edge is DISPLAY-ONLY (authority='display_only') — the
            # confirmed approved_join fact becomes feature-construction's source of truth. A malformed
            # joins_to is skipped-loud (never written as a raw edge).
            to_ref = _validated_join_target(c_ref, r.joins_to)
            if to_ref is not None:
                conn.execute(
                    "INSERT INTO graph_edge (catalog_source, kind, from_ref, to_ref, cardinality, "
                    "authority) VALUES (%s, 'joins', %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (catalog_source, c_ref, to_ref, r.cardinality or None, _join_edge_authority()))

    # Re-apply human-confirmed entity tags (entity_suggestion). The graph was just rebuilt from the
    # upload, which may not declare these; a confirmed tag must survive re-upload. Only fills a blank —
    # a freshly-declared entity on the upload wins.
    conn.execute(
        "UPDATE graph_node n SET entity = s.suggested_entity FROM entity_suggestion s "
        "WHERE s.catalog_source = n.catalog_source AND s.object_ref = n.object_ref "
        "AND s.status = 'applied' AND n.catalog_source = %s AND n.entity IS NULL",
        (catalog_source,))


def add_column_row(conn, catalog_source: str, r: CanonicalRow) -> None:
    """Incrementally add ONE canonical column row to an EXISTING source graph — the quarantine-fix path
    (a wholesale build_graph would wipe the source's other columns). No enrichment (concept/definition/
    domain arrive with an upload; a fix carries only the declared row). Idempotent via ON CONFLICT."""
    t_ref = _table_ref(r.table)
    conn.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "data_type, definition, is_grain, is_as_of, concept, domain, search_doc) "
        f"VALUES (%s, %s, 'table', %s, NULL, NULL, NULL, false, false, NULL, NULL, {_SEARCH_DOC}) "
        "ON CONFLICT DO NOTHING",
        (catalog_source, t_ref, r.table, r.table, "", r.table, "", ""))
    c_ref = _column_ref(r.table, r.column)
    definition = r.definition or None
    conn.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "data_type, definition, is_grain, is_as_of, concept, domain, sensitivity, additivity, unit, "
        f"currency, entity, search_doc) VALUES (%s, %s, 'column', %s, %s, %s, %s, %s, %s, NULL, NULL, "
        f"%s, %s, %s, %s, %s, {_SEARCH_DOC}) ON CONFLICT DO NOTHING",
        (catalog_source, c_ref, r.table, r.column, r.type, definition, r.is_grain, r.as_of,
         r.sensitivity or None, r.additivity or None, r.unit or None, r.currency or None,
         r.entity or None, r.column, definition or "", r.table, "", r.entity or ""))
    conn.execute(
        "INSERT INTO graph_edge (catalog_source, kind, from_ref, to_ref) "
        "VALUES (%s, 'contains', %s, %s) ON CONFLICT DO NOTHING", (catalog_source, t_ref, c_ref))
    if r.joins_to:
        to_ref = _validated_join_target(c_ref, r.joins_to)   # skip a malformed join, don't write raw
        if to_ref is not None:
            conn.execute(
                "INSERT INTO graph_edge (catalog_source, kind, from_ref, to_ref, cardinality, "
                "authority) VALUES (%s, 'joins', %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (catalog_source, c_ref, to_ref, r.cardinality or None, _join_edge_authority()))


@dataclass(frozen=True, slots=True)
class JoinEdge:
    from_ref: str
    to_ref: str
    cardinality: str | None
    resolved: bool   # whether to_ref is a known node (a pending/cross-source target is unresolved)
    # #10: enough authority state to tell a display-only pending/rejected edge from an operational
    # one (what find_join_path traverses: authority='operational' + VERIFIED-or-unlinked). Additive.
    authority: str | None = None             # 'operational' | 'display_only'
    approved_join_status: str | None = None  # folded fact status when fact-linked (e.g. 'VERIFIED')


def column_joins(conn, catalog_source: str, object_ref: str, *,
                 roles: Iterable[str] = ()) -> list[JoinEdge]:
    """The join edges out of a column — including ones whose target isn't loaded yet (pending). READ-
    SCOPED on BOTH endpoints (#11, matching find_join_path): an edge whose SOURCE or TARGET column has
    a sensitivity the caller's roles can't see is withheld, so the graph can't be walked to enumerate
    restricted columns AND a known sensitive object_ref can't be probed for its join endpoints (a
    resolved target with unknown sensitivity, e.g. a cross-source pending ref, is kept — nothing
    sensitive is known about it)."""
    allowed = allowed_sensitivities(roles)
    rows = conn.execute(
        "SELECT e.from_ref, e.to_ref, e.cardinality, "
        # M5: scope by catalog — a cross-source target present in ANOTHER catalog is NOT resolved here.
        "  EXISTS(SELECT 1 FROM graph_node n WHERE n.object_ref = e.to_ref "
        "         AND n.catalog_source = e.catalog_source) AS resolved, "
        "  e.authority, e.approved_join_status "
        "FROM graph_edge e "
        "LEFT JOIN graph_node fn ON fn.object_ref = e.from_ref AND fn.catalog_source = e.catalog_source "
        "LEFT JOIN graph_node tn ON tn.object_ref = e.to_ref AND tn.catalog_source = e.catalog_source "
        "WHERE e.catalog_source = %s AND e.kind = 'joins' AND e.from_ref = %s "
        "  AND (fn.sensitivity IS NULL OR fn.sensitivity = ANY(%s)) "
        "  AND (tn.sensitivity IS NULL OR tn.sensitivity = ANY(%s)) "
        "ORDER BY e.to_ref",
        (catalog_source, object_ref, allowed, allowed)).fetchall()
    return [JoinEdge(from_ref=r[0], to_ref=r[1], cardinality=r[2], resolved=r[3],
                     authority=r[4], approved_join_status=r[5]) for r in rows]
