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
from featuregen.overlay.upload.bridge_assessment import (
    LinkReviewStatus,
    available_identifier_links,
)
from featuregen.overlay.upload.object_ref import parse_ref


@dataclass(frozen=True, slots=True)
class ActiveBridgeV1:
    fact_key: str
    entity_id: str
    left_catalog_source: str
    left_object_ref: str
    right_catalog_source: str
    right_object_ref: str
    #: "confirmed" (a human endorsed the semantic relationship) or "proposed" (derived, nobody has
    #: reviewed it). An ANNOTATION: every bridge in this set is equally traversable. Defaulted so
    #: every existing positional constructor still builds.
    status: str = "confirmed"
    #: Ranking signal — a grain on either side, then an attested type match, and only then a human
    #: confirmation as a tie-break WITHIN a safety band. Lets a consumer PREFER the link the platform
    #: measured to be safer without being barred from a weaker one.
    strength: int = 0


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


def _flat_object_ref(logical_column_ref: str) -> str:
    _source, schema, table, column = parse_ref(logical_column_ref)
    if column is None:  # structurally impossible for IdentifierColumnMemberV1
        raise ValueError(f"identifier member is not a column: {logical_column_ref!r}")
    return f"{schema}.{table}.{column}"


def active_bridges(conn) -> tuple[ActiveBridgeV1, ...]:
    """The provisional cross-catalog discovery set — CONFIRMED and PROPOSED alike.

    Owner's direction: a link is usable whether or not a human has confirmed it; confirmation marks
    it approved, it does not gate consumption. This used to select VERIFIED rows only, which is why
    nine derived candidates — `cib.cust_num <-> ftr.cif_id` among them — could never be traversed.

    ACTIVE means AVAILABLE, not executable: ``available_identifier_links`` has already applied the lifecycle
    allow-list (DRAFT / PARTIALLY_CONFIRMED / VERIFIED), so a REJECTED, drift-STALEd, expired
    (REVERIFY) or unreadable bridge is absent. The legacy/shadow planner uses this set to enumerate
    provisional paths; production analysis and materialization require a current exact directional
    realization and never treat this projection as execution authority.

    Deterministic: SAFEST first — a grain-backed, attested link outranks a type-only match, and a
    human confirmation only breaks ties inside a safety band — so a consumer that takes the first
    workable path prefers what the platform measured, not what someone endorsed. Ordering inside a
    band is stable (endorsement, then entity, then left ref).
    """
    return tuple(
        ActiveBridgeV1(
            link.assessment.bridge_fact_key or "",
            link.assessment.left_endpoint.entity_id or "",
            parse_ref(link.assessment.left_endpoint.logical_table_ref)[0],
            _flat_object_ref(
                link.assessment.left_endpoint.members[0].logical_column_ref),
            parse_ref(link.assessment.right_endpoint.logical_table_ref)[0],
            _flat_object_ref(
                link.assessment.right_endpoint.members[0].logical_column_ref),
            (
                "confirmed"
                if link.availability.review_status is LinkReviewStatus.HUMAN_VERIFIED
                else "proposed"
            ),
            link.ranking_strength,
        )
        for link in available_identifier_links(conn)
    )
