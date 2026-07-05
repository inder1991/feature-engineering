"""Deterministic multi-hop join-path finding over the graph's join edges.

Feature-building rarely lives in one table — reaching from `transactions` to `customers` may need
`transactions -> accounts -> customers`. This is a plain BFS over the `joins` edges (no LLM); each
step carries its cardinality so the builder knows whether a hop fans in safely (N:1) or would
double-count. The LLM later *suggests* which path to use; this finds the paths that actually exist.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class JoinStep:
    from_ref: str
    to_ref: str
    cardinality: str | None


def _table_of(object_ref: str) -> str:
    parts = object_ref.split(".")
    return parts[1] if len(parts) >= 2 else object_ref


def find_join_path(conn, catalog_source: str, from_table: str,
                   to_table: str) -> list[JoinStep] | None:
    """The shortest join path (list of steps) between two tables, or None if unreachable.
    [] when from_table == to_table. Edges are traversed undirected (you may join either way)."""
    if from_table == to_table:
        return []
    edges = conn.execute(
        "SELECT from_ref, to_ref, cardinality FROM graph_edge "
        "WHERE catalog_source = %s AND kind = 'joins'",
        (catalog_source,)).fetchall()

    adj: dict[str, list[tuple[str, JoinStep]]] = {}
    for from_ref, to_ref, card in edges:
        step = JoinStep(from_ref=from_ref, to_ref=to_ref, cardinality=card)
        ft, tt = _table_of(from_ref), _table_of(to_ref)
        adj.setdefault(ft, []).append((tt, step))
        adj.setdefault(tt, []).append((ft, step))   # undirected traversal

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
