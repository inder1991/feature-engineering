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
    from_ref: str
    to_ref: str
    cardinality: str | None


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


def _table_of(object_ref: str) -> str:
    parts = object_ref.split(".")
    return parts[1] if len(parts) >= 2 else object_ref


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


def _adjacency(edges) -> dict[str, list[tuple[str, JoinStep]]]:
    adj: dict[str, list[tuple[str, JoinStep]]] = {}
    for from_ref, to_ref, card in edges:
        ft, tt = _table_of(from_ref), _table_of(to_ref)
        # Each step is ORIENTED to the traversal direction: the reverse edge swaps refs and inverts
        # cardinality, so a returned step reads "from `_table_of(from_ref)` join to `_table_of(to_ref)`,
        # fanning `cardinality` in that direction" (M7 — a reverse N:1 hop is really 1:N).
        fwd = JoinStep(from_ref=from_ref, to_ref=to_ref, cardinality=card)
        rev = JoinStep(from_ref=to_ref, to_ref=from_ref, cardinality=_invert(card))
        adj.setdefault(ft, []).append((tt, fwd))
        adj.setdefault(tt, []).append((ft, rev))
    return adj


def classify_join_path(conn, catalog_source: str, from_table: str, to_table: str, *,
                       roles: Iterable[str] = ()) -> JoinOutcome:
    """Discriminated per-hop join classification (spec §7). Drops the VERIFIED-status + sensitivity
    predicates from the fetch (KEEPS authority='operational' + endpoint existence, per #12) and
    classifies each edge in Python: clearing (declared or VERIFIED), unverified (fact-linked, not yet
    VERIFIED), or denied (an endpoint hidden by read-scope). Layered BFS: the shortest clearing path
    -> OPERATIONAL; else the shortest clearing+unverified path -> UNVERIFIED; else if a path exists
    only through a denied hop -> DENIED; else NO_PATH."""
    if from_table == to_table:
        return JoinOutcome(kind=JoinOutcome.OPERATIONAL)
    allowed = allowed_sensitivities(roles)
    rows = conn.execute(
        "SELECT e.from_ref, e.to_ref, e.cardinality, e.approved_join_fact_key, "
        "       e.approved_join_status, fn.sensitivity, tn.sensitivity "
        "FROM graph_edge e "
        "JOIN graph_node fn ON fn.object_ref = e.from_ref AND fn.catalog_source = e.catalog_source "
        "JOIN graph_node tn ON tn.object_ref = e.to_ref AND tn.catalog_source = e.catalog_source "
        "WHERE e.catalog_source = %s AND e.kind = 'joins' AND e.authority = 'operational'",
        (catalog_source,)).fetchall()

    clearing: list[tuple[str, str, str | None]] = []
    unverified: list[tuple[str, str, str | None]] = []
    unverified_fact: dict[tuple[str, str], str] = {}
    denied: list[tuple[str, str, str | None]] = []
    for from_ref, to_ref, card, fact_key, status, fs, ts in rows:
        visible = (fs is None or fs in allowed) and (ts is None or ts in allowed)
        if not visible:
            denied.append((from_ref, to_ref, card))
            continue
        if fact_key is None or status == "VERIFIED":
            clearing.append((from_ref, to_ref, card))
        else:
            unverified.append((from_ref, to_ref, card))
            unverified_fact[(from_ref, to_ref)] = fact_key

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
        denied_pairs = {(f, t) for f, t, _ in denied} | {(t, f) for f, t, _ in denied}
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
