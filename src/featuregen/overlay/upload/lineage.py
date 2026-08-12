"""Catalog lineage graph — the bounded BFS behind GET /graph/lineage.

The graph view is a MAP of what the catalog knows around one anchor (a table or column ref):
which tables join to it, which cross-catalog tables share its business entities, and which
features and consumers hang off its columns. Three layers, each independently toggleable:

  joins    — declared single-column joins (graph_edge kind='joins'), traversed BOTH directions
             with cardinality oriented per traversal (a reverse N:1 reads 1:N — M7, matching
             find_join_path). A declared target not loaded in this catalog renders as a
             resolved=false stub node plus a resolved=false edge: pending joins are data,
             not errors.
  entity   — CROSS-catalog entity relationships. Advisory entity matches, governed identifier
             links and executable directional realizations are explicitly distinguished; human
             review and automatic execution safety are separate fields.
  features — feature_derives_from (column -> feature) and feature_consumer (feature ->
             consumer). The only DIRECTED layer: `direction` gates it (down = toward features
             and consumers, up = from a feature back to its source columns). Joins and entity
             bridges are structural — they traverse regardless of direction.

Two deliberate stances relative to /search:

  * STALE SOURCES ARE SHOWN, flagged stale=true (search fails closed and omits their rows).
    The map marks terra incognita instead of hiding it.
  * Read-scope is the SAME hard filter: a sensitivity-tagged column the caller's roles cannot
    see is ABSENT — its node, its contains edge, any join edge through it, and its feature
    edges all disappear (lineage must not leak where sensitive data lives). A hidden anchor
    returns None, indistinguishable from a nonexistent one.

The response is bounded: expansion stops at `max_nodes`, table units stay atomic (a table is
never shown with a partial column list), and `truncated` reports that the map was cut. One
caveat by design: the ANCHOR unit installs complete even past the cap (a table is never shown
partial), so a single pathologically wide anchor table can exceed max_nodes with truncated=false
when it has no edges — acceptable under upload governance, where table widths are bounded.

After BFS a CLOSING pass emits join/entity edges between two units the map already shows but
the frontier never queried (both endpoints entered at the depth boundary, so neither was
expanded). It installs no new nodes — edges only, between already-visible units — so two
visible tables never look unrelated when a join or entity bridge is declared. The map may be
cut at the frontier, but never quietly wrong.
"""
from __future__ import annotations

from collections import Counter, deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from featuregen.overlay.catalog_changes import drift_watermark
from featuregen.overlay.upload.join_path import _invert, table_of_ref
from featuregen.overlay.upload.read_scope import allowed_sensitivities

LAYERS = frozenset({"joins", "entity", "features"})
MAX_NODES = 200


@dataclass(frozen=True, slots=True)
class TruncationReportV1:
    """What this map did NOT return, per kind — semantic Task 7's "never silently cut".

    Two DISTINCT facts, deliberately not merged:

    * ``truncated`` keeps its shipped meaning EXACTLY: a BUDGET cut. The expansion hit
      ``max_nodes`` and a unit (or a stub) was refused, so the neighbourhood shown is smaller than
      the neighbourhood that exists. Widening this to "anything was left out" would flip it true on
      almost every response and destroy the signal the view badges.
    * ``omitted`` counts everything not returned, keyed by the node kind or the edge kind that was
      dropped — including the ``_prune_to_neighbourhood`` pass, which used to drop columns and
      their edges with no accounting at all (review C Task 7). A caller can therefore always tell
      the difference between "this column has no joins" and "its joins did not fit".

    The neighbourhood SCOPE rule is not truncation and is not counted: for a COLUMN anchor a
    sibling column's cross-catalog bridge is a fact about the TABLE, not about the column the
    header names, so it is out of scope rather than cut. Counting it would report a bound that was
    never reached.
    """

    truncated: bool = False
    omitted: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"truncated": self.truncated,
                "omitted": {k: self.omitted[k] for k in sorted(self.omitted)}}

_SCHEMA = "public"   # mirrors graph.py's ref scheme: public.<table>[.<column>]

# BFS expansion units (a table enters WITH all its visible columns, so the UI can collapse them):
#   ("table", catalog_source, table_name) | ("feature", feature_id, name) | ("consumer", model_ref)
_Unit = tuple


def _prune(d: dict) -> dict:
    """Drop absent optional fields (the wire contract marks them with `?`)."""
    return {k: v for k, v in d.items() if v is not None}


