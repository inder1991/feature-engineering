"""Pass B — per-table input assembler (spec §15.2).

`assemble_table_items` joins each table's `CanonicalRow`s with the Pass A enrichment
(`concepts{content_hash: concept}` + drafted `definitions{content_hash: definition}`) by
`content_hash` and emits one `BatchItem` per table whose metadata carries each column's
egress-safe descriptor. Pass B (Task 6/7) later proposes grain/availability as human-gated typed-fact
proposals and table_role/primary_entity as advisory field evidence; this task is the input assembler
only — no driver, no propose logic (YAGNI).
"""
from __future__ import annotations

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.enrich_batch import BatchItem
from featuregen.overlay.upload.sample_parser import strip_sample_values


def _descriptor(r: CanonicalRow, concept: str | None, definition: str | None) -> dict:
    desc: dict = {"column": r.column, "type": r.type or ""}
    if concept:
        desc["concept"] = concept
    # CRITICAL (M4 egress rule): source business_definition ONLY from the CURATED `definition` (the
    # glossary sidecar meaning / Pass A draft) — NEVER from `r.definition`, the uploader's raw
    # free-text cell. enrich.py::_concept_metadata forbids egressing a technical row's r.definition;
    # we mirror that exactly. Even the curated text is sample-value-stripped as defence-in-depth and
    # bounded to 200 chars (the per-value cap the Task-3 egress filter enforces).
    if definition:
        cleaned = strip_sample_values(definition)
        if cleaned:
            desc["business_definition"] = cleaned[:200]
    return desc


def assemble_table_items(rows: list[CanonicalRow], *, concepts: dict[str, str] | None,
                         definitions: dict[str, str] | None) -> list[BatchItem]:
    """One BatchItem per table; metadata carries each column's enriched, egress-safe descriptor.

    Each descriptor is `{column, type, concept?, business_definition?}` (only non-empty keys) and the
    assembled `BatchItem.metadata` is admissible under the Task-3 metadata-only egress filter
    (`enrich_llm._item_egress_ok`).
    """
    # Pass A stages are savepointed and may fail, leaving concepts/definitions None. Degrade to empty
    # enrichment rather than AttributeError on None.get(...).
    concepts = concepts or {}
    definitions = definitions or {}
    by_table: dict[str, list[CanonicalRow]] = {}
    for r in rows:
        by_table.setdefault(r.table, []).append(r)
    items: list[BatchItem] = []
    for table, trows in by_table.items():
        profiles = [
            _descriptor(r, concepts.get(content_hash(r)), definitions.get(content_hash(r)))
            for r in trows
        ]
        items.append(BatchItem(ref=table, metadata={"table": table, "column_profiles": profiles}))
    return items
