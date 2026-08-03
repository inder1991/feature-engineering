from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from psycopg.rows import dict_row

from featuregen.overlay.upload.feature_metadata_snapshot import (
    PROJECTION_READY,
    ProjectionLagV1,
    projection_lag_marker,
)
from featuregen.overlay.upload.read_scope import allowed_sensitivities, visible_table_pairs

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

# Release-A profile facets (profile Task 5 + D13.1/D13.2), added ONLY while
# `FEATUREGEN_DATASET_PROFILES` is on — with the flag off, `search()` returns the exact facet map
# it always did, so the flag-off payload is byte-identical (profile Task 6's own rule).
#
# Every one is a LITERAL `graph_node` column, because that is the only thing this mechanism can
# read (review D: "a derived data role facet is inexpressible"). `data_role` is the migration-1052
# projection of the canonical `table_role`; `authority_role`/`temporal_storage_model` are the 1047
# table-node projections; `bian_path`/`process_path`/`sub_domain` are the 1051 column-node axes.
#
# TABLE-grain facets bucket every COLUMN row under `(none)`, which is honest rather than noisy:
# selecting `data_role=crosswalk` returns the crosswalk TABLES, which is the question being asked.
# Read scope is IDENTICAL to every other facet — the base predicates (including the derived
# table-visibility rule) are applied to the facet counts too, so a hidden table's profile can never
# be counted into a bucket.
_PROFILE_FACETS: dict[str, str] = {
    "data_role": "data_role",
    "authority_role": "authority_role",
    "temporal_storage_model": "temporal_storage_model",
    "bian_path": "bian_path",
    "process_path": "process_path",
    "sub_domain": "sub_domain",
}


def column_facets() -> dict[str, str]:
    """The active facet map. The ONE definition — the route builds its filter dict from it, so a
    facet cannot exist in the query layer and be un-passable from HTTP (or the reverse)."""
    from featuregen.overlay.upload.profile_vocab import dataset_profiles_enabled

    if dataset_profiles_enabled():
        return {**_COLUMN_FACETS, **_PROFILE_FACETS}
    return dict(_COLUMN_FACETS)


# Boolean-flag facets: presence of the filter means "only rows where the flag is true".
_FLAG_FACETS: dict[str, str] = {
    "grain": "is_grain",
    "as_of": "is_as_of",
}
_NONE = "(none)"      # bucket label for a NULL facet value (and the token that selects IS NULL)
_FACET_LIMIT = 50     # cap buckets per facet, ordered by count desc then value

# The hit projection. score keeps the FTS rank plus the grain/as-of boosts; on an empty query the
# tsquery is empty (rank 0) so score degrades to just the boosts, giving a stable browse order.
#: ALL — every term must appear (a keyword search box). ANY — any term may (a whole QUESTION).
#: `plainto_tsquery` ANDs its terms, so a natural-language question almost never matches one
#: column's document and question-driven retrieval would return nothing at all. The ANY form casts
#: the already-sanitised plainto_tsquery to text and swaps the `&` operators for `|`, so no raw user
#: text is ever fed to `to_tsquery`.
MATCH_ALL = "all"
MATCH_ANY = "any"
_TSQUERY = {
    MATCH_ALL: "plainto_tsquery('english', %(q)s)",
    MATCH_ANY: ("to_tsquery('english', "
                "replace(plainto_tsquery('english', %(q)s)::text, '&', '|'))"),
}


