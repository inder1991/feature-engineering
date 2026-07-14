"""Phase-3B.2B — project a VERIFIED entity bridge into the cross-catalog entity_bridge_edge table.

The bridge's source of truth is the overlay_fact event stream; entity_bridge_edge is a derived projection
(the active cross-catalog set the 3B.3 planner reads, replacing the permissive find_cross_catalog_path
adjacency). State is read by folding the stream directly (no adapter/no drain needed — the fold is the
authoritative status). Demotion DELETEs the derived edge; it is always rebuildable from the stream."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from featuregen.overlay.identity import EntityBridgeRef, fact_key
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact


@dataclass(frozen=True, slots=True)
class ActiveBridgeV1:
    fact_key: str
    entity_id: str
    left_catalog_source: str
    left_object_ref: str
    right_catalog_source: str
    right_object_ref: str


def _obj_ref_str(d: dict) -> str:
    return f"{d['schema']}.{d['table']}.{d['column']}"


def project_verified_bridge(conn, ref: EntityBridgeRef, *, now) -> str:
    """Project the bridge iff its folded state is VERIFIED. Returns 'projected' or 'pending'. A non-VERIFIED
    bridge is demoted (any stale edge removed). Idempotent (DELETE-then-INSERT by fact_key)."""
    key = fact_key(ref, "entity_bridge")
    state = fold_overlay_state(load_fact(conn, key))
    if state.status != "VERIFIED" or not state.value:
        conn.execute("DELETE FROM entity_bridge_edge WHERE fact_key = %s", (key,))
        return "pending"
    v = cast("dict[str, Any]", state.value)  # shape enforced by the entity_bridge write gate (Task 2)
    conn.execute("DELETE FROM entity_bridge_edge WHERE fact_key = %s", (key,))
    conn.execute(
        "INSERT INTO entity_bridge_edge (fact_key, entity_id, left_catalog_source, left_object_ref, "
        "  right_catalog_source, right_object_ref, confirmed_event_id, status, projected_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,'VERIFIED',%s)",
        (key, v["entity_id"], v["left_ref"]["catalog_source"], _obj_ref_str(v["left_ref"]),
         v["right_ref"]["catalog_source"], _obj_ref_str(v["right_ref"]), state.confirmed_event_id, now))
    return "projected"


def demote_bridge_edges(conn, fact_key_value: str) -> int:
    """Remove a projected bridge (on reject/expire/stale). Returns rows removed. The event stream retains
    the full audit; the projection is derived."""
    cur = conn.execute("DELETE FROM entity_bridge_edge WHERE fact_key = %s", (fact_key_value,))
    return cur.rowcount


def active_bridges(conn) -> tuple[ActiveBridgeV1, ...]:
    """The currently-projected VERIFIED bridges — the cross-catalog active set 3B.3 consumes. Deterministic
    (ordered)."""
    rows = conn.execute(
        "SELECT fact_key, entity_id, left_catalog_source, left_object_ref, right_catalog_source, "
        "  right_object_ref FROM entity_bridge_edge WHERE status = 'VERIFIED' "
        "ORDER BY entity_id, left_object_ref, right_object_ref").fetchall()
    return tuple(ActiveBridgeV1(*r) for r in rows)
