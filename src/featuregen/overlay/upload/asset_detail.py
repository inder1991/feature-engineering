"""Delivery F0 Task 2 — the ASSET READ MODEL: one catalog asset, bounded sections, read-scoped.

:func:`build_asset_detail` assembles the sections the prototype ``AssetDetailSampleScreen`` (5 tabs)
consumes for ONE catalog asset ``(source, object_ref)``. It is a pure READ over already-shipped
authority — it never re-derives value, authority, or readiness, and it writes nothing:

* ``identity`` — the physical/logical identity from the anchor ``graph_node`` row.
* ``effective_metadata`` — the display value per field paired with its C1 authority/provenance
  (:func:`operational_facts.read_operational_value` — governed vs hint vs missing).
* ``evidence`` — the anchor's per-field proposals (active/stale/rejected) + its latest decision.
* ``relationships`` — containment (the parent table + sibling columns) + VERIFIED approved_joins.
  The SEMANTIC subsection (candidate/verified semantic edges) is F1 — returned ``unavailable``
  (listed in ``unavailable_sections``), never an empty-success that reads as "no relationships".
* ``readiness`` — the F0-T1 per-column capability MATRIX + the parent-table blocker diagnostic.
* ``history`` — the reverse ``ingestion_run_object`` lookup (which runs observed/changed this ref,
  newest-first) + each run's per-stage outcomes.
* ``actions`` — server-calculated commands the caller may run. F0 keeps this empty: the real
  correction command is Delivery F0-T4.

READ-SCOPE (the security invariant): the anchor is loaded UNDER the caller's sensitivity scope BEFORE
any section is built. A hidden anchor (a sensitivity the caller's roles can't see) is indistinguishable
from a missing one — both yield ``None`` (the route 404s), so the catalog never leaks that a restricted
object EXISTS. Related nodes/edges (sibling columns, join endpoints) are filtered by the SAME scope IN
SQL, so a hidden sibling is simply absent — no count, no id, nothing that reveals it. Sections that
cannot be served for THIS caller are named in ``unavailable_sections`` without a hidden count.

CONSISTENCY: the caller assembles every section under ONE ``REPEATABLE READ`` transaction (the route's
``get_feature_gen_conn`` dep), so all sections describe ONE torn-free catalog snapshot; a
content-hash ``consistency_token`` (also the HTTP ``ETag``) fingerprints that snapshot.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import asdict

from featuregen.contracts import DbConn
from featuregen.overlay.upload.column_authority import logical_ref_of
from featuregen.overlay.upload.column_readiness import column_readiness
from featuregen.overlay.upload.operational_facts import OperationalValue, read_operational_value
from featuregen.overlay.upload.read_scope import allowed_sensitivities
from featuregen.overlay.upload.readiness import ReadinessScopeType, compute_readiness

# The response contract version — bump on a breaking shape change so a client can negotiate.
ASSET_DETAIL_VERSION = "asset-detail/v1"

# The F0 sections, in render order. ``identity`` is ALWAYS built (it is the anchor's core); the rest
# are selectable via the route's ``include`` param. ``relationships.semantic`` is F1 (unavailable).
_F0_SECTIONS: tuple[str, ...] = (
    "identity", "effective_metadata", "evidence", "relationships", "readiness", "history", "actions",
)

# effective_metadata fields: (label, graph_node flat column, C1 field_name). The DISPLAY value comes
# from the flat column (the decision log stores only a hash); the AUTHORITY/provenance comes from C1.
_METADATA_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("concept", "concept", "concept"),
    ("definition", "definition", "definition"),
    ("domain", "domain", "domain"),
    ("additivity", "additivity", "additivity"),
    ("unit", "unit", "unit"),
    ("currency", "currency", "currency"),
    ("entity", "entity", "entity"),
    ("type", "data_type", "logical_representation"),
)

# The anchor graph_node columns this read model surfaces (identity + display metadata + fact links).
_ANCHOR_COLUMNS = (
    "catalog_source, object_ref, kind, table_name, column_name, schema_name, data_type, "
    "declared_type, definition, is_grain, is_as_of, concept, domain, sensitivity, additivity, "
    "unit, currency, entity, grain_fact_event_id, availability_fact_event_id"
)


def _load_anchor(conn: DbConn, source: str, object_ref: str, allowed: list[str]) -> dict | None:
    """The anchor ``graph_node`` row, loaded UNDER read-scope. Returns ``None`` when the ref does
    not exist OR carries a sensitivity the caller's roles can't see — the two are deliberately
    indistinguishable so a hidden object never leaks its existence via a different status/shape."""
    row = conn.execute(
        f"SELECT {_ANCHOR_COLUMNS} FROM graph_node "
        "WHERE catalog_source = %s AND lower(object_ref) = lower(%s) "
        "AND (sensitivity IS NULL OR sensitivity = ANY(%s))",
        (source, object_ref, allowed),
    ).fetchone()
    if row is None:
        return None
    cols = _ANCHOR_COLUMNS.replace(" ", "").split(",")
    return dict(zip(cols, row, strict=True))


def _identity_section(anchor: dict, logical_ref: str) -> dict:
    """Physical + logical identity from the anchor row (never a hidden field)."""
    return {
        "graph_ref": anchor["object_ref"],
        "object_ref": anchor["object_ref"],
        "logical_ref": logical_ref,
        "source": anchor["catalog_source"],
        "kind": anchor["kind"],
        "schema_name": anchor["schema_name"],   # the physical (pre-flatten) schema, or None
        "table": anchor["table_name"],
        "column": anchor["column_name"],
        "operational_type": anchor["data_type"],   # the numeric-usable operational type
        "declared_type": anchor["declared_type"],   # the FTR-declared SQL type (non-operational)
        "is_grain": anchor["is_grain"],
        "is_as_of": anchor["is_as_of"],
    }


def _authority_label(ov: OperationalValue, display_value: object | None) -> str:
    """The authority a display value carries: ``governed`` for a C1-verified projection; else
    ``hint`` when a value is shown; else ``missing`` (no value, no decision)."""
    if ov.status == "resolved":
        return "governed"
    if display_value is not None or ov.value is not None:
        return "hint"
    return "missing"


def _effective_metadata_section(conn: DbConn, logical_ref: str, anchor: dict) -> dict:
    """The display value + C1 authority/provenance for each metadata field (columns only). Every
    field's authority/provenance is SOURCED from C1 :func:`read_operational_value` — never
    re-derived here."""
    fields: dict[str, dict] = {}
    for label, flat_col, c1_field in _METADATA_FIELDS:
        display_value = anchor.get(flat_col)
        ov = read_operational_value(conn, logical_ref, c1_field)
        fields[label] = {
            "value": display_value,
            "authority": _authority_label(ov, display_value),
            "c1_status": ov.status,
            "provenance": ov.decision_event_id or ov.fact_event_id,
            "selected_evidence_ids": list(ov.selected_evidence_ids),
        }
    return {"fields": fields}


def _evidence_section(conn: DbConn, logical_ref: str) -> dict:
    """The anchor's per-field proposals bucketed by lifecycle (active/stale/rejected/superseded) +
    each field's LATEST decision — a set query over the field stores, not a per-field round trip.

    The anchor already passed read-scope (a hidden anchor 404s before this runs), so its own
    evidence is permitted; nothing here reaches beyond the anchor's ``logical_ref``."""
    proposals = conn.execute(
        "SELECT field_name, lifecycle, evidence_id, producer, strength, proposed_value, "
        "confidence_band FROM field_evidence WHERE logical_ref = %s "
        "ORDER BY field_name, created_at, evidence_id",
        (logical_ref,),
    ).fetchall()
    by_field: dict[str, dict] = {}
    for field_name, lifecycle, eid, producer, strength, value, band in proposals:
        entry = by_field.setdefault(
            field_name, {"active": [], "stale": [], "rejected": [], "superseded": []}
        )
        bucket = entry.get(lifecycle)
        if bucket is None:   # a lifecycle outside the shown buckets is not surfaced (still counted)
            bucket = entry.setdefault(lifecycle, [])
        bucket.append({
            "evidence_id": eid, "producer": producer, "strength": strength,
            "proposed_value": value, "confidence_band": band,
        })

    decisions = conn.execute(
        "SELECT DISTINCT field_name FROM field_decision_event WHERE logical_ref = %s",
        (logical_ref,),
    ).fetchall()
    latest: dict[str, dict] = {}
    for (field_name,) in decisions:
        row = conn.execute(
            "SELECT decision_event_id, event_type, conflict_status, load_bearing_value_hash, "
            "created_at FROM field_decision_event WHERE logical_ref = %s AND field_name = %s "
            "ORDER BY created_at DESC, decision_event_id DESC LIMIT 1",
            (logical_ref, field_name),
        ).fetchone()
        latest[field_name] = {
            "decision_event_id": row[0], "event_type": row[1], "conflict_status": row[2],
            "load_bearing": row[3] is not None, "decided_at": row[4],
        }

    return {"proposals_by_field": by_field, "latest_decision_by_field": latest}