def _select_hit(match: str) -> str:
    return f"""
    n.object_ref, n.table_name, n.column_name, n.kind, n.data_type, n.definition,
    n.is_grain, n.is_as_of, n.catalog_source, n.concept, n.domain, n.sensitivity,
    n.additivity, n.unit, n.currency, n.entity,
    ts_rank_cd(n.search_doc, {_TSQUERY[match]})
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
    # Semantic Task 6: whether a load-bearing projection was BEHIND when these rows were read.
    # Search reads `graph_node`'s projected display columns, so a lagged projection means the hits
    # may not yet reflect the newest resolved semantics. DISCLOSED, never refused — an empty result
    # set would be a worse (and less honest) answer than the rows plus the marker. Defaulted so
    # every existing constructor stays valid; `PROJECTION_READY` is the normal value, always
    # present, because an omitted key cannot distinguish "checked and fine" from "never checked".
    # Computed for any page that returns HITS; a page returning none keeps the default, because the
    # marker describes the rows served and there are none (see the call site).
    projection: ProjectionLagV1 = PROJECTION_READY


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
    *, match: str = MATCH_ALL,
) -> tuple[list[str], dict[str, str]]:
    """Split the query into HARD base predicates and per-facet predicates, binding params.

    base_preds are applied to EVERY query (freshness watermark + read-scope + optional FTS).
    facet_preds maps each active facet name to its predicate — AND across facets, OR (= ANY)
    within one. Read-scope and freshness are deliberately NOT facets: they are AND-ed always."""
    base_preds = [
        # Freshness is SOURCE-level (the drift watermark) EXCEPT for a node carrying its OWN
        # attestation instant (attested_at — a quarantine-RESOLVED row, which no scan ever saw):
        # that node is fresh for the SLA window after its resolution and never re-blessed by a
        # later scan of the OTHER rows (round-3 #5). NULL attested_at = watermark, as before.
        "COALESCE(n.attested_at, w.last_completed_at) >= %(cutoff)s",
        # Read-scope hard filter. A COLUMN row carries its own requirement; a TABLE row's scope is
        # DERIVED (D11): visible iff the caller can see AT LEAST ONE of its columns — the same rule
        # the asset-detail and field-correction anchor loads apply via read_scope's shared anchor
        # predicate — because build_graph never writes sensitivity on table nodes
        # (visible_requires = {}), which made every table name/text world-matchable: an existence
        # oracle over fully-restricted tables. The anchors keep the single-row EXISTS probe; here
        # the visible-table SET is hoisted ONCE per search() call (read_scope.visible_table_pairs)
        # because every query of the ~10-query fan-out shares these base_preds — the correlated
        # EXISTS re-planned a full-catalog hashed subplan in EACH of them. Identical semantics;
        # empty arrays (nothing visible) hide every table row without a SQL edge.
        "(CASE WHEN n.kind = 'table' THEN (n.catalog_source, n.table_name) IN "
        "(SELECT * FROM unnest(%(vt_sources)s::text[], %(vt_tables)s::text[])) "
        "ELSE COALESCE(n.visible_requires, '{}') <@ %(allowed)s END)",
    ]
    if query:
        base_preds.append(f"n.search_doc @@ {_TSQUERY[match]}")

    facet_preds: dict[str, str] = {}
    for name, col in column_facets().items():
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


#: A TEST CONVENIENCE, never the production window. Every governed read takes its freshness from
#: the configured drift SLA (``OverlayConfig.drift_freshness_sla``) and the HTTP route passes that
#: explicitly. This default exists only so the many search-behaviour tests that are not about
#: freshness need not thread a timedelta through.
#:
#: A PRODUCTION caller that relies on it has created a second, private, non-configurable freshness
#: window — which is precisely the defect the route once had. On a deployment whose watermark only
#: advances at ingest (no drift scanner), raising OVERLAY_DRIFT_FRESHNESS_SLA_MIN kept every other
#: governed read working while search silently returned zero rows the moment a catalog crossed 24
#: hours. Nothing errored; the catalog simply looked empty.
_TEST_FRESHNESS_DEFAULT = timedelta(hours=24)


def search(conn, query: str = "", *, now: datetime, roles: Iterable[str] = (),
           fresh_within: timedelta = _TEST_FRESHNESS_DEFAULT, limit: int = 20, offset: int = 0,
           filters: Mapping[str, Sequence[str]] | None = None,
           match: str = MATCH_ALL) -> SearchResult:
    """Facet-aware catalog search over graph_node.

    An empty ``query`` skips the FTS match and browses ALL rows (still read-scoped + fresh + faceted).
    Returns the limit-capped hits, one facet-bucket list per facet computed with EXCLUDE-OWN-FACET
    semantics (each facet counts the set with every OTHER facet applied but not its own selection,
    so choosing one value does not collapse the sibling counts), and the unlimited total.

    ``offset`` windows the HITS only: ``total`` and every facet count still describe the whole
    matching set, so a caller can tell from any page whether another page exists. Offset paging is
    sound here because the hits ORDER BY is a TOTAL order — ``(object_ref, catalog_source)`` is
    unique, so no ties are left for the database to break arbitrarily, and a row can neither repeat
    on two pages nor fall between them."""
    filters = filters or {}
    params: dict[str, Any] = {
        "q": query, "cutoff": now - fresh_within, "limit": limit, "offset": offset,
        "allowed": allowed_sensitivities(roles), "none": _NONE, "fl": _FACET_LIMIT,
    }
    if match not in _TSQUERY:
        raise ValueError(f"unknown match mode {match!r}; use {sorted(_TSQUERY)}")
    # The derived table scope, computed ONCE for the whole fan-out (see the predicate comment).
    params["vt_sources"], params["vt_tables"] = visible_table_pairs(conn, params["allowed"])
    base_preds, facet_preds = _build_predicates(query, filters, params, match=match)
    where_all = _where(base_preds, facet_preds)

    with conn.cursor(row_factory=dict_row) as cur:
        # Hits: every facet applied, score-ordered, limit-capped.
        cur.execute(
            f"SELECT {_select_hit(match)} {_FROM} WHERE {where_all} "
            # object_ref is not unique across catalog_source, so tiebreak on both for a
            # deterministic order at the limit boundary (review Minor 1).
            "ORDER BY score DESC, n.object_ref, n.catalog_source "
            "LIMIT %(limit)s OFFSET %(offset)s", params)
        hits = [_hit(r) for r in cur.fetchall()]

        # Total: every facet applied, no limit (may exceed len(hits)).
        cur.execute(f"SELECT count(*) AS c {_FROM} WHERE {where_all}", params)
        row = cur.fetchone()
        total = int(row["c"]) if row else 0

        facets: dict[str, list[FacetBucket]] = {}
        for name, col in column_facets().items():
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

    # Task 6 disclosure — ONE readiness check per search call (the gate itself is one statement,
    # `_readiness_probe`), and only when this page actually SERVES rows. The marker qualifies the
    # rows returned: "these may not yet reflect the newest resolved semantics". A page that returns
    # no rows has nothing to qualify, so it keeps the default and pays nothing — search is the
    # highest-frequency read on the platform and an empty facet-filtered page is its most common
    # answer. A non-empty page always carries a FRESHLY computed marker; the default is never a
    # substitute for a check that was skipped over served rows.
    projection = projection_lag_marker(conn) if hits else PROJECTION_READY
    return SearchResult(hits=hits, facets=facets, total=total, projection=projection)
