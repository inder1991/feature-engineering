"""OpenMetadata connector v1: client (paginated pull) + translator into CanonicalRow + preview.

Principle (binding spec 2026-07-09): a new mouth, same stomach. This module only READS
OpenMetadata and translates; ingestion goes through the unchanged ``ingest_upload`` pipeline.
The mapping table is implemented exactly:

  service/database FQN part  -> (scope filter only; the FeatureGen `source` is explicit in config)
  table name                 -> `table` (schema prefix folded per config: `schema_table` | `table`)
  column name                -> `column` (verbatim)
  column dataType            -> `type` (lowercased OM token; empty -> quarantined by the validator)
  column description         -> `definition` (advisory, verbatim; table descriptions have no
                                per-column canonical slot and are not imported in v1)
  PII/classification tags    -> `sensitivity` via the explicit tag map; an UNMAPPED tag passes
                                through LITERALLY, fails the existing sensitivity whitelist in
                                ``validate_rows`` and lands in quarantine — an import can never
                                silently weaken read-scope
  PRIMARY_KEY constraint     -> `is_grain` on the constraint's column(s)
  FOREIGN_KEY constraint     -> `joins_to` = "table.column"; `cardinality` stays blank (unknown)
  partition/time hints       -> NEVER mapped to `as_of`; surfaced as as-of SUGGESTIONS in the
                                preview payload for a human to confirm (suggestion != ingestion)
  additivity/unit/currency/entity -> imported blank ("semantics pending")

Failure modes: any page failure fails the WHOLE pull (import never sees a partial pull); unknown
dataType tokens / tag taxonomies follow the quarantine path, never a crash. The HTTP transport
lives behind the ``FetchPage`` seam so tests inject recorded fixture pages — no network in CI.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from fnmatch import fnmatch
from typing import Any

from featuregen.overlay.upload.brake import large_change_brake
from featuregen.overlay.upload.canonical import CanonicalRow, validate_rows
from featuregen.overlay.upload.upload_catalog import UploadCatalog

# ---- Client ---------------------------------------------------------------------------------

# The transport seam: (path, query params) -> one parsed JSON page. Tests inject fixture pages;
# production uses `httpx_fetch` below.
FetchPage = Callable[[str, dict[str, Any]], dict[str, Any]]

_TABLES_PATH = "/api/v1/tables"
_FIELDS = "columns,tags,tableConstraints"


class OMError(Exception):
    """Base class for OpenMetadata connector failures."""


class OMUnreachable(OMError):
    """OM could not be reached / returned garbage — surfaces as a clean 502; nothing touched."""


class OMAuthRejected(OMError):
    """OM rejected the connector's bot token — surfaces as a clean 401; nothing touched."""


