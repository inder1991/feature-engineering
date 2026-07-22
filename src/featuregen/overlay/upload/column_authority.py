"""Slice 3 — the OperationalColumnFacts adapter (spec §4).

Separates a column field's GOVERNED authority (eligibility via the decision log / OVERLAY_FACT) from
its bare DISPLAY value (the flat graph_node column). The decision log stores only a value HASH, so a
reader NEVER dereferences a decision's load-bearing value — the usable value is the flat column, and
authority is a boolean derived from is_feature_eligible (additivity/logical_representation) or the
governed *_fact_event_id link (is_grain/is_as_of). unit/currency/entity/declared_type are hints: a
hint may only TIGHTEN a validator check (reject / needs-check), never CLEAR one.

Schema identity: the graph_node key itself is PUBLIC-FLATTENED (matching how ``graph.build_graph``
stores object_refs), but :func:`logical_ref_of` looks up the REAL (pre-flatten) schema from
``graph_node.schema_name`` to rebuild the SCHEMA-PRESERVING ``logical_ref`` that
``field_evidence``/``field_decision_event`` are actually keyed under (a non-``public``-schema source,
e.g. FTR, records decisions under its real schema — see ``field_resolution.py`` / ``object_ref.py``).
``schema_name`` NULL (public/technical upload) or the graph_node row absent both fall back to
``"public"``, so that path stays byte-identical.
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.contracts import DbConn
from featuregen.overlay.upload.field_resolution import is_feature_eligible
from featuregen.overlay.upload.object_ref import normalize_ref, parse_ref

# field_name -> the flat graph_node column holding the DISPLAY value the reader returns.
_VALUE_COLUMN: dict[str, str] = {
    "additivity": "additivity",
    "logical_representation": "data_type",   # the numeric-usable OPERATIONAL value (spec §4)
    "is_grain": "is_grain",
    "is_as_of": "is_as_of",
    "unit": "unit",
    "currency": "currency",
    "entity": "entity",
    "declared_type": "declared_type",
}
# Decision-governed fields: authority via is_feature_eligible, provenance = the *_decision_id link.
_DECISION_ID_COLUMN: dict[str, str] = {
    "additivity": "additivity_decision_id",
    "logical_representation": "logical_type_decision_id",
}
# OVERLAY_FACT-governed table facts: authority = flag true AND the *_fact_event_id link non-null.
_FACT_EVENT_COLUMN: dict[str, tuple[str, str]] = {
    "is_grain": ("is_grain", "grain_fact_event_id"),
    "is_as_of": ("is_as_of", "availability_fact_event_id"),
}


@dataclass(frozen=True, slots=True)
class OperationalColumnFacts:
    value: str | None          # from the flat graph_node column (decision log stores only a HASH)
    authority: str             # "governed" | "hint"
    provenance: str | None     # a *_decision_id or *_fact_event_id, else None


def logical_ref_of(conn: DbConn, catalog_source: str, object_ref: str) -> str:
    """Rebuild the SCHEMA-PRESERVING logical_ref for a graph_node ``(catalog_source, object_ref)``
    so the same string keys the decision log / field_evidence (via is_feature_eligible). graph_node
    object_refs are stored PUBLIC-FLATTENED (``public.table.column``), but the REAL (pre-flatten)
    schema a non-``public`` source declared lives in ``graph_node.schema_name`` — read it here (a
    single PK lookup) rather than assuming ``"public"``, so a real-schema upload's evidence/decisions
    (keyed under their schema-preserving logical_ref, per ``field_resolution.py`` / ``object_ref.py``)
    are actually reachable. ``normalize_ref`` (not string concatenation) so the case-folding EXACTLY
    matches how the writers keyed the row. Falls back to ``"public"`` when ``schema_name`` is NULL or
    the graph_node row is absent — public/technical uploads stay byte-identical."""
    parts = object_ref.split(".")
    if len(parts) >= 3:
        table, column = parts[-2], parts[-1]
    elif len(parts) == 2:
        table, column = parts[0], parts[1]
    else:
        table, column = object_ref, ""
    row = conn.execute(
        "SELECT schema_name FROM graph_node WHERE catalog_source = %s AND object_ref = %s",
        (catalog_source, object_ref)).fetchone()
    schema = row[0] if row is not None and row[0] else "public"
    return normalize_ref(catalog_source, schema, table, column or None)


def _render(raw: object) -> str | None:
    """Render a flat-column value to ``str | None`` (RF-I7): ``is_grain``/``is_as_of`` are BOOLEAN
    flat columns, and the downstream egress wrapper accepts only ``str | None`` — a raw bool would
    fail-close every flag-ON dispatch, so booleans render as ``"true"``/``"false"`` here."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return "true" if raw else "false"
    return str(raw)


def _scalar(conn: DbConn, catalog_source: str, object_ref: str, column: str):
    row = conn.execute(
        f"SELECT {column} FROM graph_node "  # column names are internal constants, not input
        "WHERE catalog_source = %s AND lower(object_ref) = %s AND kind = 'column'",
        (catalog_source, object_ref.lower())).fetchone()
    return row[0] if row is not None else None


def read_column_facts(conn: DbConn, logical_ref: str, field_name: str) -> OperationalColumnFacts:
    """Governed authority + hint separation for one column field (spec §4). See module docstring."""
    source, _schema, table, column = parse_ref(logical_ref)
    object_ref = ".".join(["public", table, *([column] if column else [])])
    value_col = _VALUE_COLUMN.get(field_name)
    raw = _scalar(conn, source, object_ref, value_col) if value_col is not None else None
    value = _render(raw)

    if field_name in _DECISION_ID_COLUMN:
        if is_feature_eligible(conn, logical_ref, field_name):
            prov = _scalar(conn, source, object_ref, _DECISION_ID_COLUMN[field_name])
            return OperationalColumnFacts(value=value, authority="governed", provenance=prov)
        return OperationalColumnFacts(value=value, authority="hint", provenance=None)

    if field_name in _FACT_EVENT_COLUMN:
        flag_col, event_col = _FACT_EVENT_COLUMN[field_name]
        flag = _scalar(conn, source, object_ref, flag_col)
        event_id = _scalar(conn, source, object_ref, event_col)
        if bool(flag) and event_id is not None:
            return OperationalColumnFacts(value=value, authority="governed", provenance=event_id)
        return OperationalColumnFacts(value=value, authority="hint", provenance=None)

    # hint-only: unit / currency / entity / declared_type (spec §4)
    return OperationalColumnFacts(value=value, authority="hint", provenance=None)
