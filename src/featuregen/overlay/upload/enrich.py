from __future__ import annotations

import hashlib

from featuregen.intake.llm import LLMClient, LLMRequest
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.concepts import UNCLASSIFIED, is_known_concept

_TASK = "overlay.enrich.concept"
_DEF_TASK = "overlay.enrich.definition"
_DOMAIN_TASK = "overlay.enrich.domain"


def content_hash(row: CanonicalRow) -> str:
    raw = f"{row.table}|{row.column}|{row.type}|{row.definition}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _table_content_hash(table: str, columns: list[str]) -> str:
    raw = f"{table}|" + "|".join(sorted(columns))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# Cache tables all share the shape (content_hash PK, <value> text). _CACHES maps the value column name.
_CACHES = {
    "enrichment_concept": "concept",
    "enrichment_definition": "definition",
    "enrichment_domain": "domain",
}


def _cache_get(conn, cache_table: str, hashes: list[str]) -> dict[str, str]:
    if not hashes:
        return {}
    col = _CACHES[cache_table]
    rows = conn.execute(
        f"SELECT content_hash, {col} FROM {cache_table} WHERE content_hash = ANY(%s)",
        (hashes,)).fetchall()
    return {r[0]: r[1] for r in rows}


def _cache_put(conn, cache_table: str, content_hash_: str, value: str) -> None:
    col = _CACHES[cache_table]
    conn.execute(
        f"INSERT INTO {cache_table} (content_hash, {col}) VALUES (%s, %s) "
        "ON CONFLICT (content_hash) DO NOTHING",
        (content_hash_, value))


def _call(client: LLMClient, task: str, prompt_id: str, schema_id: str, inputs: dict,
          out_key: str) -> str:
    req = LLMRequest(
        task=task, prompt_id=prompt_id, prompt_version=1,
        inputs=inputs,   # schema metadata only — never data values
        output_schema_id=schema_id, output_schema_version=1,
        generation_settings={"provider": "fake", "model": "test"},
    )
    return str(client.call(req).output.get(out_key, "")).strip()


def enrich_concepts(conn, rows: list[CanonicalRow], client: LLMClient) -> dict[str, str]:
    by_hash: dict[str, CanonicalRow] = {content_hash(r): r for r in rows}
    result = _cache_get(conn, "enrichment_concept", list(by_hash))
    for h, row in by_hash.items():
        if h in result:
            continue
        concept = _call(client, _TASK, "overlay_concept_v1", "overlay_concept",
                        {"table": row.table, "column": row.column, "type": row.type,
                         "definition": row.definition}, "concept")
        concept = concept if is_known_concept(concept) else UNCLASSIFIED
        _cache_put(conn, "enrichment_concept", h, concept)
        result[h] = concept
    return result


def draft_definitions(conn, rows: list[CanonicalRow], client: LLMClient) -> dict[str, str]:
    """Draft a definition ONLY for columns with no declared one (R3: never overwrite a human's)."""
    blank = {content_hash(r): r for r in rows if not r.definition}
    result = _cache_get(conn, "enrichment_definition", list(blank))
    for h, row in blank.items():
        if h in result:
            continue
        drafted = _call(client, _DEF_TASK, "overlay_definition_v1", "overlay_definition",
                        {"table": row.table, "column": row.column, "type": row.type}, "definition")
        _cache_put(conn, "enrichment_definition", h, drafted)
        result[h] = drafted
    return result


def classify_domains(conn, rows: list[CanonicalRow], client: LLMClient) -> dict[str, str]:
    """Classify each table's business domain (per-table), returning {table_name: domain}."""
    by_table: dict[str, list[str]] = {}
    for r in rows:
        by_table.setdefault(r.table, []).append(r.column)

    hash_of_table = {t: _table_content_hash(t, cols) for t, cols in by_table.items()}
    cached = _cache_get(conn, "enrichment_domain", list(hash_of_table.values()))

    result: dict[str, str] = {}
    for table, cols in by_table.items():
        h = hash_of_table[table]
        if h in cached:
            result[table] = cached[h]
            continue
        domain = _call(client, _DOMAIN_TASK, "overlay_domain_v1", "overlay_domain",
                       {"table": table, "columns": sorted(cols)}, "domain")
        _cache_put(conn, "enrichment_domain", h, domain)
        result[table] = domain
    return result
