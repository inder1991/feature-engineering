from __future__ import annotations

import hashlib

from featuregen.intake.llm import LLMClient, LLMRequest
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.concepts import UNCLASSIFIED, is_known_concept

_TASK = "overlay.enrich.concept"


def content_hash(row: CanonicalRow) -> str:
    raw = f"{row.table}|{row.column}|{row.type}|{row.definition}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cached(conn, hashes: list[str]) -> dict[str, str]:
    if not hashes:
        return {}
    rows = conn.execute(
        "SELECT content_hash, concept FROM enrichment_concept WHERE content_hash = ANY(%s)",
        (hashes,)).fetchall()
    return {r[0]: r[1] for r in rows}


def _classify(client: LLMClient, row: CanonicalRow) -> str:
    req = LLMRequest(
        task=_TASK,
        prompt_id="overlay_concept_v1",
        prompt_version=1,
        inputs={"table": row.table, "column": row.column, "type": row.type,
                "definition": row.definition},   # schema metadata only — no data values
        output_schema_id="overlay_concept",
        output_schema_version=1,
        generation_settings={"provider": "fake", "model": "test"},
    )
    concept = str(client.call(req).output.get("concept", "")).strip()
    return concept if is_known_concept(concept) else UNCLASSIFIED


def enrich_concepts(conn, rows: list[CanonicalRow], client: LLMClient) -> dict[str, str]:
    by_hash: dict[str, CanonicalRow] = {content_hash(r): r for r in rows}
    result = _cached(conn, list(by_hash))
    for h, row in by_hash.items():
        if h in result:
            continue
        concept = _classify(client, row)
        conn.execute(
            "INSERT INTO enrichment_concept (content_hash, concept) VALUES (%s, %s) "
            "ON CONFLICT (content_hash) DO NOTHING",
            (h, concept))
        result[h] = concept
    return result
