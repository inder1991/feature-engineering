from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from psycopg.rows import dict_row

from featuregen.overlay.upload.read_scope import allowed_sensitivities

# Facet name -> graph_node column. AND across facet groups, OR (= ANY) within one group.
# These are the ONLY facetable columns; read-scope (sensitivity gate) and freshness stay HARD
# filters below, applied to every query and never turned into a selectable/countable facet.
_COLUMN_FACETS: dict[str, str] = {
    "source": "catalog_source",
    "domain": "domain",
    "sensitivity": "sensitivity",
    "additivity": "additivity",
    "entity": "entity",
    "kind": "kind",
}
# Boolean-flag facets: presence of the filter means "only rows where the flag is true".
_FLAG_FACETS: dict[str, str] = {
    "grain": "is_grain",
    "as_of": "is_as_of",
}
_NONE = "(none)"      # bucket label for a NULL facet value (and the token that selects IS NULL)
_FACET_LIMIT = 50     # cap buckets per facet, ordered by count desc then value

# The hit projection. score keeps the FTS rank plus the grain/as-of boosts; on an empty query the
# tsquery is empty (rank 0) so score degrades to just the boosts, giving a stable browse order.
_SELECT_HIT = """
    n.object_ref, n.table_name, n.column_name, n.kind, n.data_type, n.definition,
    n.is_grain, n.is_as_of, n.catalog_source, n.concept, n.domain, n.sensitivity,
    n.additivity, n.unit, n.currency, n.entity,
    ts_rank_cd(n.search_doc, plainto_tsquery('english', %(q)s))
      + (CASE WHEN n.is_grain THEN 0.5 ELSE 0 END)
      + (CASE WHEN n.is_as_of THEN 0.3 ELSE 0 END) AS score
"""
_FROM = ("FROM graph_node n "
         "JOIN overlay_drift_watermark w ON w.catalog_source = n.catalog_source")


@dataclass(frozen=True, slots=True)
class SearchHit:
    object_ref: str
    table: str
    column: str | None
    kind: str
    data_type: str | None
    definition: str | None
    is_grain: bool
    is_as_of: bool
    catalog_source: str
    concept: str | None
    domain: str | None
    sensitivity: str | None
    additivity: str | None
    unit: str | None
    currency: str | None
    entity: str | None
    score: float


@dataclass(frozen=True, slots=True)
class FacetBucket:
    value: str
    count: int


@dataclass(frozen=True, slots=True)
class SearchResult:
    hits: list[SearchHit]              # limit-capped, score-ordered (all facets applied)
    facets: dict[str, list[FacetBucket]]
    total: int                         # count of ALL matching rows (may exceed len(hits))


def _hit(r: dict[str, Any]) -> SearchHit:
    return SearchHit(
        object_ref=r["object_ref"], table=r["table_name"], column=r["column_name"],
        kind=r["kind"], data_type=r["data_type"], definition=r["definition"],
        is_grain=r["is_grain"], is_as_of=r["is_as_of"], catalog_source=r["catalog_source"],
        concept=r["concept"], domain=r["domain"], sensitivity=r["sensitivity"],
        additivity=r["additivity"], unit=r["unit"], currency=r["currency"], entity=r["entity"],
        score=float(r["score"]))


