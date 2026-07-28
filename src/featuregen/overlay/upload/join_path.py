"""Deterministic multi-hop join-path finding over the graph's join edges.

Feature-building rarely lives in one table — reaching from `transactions` to `customers` may need
`transactions -> accounts -> customers`. This is a plain BFS over the `joins` edges (no LLM); each
step carries its cardinality so the builder knows whether a hop fans in safely (N:1) or would
double-count. The LLM later *suggests* which path to use; this finds the paths that actually exist.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass

from featuregen.overlay.upload.read_scope import allowed_sensitivities


@dataclass(frozen=True, slots=True)
class JoinStep:
    """One traversal hop, oriented to the direction of travel.

    The last three fields are the hop's AUTHORITY — why this edge was allowed to be traversed:

    * ``approved_join_fact_key`` — the governed ``approved_join`` fact backing the edge, or ``None``
      for a FILE-DECLARED edge (declared by an upload, never confirmed by anyone). ``None`` is a
      meaningful answer, not a missing one.
    * ``approved_join_status`` — that fact's folded status (``VERIFIED`` on an operational path;
      ``DRAFT``/``PARTIALLY_CONFIRMED``/… on an unverified one). ``None`` iff there is no fact.
    * ``authority`` — ``graph_edge.authority``: ``operational`` (usable for feature construction) vs
      ``display_only`` (an ungoverned edge, kept for lineage display only). ``classify_join_path``
      only ever fetches operational edges, so that is the default a hand-built step takes.

    They default so this is a purely ADDITIVE extension: every existing caller that reads
    ``from_ref``/``to_ref``/``cardinality`` — and every test that constructs a step positionally —
    keeps working. Consumers that must record WHICH governed fact authorized a hop (materialization)
    read them from the same query that planned the path, never from a second, potentially-drifted
    read of ``graph_edge``.
    """

    from_ref: str
    to_ref: str
    cardinality: str | None
    approved_join_fact_key: str | None = None
    approved_join_status: str | None = None
    authority: str = "operational"


@dataclass(frozen=True, slots=True)
class JoinOutcome:
    """A discriminated join result (spec §7). `kind` is one of the four class attributes below.
      OPERATIONAL(steps)                      -> clears the join check (VERIFIED or file-declared edge)
      UNVERIFIED(steps, endpoints, fact_keys) -> NEEDS_EXTERNAL_VALIDATION / JOIN_CONNECTIVITY
      NO_PATH                                 -> REJECTED (no structural path)
      DENIED(endpoints)                       -> REJECTED (a hop hidden by read-scope)"""
    kind: str
    steps: tuple[JoinStep, ...] = ()
    endpoints: tuple[tuple[str, str], ...] = ()
    fact_keys: tuple[str, ...] = ()

    OPERATIONAL = "OPERATIONAL"
    UNVERIFIED = "UNVERIFIED"
    NO_PATH = "NO_PATH"
    DENIED = "DENIED"

    @property
    def clears(self) -> bool:
        return self.kind == JoinOutcome.OPERATIONAL


def table_of_ref(object_ref: str) -> str:
    """The BARE table name the BFS indexes an object_ref under (``public.txn.amt`` -> ``txn``).

    Public because an ADAPTER over this planner must ask the same question the BFS asked: which
    table name did this node collapse to? A caller that re-derived it differently would check a
    different graph than the one that was traversed.
    """
    parts = object_ref.split(".")
    return parts[1] if len(parts) >= 2 else object_ref


#: One fetched `graph_edge` row as it flows through classification into the BFS:
#: ``(from_ref, to_ref, cardinality, approved_join_fact_key, approved_join_status, authority)``.
#: The authority columns travel INSIDE this one tuple — and therefore inside the ONE query that
#: planned the path — because a second read of `graph_edge` to recover them later would be a
#: different snapshot of a table the join projection mutates, and could disagree about which fact
#: authorized the hop that was actually traversed.
_Edge = tuple[str, str, str | None, str | None, str | None, str]


def _invert(cardinality: str | None) -> str | None:
    """Flip fan direction for a reverse traversal. `1:1`/`None` are direction-agnostic."""
    if cardinality == "N:1":
        return "1:N"
    if cardinality == "1:N":
        return "N:1"
    return cardinality


def _bfs(adj: dict[str, list[tuple[str, JoinStep]]], from_table: str,
         to_table: str) -> list[JoinStep] | None:
    queue: deque[tuple[str, list[JoinStep]]] = deque([(from_table, [])])
    seen = {from_table}
    while queue:
        table, path = queue.popleft()
        for neighbor, step in adj.get(table, []):
            if neighbor in seen:
                continue
            new_path = path + [step]
            if neighbor == to_table:
                return new_path
            seen.add(neighbor)
            queue.append((neighbor, new_path))
    return None


def _adjacency(edges: Iterable[_Edge]) -> dict[str, list[tuple[str, JoinStep]]]:
    adj: dict[str, list[tuple[str, JoinStep]]] = {}
    for from_ref, to_ref, card, fact_key, status, authority in edges:
        ft, tt = table_of_ref(from_ref), table_of_ref(to_ref)
        # Each step is ORIENTED to the traversal direction: the reverse edge swaps refs and inverts
        # cardinality, so a returned step reads "from `table_of_ref(from_ref)` join to
        # `table_of_ref(to_ref)`, fanning `cardinality` in that direction" (M7 — a reverse N:1 hop
        # is really 1:N). AUTHORITY is a property of the EDGE, not of the direction of travel, so
        # the fact key / status / authority ride BOTH orientations unchanged while the cardinality
        # flips. Losing either half on the reverse hop is the same defect: one lets an unapproved
        # join look approved, the other lets a fan-out look safe.
        fwd = JoinStep(from_ref=from_ref, to_ref=to_ref, cardinality=card,
                       approved_join_fact_key=fact_key, approved_join_status=status,
                       authority=authority)
        rev = JoinStep(from_ref=to_ref, to_ref=from_ref, cardinality=_invert(card),
                       approved_join_fact_key=fact_key, approved_join_status=status,
                       authority=authority)
        adj.setdefault(ft, []).append((tt, fwd))
        adj.setdefault(tt, []).append((ft, rev))
    return adj


@dataclass(frozen=True, slots=True)
class _ClassifiedEdges:
    """This catalog's operational ``joins`` edges, split by the SINGLE authority + read-scope rule.

    Extracted so that every consumer of "which joins may this caller traverse" — the per-hop
    classifier below and the suggestions screen's grounding-set widening — decides it with the same
    machinery. A second, hand-rolled copy of the clearing rule (or of the read-scope check) would be
    free to drift, and the two ways it could drift are both defects: a widening that admits a hop the
    gauntlet then refuses (mis-attributed JOIN_CONNECTIVITY / NO_JOIN_PATH noise on the screen), or a
    widening that crosses a hop the caller cannot see (a read-scope leak).
    """
    clearing: tuple[_Edge, ...]         # declared (no fact) or governed-VERIFIED — traversable
    unverified: tuple[_Edge, ...]       # fact-linked but not yet VERIFIED — authorized, not cleared
    unverified_fact: dict[tuple[str, str], str]   # (from_ref, to_ref) -> the approved_join fact key
    denied: tuple[_Edge, ...]           # an endpoint hidden by this caller's read scope


def _classified_edges(conn, catalog_source: str, roles: Iterable[str]) -> _ClassifiedEdges:
    """Fetch this catalog's operational join edges and classify each one. Drops the VERIFIED-status +
    sensitivity predicates from the fetch (KEEPS authority='operational' + endpoint existence, per
    #12) and classifies in Python: an edge whose BOTH endpoint columns are visible under
    ``allowed_sensitivities(roles)`` is clearing when ``fact_key is None or status == 'VERIFIED'``
    and unverified otherwise; an edge with a hidden endpoint is denied outright. ONE statement."""
    allowed = allowed_sensitivities(roles)
    rows = conn.execute(
        "SELECT e.from_ref, e.to_ref, e.cardinality, e.approved_join_fact_key, "
        "       e.approved_join_status, e.authority, fn.sensitivity, tn.sensitivity "
        "FROM graph_edge e "
        "JOIN graph_node fn ON fn.object_ref = e.from_ref AND fn.catalog_source = e.catalog_source "
        "JOIN graph_node tn ON tn.object_ref = e.to_ref AND tn.catalog_source = e.catalog_source "
        "WHERE e.catalog_source = %s AND e.kind = 'joins' AND e.authority = 'operational'",
        (catalog_source,)).fetchall()

    clearing: list[_Edge] = []
    unverified: list[_Edge] = []
    unverified_fact: dict[tuple[str, str], str] = {}
    denied: list[_Edge] = []
    for from_ref, to_ref, card, fact_key, status, authority, fs, ts in rows:
        edge: _Edge = (from_ref, to_ref, card, fact_key, status, authority)
        visible = (fs is None or fs in allowed) and (ts is None or ts in allowed)
        if not visible:
            denied.append(edge)
            continue
        if fact_key is None or status == "VERIFIED":
            clearing.append(edge)
        else:
            unverified.append(edge)
            unverified_fact[(from_ref, to_ref)] = fact_key
    return _ClassifiedEdges(tuple(clearing), tuple(unverified), unverified_fact, tuple(denied))


def _reachable(adj: dict[str, list[tuple[str, JoinStep]]], from_table: str,
               max_hops: int | None = None) -> dict[str, int]:
    """``table -> hop distance`` for every table reachable from ``from_table`` over ``adj`` within
    ``max_hops`` hops (``from_table`` itself at distance 0). The closure form of :func:`_bfs` — same
    adjacency, same hops, no target. ``max_hops=None`` is the UNBOUNDED closure; ``1`` is "the
    directly joined tables and nothing further". BFS visits in non-decreasing distance, so the first
    time a table is seen carries its shortest distance."""
    seen = {from_table: 0}
    queue: deque[str] = deque([from_table])
    while queue:
        table = queue.popleft()
        hop = seen[table] + 1
        if max_hops is not None and hop > max_hops:
            continue
        for neighbor, _step in adj.get(table, []):
            if neighbor not in seen:
                seen[neighbor] = hop
                queue.append(neighbor)
    return seen


# ── the automatic widening's RESOURCE BOUND ──────────────────────────────────────────────────────
# `clearing_reachable_tables` answers a question with no bound: on a real catalog almost every table
# reaches the customer/account hub over SOME chain of joins, so its closure from a hub table is most
# of the catalog — and a screen that grounds the template registry against that closure has a cost
# that is a property of the CATALOG, not of the request. These three constants are what turn an
# automatic page load back into a bounded operation. They are deliberately plain module constants:
# they are quoted to the user in the screen's own copy, and changing the bound is a one-line change.
#
#: How far an AUTOMATIC page load widens. ONE hop — the opened table and the tables joined DIRECTLY
#: to it. A second hop is where the closure stops being a neighbourhood and starts being the catalog.
MAX_HOPS_DEFAULT = 1
#: How many of those direct neighbours may be widened into, at most.
MAX_NEIGHBOUR_TABLES = 20
#: How many read-scoped COLUMNS a page may ground against in total, the opened table included. The
#: table cap alone is not a bound — one 400-column neighbour blows the page's cost on its own. 300 is
#: ~2.4x the widest real table in the reference catalog (126 columns), so a widened page costs at
#: most a small multiple of an ordinary one.
MAX_COLUMNS_CONSIDERED = 300
#: The most hops an EXPLICIT request may ask for. Expansion is a deliberate act, not a way to opt out
#: of the bound: it changes which tables are ELIGIBLE, never how many are admitted.
MAX_HOPS_CEILING = 3

#: ``limit_reason`` values — which bound actually bit, or ``None`` when nothing was left out.
LIMIT_TABLE_CAP = "table_cap"
LIMIT_COLUMN_BUDGET = "column_budget"


@dataclass(frozen=True, slots=True)
class JoinNeighbourhood:
    """The bounded join neighbourhood of one table, plus the HONEST account of what was left out.

    ``tables`` is the anchor followed by the kept neighbours IN THE ORDER THEY WERE KEPT. The counts
    are about NEIGHBOURS only — the anchor is never a neighbour of itself and is never truncated
    away, so ``tables_considered``/``tables_available`` read exactly as the screen states them
    ("showing 20 of 73 directly joined tables")."""

    tables: tuple[str, ...]
    tables_considered: int
    tables_available: int
    truncated: bool
    max_hops: int
    limit_reason: str | None

    @property
    def neighbours(self) -> tuple[str, ...]:
        return self.tables[1:]

    def as_metadata(self) -> dict:
        """The payload's ``neighbourhood`` block. A screen that shows a SUBSET must say so, so these
        five fields travel with every response — the two counts, whether anything was dropped, the
        hop bound that was applied (stated even when nothing was truncated, because deeper joins
        exist and were not loaded), and which bound bit."""
        return {"tables_considered": self.tables_considered,
                "tables_available": self.tables_available,
                "truncated": self.truncated,
                "max_hops": self.max_hops,
                "limit_reason": self.limit_reason}


def _visible_column_counts(conn, catalog_source: str, tables: Iterable[str],
                           roles: Iterable[str]) -> dict[str, int]:
    """``table -> number of columns THIS CALLER may see``, in ONE statement. The read-scope predicate
    is character-for-character :func:`templates._load_columns`' own, because this is what prices the
    budget: pricing a table by columns the caller cannot see would spend budget on nothing."""
    rows = conn.execute(
        "SELECT table_name, count(*) FROM graph_node "
        "WHERE kind = 'column' AND catalog_source = %s "
        "  AND visible_requires <@ %s "
        "  AND table_name = ANY(%s) "
        "GROUP BY table_name",
        (catalog_source, allowed_sensitivities(roles), list(tables))).fetchall()
    return {table: count for table, count in rows}


def clearing_neighbourhood(conn, catalog_source: str, from_table: str, *,
                           roles: Iterable[str] = (),
                           max_hops: int | None = None,
                           max_tables: int | None = None,
                           max_columns: int | None = None) -> JoinNeighbourhood:
    """The BOUNDED clearing-join neighbourhood of ``from_table`` — what an automatic page load may
    widen its grounding set to.

    Same edges, same clearing rule and same read scope as :func:`clearing_reachable_tables` (it is
    the same :func:`_classified_edges` fetch); what is added is a bound. ``max_hops`` defaults to
    :data:`MAX_HOPS_DEFAULT` (one hop — the directly joined tables), ``max_tables`` to
    :data:`MAX_NEIGHBOUR_TABLES` and ``max_columns`` to :data:`MAX_COLUMNS_CONSIDERED`; ``None``
    resolves to the constant at CALL time, and each may be passed explicitly for a deliberate,
    wider request. Costs ONE statement, plus ONE aggregate to price the budget when there is at
    least one candidate neighbour to price.

    ORDER, before anything is dropped: **(hop distance, then table name)**. Distance is the only
    relevance signal this system actually holds — a directly joined table is more likely to be part
    of the same feature than one three joins away — and beyond it the tie is broken lexically rather
    than by an invented score (this codebase does not have, and deliberately does not fake, a
    relevance ranking). Both keys are properties of the graph, never of DB row order, so the same
    request always keeps the same neighbours.

    TRUNCATION only ever NARROWS. The anchor is admitted first and never dropped; neighbours are then
    admitted in order while both bounds hold, and the walk STOPS at the first neighbour that would
    breach one (rather than skipping it for a smaller one further down — "the nearest N" is a rule a
    user can predict, "whatever happened to fit" is not). Nothing here can admit a column the caller
    could not already see: the caller names TABLES, and a table contributes only the columns the
    read-scoped load already cleared.
    """
    hops = MAX_HOPS_DEFAULT if max_hops is None else max_hops
    table_cap = MAX_NEIGHBOUR_TABLES if max_tables is None else max_tables
    column_budget = MAX_COLUMNS_CONSIDERED if max_columns is None else max_columns

    edges = _classified_edges(conn, catalog_source, roles)
    distance = _reachable(_adjacency(edges.clearing), from_table, hops)
    ordered = sorted((t for t in distance if t != from_table),
                     key=lambda t: (distance[t], t))
    if not ordered:
        return JoinNeighbourhood(tables=(from_table,), tables_considered=0, tables_available=0,
                                 truncated=False, max_hops=hops, limit_reason=None)

    counts = _visible_column_counts(conn, catalog_source, [from_table, *ordered], roles)
    kept: list[str] = []
    used = counts.get(from_table, 0)          # the opened table is never budgeted away
    reason: str | None = None
    for table in ordered:
        if len(kept) >= table_cap:
            reason = LIMIT_TABLE_CAP
            break
        if used + counts.get(table, 0) > column_budget:
            reason = LIMIT_COLUMN_BUDGET
            break
        kept.append(table)
        used += counts.get(table, 0)
    return JoinNeighbourhood(tables=(from_table, *kept), tables_considered=len(kept),
                             tables_available=len(ordered), truncated=len(kept) < len(ordered),
                             max_hops=hops, limit_reason=reason)


def clearing_reachable_tables(conn, catalog_source: str, from_table: str, *,
                              roles: Iterable[str] = ()) -> frozenset[str]:
    """The tables reachable from ``from_table`` over CLEARING join edges only, ``from_table``
    included. Read-scoped exactly like :func:`classify_join_path`: an edge with an endpoint this
    caller cannot see is DENIED, so it never widens reachability.

    "Clearing" is deliberately the same predicate :func:`classify_join_path` calls OPERATIONAL —
    declared or governed-VERIFIED. UNVERIFIED edges are excluded ON PURPOSE: a table reached over one
    would yield candidates the gauntlet then either burdens with a ``JOIN_CONNECTIVITY`` requirement
    or rejects ``NO_JOIN_PATH``, which is exactly the mis-attributed noise the per-table screen exists
    to remove. Surfacing "you could build X if you confirmed this join" is a separate product
    decision, not a side effect of this helper.

    UNBOUNDED BY CONSTRUCTION — this is the full transitive closure, and its size is a property of
    the CATALOG, not of the question asked. On a real catalog almost everything reaches the
    customer/account hub, so from a hub table this is most of the catalog. It is correct, and it is
    NOT what an automatic page load may use: :func:`clearing_neighbourhood` is the bounded form the
    suggestions screen calls. Keep this one for a caller that genuinely wants the closure and can
    afford it.

    Costs ONE statement — the same single ``graph_edge`` fetch the classifier makes."""
    edges = _classified_edges(conn, catalog_source, roles)
    return frozenset(_reachable(_adjacency(edges.clearing), from_table))


def classify_join_path(conn, catalog_source: str, from_table: str, to_table: str, *,
                       roles: Iterable[str] = ()) -> JoinOutcome:
    """Discriminated per-hop join classification (spec §7). Classifies every operational edge
    (:func:`_classified_edges`) as clearing (declared or VERIFIED), unverified (fact-linked, not yet
    VERIFIED), or denied (an endpoint hidden by read-scope), then runs a layered BFS: the shortest
    clearing path -> OPERATIONAL; else the shortest clearing+unverified path -> UNVERIFIED; else if a
    path exists only through a denied hop -> DENIED; else NO_PATH."""
    if from_table == to_table:
        return JoinOutcome(kind=JoinOutcome.OPERATIONAL)
    edges = _classified_edges(conn, catalog_source, roles)
    clearing = list(edges.clearing)
    unverified = list(edges.unverified)
    unverified_fact = edges.unverified_fact
    denied = list(edges.denied)

    path = _bfs(_adjacency(clearing), from_table, to_table)
    if path is not None:
        return JoinOutcome(kind=JoinOutcome.OPERATIONAL, steps=tuple(path))
    path = _bfs(_adjacency(clearing + unverified), from_table, to_table)
    if path is not None:
        endpoints = tuple((s.from_ref, s.to_ref) for s in path
                          if (s.from_ref, s.to_ref) in unverified_fact
                          or (s.to_ref, s.from_ref) in unverified_fact)
        keys = tuple(unverified_fact.get((f, t)) or unverified_fact[(t, f)] for f, t in endpoints)
        return JoinOutcome(kind=JoinOutcome.UNVERIFIED, steps=tuple(path),
                           endpoints=endpoints, fact_keys=keys)
    path = _bfs(_adjacency(clearing + unverified + denied), from_table, to_table)
    if path is not None:
        denied_pairs = {(f, t) for f, t, *_ in denied} | {(t, f) for f, t, *_ in denied}
        endpoints = tuple((s.from_ref, s.to_ref) for s in path
                          if (s.from_ref, s.to_ref) in denied_pairs)
        return JoinOutcome(kind=JoinOutcome.DENIED, endpoints=endpoints)
    return JoinOutcome(kind=JoinOutcome.NO_PATH)


def find_join_path(conn, catalog_source: str, from_table: str,
                   to_table: str, *, roles: Iterable[str] = ()) -> list[JoinStep] | None:
    """The shortest OPERATIONAL join path (list of steps) between two tables, or None. [] when
    from_table == to_table. Backward-compatible façade over classify_join_path: an unverified /
    denied / no-path result collapses to None exactly as the pre-Slice-3 filtered BFS did."""
    outcome = classify_join_path(conn, catalog_source, from_table, to_table, roles=roles)
    return list(outcome.steps) if outcome.kind == JoinOutcome.OPERATIONAL else None
