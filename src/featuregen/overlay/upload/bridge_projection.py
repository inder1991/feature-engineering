"""Phase-3B.2B — project a VERIFIED entity bridge into the cross-catalog entity_bridge_edge table.

The bridge's source of truth is the overlay_fact event stream; entity_bridge_edge is a derived projection
(the active cross-catalog set the 3B.3 planner reads, replacing the permissive find_cross_catalog_path
adjacency). State is read by folding the stream directly — the fold is the authoritative status for THIS
edge, so deciding it needs no drain. Demotion DELETEs the derived edge; it is always rebuildable from
the stream.

WHAT THE FOLD PREMISE MISSED (live cluster, 2026-08-09). "No drain needed" is true of this edge and
false of the confirm that produces it. `confirm_fact` branches by fact type, and three of the four
branches drain the shared 'overlay' checkpoint to head on the caller's connection
(`project_verified_join`, `project_verified_semantic_binding`, the table-fact surface). This branch
did not, so a bridge confirm appended OVERLAY_FACT_CONFIRMED and left the checkpoint one event
behind — permanently, since nothing else advances it between ingests. Every load-bearing catalog
read gates on that checkpoint with a bare `checkpoint < head` and no tolerance, so one bridge confirm
fail-closed the whole governed read path (`/suggestions` 500ed until an unrelated upload drained it).

`_drain_overlay` below is therefore about the SIDE EFFECT of the confirm, not about this projection's
own correctness — which is why it never gates the edge write and never changes the return value."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, cast

from featuregen.overlay.identity import EntityBridgeRef, canonical_bridge_value, fact_key
from featuregen.overlay.projection import OverlayProjection
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.bridge_assessment import (
    BridgeContractError,
    LinkReviewStatus,
    available_identifier_links,
)
from featuregen.overlay.upload.object_ref import normalize_ref, parse_ref
from featuregen.projections.runner import run_projection, try_lock_checkpoint_nowait
from featuregen.runtime.observability import counters

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BridgeCurrentnessV1:
    """The link's CURRENTNESS DEPENDENCIES (the staleness law / R9's provenance split): the exact
    revision facts a consumer pins to know when the SAME link has MOVED — never part of the link's
    semantic identity, never display material.

    * ``candidate_revision_id`` — the current assessment revision (evidence, bindings, verdicts,
      wording). A display/evidence change mints a NEW revision here while the semantic revision
      stands still: the no-rekey law, made structural.
    * ``overlay_head_event_id`` — the governed lifecycle stream head the availability read saw;
      lifecycle movement (a confirmation, an expiry) advances it without touching identity."""

    candidate_revision_id: str
    overlay_head_event_id: str | None


@dataclass(frozen=True, slots=True)
class ActiveBridgeV1:
    """One available cross-catalog link, split into THREE surfaces (R9 / the staleness law):

    * LOAD-BEARING SEMANTIC MATERIAL — ``entity_id``, the two endpoint catalogs with their FULL
      ordered member tuples (``left_member_refs`` / ``right_member_refs``: composite keys survive),
      and ``link_semantic_revision``. Changing any of it means a DIFFERENT link.
    * CURRENTNESS DEPENDENCIES — ``fact_key`` (the governed lifecycle stream ADDRESS,
      orientation-free) and ``currentness`` (what the projection read there). They say when the
      same link moved; they never enter its identity.
    * DISPLAY/RANKING ANNOTATIONS — ``status`` and ``strength``. Free to change; never rekey.

    Direction is deliberately ABSENT: left/right is the assessment surface's canonical (lexical)
    endpoint ordering, not a traversal — direction belongs to path segments and to the realization
    producer's per-call traversal input (:func:`ordered_member_pairs`), never to the link type.

    Every field added after the original eight is DEFAULTED so existing positional constructors
    still build."""

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
    #: The COMPLETE ordered member tuple of each endpoint, as canonical logical column refs
    #: (``source::schema.table.column``), in DECLARED composite order. ``left_object_ref`` /
    #: ``right_object_ref`` above remain the legacy single-member flattening (the FIRST member,
    #: catalog-less ``schema.table.column``) — exact for single-member links, a compat surface for
    #: composite ones. These tuples are the authoritative endpoint shape: nothing is discarded.
    left_member_refs: tuple[str, ...] = ()
    right_member_refs: tuple[str, ...] = ()
    #: The link's stable SEMANTIC identity — the assessment surface's own ``candidate_id`` (content
    #: hash over the candidate family + BOTH endpoints' ordered logical member tuples), REUSED here
    #: rather than a parallel identity being minted. Join-column changes rekey it; evidence
    #: wording, proposed→confirmed and strength changes never do.
    link_semantic_revision: str = ""
    #: Currentness dependencies (``None`` only on a positionally-built thin value; always populated
    #: by :func:`active_bridges`).
    currentness: BridgeCurrentnessV1 | None = None


def _obj_ref_str(d: dict) -> str:
    return f"{d['schema']}.{d['table']}.{d['column']}"


def _drain_overlay(conn) -> None:
    """Catch the shared 'overlay' checkpoint up to head on the CALLER'S connection — the step this
    module used to skip (see the module docstring).

    Mirrors `join_governance.project_verified_join`'s drain, with two deliberate differences that
    follow from this projection being fold-based rather than read-model-based:

    * It runs for its SIDE EFFECT only. The bridge edge is decided by folding the stream, so a
      deferred drain cannot make the edge wrong — the caller therefore projects either way and this
      never gates the write or the return value. The join surface must defer instead, because ITS
      projection reads the read model and would otherwise serve a stale status.
    * There is no residual-lag check. A poison-HALT short of head leaves the same fail-closed lag
      the callers already handle; re-reporting it here would imply this function owns a decision it
      does not.

    Fail-soft and savepoint-guarded: a projection fault must never roll back an accepted confirm.
    A lock held by an in-flight ingest is a DEFERRAL, not an error — draining would block the
    confirm behind a multi-minute ingest transaction (audit finding [9]) — and the worker's tick
    picks it up, which is the difference between "deferred by a second" and "deferred until
    somebody happens to upload a CSV"."""
    try:
        with conn.transaction():   # savepoint: a projection fault must not roll back the confirm
            if not try_lock_checkpoint_nowait(conn, "overlay"):
                counters.incr("overlay.bridge_projection.drain_skipped_lock")
                logger.warning("bridge projection: overlay checkpoint lock held by an in-flight "
                               "ingest — deferring the drain; the worker tick will catch it up")
                return
            while run_projection(conn, OverlayProjection()) >= 500:
                pass               # one pass caps at 500 events — loop until caught up
    except Exception:  # noqa: BLE001 — fail-soft: the confirm stands; the worker re-drains
        counters.incr("overlay.bridge_projection.drain_error")
        logger.warning("bridge projection: overlay drain failed — the fact stays VERIFIED and the "
                       "worker tick will catch the checkpoint up", exc_info=True)