def _relationships_section(
    conn: DbConn, source: str, anchor: dict, allowed: list[str]
) -> tuple[dict, list[str]]:
    """Containment (parent table + read-scoped sibling columns) + VERIFIED approved_joins touching
    the ref (read-scoped on BOTH endpoints). The SEMANTIC subsection is F1 — returned
    ``unavailable`` (and named in the returned list), never an empty-success.

    Every related read is sensitivity-filtered IN SQL, so a hidden sibling / hidden join endpoint
    is simply absent — no count, no id, no leak."""
    is_column = anchor["kind"] == "column"
    table = anchor["table_name"]

    # Containment: the sibling/child COLUMN nodes of the anchor's table, read-scoped. For a column
    # anchor the anchor itself is excluded; for a table anchor these are its children.
    sibling_rows = conn.execute(
        "SELECT object_ref, column_name, data_type, sensitivity FROM graph_node "
        "WHERE catalog_source = %s AND table_name = %s AND kind = 'column' "
        "AND lower(object_ref) <> lower(%s) "
        "AND (sensitivity IS NULL OR sensitivity = ANY(%s)) "
        "ORDER BY object_ref",
        (source, table, anchor["object_ref"], allowed),
    ).fetchall()
    siblings = [
        {"object_ref": r[0], "column": r[1], "data_type": r[2], "sensitivity": r[3]}
        for r in sibling_rows
    ]
    table_ref = f"public.{table}"
    containment = {
        "table": {"object_ref": table_ref, "table": table},
        "columns": siblings,   # sibling columns (column anchor) or child columns (table anchor)
    }

    # VERIFIED approved_joins touching the ref. For a column anchor the ref itself is the endpoint;
    # for a table anchor the endpoints are its (read-scoped) column refs.
    if is_column:
        endpoint_refs = [anchor["object_ref"]]
    else:
        endpoint_refs = [s["object_ref"] for s in siblings]
    approved_joins: list[dict] = []
    if endpoint_refs:
        join_rows = conn.execute(
            "SELECT e.from_ref, e.to_ref, e.cardinality, e.approved_join_status, "
            "e.approved_join_fact_key FROM graph_edge e "
            "LEFT JOIN graph_node fn ON fn.object_ref = e.from_ref "
            "  AND fn.catalog_source = e.catalog_source "
            "LEFT JOIN graph_node tn ON tn.object_ref = e.to_ref "
            "  AND tn.catalog_source = e.catalog_source "
            "WHERE e.catalog_source = %s AND e.kind = 'joins' "
            "AND e.approved_join_status = 'VERIFIED' "
            "AND (e.from_ref = ANY(%s) OR e.to_ref = ANY(%s)) "
            "AND (fn.sensitivity IS NULL OR fn.sensitivity = ANY(%s)) "
            "AND (tn.sensitivity IS NULL OR tn.sensitivity = ANY(%s)) "
            "ORDER BY e.from_ref, e.to_ref",
            (source, endpoint_refs, endpoint_refs, allowed, allowed),
        ).fetchall()
        approved_joins = [
            {"from_ref": r[0], "to_ref": r[1], "cardinality": r[2], "status": r[3],
             "approved_join_fact_key": r[4]}
            for r in join_rows
        ]

    section = {
        "containment": containment,
        "approved_joins": approved_joins,
        # F1 (after Delivery E): semantic candidates + verified semantic edges. Marked unavailable
        # so the client shows "not yet available", never an empty-success "no semantic links".
        "semantic": {"status": "unavailable", "available_in": "F1"},
    }
    return section, ["relationships.semantic"]