def _stub_node(source: str, to_ref: str) -> dict:
    """A declared-but-not-uploaded join target: known only by its ref, resolved=false."""
    return {"id": f"{source}:{to_ref}", "kind": "column", "object_ref": to_ref,
            "table": table_of_ref(to_ref), "column": to_ref.split(".")[-1],
            "grain": False, "as_of": False, "stale": False, "resolved": False}


def lineage_graph(conn, catalog_source: str, ref: str, *, now: datetime,
                  direction: str = "both", depth: int = 1,
                  layers: Iterable[str] = LAYERS, roles: Iterable[str] = (),
                  fresh_within: timedelta = timedelta(hours=24),
                  max_nodes: int = MAX_NODES) -> dict | None:
    """The lineage graph around one anchor: {"nodes": [...], "edges": [...], "truncated": bool}.

    Returns None when the anchor is unknown OR hidden by read-scope — absence must be
    indistinguishable from nonexistence (the route 404s either way, exactly like search
    omitting a hit). An anchor with no edges returns just its own table unit.
    """
    anchor = conn.execute(
        "SELECT table_name FROM graph_node WHERE catalog_source = %s AND object_ref = %s "
        "AND visible_requires <@ %s",
        (catalog_source, ref, allowed_sensitivities(roles))).fetchone()
    if anchor is None:
        return None
    b = _Builder(conn, layers=frozenset(layers), direction=direction, roles=roles,
                 now=now, fresh_within=fresh_within, max_nodes=max_nodes)
    b.run(("table", catalog_source, anchor[0]), depth)
    nodes, edges, pruned = _prune_to_neighbourhood(
        list(b.nodes.values()), b.edges, anchor_id=f"{catalog_source}:{ref}",
        # A COLUMN anchor asks "what does THIS column link to". Expansion starts from the column's
        # TABLE (that is how neighbours are discovered), so without this the answer was the table's
        # whole neighbourhood — nine links on the real catalog, eight belonging to other columns.
        anchor_is_column=ref.count(".") >= 2)
    omitted = Counter(b.omitted)
    omitted.update(pruned)
    truncation = TruncationReportV1(truncated=b.truncated,
                                    omitted={k: v for k, v in omitted.items() if v})
    # `truncated` stays a top-level BOOLEAN with its shipped meaning (a budget cut) — the graph
    # view merges two responses on it and a shape change there would be a silent client break.
    # `truncation` carries the full per-kind accounting beside it (semantic Task 7); the boolean is
    # a projection of the same report, never a second verdict.
    return {"nodes": nodes, "edges": edges, "truncated": truncation.truncated,
            "truncation": truncation.as_dict()}


def _prune_to_neighbourhood(
    nodes: list[dict], edges: list[dict], *, anchor_id: str, anchor_is_column: bool = False,
) -> tuple[list[dict], list[dict], dict[str, int]]:
    """Drop columns that neither anchor the view nor participate in a relationship.

    A table unit expands to EVERY visible column, so opening one column of a 111-column table beside
    a 126-column one returned 188 nodes — the screen showed "88 COLUMNS" and "+90 more columns",
    which is the UI collapsing a payload it cannot draw. Browsing a table's columns is the asset
    screen's job; this is a neighbourhood.

    KEPT: the anchor; any column on a real edge (join key, bridge endpoint, feature source); and the
    grain / as-of columns, which define the table's identity and its time axis, are few, and whose
    badges the table card renders. Non-column nodes (tables, features, consumers) are never pruned.

    A dropped column takes its `contains` edge with it, so the graph can never draw an edge to a
    node that is not there.

    Returns the kept nodes, the kept edges, and the per-kind count of what was dropped — the
    accounting the review found missing. The column-anchor entity-bridge narrowing below is a SCOPE
    rule, not a cut, so it is deliberately NOT counted (see :class:`TruncationReportV1`).
    """
    if anchor_is_column:
        # Cross-catalog links ONLY: a bridge is column-to-column, so a SIBLING column's bridge is a
        # fact about the table, not about the column the header names. Anchoring on `cust_num` drew
        # nine, eight of them belonging to other columns' branch codes.
        #
        # Narrow on purpose. Joins and feature lineage are how you EXPLORE from a column — a join
        # two hops away is exactly what someone is looking for — so they keep the table's
        # neighbourhood. Applying this to every edge kind collapsed multi-hop lineage entirely.
        edges = [e for e in edges
                 if e.get("kind") != "entity_bridge" or anchor_id in (e["from"], e["to"])]
    participating = {end for e in edges if e.get("kind") != "contains"
                     for end in (e["from"], e["to"])}
    keep = {
        n["id"] for n in nodes
        if n.get("kind") != "column"
        or n["id"] == anchor_id
        or n["id"] in participating
        or n.get("grain") or n.get("as_of")
    }
    kept_nodes = [n for n in nodes if n["id"] in keep]
    kept_edges = [e for e in edges if e["from"] in keep and e["to"] in keep]
    dropped: Counter[str] = Counter(
        n.get("kind", "unknown") for n in nodes if n["id"] not in keep)
    dropped.update(
        e.get("kind", "unknown") for e in edges if e["from"] not in keep or e["to"] not in keep)
    return kept_nodes, kept_edges, dict(dropped)