def _build_predicates(
    query: str, filters: Mapping[str, Sequence[str]], params: dict[str, Any],
) -> tuple[list[str], dict[str, str]]:
    """Split the query into HARD base predicates and per-facet predicates, binding params.

    base_preds are applied to EVERY query (freshness watermark + read-scope + optional FTS).
    facet_preds maps each active facet name to its predicate — AND across facets, OR (= ANY)
    within one. Read-scope and freshness are deliberately NOT facets: they are AND-ed always."""
    base_preds = [
        "w.last_completed_at >= %(cutoff)s",
        "(n.sensitivity IS NULL OR n.sensitivity = ANY(%(allowed)s))",   # read-scope hard filter
    ]
    if query:
        base_preds.append("n.search_doc @@ plainto_tsquery('english', %(q)s)")

    facet_preds: dict[str, str] = {}
    for name, col in _COLUMN_FACETS.items():
        selected = list(filters.get(name, ()))
        if not selected:
            continue
        reals = [v for v in selected if v != _NONE]
        terms: list[str] = []
        if reals:
            key = f"f_{name}"
            params[key] = reals
            terms.append(f"n.{col} = ANY(%({key})s)")
        if _NONE in selected:                      # selecting "(none)" means the NULL bucket
            terms.append(f"n.{col} IS NULL")
        facet_preds[name] = "(" + " OR ".join(terms) + ")"
    for name, col in _FLAG_FACETS.items():
        if filters.get(name):                      # presence => only rows where the flag is true
            facet_preds[name] = f"n.{col}"
    return base_preds, facet_preds


def _where(base_preds: list[str], facet_preds: dict[str, str], *, exclude: str | None = None) -> str:
    preds = list(base_preds) + [p for n, p in facet_preds.items() if n != exclude]
    return " AND ".join(preds)


def search(conn, query: str = "", *, now: datetime, roles: Iterable[str] = (),
           fresh_within: timedelta = timedelta(hours=24), limit: int = 20,
           filters: Mapping[str, Sequence[str]] | None = None) -> SearchResult:
    """Facet-aware catalog search over graph_node.

    An empty ``query`` skips the FTS match and browses ALL rows (still read-scoped + fresh + faceted).
    Returns the limit-capped hits, one facet-bucket list per facet computed with EXCLUDE-OWN-FACET
    semantics (each facet counts the set with every OTHER facet applied but not its own selection,
    so choosing one value does not collapse the sibling counts), and the unlimited total."""
    filters = filters or {}
    params: dict[str, Any] = {
        "q": query, "cutoff": now - fresh_within, "limit": limit,
        "allowed": allowed_sensitivities(roles), "none": _NONE, "fl": _FACET_LIMIT,
    }
    base_preds, facet_preds = _build_predicates(query, filters, params)
    where_all = _where(base_preds, facet_preds)

    with conn.cursor(row_factory=dict_row) as cur:
        # Hits: every facet applied, score-ordered, limit-capped.
        cur.execute(
            f"SELECT {_SELECT_HIT} {_FROM} WHERE {where_all} "
            "ORDER BY score DESC, n.object_ref LIMIT %(limit)s", params)
        hits = [_hit(r) for r in cur.fetchall()]

        # Total: every facet applied, no limit (may exceed len(hits)).
        cur.execute(f"SELECT count(*) AS c {_FROM} WHERE {where_all}", params)
        row = cur.fetchone()
        total = int(row["c"]) if row else 0

        facets: dict[str, list[FacetBucket]] = {}
        for name, col in _COLUMN_FACETS.items():
            # GROUP BY the facet column over the set filtered by everything EXCEPT this facet.
            # read-scope stays in base_preds, so a forbidden sensitivity value never appears here.
            cur.execute(
                f"SELECT COALESCE(n.{col}, %(none)s) AS value, count(*) AS c {_FROM} "
                f"WHERE {_where(base_preds, facet_preds, exclude=name)} "
                "GROUP BY 1 ORDER BY c DESC, value LIMIT %(fl)s", params)
            facets[name] = [FacetBucket(value=r["value"], count=r["c"]) for r in cur.fetchall()]
        for name, col in _FLAG_FACETS.items():
            cur.execute(
                f"SELECT count(*) AS c {_FROM} "
                f"WHERE {_where(base_preds, facet_preds, exclude=name)} AND n.{col}", params)
            row = cur.fetchone()
            facets[name] = [FacetBucket(value="true", count=int(row["c"]) if row else 0)]

    return SearchResult(hits=hits, facets=facets, total=total)
