"""Catalog lineage graph — the bounded BFS behind GET /graph/lineage.

The graph view is a MAP of what the catalog knows around one anchor (a table or column ref):
which tables join to it, which cross-catalog tables share its business entities, and which
features and consumers hang off its columns. Three layers, each independently toggleable:

  joins    — declared single-column joins (graph_edge kind='joins'), traversed BOTH directions
             with cardinality oriented per traversal (a reverse N:1 reads 1:N — M7, matching
             find_join_path). A declared target not loaded in this catalog renders as a
             resolved=false stub node plus a resolved=false edge: pending joins are data,
             not errors.
  entity   — CROSS-catalog entity bridges (columns sharing graph_node.entity, the machinery
             behind entity.py's cross-catalog paths). Bridges are declared/entity-resolved,
             never value-verified, so bridge edges are always resolved=false.
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

from collections import deque
from collections.abc import Iterable
from datetime import datetime, timedelta

from featuregen.overlay.catalog_changes import drift_watermark
from featuregen.overlay.upload.join_path import _invert, _table_of
from featuregen.overlay.upload.read_scope import allowed_sensitivities

LAYERS = frozenset({"joins", "entity", "features"})
MAX_NODES = 200

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
            "table": _table_of(to_ref), "column": to_ref.split(".")[-1],
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
        "AND (sensitivity IS NULL OR sensitivity = ANY(%s))",
        (catalog_source, ref, allowed_sensitivities(roles))).fetchone()
    if anchor is None:
        return None
    b = _Builder(conn, layers=frozenset(layers), direction=direction, roles=roles,
                 now=now, fresh_within=fresh_within, max_nodes=max_nodes)
    b.run(("table", catalog_source, anchor[0]), depth)
    return {"nodes": list(b.nodes.values()), "edges": b.edges, "truncated": b.truncated}


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
        self._edge_keys: set[tuple] = set()
        self._wm: dict[str, datetime | None] = {}               # per-source drift watermark (cached)
        self._as_of: dict[tuple[str, str], tuple[str | None, str | None]] = {}  # (source, table) -> (as-of col, basis)
        self._table_cols: dict[tuple[str, str], list[str]] = {}  # (source, table) -> visible col refs

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
                            continue
                        self.nodes[stub["id"]] = stub
                    self._add_edge(edge)
                    continue
                assert neighbor is not None
                if neighbor not in seen:
                    if not self._try_install(neighbor):
                        continue   # over the cap: skip the unit AND its edge (no dangling ends)
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
                "SELECT object_ref, column_name, is_grain, is_as_of, sensitivity, entity, "
                "concept, domain FROM graph_node WHERE catalog_source = %s AND kind = 'column' "
                "AND table_name = %s AND (sensitivity IS NULL OR sensitivity = ANY(%s)) "
                "ORDER BY object_ref",
                (source, table, self.allowed)).fetchall()
            self._table_cols[(source, table)] = [c[0] for c in cols]
            for c_ref, column, is_grain, is_as_of, sensitivity, entity, concept, domain in cols:
                out.append(_prune({"id": f"{source}:{c_ref}", "kind": "column",
                                   "object_ref": c_ref, "table": table, "column": column,
                                   "catalog_source": source, "grain": is_grain,
                                   "as_of": is_as_of, "sensitivity": sensitivity,
                                   "entity": entity, "concept": concept, "domain": domain,
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
            "  AND (fn.sensitivity IS NULL OR fn.sensitivity = ANY(%s)) "
            "  AND (tn.sensitivity IS NULL OR tn.sensitivity = ANY(%s)) "
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
                    out.append((("table", source, _table_of(to_ref)), None, edge))
                else:
                    out.append((None, _stub_node(source, to_ref), edge))
            else:   # reverse: orient the step to the traversal and invert the fan (M7)
                edge = _prune({"from": f"{source}:{to_ref}", "to": f"{source}:{from_ref}",
                               "layer": "joins", "kind": "join", "cardinality": _invert(card),
                               "resolved": True,   # from_ref always exists in its own catalog
                               "authority": authority, "approved_join_status": join_status})
                out.append((("table", source, _table_of(from_ref)), None, edge))
        return out

    def _expand_entity(self, unit: _Unit) -> list[tuple[_Unit | None, dict | None, dict]]:
        """Cross-catalog entity bridges: this table's visible entity-key columns to one key
        column per (catalog, table) sharing that entity elsewhere. Same-catalog relationships
        are the joins layer's job. Declared, never value-verified: resolved=false."""
        _, source, table = unit
        keys = self.conn.execute(
            "SELECT entity, min(object_ref) FROM graph_node "
            "WHERE kind = 'column' AND catalog_source = %s AND table_name = %s "
            "AND entity IS NOT NULL AND (sensitivity IS NULL OR sensitivity = ANY(%s)) "
            "GROUP BY entity ORDER BY entity",
            (source, table, self.allowed)).fetchall()
        out: list[tuple[_Unit | None, dict | None, dict]] = []
        for entity, key_ref in keys:
            partners = self.conn.execute(
                "SELECT catalog_source, table_name, min(object_ref) FROM graph_node "
                "WHERE kind = 'column' AND entity = %s AND catalog_source <> %s "
                "AND (sensitivity IS NULL OR sensitivity = ANY(%s)) "
                "GROUP BY catalog_source, table_name ORDER BY catalog_source, table_name",
                (entity, source, self.allowed)).fetchall()
            for p_source, p_table, p_ref in partners:
                edge = {"from": f"{source}:{key_ref}", "to": f"{p_source}:{p_ref}",
                        "layer": "entity", "kind": "entity_bridge", "resolved": False}
                out.append((("table", p_source, p_table), None, edge))
        return out

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
            "WHERE d.feature_id = %s AND (n.sensitivity IS NULL OR n.sensitivity = ANY(%s)) "
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
