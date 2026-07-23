"""Child-1 Task 9 — the 7 governed catalog-authoring tools (spec §I).

ALL tools are READ/VALIDATE-ONLY over the governed catalog, READ-SCOPED (``roles`` gates
sensitivity-tagged columns exactly as /search does — a hidden column is indistinguishable from a
nonexistent one), and METADATA-ONLY on egress: a result carries column/grain/operation METADATA —
names, types, roles, governed facts, verdicts — NEVER raw data values and NEVER catalog free text
(``graph_node.definition`` is uploader-authored prose, so no tool returns it; it must not ride the
tool trail into a provider payload). This mirrors the ``LLMRequest.inputs`` "NO data values (§9.4)"
rule: what a tool returns is exactly what the next turn's ``catalog_metadata`` may carry.

Tool results are DATA, not instructions: the author threads a result into the NEXT turn's
``catalog_metadata`` verbatim — never into the instruction text — so a value that tried to smuggle
instructions stays inert payload (and still rides through the egress guard's PII backstop).

Each tool WRAPS the real shipped reader where one exists (never re-implementing governance):

* ``search_columns``            → ``overlay.upload.search.search`` (read-scoped FTS + facets)
* ``get_column_metadata``       → ``overlay.upload.column_authority.read_column_facts``
* ``get_governed_grain``        → graph_node grain flags + ``read_column_facts("is_grain")``
* ``get_time_anchor``           → graph_node as-of flags + ``read_column_facts("is_as_of")``
* ``get_verified_lineage``      → ``overlay.upload.lineage.lineage_graph`` filtered to VERIFIED
                                  approved joins (the ONLY operational join path)
* ``list_supported_operations`` → the §B ``operations.to_path_aggregation`` map
* ``validate_draft_formula``    → ``parse.parse_proposal_v1`` (shape + semantics) +
                                  ``capability.classify_formula_capability``

A bad ARGUMENT returns an ``{"error": ...}`` result (data the model can correct from next turn);
only an unknown tool NAME raises (the turn schema's enum makes that unreachable from a validated
turn — reaching it means the boundary was breached, and the author fails the run closed).
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from featuregen.formula.capability import (
    CAPABILITY_POLICY_VERSION,
    classify_formula_capability,
)
from featuregen.formula.operations import to_path_aggregation
from featuregen.formula.parse import parse_proposal_v1
from featuregen.formula.schema import (
    OPERATION_GRAMMAR_VERSION,
    AggregateFunction,
    FinalOperation,
    SchemaError,
)
from featuregen.formula.turns import TOOL_NAMES
from featuregen.overlay.upload.column_authority import logical_ref_of, read_column_facts
from featuregen.overlay.upload.lineage import lineage_graph
from featuregen.overlay.upload.object_ref import normalize_ref, parse_ref
from featuregen.overlay.upload.read_scope import allowed_sensitivities
from featuregen.overlay.upload.search import search

__all__ = ["TOOLS", "ToolSpec", "run_tool"]

# The governable column fields get_column_metadata reads through the C1 authority adapter.
_COLUMN_FACT_FIELDS: tuple[str, ...] = (
    "additivity", "logical_representation", "is_grain", "is_as_of",
    "unit", "currency", "entity", "declared_type",
)

_DEFAULT_SEARCH_LIMIT = 10
_MAX_SEARCH_LIMIT = 25
_MAX_LINEAGE_DEPTH = 3


def _error(message: str) -> dict:
    return {"error": message}


def _clamped_int(value: object, default: int, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(lo, min(value, hi))


def _require_str(arguments: Mapping, key: str) -> str | None:
    value = arguments.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


# ---- the tools ---------------------------------------------------------------------------------


def _search_columns(conn, arguments: Mapping, roles: tuple[str, ...]) -> dict:
    """Read-scoped catalog search; COLUMN hits only, as structural metadata + the schema-preserving
    ``logical_ref`` the formula vocabulary uses. ``definition`` (free text) is deliberately absent."""
    query = arguments.get("query")
    if not isinstance(query, str):
        return _error("search_columns requires a string 'query'")
    limit = _clamped_int(arguments.get("limit"), _DEFAULT_SEARCH_LIMIT, 1, _MAX_SEARCH_LIMIT)
    result = search(conn, query.strip(), now=datetime.now(UTC), roles=roles, limit=limit)
    columns = []
    for hit in result.hits:
        if hit.kind != "column" or hit.column is None:
            continue
        columns.append({
            "logical_ref": logical_ref_of(conn, hit.catalog_source, hit.object_ref),
            "catalog_source": hit.catalog_source,
            "object_ref": hit.object_ref,
            "table": hit.table,
            "column": hit.column,
            "data_type": hit.data_type,
            "concept": hit.concept,
            "domain": hit.domain,
            "is_grain": hit.is_grain,
            "is_as_of": hit.is_as_of,
            "additivity": hit.additivity,
            "unit": hit.unit,
            "currency": hit.currency,
            "entity": hit.entity,
        })
    return {"columns": columns, "total_matches": result.total}


def _get_column_metadata(conn, arguments: Mapping, roles: tuple[str, ...]) -> dict:
    """One column's governed facts via ``read_column_facts`` — value/authority/provenance per
    field, metadata only. Read-scoped: a sensitivity-hidden column reads as not found."""
    ref = _require_str(arguments, "logical_ref")
    if ref is None:
        return _error("get_column_metadata requires 'logical_ref'")
    try:
        source, schema, table, column = parse_ref(ref)
    except ValueError as exc:
        return _error(str(exc))
    if column is None:
        return _error("logical_ref must be a COLUMN ref (source::schema.table.column)")
    normalized = normalize_ref(source, schema, table, column)
    object_ref = f"public.{table}.{column}"
    row = conn.execute(
        "SELECT data_type, sensitivity FROM graph_node "
        "WHERE catalog_source = %s AND lower(object_ref) = %s AND kind = 'column'",
        (source, object_ref.lower())).fetchone()
    if row is None or (row[1] is not None and row[1] not in allowed_sensitivities(roles)):
        return {"found": False}   # hidden by read-scope == nonexistent (never leak existence)
    facts = {}
    for field_name in _COLUMN_FACT_FIELDS:
        col = read_column_facts(conn, normalized, field_name)
        facts[field_name] = {
            "value": col.value, "authority": col.authority, "provenance": col.provenance}
    return {"found": True, "logical_ref": normalized, "table": table, "column": column,
            "data_type": row[0], "facts": facts}


def _fact_columns(conn, arguments: Mapping, roles: tuple[str, ...], *,
                  flag_column: str, field_name: str) -> dict | list[dict]:
    """Shared grain/as-of scan: the table's read-scope-visible flagged columns, each with the REAL
    authority verdict from ``read_column_facts`` (governed iff the *_fact_event_id link stands)."""
    source = _require_str(arguments, "catalog_source")
    table = _require_str(arguments, "table")
    if source is None or table is None:
        return _error("requires 'catalog_source' and 'table'")
    rows = conn.execute(
        f"SELECT object_ref, column_name FROM graph_node "  # flag_column is an internal constant
        f"WHERE catalog_source = %s AND kind = 'column' AND table_name = %s AND {flag_column} "
        "AND (sensitivity IS NULL OR sensitivity = ANY(%s)) ORDER BY object_ref",
        (source.lower(), table, allowed_sensitivities(roles))).fetchall()
    out = []
    for object_ref, column in rows:
        logical_ref = logical_ref_of(conn, source.lower(), object_ref)
        col = read_column_facts(conn, logical_ref, field_name)
        out.append({"logical_ref": logical_ref, "column": column,
                    "authority": col.authority, "provenance": col.provenance})
    return out


def _get_governed_grain(conn, arguments: Mapping, roles: tuple[str, ...]) -> dict:
    """The table's grain columns (``Grain.keys`` candidates) with their governed-fact authority."""
    columns = _fact_columns(conn, arguments, roles, flag_column="is_grain", field_name="is_grain")
    if isinstance(columns, dict):
        return columns   # argument error
    source = arguments["catalog_source"].strip().lower()
    table = arguments["table"].strip()
    return {"table_ref": normalize_ref(source, None, table), "grain_columns": columns,
            "governed": bool(columns) and all(c["authority"] == "governed" for c in columns)}


