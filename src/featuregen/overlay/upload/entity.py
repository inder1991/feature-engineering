"""Entity layer — the cross-domain anchor.

A feature is *the customer's* feature, not *a deposits* feature, only if the platform knows which
columns across different catalogs denote the same business entity. Columns carry a declared `entity`
tag (`Customer`, `Account`); this module reads that tag out of the graph to expose entity membership
**across catalogs** — the raw material for cross-source join paths and cross-domain candidate gathering.

No new tables: entity membership is derived from `graph_node.entity`. Reads are read-scoped (an entity
key column may itself be sensitive).

STATUS: NOT YET WIRED into the live flow. The live cross-domain gather (`feature_assist._candidate_columns`
with `entity=`) queries `graph_node.entity` directly; this module (`cross_join_via_entity`,
`find_cross_catalog_path`, `suggest_entity`) is the scaffolding for the DEFERRED cross-catalog contract
authoring / entity-resolution follow-up (see `contract/author.py` + the loop design spec). It is fully
tested so it is ready to wire — it is NOT dead-by-accident.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from featuregen.overlay.upload.enrich_llm import audited_enrich_call
from featuregen.overlay.upload.read_scope import allowed_sensitivities

# A blank / unknown / list-stringified entity suggestion is not applied.
_KNOWN_ENTITYISH = 40   # max plausible entity-name length


@dataclass(frozen=True, slots=True)
class EntityColumn:
    entity: str
    catalog_source: str
    table: str
    object_ref: str


def list_entities(conn) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT entity FROM graph_node WHERE entity IS NOT NULL ORDER BY entity").fetchall()
    return [r[0] for r in rows]


def entity_of(conn, catalog_source: str, object_ref: str) -> str | None:
    row = conn.execute(
        "SELECT entity FROM graph_node WHERE catalog_source = %s AND object_ref = %s",
        (catalog_source, object_ref)).fetchone()
    return row[0] if row else None


def entity_key_columns(conn, entity: str, *, roles: Iterable[str] = ()) -> list[EntityColumn]:
    """Every column that denotes `entity`, ACROSS all catalogs (read-scoped). These are the keys a
    cross-source join hangs on — e.g. deposits.cust_ref and cards.cust_id both → Customer."""
    rows = conn.execute(
        "SELECT catalog_source, table_name, object_ref FROM graph_node "
        "WHERE kind = 'column' AND entity = %s "
        "AND (sensitivity IS NULL OR sensitivity = ANY(%s)) "
        "ORDER BY catalog_source, object_ref",
        (entity, allowed_sensitivities(roles))).fetchall()
    return [EntityColumn(entity=entity, catalog_source=r[0], table=r[1], object_ref=r[2])
            for r in rows]


@dataclass(frozen=True, slots=True)
class EntityBridge:
    entity: str
    from_ref: str          # the from-table's entity key column
    to_ref: str            # the to-table's entity key column


def _table_entity_keys(conn, catalog_source: str, table: str,
                       roles: Iterable[str]) -> dict[str, str]:
    rows = conn.execute(
        "SELECT entity, object_ref FROM graph_node "
        "WHERE kind = 'column' AND catalog_source = %s AND table_name = %s AND entity IS NOT NULL "
        "AND (sensitivity IS NULL OR sensitivity = ANY(%s))",
        (catalog_source, table, allowed_sensitivities(roles))).fetchall()
    return {r[0]: r[1] for r in rows}


def cross_join_via_entity(conn, from_source: str, from_table: str, to_source: str, to_table: str, *,
                          roles: Iterable[str] = ()) -> EntityBridge | None:
    """Bridge two tables in (possibly) different catalogs via a shared entity — the cross-domain join
    primitive. Returns the entity + the key columns to join on, or None if they share no entity. The
    link is declared/entity-resolved, NOT value-verified (no DB), so callers surface it for human
    confirmation before a feature that uses it is registered."""
    from_keys = _table_entity_keys(conn, from_source, from_table, roles)
    to_keys = _table_entity_keys(conn, to_source, to_table, roles)
    for entity, from_ref in from_keys.items():
        if entity in to_keys:
            return EntityBridge(entity=entity, from_ref=from_ref, to_ref=to_keys[entity])
    return None


def suggest_entity(conn, client, *, table: str, column: str, type: str, concept: str | None = None,
                   actor=None) -> str | None:
    """ADVISORY: ask the LLM which business entity an id-like column denotes (Customer, Account, ...),
    from metadata only (name/type/concept — no data). A SUGGESTION for a human to confirm before it's
    written as the column's entity — never auto-applied (a wrong entity mis-links catalogs). Returns
    the suggested entity name, or None on failure / empty / implausible output."""
    raw = audited_enrich_call(
        conn, client, task="overlay.enrich.entity", prompt_id="overlay_entity_v1",
        schema_id="overlay_entity",
        catalog_metadata={"table": table, "column": column, "type": type, "concept": concept or ""},
        out_key="entity",
        instruction="Which business entity (e.g. Customer, Account) does this id-like column denote, "
                    "if any? Reply with the entity name only, or empty if it denotes none.",
        actor=actor)
    if not raw or len(raw) > _KNOWN_ENTITYISH or "\n" in raw or raw.startswith("["):
        return None
    return raw


from collections import deque  # noqa: E402

from featuregen.overlay.upload.join_path import _table_of  # noqa: E402


@dataclass(frozen=True, slots=True)
class CrossStep:
    kind: str            # "join" (intra-catalog FK) | "entity" (cross-catalog bridge)
    from_source: str
    from_table: str
    to_source: str
    to_table: str
    detail: str          # cardinality (join) or entity name (entity bridge)


def _cross_adjacency(conn, roles: Iterable[str]) -> dict:
    """(catalog_source, table) adjacency over BOTH intra-catalog join edges and cross-catalog entity
    bridges — the graph a cross-catalog path traverses."""
    adj: dict[tuple[str, str], list] = {}

    def link(a, b, step):
        adj.setdefault(a, []).append((b, step))

    for src, fr, to, card in conn.execute(
            "SELECT catalog_source, from_ref, to_ref, cardinality FROM graph_edge "
            "WHERE kind = 'joins'").fetchall():
        a, b = (src, _table_of(fr)), (src, _table_of(to))
        if a == b:
            continue
        link(a, b, CrossStep("join", src, a[1], src, b[1], card or ""))
        link(b, a, CrossStep("join", src, b[1], src, a[1], card or ""))

    for entity in list_entities(conn):
        tables = sorted({(k.catalog_source, k.table) for k in entity_key_columns(conn, entity, roles=roles)})
        for i in range(len(tables)):
            for j in range(i + 1, len(tables)):
                a, b = tables[i], tables[j]
                link(a, b, CrossStep("entity", a[0], a[1], b[0], b[1], entity))
                link(b, a, CrossStep("entity", b[0], b[1], a[0], a[1], entity))
    return adj


def find_cross_catalog_path(conn, from_source: str, from_table: str, to_source: str, to_table: str, *,
                            roles: Iterable[str] = ()) -> list[CrossStep] | None:
    """Shortest path between two tables in (possibly different) catalogs, traversing intra-catalog
    joins and cross-catalog entity bridges. [] when start == goal; None if unreachable. Entity-bridge
    hops are declared/entity-resolved (no-DB) — callers surface them for human confirmation."""
    start, goal = (from_source, from_table), (to_source, to_table)
    if start == goal:
        return []
    adj = _cross_adjacency(conn, roles)
    queue: deque = deque([(start, [])])
    seen = {start}
    while queue:
        node, path = queue.popleft()
        for nbr, step in adj.get(node, []):
            if nbr in seen:
                continue
            new_path = path + [step]
            if nbr == goal:
                return new_path
            seen.add(nbr)
            queue.append((nbr, new_path))
    return None