def project_verified_bridge(conn, ref: EntityBridgeRef, *, now) -> str:
    """Project the bridge iff its folded state is VERIFIED. Returns 'projected' or 'pending'. A non-VERIFIED
    bridge is demoted (any stale edge removed). Idempotent (DELETE-then-INSERT by fact_key).

    Also drains the shared 'overlay' checkpoint (`_drain_overlay`), because the confirm that calls
    this appended an event that nothing else here would have projected. That drain is fail-soft and
    never changes what this function returns."""
    _drain_overlay(conn)
    key = fact_key(ref, "entity_bridge")
    state = fold_overlay_state(load_fact(conn, key))
    if state.status != "VERIFIED" or not state.value:
        conn.execute("DELETE FROM entity_bridge_edge WHERE fact_key = %s", (key,))
        return "pending"
    # Canonical endpoints on the way out. Values PROPOSED before bridge identity was canonicalized
    # keep whatever orientation they were written with, and the projection is keyed by fact_key —
    # which is orientation-free — so without this one bridge would describe itself one way in
    # `entity_bridge_edge` and the other in the candidate ledger, leaving the read model to
    # reconcile two shapes of the same link.
    v = cast("dict[str, Any]", canonical_bridge_value(state.value))  # shape: entity_bridge write gate
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

    A2: each link carries its ENDPOINTS' COMPLETE ordered member tuples (composite keys survive —
    this used to collapse both endpoints to ``members[0]`` and discard the rest), its stable
    ``link_semantic_revision`` (the assessment surface's own ``candidate_id``, reused) and its
    ``currentness`` dependencies — see :class:`ActiveBridgeV1` for the three-surface split. The
    tuples come from the SAME governed assessment path (``available_identifier_links``), never
    from a raw candidate-ledger read.
    """
    out: list[ActiveBridgeV1] = []
    for link in available_identifier_links(conn):
        left_members = tuple(
            member.logical_column_ref for member in link.assessment.left_endpoint.members)
        right_members = tuple(
            member.logical_column_ref for member in link.assessment.right_endpoint.members)
        out.append(ActiveBridgeV1(
            link.assessment.bridge_fact_key or "",
            link.assessment.left_endpoint.entity_id or "",
            parse_ref(link.assessment.left_endpoint.logical_table_ref)[0],
            # The legacy thin fields flatten the FIRST member only — the pre-A2 shape, kept as a
            # compat surface (exact for single-member links). The full tuples ride alongside.
            _flat_object_ref(left_members[0]),
            parse_ref(link.assessment.right_endpoint.logical_table_ref)[0],
            _flat_object_ref(right_members[0]),
            (
                "confirmed"
                if link.availability.review_status is LinkReviewStatus.HUMAN_VERIFIED
                else "proposed"
            ),
            link.ranking_strength,
            left_member_refs=left_members,
            right_member_refs=right_members,
            link_semantic_revision=link.assessment.candidate_id,
            currentness=BridgeCurrentnessV1(
                candidate_revision_id=link.assessment.candidate_revision_id,
                overlay_head_event_id=link.availability.overlay_head_event_id,
            ),
        ))
    return tuple(out)


def ordered_member_pairs(
    bridge: ActiveBridgeV1, *, from_logical_table_ref: str
) -> tuple[tuple[str, str], ...]:
    """The step-4 producer's ``ordered_member_pairs`` input, SUPPLIED by the projection.

    Zips the two endpoints' complete ordered member tuples in the caller's traversal orientation —
    one ``(from_ref, to_ref)`` per composite position, DECLARED order preserved. Direction is
    resolved HERE, per call, from ``from_logical_table_ref`` (the same orientation input
    ``produce_provisional_realization`` takes) and is never stored on the link type: a link is
    symmetric; a traversal is not.

    Refuses (:class:`BridgeContractError`) rather than guessing: a thin value carrying no member
    tuples, a ref that is not a TABLE ref, a table naming neither endpoint, endpoints on the same
    table (orientation undecidable), and endpoints of different arity (no positional mapping
    exists to supply — the caller must state the pairs explicitly)."""
    if not bridge.left_member_refs or not bridge.right_member_refs:
        raise BridgeContractError(
            "this bridge value carries no endpoint member tuples — read it through active_bridges")
    source, schema, table, column = parse_ref(from_logical_table_ref.strip().lower())
    if column is not None:
        raise BridgeContractError(
            f"from_logical_table_ref must address a TABLE, got {from_logical_table_ref!r}")
    wanted = normalize_ref(source, schema, table)

    def _table_ref(member_ref: str) -> str:
        m_source, m_schema, m_table, _m_column = parse_ref(member_ref)
        return normalize_ref(m_source, m_schema, m_table)

    left_table = _table_ref(bridge.left_member_refs[0])
    right_table = _table_ref(bridge.right_member_refs[0])
    if left_table == right_table:
        raise BridgeContractError(
            "both link endpoints live on the same table; a table ref cannot orient the traversal")
    if wanted == left_table:
        from_members, to_members = bridge.left_member_refs, bridge.right_member_refs
    elif wanted == right_table:
        from_members, to_members = bridge.right_member_refs, bridge.left_member_refs
    else:
        raise BridgeContractError(
            f"from_logical_table_ref {wanted!r} names neither link endpoint "
            f"({left_table!r}, {right_table!r})")
    if len(from_members) != len(to_members):
        raise BridgeContractError(
            f"endpoints have different member arity ({len(from_members)} vs {len(to_members)}); "
            "no positional mapping can be supplied — state the pairs explicitly")
    return tuple(zip(from_members, to_members, strict=True))