def _readiness_section(
    conn: DbConn, source: str, anchor: dict, roles: Iterable[str]
) -> dict:
    """The F0-T1 per-column capability MATRIX (columns only) + the parent-table blocker diagnostic
    (:func:`compute_readiness` TABLE scope). Both are pure reads; ``roles`` read-scopes the matrix's
    join-connectivity read."""
    section: dict = {}
    if anchor["kind"] == "column":
        section["column_capabilities"] = asdict(
            column_readiness(conn, source=source, object_ref=anchor["object_ref"], roles=roles)
        )
    else:
        section["column_capabilities"] = None   # a table asset has no per-column matrix
    section["table_diagnostic"] = asdict(
        compute_readiness(
            conn, source=source, scope=ReadinessScopeType.TABLE, subset=anchor["table_name"]
        )
    )
    return section


def _history_section(conn: DbConn, source: str, object_ref: str) -> dict:
    """The reverse ``ingestion_run_object`` lookup (which runs observed/changed this ref, newest
    first) + each run's per-stage outcomes — set queries, not a per-run round trip for the runs."""
    run_rows = conn.execute(
        "SELECT o.ingestion_run_id, o.relation, o.at, r.status, r.origin_type, r.started_at, "
        "r.completed_at FROM ingestion_run_object o "
        "JOIN ingestion_run r ON r.id = o.ingestion_run_id "
        "WHERE o.catalog_source = %s AND lower(o.object_ref) = lower(%s) "
        "ORDER BY o.at DESC, o.ingestion_run_id",
        (source, object_ref),
    ).fetchall()
    runs: list[dict] = []
    for run_id, relation, at, status, origin_type, started_at, completed_at in run_rows:
        stages = [
            {"stage": s, "attempt": a, "state": st, "reason_code": rc}
            for s, a, st, rc in conn.execute(
                "SELECT stage, attempt, state, reason_code FROM ingestion_run_stage "
                "WHERE ingestion_run_id = %s ORDER BY id",
                (run_id,),
            ).fetchall()
        ]
        runs.append({
            "ingestion_run_id": run_id, "relation": relation, "at": at, "status": status,
            "origin_type": origin_type, "started_at": started_at, "completed_at": completed_at,
            "stages": stages,
        })
    return {"runs": runs}


