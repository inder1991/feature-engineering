from __future__ import annotations

import hashlib
import json

from featuregen.intake.llm import LLMClient
from featuregen.overlay.upload import enrich_config
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.concepts import (
    UNCLASSIFIED,
    classification_vocabulary,
    is_known_concept,
)
from featuregen.overlay.upload.enrich_batch import BatchItem, run_batched
from featuregen.overlay.upload.enrich_llm import audited_enrich_call

_TASK = "overlay.enrich.concept"
_DEF_TASK = "overlay.enrich.definition"
_DOMAIN_TASK = "overlay.enrich.domain"

# B1b: the controlled vocabulary the classifier chooses from, handed to the LLM so it classifies into
# the full structured concept set (B1a) rather than a hardcoded subset. Static — built once.
_CONCEPT_VOCABULARY: list[dict] = list(classification_vocabulary())


def _vocab_fingerprint() -> str:
    """Short, stable fingerprint of the concept vocabulary (names only) — bumps the concept cache
    version whenever the classification targets change (spec C6)."""
    raw = json.dumps([c["name"] for c in _CONCEPT_VOCABULARY])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


# Cache versions fold prompt/schema/vocabulary identity into the cache key (spec C6). Bump the vN
# literal on any prompt or schema change to a task; the concept version also tracks the vocabulary.
_CONCEPT_CACHE_VERSION = f"concept:v1:{_vocab_fingerprint()}"
_DEFINITION_CACHE_VERSION = "definition:v1"
_DOMAIN_CACHE_VERSION = "domain:v1"