def httpx_fetch(base_url: str, token: str, *, timeout: float = 10.0,
                transport: Any = None) -> FetchPage:
    """The real transport: httpx with a bounded timeout and clean 502/401 error mapping.

    A fresh client per page keeps the seam leak-free (a pull is a handful of pages); `transport`
    is injectable so the error mapping itself is testable via httpx.MockTransport — still no
    network.
    """
    import httpx  # imported here so the translator has no hard runtime coupling to the HTTP dep

    base = base_url.rstrip("/")

    def fetch(path: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            # follow_redirects is OFF (also httpx's default, pinned here for security): a redirect
            # to an off-allowlist host would bypass the caller's egress allowlist, so a 3xx is
            # refused below rather than chased.
            with httpx.Client(base_url=base, timeout=timeout, transport=transport,
                              follow_redirects=False,
                              headers={"Authorization": f"Bearer {token}"}) as client:
                resp = client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise OMUnreachable(f"OpenMetadata unreachable: {exc}") from exc
        if resp.status_code in (401, 403):
            raise OMAuthRejected(
                f"OpenMetadata rejected the connector token (HTTP {resp.status_code})")
        if 300 <= resp.status_code < 400:
            raise OMUnreachable(
                f"OpenMetadata attempted a redirect (HTTP {resp.status_code}); refusing to "
                "follow it (egress allowlist)")
        if resp.status_code >= 400:
            raise OMUnreachable(f"OpenMetadata returned HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise OMUnreachable("OpenMetadata returned a non-JSON response") from exc
        if not isinstance(body, dict):
            raise OMUnreachable("OpenMetadata returned an unexpected JSON shape")
        return body

    return fetch


def fetch_tables(fetch: FetchPage, *, page_size: int = 100) -> list[dict[str, Any]]:
    """Pull every table entity in scope, following the cursor ('after') pagination.

    Any page failure raises and fails the WHOLE pull — preview fails whole and import never sees
    a partial pull (spec failure mode). A repeated cursor (a misbehaving server) raises instead
    of looping forever.
    """
    tables: list[dict[str, Any]] = []
    after: str | None = None
    seen_cursors: set[str] = set()
    while True:
        params: dict[str, Any] = {"fields": _FIELDS, "limit": page_size}
        if after:
            params["after"] = after
        page = fetch(_TABLES_PATH, params)
        data = page.get("data")
        if not isinstance(data, list):
            raise OMUnreachable("OpenMetadata page has no 'data' list")
        tables.extend(data)
        paging = page.get("paging") or {}
        after = paging.get("after") if isinstance(paging, dict) else None
        if not after:
            return tables
        if after in seen_cursors:
            raise OMUnreachable("OpenMetadata pagination repeated a cursor")
        seen_cursors.add(after)


# ---- Translator -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OMConfig:
    """One configured connection's translation scope (stored in connector_config)."""

    base_url: str
    target_source: str                                  # the FeatureGen catalog source
    tag_map: Mapping[str, str] = field(default_factory=dict)   # OM tagFQN -> sensitivity ('' = ignore)
    filters: Mapping[str, str] = field(default_factory=dict)   # service|database|schema -> fnmatch pattern
    table_naming: str = "table"                         # 'table' | 'schema_table'


@dataclass(frozen=True, slots=True)
class AsOfSuggestion:
    """A partition / time-column hint. NEVER mapped to as_of — a human confirms as-of + basis."""

    table: str
    column: str
    hint: str


@dataclass(frozen=True, slots=True)
class Translation:
    rows: list[CanonicalRow]
    as_of_suggestions: list[AsOfSuggestion]
    tag_counts: dict[str, int]     # every OM tagFQN seen in the pull -> column count (tag-map panel)


def _entity_name(entity: Any) -> str:
    if isinstance(entity, dict):
        return str(entity.get("name") or "")
    if isinstance(entity, str):
        return entity
    return ""


def _fqn_parts(t: dict[str, Any]) -> dict[str, str]:
    """service/database/schema names for scope filtering: explicit entity refs first, then the
    fullyQualifiedName (service.database.schema.table) as fallback."""
    parts = str(t.get("fullyQualifiedName") or "").split(".")
    fallback = parts if len(parts) >= 4 else ["", "", ""]
    return {
        "service": _entity_name(t.get("service")) or fallback[0],
        "database": _entity_name(t.get("database")) or fallback[1],
        "schema": _entity_name(t.get("databaseSchema")) or fallback[2],
    }


def _in_scope(parts: Mapping[str, str], filters: Mapping[str, str]) -> bool:
    return all(fnmatch(parts.get(key, ""), pattern)
               for key, pattern in filters.items() if pattern)


def _fold_table(name: str, schema: str, table_naming: str) -> str:
    if table_naming == "schema_table" and schema:
        return f"{schema}_{name}"
    return name


def _grain_columns(t: dict[str, Any]) -> set[str]:
    """PRIMARY_KEY columns: the table-level constraint plus any column-level `constraint` marker."""
    cols: set[str] = set()
    for c in t.get("tableConstraints") or []:
        if isinstance(c, dict) and c.get("constraintType") == "PRIMARY_KEY":
            cols.update(str(name) for name in c.get("columns") or [])
    for col in t.get("columns") or []:
        if isinstance(col, dict) and col.get("constraint") == "PRIMARY_KEY" and col.get("name"):
            cols.add(str(col["name"]))
    return cols


def _join_targets(t: dict[str, Any], config: OMConfig) -> dict[str, str]:
    """FOREIGN_KEY constraints -> {local column: 'table.column'}. CanonicalRow carries
    single-column joins only, so composite FKs are skipped (v1); the referred table name is
    folded with the SAME naming rule as imported tables so the edge resolves in-scope.
    Cardinality is unknown to OM and stays blank (the UI renders 'cardinality unknown')."""
    out: dict[str, str] = {}
    for c in t.get("tableConstraints") or []:
        if not isinstance(c, dict) or c.get("constraintType") != "FOREIGN_KEY":
            continue
        cols = c.get("columns") or []
        refs = c.get("referredColumns") or []
        if len(cols) != 1 or len(refs) != 1:
            continue
        parts = str(refs[0]).split(".")
        if len(parts) < 2:
            continue
        target_schema = parts[-3] if len(parts) >= 3 else ""
        folded = _fold_table(parts[-2], target_schema, config.table_naming)
        out[str(cols[0])] = f"{folded}.{parts[-1]}"
    return out


def _partition_hints(t: dict[str, Any]) -> list[tuple[str, str]]:
    """tablePartition columns -> (column, hint). Handles both OM shapes: a plain column-name list
    with a table-level intervalType, and the newer per-column dicts."""
    part = t.get("tablePartition") or {}
    if not isinstance(part, dict):
        return []
    hints: list[tuple[str, str]] = []
    for c in part.get("columns") or []:
        if isinstance(c, str):
            name, interval = c, part.get("intervalType")
        elif isinstance(c, dict):
            name = c.get("columnName") or ""
            interval = c.get("intervalType") or part.get("intervalType")
        else:
            continue
        if name:
            hints.append((str(name),
                          f"partition column ({interval})" if interval else "partition column"))
    return hints


# Time-typed columns whose NAME reads like a time axis are surfaced as as-of suggestions too.
_TIME_TYPES = frozenset({"timestamp", "timestamptz", "timestampz", "datetime", "date"})
_TIME_NAME = re.compile(r"(_at|_date|_time|_ts|_on)$", re.IGNORECASE)


def _sensitivity(tag_fqns: list[str], tag_map: Mapping[str, str]) -> str:
    """Resolve a column's tags through the explicit tag map, FAIL-CLOSED: the first unmapped tag
    passes through literally so the existing sensitivity whitelist quarantines the column. Among
    mapped tags a non-empty mapping wins over an ignore (''); when several map, 'restricted' is
    preferred over 'pii' purely for determinism (each gates on its own reader role)."""
    mapped: list[str] = []
    for tag in tag_fqns:
        if tag not in tag_map:
            return tag           # literal pass-through -> whitelist quarantine (never weakened)
        if tag_map[tag]:
            mapped.append(tag_map[tag])
    if "restricted" in mapped:
        return "restricted"
    if "pii" in mapped:
        return "pii"
    return mapped[0] if mapped else ""


def read_openmetadata(tables_json: list[dict[str, Any]], config: OMConfig) -> Translation:
    """Translate OM table entities into CanonicalRows per the spec mapping table (module doc)."""
    rows: list[CanonicalRow] = []
    suggestions: list[AsOfSuggestion] = []
    tag_counts: dict[str, int] = {}
    for t in tables_json:
        if not isinstance(t, dict):
            continue
        parts = _fqn_parts(t)
        if not _in_scope(parts, config.filters):
            continue
        table = _fold_table(str(t.get("name") or ""), parts["schema"], config.table_naming)
        grain = _grain_columns(t)
        joins = _join_targets(t, config)
        suggested: set[str] = set()
        for col_name, hint in _partition_hints(t):
            if col_name not in suggested:
                suggestions.append(AsOfSuggestion(table, col_name, hint))
                suggested.add(col_name)
        for col in t.get("columns") or []:
            if not isinstance(col, dict):
                continue
            name = str(col.get("name") or "")
            data_type = str(col.get("dataType") or "").lower()
            tags = [str(tg.get("tagFQN")) for tg in col.get("tags") or []
                    if isinstance(tg, dict) and tg.get("tagFQN")]
            for tag in tags:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
            # as_of / as_of_basis / additivity / unit / currency / entity stay BLANK by design:
            # structure is vouched, semantics are pending a human owner's confirmation.
            rows.append(CanonicalRow(
                source=config.target_source, table=table, column=name, type=data_type,
                is_grain=name in grain,
                definition=str(col.get("description") or ""),
                sensitivity=_sensitivity(tags, config.tag_map),
                joins_to=joins.get(name, "")))
            if name not in suggested and data_type in _TIME_TYPES and _TIME_NAME.search(name):
                suggestions.append(AsOfSuggestion(
                    table, name, f"{data_type} column named like a time axis"))
                suggested.add(name)
    return Translation(rows=rows, as_of_suggestions=suggestions, tag_counts=tag_counts)


def snapshot_hash(rows: Iterable[CanonicalRow]) -> str:
    """Deterministic hash of the translated rows (sorted, so OM page order can't flip it).
    Preview returns it; import recomputes it after the re-pull and 409s on a mismatch —
    stale-preview protection."""
    canon = sorted((asdict(r) for r in rows), key=lambda d: (d["table"], d["column"]))
    return hashlib.sha256(json.dumps(canon, sort_keys=True).encode()).hexdigest()


def semantics_pending_count(rows: Iterable[CanonicalRow]) -> int:
    """Columns arriving without ANY of the safety facts the gauntlet depends on (as-of basis,
    additivity, unit/currency, entity) — flagged 'semantics pending' for owner confirmation."""
    return sum(1 for r in rows
               if not (r.as_of or r.additivity or r.unit or r.currency or r.entity))


# ---- Preview (dry run — NEVER writes) --------------------------------------------------------


def build_preview(conn: Any, config: OMConfig, translation: Translation) -> dict[str, Any]:
    """The dry-run the human approves: validation verdicts, per-table diff vs the CURRENT catalog
    (graph_node), tag-map panel, brake PREDICTION, as-of suggestions, and the snapshot hash.

    Read-only by construction: `validate_rows` is pure, the diff is a SELECT, and the brake
    verdict comes from the SAME `large_change_brake` the ingest pipeline runs (imported, not
    duplicated) — which only reads the prior snapshot. Preview never ingests.
    """
    vr = validate_rows(list(translation.rows), config.target_source)
    if vr.structural_error:
        raise ValueError(f"nothing to import: {vr.structural_error}")

    # Current catalog columns for the target source: {table: {column: (type, sensitivity, grain)}}.
    existing: dict[str, dict[str, tuple[str, str, bool]]] = {}
    for tbl, coln, dtype, sens, grain in conn.execute(
            "SELECT table_name, column_name, data_type, COALESCE(sensitivity, ''), is_grain "
            "FROM graph_node WHERE catalog_source = %s AND kind = 'column'",
            (config.target_source,)).fetchall():
        existing.setdefault(tbl, {})[coln] = (dtype or "", sens, bool(grain))

    good_by_table: dict[str, dict[str, CanonicalRow]] = {}
    for r in vr.good:
        good_by_table.setdefault(r.table, {})[r.column] = r
    pulled_by_table: dict[str, set[str]] = {}
    for r in translation.rows:
        pulled_by_table.setdefault(r.table, set()).add(r.column)
    quarantine_by_table: dict[str, list[dict[str, str]]] = {}
    for err in vr.quarantined:
        if err.row is not None:
            quarantine_by_table.setdefault(err.row.table, []).append(
                {"column": err.row.column, "reason": err.message})

    tables: list[dict[str, Any]] = []
    for table in sorted(pulled_by_table):
        changes: list[str] = []
        if table not in existing:
            status = "new"
        else:
            ex = existing[table]
            for cname, row in sorted(good_by_table.get(table, {}).items()):
                if cname not in ex:
                    changes.append(f"column {cname} added")
                    continue
                old_type, old_sens, old_grain = ex[cname]
                if (row.type or "") != old_type:
                    changes.append(f"{cname} type: {old_type or 'none'} -> {row.type or 'none'}")
                if (row.sensitivity or "") != old_sens:
                    changes.append(f"{cname} sensitivity: {old_sens or 'none'} -> "
                                   f"{row.sensitivity or 'none'}")
                if row.is_grain != old_grain:
                    changes.append(f"{cname} grain: {old_grain} -> {row.is_grain}")
            # Removal is judged against the whole PULL (good + quarantined): a quarantined column
            # is held for review, not removed — reporting it as removed would be dishonest.
            for cname in sorted(ex):
                if cname not in pulled_by_table[table]:
                    changes.append(f"column {cname} removed")
            status = "changed" if changes else "unchanged"
        tables.append({
            "table": table,
            "status": status,
            "columns": len(pulled_by_table[table]),
            "quarantine": quarantine_by_table.get(table, []),
            "changes": changes,
        })

    # Whole-table removals: a table in the CURRENT catalog that the new pull does not include at
    # all. build_graph does DELETE-then-rebuild per source, so on import these tables are dropped
    # and their facts staled — the human must see that in the dry run, or they'd approve a loss the
    # preview never showed. (The brake still weighs it; a removal under the 30% threshold clears the
    # brake, which is exactly the case this line exists to keep honest.)
    for table in sorted(set(existing) - set(pulled_by_table)):
        dropped = len(existing[table])
        tables.append({
            "table": table,
            "status": "removed",
            "columns": dropped,
            "quarantine": [],
            "changes": [f"no longer in the pull; import will drop this table and stale its "
                        f"{dropped} column{'' if dropped == 1 else 's'}"],
        })

    brake = large_change_brake(conn, config.target_source,
                               UploadCatalog(config.target_source, vr.good))
    statuses = [t["status"] for t in tables]
    return {
        "summary": {
            "tables": len(pulled_by_table),   # tables in the PULL; removed ones are counted below
            "columns": len(translation.rows),
            "new": statuses.count("new"),
            "changed": statuses.count("changed"),
            "unchanged": statuses.count("unchanged"),
            "removed": statuses.count("removed"),
            "would_quarantine": len(vr.quarantined),
            "semantics_pending": semantics_pending_count(vr.good),
        },
        "tag_map": [
            {"om_tag": tag, "mapped_to": config.tag_map.get(tag, ""),
             "unmapped": tag not in config.tag_map, "count": count}
            for tag, count in sorted(translation.tag_counts.items())
        ],
        "tables": tables,
        "brake": {"would_hold": brake.held, "reason": brake.reason},
        "as_of_suggestions": [asdict(s) for s in translation.as_of_suggestions],
        "snapshot_hash": snapshot_hash(translation.rows),
    }