def _consistency_token(body: dict) -> str:
    """A content-hash fingerprint of the assembled snapshot — the ``consistency_token`` and HTTP
    ``ETag``. Canonical JSON (sorted keys, ``default=str`` for datetimes) so the same snapshot
    always yields the same token."""
    blob = json.dumps(body, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def build_asset_detail(
    conn: DbConn, *, source: str, object_ref: str, roles: Iterable[str], include: Iterable[str] | None = None
) -> dict | None:
    """Assemble the bounded F0 sections for ONE catalog asset ``(source, object_ref)``.

    READ-ONLY, and read-scoped: the anchor is loaded under the caller's sensitivity scope FIRST.
    Returns ``None`` when the anchor does not exist OR is hidden from these ``roles`` (the route
    maps ``None`` to 404 — a hidden object is indistinguishable from a missing one, no existence
    leak). ``include`` selects which sections to build (default: every F0 section); ``identity`` is
    always built. Every section is assembled from already-shipped authority (C0/C1/F0-T1/readiness/
    the run manifest) — nothing here re-derives value, authority, or readiness.

    Sections that cannot be served for THIS caller are listed in ``unavailable_sections`` without a
    hidden count; the SEMANTIC relationship subsection is always ``unavailable`` in F0 (it is F1).
    Assemble under ONE ``REPEATABLE READ`` transaction (the caller's ``conn``) so every section
    describes one torn-free snapshot; ``consistency_token`` fingerprints it.
    """
    norm_source = source.strip().lower()
    allowed = allowed_sensitivities(roles)
    anchor = _load_anchor(conn, norm_source, object_ref, allowed)
    if anchor is None:
        return None

    requested = set(include) if include is not None else set(_F0_SECTIONS)
    logical_ref = logical_ref_of(norm_source, anchor["object_ref"])
    is_column = anchor["kind"] == "column"

    unavailable: list[str] = []
    body: dict = {
        "version": ASSET_DETAIL_VERSION,
        "source": norm_source,
        "object_ref": anchor["object_ref"],
        "kind": anchor["kind"],
    }

    # identity is always present (the anchor's core).
    body["identity"] = _identity_section(anchor, logical_ref)
    built = ["identity"]

    if "effective_metadata" in requested:
        if is_column:
            body["effective_metadata"] = _effective_metadata_section(conn, logical_ref, anchor)
        else:
            body["effective_metadata"] = {"fields": {}, "note": "table asset — no per-field metadata"}
        built.append("effective_metadata")

    if "evidence" in requested:
        body["evidence"] = _evidence_section(conn, logical_ref)
        built.append("evidence")

    if "relationships" in requested:
        relationships, rel_unavailable = _relationships_section(conn, norm_source, anchor, allowed)
        body["relationships"] = relationships
        unavailable.extend(rel_unavailable)
        built.append("relationships")

    if "readiness" in requested:
        body["readiness"] = _readiness_section(conn, norm_source, anchor, roles)
        built.append("readiness")

    if "history" in requested:
        body["history"] = _history_section(conn, norm_source, anchor["object_ref"])
        built.append("history")

    if "actions" in requested:
        # F0: no server-calculated commands yet — the correction command is Delivery F0-T4.
        body["actions"] = []
        built.append("actions")

    body["included_sections"] = built
    body["unavailable_sections"] = unavailable
    body["consistency_token"] = _consistency_token(body)
    return body