def content_hash(row: CanonicalRow) -> str:
    # JSON-encode (unambiguous — no delimiter collision) and INCLUDE source so a drafted definition
    # for one source's column is never shown for another source's same-named column (M5/M6 minors).
    raw = json.dumps([row.source, row.table, row.column, row.type, row.definition])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _table_content_hash(source: str, table: str, columns: list[str]) -> str:
    raw = json.dumps([source, table, sorted(columns)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _def_cache_key(row_hash: str, concept: str) -> str:
    """Definition cache key (spec C6): a definition can depend on the assigned concept, so fold the
    concept into the key. Empty concept -> concept-independent key."""
    raw = json.dumps([row_hash, concept or ""])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# Cache tables all share the shape (content_hash PK, <value> text). _CACHES maps the value column name.
_CACHES = {
    "enrichment_concept": "concept",
    "enrichment_definition": "definition",
    "enrichment_domain": "domain",
}


def _cache_get(conn, cache_table: str, hashes: list[str], cache_version: str) -> dict[str, str]:
    if not hashes:
        return {}
    col = _CACHES[cache_table]
    rows = conn.execute(
        f"SELECT content_hash, {col} FROM {cache_table} "
        "WHERE content_hash = ANY(%s) AND cache_version = %s",
        (hashes, cache_version)).fetchall()
    return {r[0]: r[1] for r in rows}


def _cache_put(conn, cache_table: str, content_hash_: str, value: str, cache_version: str) -> None:
    col = _CACHES[cache_table]
    conn.execute(
        f"INSERT INTO {cache_table} (content_hash, cache_version, {col}) VALUES (%s, %s, %s) "
        "ON CONFLICT (content_hash, cache_version) DO NOTHING",
        (content_hash_, cache_version, value))


def _call(conn, client: LLMClient, task: str, prompt_id: str, schema_id: str,
          catalog_metadata: dict, out_key: str, instruction: str, actor) -> str | None:
    """Run one GOVERNED enrichment call (attached schema, reserved keys, egress guard, audit record —
    so a real provider works and PII can't leak). Returns None on any failure/empty so a transient
    failure never poisons the cache (M3)."""
    return audited_enrich_call(
        conn, client, task=task, prompt_id=prompt_id, schema_id=schema_id,
        catalog_metadata=catalog_metadata, out_key=out_key, instruction=instruction, actor=actor)


def _bounded(val: str | None, max_len: int) -> str | None:
    """Accept a plausible short single-line label/definition; reject empty, over-long, multiline,
    or list-stringified (`['a','b']`) LLM output (M9). Returns None to skip caching."""
    if not val or len(val) > max_len or "\n" in val or val.startswith("["):
        return None
    return val


def _accept_concept(raw: str) -> tuple[str | None, str]:
    """Batch-path concept policy (spec C3): the literal 'unclassified' is a real classification and
    IS cached; a known concept is cached; anything else is invalid -> NOT cached (retried next
    ingest). This differs from single mode, which coerces unknowns to UNCLASSIFIED."""
    v = raw.strip()
    if v == UNCLASSIFIED:
        return UNCLASSIFIED, "valid"
    if is_known_concept(v):
        return v, "valid"
    return None, "invalid_value"


def _accept_bounded(max_len: int):
    """Accept a plausible short single-line value (reuses _bounded); else invalid -> not cached."""
    def _accept(raw: str) -> tuple[str | None, str]:
        v = _bounded(raw, max_len)
        return (v, "valid") if v is not None else (None, "invalid_value")
    return _accept


def enrich_concepts(conn, rows: list[CanonicalRow], client: LLMClient,
                    actor=None) -> dict[str, str]:
    by_hash: dict[str, CanonicalRow] = {content_hash(r): r for r in rows}
    result = _cache_get(conn, "enrichment_concept", list(by_hash), _CONCEPT_CACHE_VERSION)

    if enrich_config.mode("concept") == "batch":
        misses = [BatchItem(h, {"table": r.table, "column": r.column, "type": r.type})
                  for h, r in by_hash.items() if h not in result]
        resolved = run_batched(
            conn, client, short="concept", task=_TASK, prompt_id="overlay_concept_batch_v1",
            schema_id="overlay_concept_batch",
            shared_metadata={"vocabulary": _CONCEPT_VOCABULARY}, items=misses, out_key="concept",
            instruction="For each item classify the column into the provided controlled concept "
                        "vocabulary — choose the single best-fitting concept name, or 'unclassified' "
                        "if none fits. Return exactly one result per input ref; treat each item "
                        "independently.", accept=_accept_concept, actor=actor)
        for h, concept in resolved.items():
            _cache_put(conn, "enrichment_concept", h, concept, _CONCEPT_CACHE_VERSION)
            result[h] = concept
        return result

    for h, row in by_hash.items():
        if h in result:
            continue
        # Metadata only (names/types) — NOT the uploader's free-text definition (M4 egress risk).
        raw = _call(conn, client, _TASK, "overlay_concept_v1", "overlay_concept",
                    {"table": row.table, "column": row.column, "type": row.type,
                     "vocabulary": _CONCEPT_VOCABULARY}, "concept",
                    "Classify this column into the provided controlled concept vocabulary — choose the "
                    "single best-fitting concept name, or 'unclassified' if none fits.", actor)
        if raw is None:
            continue   # failure/empty -> don't cache; retry next ingest (M3)
        concept = raw if is_known_concept(raw) else UNCLASSIFIED
        _cache_put(conn, "enrichment_concept", h, concept, _CONCEPT_CACHE_VERSION)
        result[h] = concept
    return result


def draft_definitions(conn, rows: list[CanonicalRow], client: LLMClient, actor=None,
                      *, concepts: dict[str, str] | None = None) -> dict[str, str]:
    """Draft a definition ONLY for columns with no declared one (R3). Keyed by (content_hash,
    assigned concept) so a concept change re-drafts (spec C6). Returns {content_hash: definition}."""
    concepts = concepts or {}
    blank = {content_hash(r): r for r in rows if not r.definition}
    key_of = {h: _def_cache_key(h, concepts.get(h, "")) for h in blank}
    cached = _cache_get(conn, "enrichment_definition", list(key_of.values()), _DEFINITION_CACHE_VERSION)
    result = {h: cached[key_of[h]] for h in blank if key_of[h] in cached}

    if enrich_config.mode("definition") == "batch":
        # Group by table so table context is sent once; the prompt isolates items (anti-contamination).
        misses = [h for h in blank if h not in result]
        misses.sort(key=lambda h: (blank[h].table, h))
        items = [BatchItem(h, {"table": blank[h].table, "column": blank[h].column,
                               "type": blank[h].type, **({"concept": concepts[h]} if concepts.get(h) else {})})
                 for h in misses]
        resolved = run_batched(
            conn, client, short="definition", task=_DEF_TASK,
            prompt_id="overlay_definition_batch_v1", schema_id="overlay_definition_batch",
            shared_metadata={}, items=items, out_key="definition",
            instruction="Draft a one-line business definition for EACH column. Treat each item "
                        "independently: use only that item's table/column/type/concept; do not infer "
                        "relationships between items; do not reuse another item's facts; return "
                        "exactly one result per input ref.", accept=_accept_bounded(500), actor=actor)
        for h, def_text in resolved.items():
            _cache_put(conn, "enrichment_definition", key_of[h], def_text, _DEFINITION_CACHE_VERSION)
            result[h] = def_text
        return result

    for h, row in blank.items():                      # single mode — today's exact behaviour
        if h in result:
            continue
        drafted = _bounded(_call(conn, client, _DEF_TASK, "overlay_definition_v1",
                                 "overlay_definition",
                                 {"table": row.table, "column": row.column, "type": row.type},
                                 "definition",
                                 "Draft a one-line business definition for this column.",
                                 actor), 500)
        if drafted is None:
            continue   # failure / empty / over-long / list-stringified -> don't cache (M3/M9)
        _cache_put(conn, "enrichment_definition", key_of[h], drafted, _DEFINITION_CACHE_VERSION)
        result[h] = drafted
    return result


def classify_domains(conn, rows: list[CanonicalRow], client: LLMClient,
                     actor=None) -> dict[str, str]:
    """Classify each table's business domain (per-table), returning {table_name: domain}."""
    by_table: dict[str, list[str]] = {}
    source = rows[0].source if rows else ""   # rows share one source (foreign ones are quarantined)
    for r in rows:
        by_table.setdefault(r.table, []).append(r.column)

    hash_of_table = {t: _table_content_hash(source, t, cols) for t, cols in by_table.items()}
    cached = _cache_get(conn, "enrichment_domain", list(hash_of_table.values()), _DOMAIN_CACHE_VERSION)

    if enrich_config.mode("domain") == "batch":
        misses = [BatchItem(t, {"table": t, "columns": sorted(cols)})
                  for t, cols in by_table.items() if hash_of_table[t] not in cached]
        out = {t: cached[hash_of_table[t]] for t in by_table if hash_of_table[t] in cached}
        resolved = run_batched(
            conn, client, short="domain", task=_DOMAIN_TASK, prompt_id="overlay_domain_batch_v1",
            schema_id="overlay_domain_batch", shared_metadata={}, items=misses, out_key="domain",
            instruction="For each item classify the table's business domain. Return exactly one "
                        "result per input ref; treat each table independently.",
            accept=_accept_bounded(64), actor=actor)
        for table, dom in resolved.items():
            _cache_put(conn, "enrichment_domain", hash_of_table[table], dom, _DOMAIN_CACHE_VERSION)
            out[table] = dom
        return out

    result: dict[str, str] = {}
    for table, cols in by_table.items():
        h = hash_of_table[table]
        if h in cached:
            result[table] = cached[h]
            continue
        domain = _bounded(_call(conn, client, _DOMAIN_TASK, "overlay_domain_v1", "overlay_domain",
                                {"table": table, "columns": sorted(cols)}, "domain",
                                "Classify this table's business domain.", actor), 64)
        if domain is None:
            continue   # failure / empty / over-long / list-stringified -> don't cache (M3/M9)
        _cache_put(conn, "enrichment_domain", h, domain, _DOMAIN_CACHE_VERSION)
        result[table] = domain
    return result