def _get_time_anchor(conn, arguments: Mapping, roles: tuple[str, ...]) -> dict:
    """The table's as-of columns — each ``logical_ref`` is directly usable as a
    ``WindowPolicy.event_time_ref`` — with their governed-fact authority."""
    columns = _fact_columns(conn, arguments, roles, flag_column="is_as_of", field_name="is_as_of")
    if isinstance(columns, dict):
        return columns   # argument error
    source = arguments["catalog_source"].strip().lower()
    table = arguments["table"].strip()
    return {"table_ref": normalize_ref(source, None, table), "time_anchor_columns": columns,
            "governed": bool(columns) and all(c["authority"] == "governed" for c in columns)}


def _get_verified_lineage(conn, arguments: Mapping, roles: tuple[str, ...]) -> dict:
    """The joins-layer lineage map around one ref, filtered to VERIFIED approved joins — the ONLY
    operational join path (dual platform-admin confirm). Structural ``contains`` edges are kept;
    candidate/declared/pending joins are NOT verified lineage and are withheld."""
    source = _require_str(arguments, "catalog_source")
    ref = _require_str(arguments, "ref")
    if source is None or ref is None:
        return _error("get_verified_lineage requires 'catalog_source' and 'ref'")
    depth = _clamped_int(arguments.get("depth"), 1, 1, _MAX_LINEAGE_DEPTH)
    graph = lineage_graph(conn, source.lower(), ref, now=datetime.now(UTC), roles=roles,
                          layers=("joins",), depth=depth)
    if graph is None:
        return {"found": False}   # unknown or read-scope-hidden anchor — indistinguishable
    edges = [e for e in graph["edges"]
             if e.get("kind") == "contains" or e.get("approved_join_status") == "VERIFIED"]
    return {"found": True, "nodes": graph["nodes"], "edges": edges,
            "truncated": graph["truncated"]}


