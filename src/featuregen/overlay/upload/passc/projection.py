"""Pass C — the REVERSE projector (Phase 3A Task 8): VERIFIED `approved_join` fact -> operational
`graph_edge`, plus the async demotion applied the moment a fact leaves VERIFIED.

The load-bearing truth is the fact stream; the `joins` graph_edge is its operational projection —
what `find_join_path` / `_cross_adjacency` / `route_strategies` actually traverse. Safety
properties (each one is a reviewed invariant):

* **DECLARED-SPARE** — demotion touches ONLY fact-linked edges (`approved_join_fact_key IS NOT
  NULL`). A file-declared edge (NULL link) is never demoted, so a flag-off pure-declared catalog
  is byte-for-byte untouched by a projector run.
* **Orientation-safe** — a VERIFIED fact replaces BOTH orientations of its unordered column pair
  with exactly ONE edge in the confirmed direction (a declared reverse row would otherwise
  survive as a stale duplicate with an inverted fan).
* **Scope-safe** — edges are COLUMN-keyed; only THE candidate's column pair is touched, rendered
  in PUBLIC graph scope (``public.{table}.{column}``, matching ``graph_node.object_ref`` — never
  the ``src::public.…`` evidence form), and only within the projected `source`.
* **Fail-closed** — anything other than a currently-servable VERIFIED resolution (DRAFT,
  PARTIALLY_CONFIRMED, REJECTED, REVERIFY, STALE, read-time expiry) demotes the pair's governed
  edge instead of projecting one.

`pairs` MUST be enumerated from the source's `approved_join` facts / gate tasks (e.g.
`_ref_from_payload` over the proposal payloads) — NEVER from raw `graph_edge` rows, which would
make the projection self-referential and unable to recover a dropped edge.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from featuregen.overlay.catalog import current_catalog_adapter
from featuregen.overlay.identity import (
    ApprovedJoinRef,
    CatalogObjectRef,
    _norm,
    _ref_from_payload,
    fact_key,
)
from featuregen.overlay.resolve import resolve_fact


def _endpoint(ref: CatalogObjectRef) -> str:
    """A join endpoint in PUBLIC graph scope — the `graph_node.object_ref` rendering."""
    return f"public.{ref.table}.{ref.column}"


def _pair_key(ref: ApprovedJoinRef) -> tuple[str, str]:
    """The UNORDERED column-pair key — the sorted pair of public-scope endpoints, i.e. exactly the
    span the projector's both-orientation SQL (`(a,b) OR (b,a)`) touches. Two approved_join facts
    with the same `_pair_key` are RIVALS for the same operational edge (the reject→re-propose
    correction path makes that reachable), so outcomes must be decided per key, not per ref."""
    return tuple(sorted((_endpoint(ref.from_ref), _endpoint(ref.to_ref))))


def list_approved_join_refs(conn, source: str) -> list[ApprovedJoinRef]:
    """Every `approved_join` ref ever proposed for `source`, rebuilt from the `overlay_proposal`
    read model's schema-pinned payloads (`_ref_from_payload`) — the FACT-side enumeration
    `project_confirmed_joins` requires (never `graph_edge`: self-referential, and build_graph just
    wiped it; never the Pass-C ledger: Task 10 clears+rewrites it every cycle, while a prior-cycle
    VERIFIED join lives on only in its fact). Mirrors the Task-9 readiness enumeration
    (`_relationship_candidates` store (a)); callers gate on `projection_lag == 0` so the read model
    is at head. Passing EVERY ref — VERIFIED or not — is safe: the projector groups refs by
    unordered column pair and resolves each one, so a non-VERIFIED sibling can never clobber the
    pair's VERIFIED projection, and an all-non-VERIFIED pair becomes ONE declared-spare
    demotion/no-op. Enumeration is DETERMINISTIC (sorted by unordered pair key, then fact_key) so
    any caller iterating the refs directly is reproducible too."""
    norm_source = _norm(source)
    refs: list[ApprovedJoinRef] = []
    for csource, value in conn.execute(
            "SELECT catalog_source, proposed_value FROM overlay_proposal"
            " WHERE fact_type = 'approved_join'").fetchall():
        if _norm(csource) != norm_source:
            continue
        refs.append(_ref_from_payload(value))
    refs.sort(key=lambda r: (_pair_key(r), fact_key(r, "approved_join")))
    return refs


def demote_join_edges(conn, *, fact_key: str, status: str, now: datetime | None = None) -> int:
    """ASYNC demotion (the ingest-latency closer): flip every graph_edge LINKED to `fact_key` to
    display_only and stamp the fact's new folded status, the moment it leaves VERIFIED (reject /
    expiry) — traversal stops immediately, not at the next upload's projector run. KEEPS the fact
    link (unlike the projector's demotion, which clears it): the link is what lets the next
    projector run — and any auditor — trace the demoted edge back to its fact. Declared-spare by
    construction: a declared edge has a NULL link and can never match. Returns rows updated."""
    now = now or datetime.now(UTC)
    rows = conn.execute(
        "UPDATE graph_edge SET authority = 'display_only', approved_join_status = %s,"
        " authority_updated_at = %s WHERE approved_join_fact_key = %s RETURNING 1",
        (status, now, fact_key)).fetchall()
    return len(rows)


def project_confirmed_joins(conn, *, source: str, pairs: Iterable[ApprovedJoinRef],
                            now: datetime | None = None) -> None:
    """Project each pair's CURRENT `approved_join` resolution onto `graph_edge` — IDEMPOTENTLY.

    Per pair: `resolve_fact` (VERIFIED-only serving; `now` forwarded so ingest keeps ONE clock
    basis for the read-time expiry guard).

    * **VERIFIED** — DELETE any `joins` edge for THIS unordered column pair in EITHER orientation
      (a declared reverse/duplicate row included — it is being *replaced* by the governed truth,
      not demoted), then INSERT exactly ONE operational edge in the confirmed direction carrying
      the confirmed cardinality + the fact links (`approved_join_fact_key`, the confirming
      event id, status 'VERIFIED', `authority_updated_at`).
    * **anything else** — demote to display_only and CLEAR the fact links, for BOTH orientations,
      but ONLY on edges whose `approved_join_fact_key IS NOT NULL`: a file-declared edge is never
      demoted (THE flag-off byte-for-byte guarantee), and no other column pair is touched.

    ORDER-INDEPENDENT + VERIFIED-WINS (Task 10 review fix): refs are GROUPED by their unordered
    column pair (`_pair_key` — the exact span the both-orientation SQL touches) and each pair's
    outcome is decided ONCE. Two distinct approved_join fact_keys CAN share a pair — reject→
    re-propose is the correction path (a REJECTED `A→B` sibling plus a VERIFIED `B→A`) — and the
    demote branch matches by column pair, fact_key-agnostically, so without grouping a
    non-VERIFIED sibling processed AFTER the VERIFIED ref would clobber the just-written
    operational edge back to display_only. Per pair: ANY VERIFIED ref wins (project it; no
    sibling demote runs); NONE verified → ONE declared-spare demotion. Should two VERIFIED refs
    ever share a pair (impossible under the Pass-C ledger's one-row-per-unordered-pair
    invariant), the lexicographically-smallest fact_key is projected, deterministically.
    """
    now = now or datetime.now(UTC)
    adapter = current_catalog_adapter()
    groups: dict[tuple[str, str], list[ApprovedJoinRef]] = {}
    for ref in pairs:
        if _norm(ref.from_ref.catalog_source) != _norm(source):
            continue    # defensive: a foreign-source ref must never touch THIS source's edges
        groups.setdefault(_pair_key(ref), []).append(ref)
    for group in groups.values():
        verified = [
            (fact_key(ref, "approved_join"), ref, resolved)
            for ref in group
            for resolved in (resolve_fact(conn, adapter, ref, "approved_join", now=now),)
            if resolved.status == "VERIFIED" and resolved.value is not None
        ]
        if verified:
            # Deterministic pick: smallest fact_key (key= keeps a duplicate-ref tie from ever
            # comparing ApprovedJoinRef dataclasses, which define no ordering).
            key, ref, resolved = min(verified, key=lambda entry: entry[0])
            a, b = _endpoint(ref.from_ref), _endpoint(ref.to_ref)
            conn.execute(
                "DELETE FROM graph_edge WHERE catalog_source = %s AND kind = 'joins' AND"
                " ((from_ref = %s AND to_ref = %s) OR (from_ref = %s AND to_ref = %s))",
                (source, a, b, b, a))
            conn.execute(
                "INSERT INTO graph_edge (catalog_source, kind, from_ref, to_ref, cardinality,"
                " authority, approved_join_fact_key, approved_join_event_id,"
                " approved_join_status, authority_updated_at)"
                " VALUES (%s, 'joins', %s, %s, %s, 'operational', %s, %s, 'VERIFIED', %s)",
                (source, a, b, resolved.value["cardinality"], key,
                 (resolved.provenance or {}).get("confirmed_event_id"), now))
        else:
            a, b = _endpoint(group[0].from_ref), _endpoint(group[0].to_ref)
            conn.execute(
                "UPDATE graph_edge SET authority = 'display_only', approved_join_fact_key = NULL,"
                " approved_join_event_id = NULL, approved_join_status = NULL,"
                " authority_updated_at = %s"
                " WHERE catalog_source = %s AND kind = 'joins'"
                " AND approved_join_fact_key IS NOT NULL AND"
                " ((from_ref = %s AND to_ref = %s) OR (from_ref = %s AND to_ref = %s))",
                (now, source, a, b, b, a))