class _Builder:
    """Accumulates nodes/edges over a breadth-first expansion from the anchor unit."""

    _SYMMETRIC = frozenset({"join", "entity_bridge"})   # dedupe regardless of traversal direction

    def __init__(self, conn, *, layers: frozenset[str], direction: str, roles: Iterable[str],
                 now: datetime, fresh_within: timedelta, max_nodes: int) -> None:
        self.conn = conn
        self.layers = layers
        self.direction = direction
        self.allowed = allowed_sensitivities(roles)
        self.now = now
        self.fresh_within = fresh_within
        self.max_nodes = max_nodes
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []
        self.truncated = False
        #: Per-kind count of what the node cap refused — node kinds for units/stubs that could not
        #: be installed, edge kinds for the edges that went with them. A refused unit's edge must
        #: be counted too, or "no joins" and "joins that did not fit" stay indistinguishable.
        self.omitted: Counter[str] = Counter()
        self._edge_keys: set[tuple] = set()
        self._wm: dict[str, datetime | None] = {}               # per-source drift watermark (cached)
        self._as_of: dict[tuple[str, str], tuple[str | None, str | None]] = {}  # (source, table) -> (as-of col, basis)
        self._table_cols: dict[tuple[str, str], list[str]] = {}  # (source, table) -> visible col refs
        self.roles = tuple(roles)
        self._realization_views: dict[str, list[dict]] | None = None

    # ---- traversal -------------------------------------------------------------------------
    def run(self, anchor_unit: _Unit, depth: int) -> None:
        self._install(anchor_unit)   # the anchor unit is always complete, even past the cap
        seen = {anchor_unit}
        queue: deque[tuple[_Unit, int]] = deque([(anchor_unit, 0)])
        while queue:
            unit, d = queue.popleft()
            if d >= depth:
                continue
            for neighbor, stub, edge in self._expand(unit):
                if stub is not None:
                    if stub["id"] not in self.nodes:
                        if len(self.nodes) >= self.max_nodes:
                            self.truncated = True
                            self.omitted[stub.get("kind", "unknown")] += 1
                            self.omitted[edge.get("kind", "unknown")] += 1
                            continue
                        self.nodes[stub["id"]] = stub
                    self._add_edge(edge)
                    continue
                assert neighbor is not None
                if neighbor not in seen:
                    if not self._try_install(neighbor):
                        # Over the cap: skip the unit AND its edge (no dangling ends). Both are
                        # counted — the map is cut, and it says by how much and of what.
                        self.omitted[edge.get("kind", "unknown")] += 1
                        continue
                    seen.add(neighbor)
                    queue.append((neighbor, d + 1))
                self._add_edge(edge)
        self._close_frontier(seen)

    def _close_frontier(self, seen: set[_Unit]) -> None:
        """Emit join/entity edges BETWEEN two units the map already shows but BFS never queried.

        A join whose BOTH endpoint tables entered at the depth boundary is otherwise silently
        dropped: neither table is expanded (`d >= depth` skips it), so the declared edge is
        never emitted and two visible tables look unrelated. This pass re-runs the SAME
        read-scoped join/entity expansions over every installed table unit and keeps only the
        edges whose other endpoint is ALSO already installed (a real unit in `seen`, or a stub
        BFS already placed). It installs NOTHING new — edges only — so the response stays
        bounded while the map stops lying by omission. Dedup (`_add_edge`) drops the edges BFS
        already emitted, so re-running over already-expanded units is a no-op, not a doubling.
        """
        for unit in [u for u in seen if u[0] == "table"]:
            frontier: list[tuple[_Unit | None, dict | None, dict]] = []
            if "joins" in self.layers:
                frontier += self._expand_joins(unit)
            if "entity" in self.layers:
                frontier += self._expand_entity(unit)
            for neighbor, stub, edge in frontier:
                if stub is not None:
                    if stub["id"] in self.nodes:   # a stub BFS already placed: close the edge to it
                        self._add_edge(edge)
                elif neighbor in seen:
                    self._add_edge(edge)

    def _expand(self, unit: _Unit) -> list[tuple[_Unit | None, dict | None, dict]]:
        out: list[tuple[_Unit | None, dict | None, dict]] = []
        if unit[0] == "table":
            if "joins" in self.layers:
                out += self._expand_joins(unit)
            if "entity" in self.layers:
                out += self._expand_entity(unit)
            # derives points column -> feature, so features sit DOWNSTREAM of a column.
            if "features" in self.layers and self.direction in ("down", "both"):
                out += self._expand_derived_features(unit)
        elif unit[0] == "feature":
            if self.direction in ("down", "both"):
                out += self._expand_consumers(unit)
            if self.direction in ("up", "both"):
                out += self._expand_feature_sources(unit)
        else:   # consumer — its features are upstream
            if self.direction in ("up", "both"):
                out += self._expand_consumer_features(unit)
        return out

    # ---- node installation -----------------------------------------------------------------
    def _install(self, unit: _Unit) -> None:
        for n in self._unit_nodes(unit):
            self.nodes.setdefault(n["id"], n)
        self._contains_edges(unit)

    def _try_install(self, unit: _Unit) -> bool:
        """Install a unit atomically unless it would blow the node cap (a table is never shown
        with a partial column list — the map may be cut, but never quietly wrong)."""
        new = [n for n in self._unit_nodes(unit) if n["id"] not in self.nodes]
        if len(self.nodes) + len(new) > self.max_nodes:
            self.truncated = True
            for n in new:
                self.omitted[n.get("kind", "unknown")] += 1
            return False
        for n in new:
            self.nodes[n["id"]] = n
        self._contains_edges(unit)
        return True

    def _unit_nodes(self, unit: _Unit) -> list[dict]:
        if unit[0] == "table":
            _, source, table = unit
            wm = self._watermark(source)
            stale = wm is None or wm < self.now - self.fresh_within
            # Rows this table couldn't ingest, still sitting in the review queue (quarantine_row keys
            # the table name inside its raw jsonb). Surfaced so the map shows operational state.
            pending = self.conn.execute(
                "SELECT count(*) FROM quarantine_row WHERE catalog_source = %s "
                "AND raw->>'table' = %s", (source, table)).fetchone()[0]
            out = [_prune({"id": f"{source}:{_SCHEMA}.{table}", "kind": "table",
                           "object_ref": f"{_SCHEMA}.{table}", "table": table,
                           "catalog_source": source, "grain": False, "as_of": False,
                           "stale": stale, "resolved": True,
                           # the source's last drift-vouch; omitted when it has never been scanned
                           "last_vouched_at": wm.isoformat() if wm is not None else None,
                           "quarantine_pending": pending or None})]   # omit when nothing pending
            as_of_col, basis = self._as_of_basis(source, table)
            cols = self.conn.execute(
                # data_type rides along so the canvas can state a column's declared type
                # without a second round trip: the graph cards showed a concept but no type,
                # which is half of what identifies a column.
                "SELECT object_ref, column_name, is_grain, is_as_of, sensitivity, entity, "
                "concept, domain, data_type FROM graph_node "
                "WHERE catalog_source = %s AND kind = 'column' "
                "AND table_name = %s AND visible_requires <@ %s "
                "ORDER BY object_ref",
                (source, table, self.allowed)).fetchall()
            self._table_cols[(source, table)] = [c[0] for c in cols]
            for (c_ref, column, is_grain, is_as_of, sensitivity, entity, concept, domain,
                 data_type) in cols:
                out.append(_prune({"id": f"{source}:{c_ref}", "kind": "column",
                                   "object_ref": c_ref, "table": table, "column": column,
                                   "catalog_source": source, "grain": is_grain,
                                   "as_of": is_as_of, "sensitivity": sensitivity,
                                   "entity": entity, "concept": concept, "domain": domain,
                                   "data_type": data_type,
                                   # as-of BASIS lives only in the availability_time fact, keyed on
                                   # the table's as-of column; attach it to that column alone
                                   "as_of_basis": basis if (is_as_of and column == as_of_col)
                                   else None,
                                   "stale": stale, "resolved": True}))
            return out
        if unit[0] == "feature":
            _, feature_id, name = unit
            verification, rationale = self._feature_stamp(feature_id)
            return [_prune({"id": f"feature:{feature_id}", "kind": "feature",
                            "feature_id": feature_id, "name": name, "grain": False, "as_of": False,
                            "verification": verification, "rationale": rationale,
                            "stale": self._feature_stale(feature_id), "resolved": True})]
        _, model_ref = unit
        return [{"id": f"consumer:{model_ref}", "kind": "consumer", "name": model_ref,
                 "grain": False, "as_of": False, "stale": False, "resolved": True}]

    def _contains_edges(self, unit: _Unit) -> None:
        """Structural table->column edges — ALWAYS emitted (the UI collapses columns into the
        table card; the layers param toggles traversal classes, not containment)."""
        if unit[0] != "table":
            return
        _, source, table = unit
        t_id = f"{source}:{_SCHEMA}.{table}"
        for c_ref in self._table_cols[(source, table)]:
            self._add_edge({"from": t_id, "to": f"{source}:{c_ref}", "layer": "joins",
                            "kind": "contains", "resolved": True})

    def _add_edge(self, edge: dict) -> None:
        ends = (edge["from"], edge["to"])
        key: tuple = (edge["kind"],) + (tuple(sorted(ends))
                                        if edge["kind"] in self._SYMMETRIC else ends)
        if key in self._edge_keys:   # symmetric kinds: the first (anchor-outward) orientation wins
            return
        self._edge_keys.add(key)
        self.edges.append(edge)

    # ---- layer expansions ------------------------------------------------------------------
    def _expand_joins(self, unit: _Unit) -> list[tuple[_Unit | None, dict | None, dict]]:
        """Join edges touching any visible column of this table, both directions. Read-scope
        matches column_joins/find_join_path: an edge whose endpoint column carries a sensitivity
        the caller can't see is withheld entirely (no stub — a stub would leak existence); a
        target that simply isn't loaded in this catalog (tn IS NULL) is kept as unresolved."""
        _, source, table = unit
        cols = self._table_cols[(source, table)]
        if not cols:
            return []
        rows = self.conn.execute(
            "SELECT e.from_ref, e.to_ref, e.cardinality, "
            "  EXISTS(SELECT 1 FROM graph_node n WHERE n.object_ref = e.to_ref "
            "         AND n.catalog_source = e.catalog_source) AS resolved, "
            "  e.authority, e.approved_join_status "
            "FROM graph_edge e "
            "LEFT JOIN graph_node fn ON fn.object_ref = e.from_ref "
            "  AND fn.catalog_source = e.catalog_source "
            "LEFT JOIN graph_node tn ON tn.object_ref = e.to_ref "
            "  AND tn.catalog_source = e.catalog_source "
            "WHERE e.catalog_source = %s AND e.kind = 'joins' "
            "  AND (e.from_ref = ANY(%s) OR e.to_ref = ANY(%s)) "
            "  AND COALESCE(fn.visible_requires, '{}') <@ %s "
            "  AND COALESCE(tn.visible_requires, '{}') <@ %s "
            "ORDER BY e.from_ref, e.to_ref",
            (source, cols, cols, self.allowed, self.allowed)).fetchall()
        colset = set(cols)
        out: list[tuple[_Unit | None, dict | None, dict]] = []
        for from_ref, to_ref, card, resolved, authority, join_status in rows:
            # #10: carry the edge's authority (+ folded fact status when fact-linked) so a
            # consumer can tell a display-only pending/rejected join from an operational one.
            if from_ref in colset:   # forward: declared orientation + declared cardinality
                edge = _prune({"from": f"{source}:{from_ref}", "to": f"{source}:{to_ref}",
                               "layer": "joins", "kind": "join", "cardinality": card,
                               "resolved": bool(resolved), "authority": authority,
                               "approved_join_status": join_status})
                if resolved:
                    out.append((("table", source, table_of_ref(to_ref)), None, edge))
                else:
                    out.append((None, _stub_node(source, to_ref), edge))
            else:   # reverse: orient the step to the traversal and invert the fan (M7)
                edge = _prune({"from": f"{source}:{to_ref}", "to": f"{source}:{from_ref}",
                               "layer": "joins", "kind": "join", "cardinality": _invert(card),
                               "resolved": True,   # from_ref always exists in its own catalog
                               "authority": authority, "approved_join_status": join_status})
                out.append((("table", source, table_of_ref(from_ref)), None, edge))
        return out

    def _expand_entity(self, unit: _Unit) -> list[tuple[_Unit | None, dict | None, dict]]:
        """Cross-catalog entity bridges: this table's visible entity-key columns to one key
        column per (catalog, table) sharing that entity elsewhere. Same-catalog relationships
        are the joins layer's job. Declared, never value-verified: resolved=false."""
        _, source, table = unit
        keys = self.conn.execute(
            "SELECT entity, min(object_ref) FROM graph_node "
            "WHERE kind = 'column' AND catalog_source = %s AND table_name = %s "
            "AND entity IS NOT NULL AND visible_requires <@ %s "
            "GROUP BY entity ORDER BY entity",
            (source, table, self.allowed)).fetchall()
        out: list[tuple[_Unit | None, dict | None, dict]] = []
        for entity, key_ref in keys:
            partners = self.conn.execute(
                "SELECT catalog_source, table_name, min(object_ref) FROM graph_node "
                "WHERE kind = 'column' AND entity = %s AND catalog_source <> %s "
                "AND visible_requires <@ %s "
                "GROUP BY catalog_source, table_name ORDER BY catalog_source, table_name",
                (entity, source, self.allowed)).fetchall()
            for p_source, p_table, p_ref in partners:
                edge = {"from": f"{source}:{key_ref}", "to": f"{p_source}:{p_ref}",
                        "layer": "entity", "kind": "entity_bridge",
                        "trust_kind": "advisory_lineage",
                        "endpoint_resolved": True,
                        "link_review_status": "not_governed",
                        "realization_safety_status": "not_evaluated",
                        "execution_eligible": False}
                out.append((("table", p_source, p_table), None, edge))
        out.extend(self._expand_derived_bridges(unit))
        return out

    def _expand_derived_bridges(self, unit: _Unit) -> list[tuple[_Unit | None, dict | None, dict]]:
        """The DERIVED cross-catalog links, from the bridge ledger.

        The entity-column path above finds partners by matching ``graph_node.entity``, which is NULL
        on every column of every real source loaded so far — nothing populates it (the same
        bootstrap gap that leaves entity_assignment with zero candidates). So that path has never
        produced an edge here, and clicking Graph on a linked column drew nothing.

        The links are not in doubt; they are in the ledger. Read them. Shown whether or not a human
        confirmed one — confirmation annotates, it does not gate. The old ``resolved`` field fused
        endpoint existence with review and execution; this path emits all three explicitly.

        READ-SCOPE is preserved exactly as the entity path preserves it: the partner column must be
        visible to THIS caller, or a bridge would become an existence oracle for a column the caller
        may not see.
        """
        from featuregen.overlay.upload.cross_catalog_links import (
            LinkStatus,
            cross_catalog_links,
        )

        _, source, table = unit
        mine = {
            r[0] for r in self.conn.execute(
                "SELECT object_ref FROM graph_node WHERE kind = 'column' "
                "AND catalog_source = %s AND table_name = %s AND visible_requires <@ %s",
                (source, table, self.allowed)).fetchall()
        }
        if not mine:
            return []
        out: list[tuple[_Unit | None, dict | None, dict]] = []
        seen: set[tuple[str, str, str]] = set()
        for link in cross_catalog_links(self.conn):
            if link.left_catalog_source == source and link.left_object_ref in mine:
                near_ref, far_src, far_ref = link.left_object_ref, link.right_catalog_source, \
                    link.right_object_ref
            elif link.right_catalog_source == source and link.right_object_ref in mine:
                near_ref, far_src, far_ref = link.right_object_ref, link.left_catalog_source, \
                    link.left_object_ref
            else:
                continue
            far = self.conn.execute(
                "SELECT table_name FROM graph_node WHERE kind = 'column' "
                "AND catalog_source = %s AND object_ref = %s AND visible_requires <@ %s",
                (far_src, far_ref, self.allowed)).fetchone()
            if far is None:      # hidden from this caller — absence must equal nonexistence
                continue
            # Keyed on the PAIR, not the far column alone: two different near columns can link to
            # the SAME far column (cust_pref_branch_cd and cust_prim_branch_cd both reach
            # tran_branch_sol_id), and keying on the far ref silently dropped all but the first —
            # 6 edges drawn for 9 real links.
            if (near_ref, far_src, far_ref) in seen:
                continue
            seen.add((near_ref, far_src, far_ref))
            trust = self._bridge_trust(link.fact_key)
            out.append((("table", far_src, far[0]), None, {
                "from": f"{source}:{near_ref}", "to": f"{far_src}:{far_ref}",
                "layer": "entity", "kind": "entity_bridge",
                "trust_kind": (
                    "executable_realization"
                    if trust["execution_eligible"]
                    else "governed_identifier_link"
                ),
                "endpoint_resolved": True,
                "link_review_status": (
                    "human_verified"
                    if link.status is LinkStatus.CONFIRMED
                    else "unreviewed"
                ),
                "realization_safety_status": trust["safety_status"],
                "execution_eligible": trust["execution_eligible"],
                "cardinality": trust["cardinality_label"],
                "entity_id": link.entity_id,
                # Ranking, carried onto the edge so the CANVAS can distinguish a grain-backed link
                # from a type-only match. Without it `cust_num <-> cif_id` drew identically to
                # `cust_prim_branch_nm <-> sol_desc`, which pairs a name with a description and is
                # not a real join — the list could say so and the graph could not.
                "strength": link.strength,
                "why": link.why,
            }))
        return out

    def _bridge_trust(self, fact_key: str | None) -> dict[str, object]:
        """Current realization truth for one governed link, loaded once for the whole graph."""
        if self._realization_views is None:
            from featuregen.overlay.upload.bridge_realization_governance import (
                list_bridge_realization_views,
            )

            grouped: dict[str, list[dict]] = {}
            for view in list_bridge_realization_views(self.conn, roles=self.roles):
                grouped.setdefault(str(view["bridge_fact_key"]), []).append(view)
            self._realization_views = grouped
        views = self._realization_views.get(str(fact_key), [])
        if not views:
            return {
                "safety_status": "not_evaluated",
                "execution_eligible": False,
                "cardinality_label": "Not evaluated",
            }
        eligible = [view for view in views if view["execution_eligible"]]
        chosen = eligible[0] if eligible else views[0]
        return {
            "safety_status": chosen["safety_status"],
            "execution_eligible": bool(eligible),
            "cardinality_label": chosen["cardinality_label"],
        }

    def _expand_derived_features(self, unit: _Unit) -> list[tuple[_Unit | None, dict | None, dict]]:
        """column -> feature over feature_derives_from, from VISIBLE columns only (a read-scoped
        column's feature edges disappear with it — lineage must not leak existence)."""
        _, source, table = unit
        cols = self._table_cols[(source, table)]
        if not cols:
            return []
        rows = self.conn.execute(
            "SELECT d.feature_id, f.name, d.object_ref "
            "FROM feature_derives_from d JOIN feature f ON f.feature_id = d.feature_id "
            "WHERE d.catalog_source = %s AND d.object_ref = ANY(%s) "
            "ORDER BY f.name, d.object_ref",
            (source, cols)).fetchall()
        return [(("feature", fid, name), None,
                 {"from": f"{source}:{c_ref}", "to": f"feature:{fid}",
                  "layer": "features", "kind": "derives", "resolved": True})
                for fid, name, c_ref in rows]

    def _expand_feature_sources(self, unit: _Unit) -> list[tuple[_Unit | None, dict | None, dict]]:
        """feature -> its source columns (upstream), read-scoped like get_feature: a derives row
        whose column the caller can't see is withheld, node and edge both."""
        _, feature_id, _name = unit
        rows = self.conn.execute(
            "SELECT d.catalog_source, d.object_ref, n.table_name "
            "FROM feature_derives_from d "
            "JOIN graph_node n ON n.catalog_source = d.catalog_source "
            "  AND n.object_ref = d.object_ref "
            "WHERE d.feature_id = %s AND COALESCE(n.visible_requires, '{}') <@ %s "
            "ORDER BY d.catalog_source, d.object_ref",
            (feature_id, self.allowed)).fetchall()
        return [(("table", src, tname), None,
                 {"from": f"{src}:{oref}", "to": f"feature:{feature_id}",
                  "layer": "features", "kind": "derives", "resolved": True})
                for src, oref, tname in rows]

    def _expand_consumers(self, unit: _Unit) -> list[tuple[_Unit | None, dict | None, dict]]:
        _, feature_id, _name = unit
        rows = self.conn.execute(
            "SELECT DISTINCT model_ref FROM feature_consumer WHERE feature_id = %s "
            "ORDER BY model_ref", (feature_id,)).fetchall()
        return [(("consumer", r[0]), None,
                 {"from": f"feature:{feature_id}", "to": f"consumer:{r[0]}",
                  "layer": "features", "kind": "consumes", "resolved": True})
                for r in rows]

    def _expand_consumer_features(self, unit: _Unit) -> list[tuple[_Unit | None, dict | None, dict]]:
        _, model_ref = unit
        rows = self.conn.execute(
            "SELECT DISTINCT fc.feature_id, f.name FROM feature_consumer fc "
            "JOIN feature f ON f.feature_id = fc.feature_id "
            "WHERE fc.model_ref = %s ORDER BY f.name", (model_ref,)).fetchall()
        return [(("feature", fid, name), None,
                 {"from": f"feature:{fid}", "to": f"consumer:{model_ref}",
                  "layer": "features", "kind": "consumes", "resolved": True})
                for fid, name in rows]

    # ---- as-of basis + feature stamp (metadata the map surfaces) ----------------------------
    def _as_of_basis(self, source: str, table: str) -> tuple[str | None, str | None]:
        """(as-of column, availability basis) for a table, from its VERIFIED availability_time fact.
        The BASIS (posted_at | ingested_at | event_time_plus_lag) lives ONLY in the fact stream —
        graph_node carries just the is_as_of flag — so we read the projected read model
        (overlay_fact_state) resolve_fact serves from, by the same (catalog_source, object_ref,
        fact_type) key. This is a DESCRIPTIVE label, so we read the VERIFIED value directly rather
        than re-run resolve_fact's adapter/config-gated expiry+drift machinery; node staleness is
        surfaced separately via the `stale` flag. (None, None) when no VERIFIED fact exists."""
        if (source, table) not in self._as_of:
            row = self.conn.execute(
                "SELECT value->>'column', value->>'basis' FROM overlay_fact_state "
                "WHERE catalog_source = %s AND object_ref = %s "
                "AND fact_type = 'availability_time' AND status = 'VERIFIED'",
                (source, f"{_SCHEMA}.{table}")).fetchone()
            self._as_of[(source, table)] = (row[0], row[1]) if row else (None, None)
        return self._as_of[(source, table)]

    def _feature_stamp(self, feature_id: str) -> tuple[str | None, str | None]:
        """The feature's verification stamp + the causal WHY it was born (the hypothesis behind its
        CURRENT governed contract).

        [4] composition-audit (double-authority): a GOVERNED feature's stamp is the READ-GATED effective
        verification — its current contract (the ``feature_current_contract`` pointer) routed through
        ``contract_read_status`` — NEVER the mutable ``feature.verification`` column (confirm promotes it,
        drift never demotes it). So a drifted feature shows a DOWNGRADED stamp on the lineage graph,
        matching Feature 360. A directly-registered feature (no contract) keeps its honest ``feature``
        stamp; rationale is None for a feature with no hypothesis-driven contract (dropped by _prune)."""
        # Local import: the govern module is heavy + adjacent to this layer — a function-local import
        # keeps the lineage module free of the contract-layer import graph.
        from featuregen.overlay.upload.contract.govern import (
            contract_read_status,
            feature_current_contract,
        )
        row = self.conn.execute(
            "SELECT verification FROM feature WHERE feature_id = %s", (feature_id,)).fetchone()
        if row is None:
            return None, None
        verification = row[0]
        rationale = None
        contract_id = feature_current_contract(self.conn, feature_id)
        if contract_id is not None:
            _eff_status, eff_verif = contract_read_status(self.conn, contract_id)
            verification = eff_verif                    # the gated truth, never the mutable feature stamp
            hyp = self.conn.execute(
                "SELECT ci.hypothesis FROM contract c "
                "LEFT JOIN contract_intent ci ON ci.intent_id = c.intent_id "
                "WHERE c.contract_id = %s", (contract_id,)).fetchone()
            rationale = (hyp[0] if hyp else None) or None
        return verification, rationale

    # ---- freshness (drift watermark vs 24h, same rule as search/feature_freshness) ----------
    def _watermark(self, source: str) -> datetime | None:
        """The source's last successful drift-scan completion (its vouch time), cached per source."""
        if source not in self._wm:
            self._wm[source] = drift_watermark(self.conn, source)
        return self._wm[source]

    def _source_stale(self, source: str) -> bool:
        return (wm := self._watermark(source)) is None or wm < self.now - self.fresh_within

    def _feature_stale(self, feature_id: str) -> bool:
        """A feature is stale if ANY source it derives from is (feature_freshness semantics)."""
        rows = self.conn.execute(
            "SELECT DISTINCT catalog_source FROM feature_derives_from WHERE feature_id = %s",
            (feature_id,)).fetchall()
        return any(self._source_stale(r[0]) for r in rows)