def _list_supported_operations(conn, arguments: Mapping, roles: tuple[str, ...]) -> dict:
    """The §B supported-operation set: every ``AggregateFunction`` with its path-aggregation
    compatibility (``unsupported ≠ invalid``), plus the body-shape ``FinalOperation`` vocabulary."""
    aggregate_functions = []
    for fn in AggregateFunction:
        path_aggregation = to_path_aggregation(fn)
        aggregate_functions.append({
            "name": fn.value,
            "supported": path_aggregation is not None,
            "path_aggregation": path_aggregation.value if path_aggregation is not None else None,
        })
    return {"aggregate_functions": aggregate_functions,
            "final_operations": [op.value for op in FinalOperation],
            "operation_grammar_version": OPERATION_GRAMMAR_VERSION}


def _validate_draft_formula(conn, arguments: Mapping, roles: tuple[str, ...]) -> dict:
    """A structural/capability VERDICT on a draft proposal — metadata for the model's next turn,
    NEVER a disposition (§F folding stays with Task 12). ``parse_proposal_v1`` runs the full
    shape + ``validate_semantics`` gauntlet; a valid draft is then capability-classified."""
    proposal = arguments.get("proposal")
    if not isinstance(proposal, Mapping):
        return _error("validate_draft_formula requires an object 'proposal'")
    try:
        parsed = parse_proposal_v1(proposal)
    except SchemaError as exc:
        return {"verdict": "invalid", "detail": str(exc)[:500],
                "capability_policy_version": CAPABILITY_POLICY_VERSION}
    return {"verdict": classify_formula_capability(parsed), "detail": None,
            "capability_policy_version": CAPABILITY_POLICY_VERSION}


# ---- the registry ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One governed catalog-authoring tool: its handler + its input/output contracts."""

    name: str
    description: str
    input_schema: dict
    output_schema: dict
    handler: Callable[..., dict]


def _obj(properties: dict, required: tuple[str, ...] = ()) -> dict:
    schema: dict = {"type": "object", "additionalProperties": False, "properties": properties}
    if required:
        schema["required"] = list(required)
    return schema


_FACT = _obj({"value": {"type": ["string", "null"]}, "authority": {"enum": ["governed", "hint"]},
              "provenance": {"type": ["string", "null"]}})
_FLAGGED_COLUMN = _obj({"logical_ref": {"type": "string"}, "column": {"type": "string"},
                        "authority": {"enum": ["governed", "hint"]},
                        "provenance": {"type": ["string", "null"]}})

