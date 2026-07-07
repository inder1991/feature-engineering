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


def find_join_path(conn, catalog_source: str, from_table: str,
                   to_table: str, *, roles: Iterable[str] = ()) -> list[JoinStep] | None:
    """The shortest join path (list of steps) between two tables, or None if unreachable.
    [] when from_table == to_table. Edges are traversed undirected (you may join either way). READ-
    SCOPED: an edge whose from/to column has a sensitivity the caller's roles can't see is excluded, so
    a path can't be walked THROUGH a restricted join key the caller isn't cleared to know about."""
    if from_table == to_table:
        return []
    allowed = allowed_sensitivities(roles)
    edges = conn.execute(
        "SELECT e.from_ref, e.to_ref, e.cardinality FROM graph_edge e "
        "LEFT JOIN graph_node fn ON fn.object_ref = e.from_ref AND fn.catalog_source = e.catalog_source "
        "LEFT JOIN graph_node tn ON tn.object_ref = e.to_ref AND tn.catalog_source = e.catalog_source "
        "WHERE e.catalog_source = %s AND e.kind = 'joins' "
        "  AND (fn.sensitivity IS NULL OR fn.sensitivity = ANY(%s)) "
        "  AND (tn.sensitivity IS NULL OR tn.sensitivity = ANY(%s))",
        (catalog_source, allowed, allowed)).fetchall()

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