TOOLS: dict[str, ToolSpec] = {spec.name: spec for spec in (
    ToolSpec(
        name="search_columns",
        description="Read-scoped catalog search; returns column metadata (names/types/roles/"
                    "governed flags) and each column's logical_ref — never data values.",
        input_schema=_obj({"query": {"type": "string"}, "limit": {"type": "integer"}},
                          required=("query",)),
        output_schema=_obj({"columns": {"type": "array"}, "total_matches": {"type": "integer"}}),
        handler=_search_columns),
    ToolSpec(
        name="get_column_metadata",
        description="One column's governed facts (value/authority/provenance per field) via the "
                    "C1 column-authority reader; metadata only.",
        input_schema=_obj({"logical_ref": {"type": "string"}}, required=("logical_ref",)),
        output_schema=_obj({"found": {"type": "boolean"}, "logical_ref": {"type": "string"},
                            "table": {"type": "string"}, "column": {"type": "string"},
                            "data_type": {"type": ["string", "null"]},
                            "facts": {"type": "object", "additionalProperties": _FACT}}),
        handler=_get_column_metadata),
    ToolSpec(
        name="get_governed_grain",
        description="The table's grain columns (Grain.keys candidates) with governed-fact "
                    "authority.",
        input_schema=_obj({"catalog_source": {"type": "string"}, "table": {"type": "string"}},
                          required=("catalog_source", "table")),
        output_schema=_obj({"table_ref": {"type": "string"},
                            "grain_columns": {"type": "array", "items": _FLAGGED_COLUMN},
                            "governed": {"type": "boolean"}}),
        handler=_get_governed_grain),
    ToolSpec(
        name="get_time_anchor",
        description="The table's as-of columns — each logical_ref is usable as "
                    "WindowPolicy.event_time_ref — with governed-fact authority.",
        input_schema=_obj({"catalog_source": {"type": "string"}, "table": {"type": "string"}},
                          required=("catalog_source", "table")),
        output_schema=_obj({"table_ref": {"type": "string"},
                            "time_anchor_columns": {"type": "array", "items": _FLAGGED_COLUMN},
                            "governed": {"type": "boolean"}}),
        handler=_get_time_anchor),
    ToolSpec(
        name="get_verified_lineage",
        description="The joins-layer lineage around a table/column ref, filtered to VERIFIED "
                    "approved joins (the only operational join path).",
        input_schema=_obj({"catalog_source": {"type": "string"}, "ref": {"type": "string"},
                           "depth": {"type": "integer"}}, required=("catalog_source", "ref")),
        output_schema=_obj({"found": {"type": "boolean"}, "nodes": {"type": "array"},
                            "edges": {"type": "array"}, "truncated": {"type": "boolean"}}),
        handler=_get_verified_lineage),
    ToolSpec(
        name="list_supported_operations",
        description="The supported aggregate-function set (with path-aggregation compatibility) "
                    "and the final-operation body shapes.",
        input_schema=_obj({}),
        output_schema=_obj({"aggregate_functions": {"type": "array"},
                            "final_operations": {"type": "array"},
                            "operation_grammar_version": {"type": "integer"}}),
        handler=_list_supported_operations),
    ToolSpec(
        name="validate_draft_formula",
        description="Structural + capability verdict on a draft proposal (invalid / ok / "
                    "unsupported_capability) — a verdict, never a disposition.",
        input_schema=_obj({"proposal": {"type": "object"}}, required=("proposal",)),
        output_schema=_obj({"verdict": {"enum": ["ok", "invalid", "unsupported_capability"]},
                            "detail": {"type": ["string", "null"]},
                            "capability_policy_version": {"type": "integer"}}),
        handler=_validate_draft_formula),
)}

assert tuple(TOOLS) == TOOL_NAMES  # the registry and the turn-schema enum can never drift


def run_tool(conn, name: str, arguments: Mapping | None, *, roles: Iterable[str] = ()) -> dict:
    """Run one governed catalog-authoring tool and return its CANONICAL result dict.

    Read-only over ``conn``; ``roles`` gates read-scope. Raises ``KeyError`` for a tool name
    outside the registry (unreachable from a schema-validated turn — see module docstring); an
    argument problem comes back as an ``{"error": ...}`` result the model can correct from."""
    spec = TOOLS.get(name)
    if spec is None:
        raise KeyError(f"unknown catalog-authoring tool {name!r}")
    return spec.handler(conn, dict(arguments or {}), tuple(roles))
