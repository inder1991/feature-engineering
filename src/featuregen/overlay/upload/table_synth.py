"""Pass B — per-table input assembler (spec §15.2).

`assemble_table_items` joins each table's `CanonicalRow`s with the Pass A enrichment
(`concepts{content_hash: concept}` + drafted `definitions{content_hash: definition}`) by
`content_hash` and emits one `BatchItem` per table whose metadata carries each column's
egress-safe descriptor. Pass B (Task 6/7) later proposes grain/availability as human-gated typed-fact
proposals and table_role/primary_entity as advisory field evidence; this task is the input assembler
only — no driver, no propose logic (YAGNI).
"""
from __future__ import annotations

import json

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.enrich_batch import BatchItem, run_batched
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


_VALID_BASIS = {"posted_at", "ingested_at"}  # lag-free bases only (event_time_plus_lag needs lag_hours)


def make_ref_accept(columns_by_table: dict[str, set[str]]):
    """A ref-aware accept for `validate_batch_results(..., ref_aware=True)`. `ref` is the table name;
    validate the serialized `synthesis` against THAT table's real columns and map a valid result onto
    the FACT_VALUE_SCHEMAS shapes (grain `{columns, is_unique}` / availability `{column, basis}`)."""
    def accept(raw: str, ref: str) -> tuple[str | None, str]:
        cols = columns_by_table.get(ref, set())
        try:
            s = json.loads(raw)
        except (ValueError, TypeError):
            return None, "unparseable"
        grain_cols = [c for c in (s.get("grain_columns") or []) if isinstance(c, str)]
        if any(c not in cols for c in grain_cols):
            return None, "grain_col_not_in_table"
        as_of_col = s.get("as_of_column")
        as_of_basis = s.get("as_of_basis")
        # `is_unique=True` is the CLAIM being proposed (these columns are asserted to identify a row),
        # NOT empirical proof — there is no profiling in Phase 2. Human confirmation IS the uniqueness
        # attestation; the proposal's LLM origin (proposed_by=service actor) is what a reviewer sees.
        # The fact schema {columns,is_unique} forbids a caveat field, so origin is surfaced via the
        # worklist, not the value. An empty grain_columns == the model ABSTAINING (skip, not error).
        grain = {"columns": grain_cols, "is_unique": True} if grain_cols else None
        availability = None
        if as_of_col is not None:
            if as_of_col not in cols:
                return None, "as_of_col_not_in_table"
            if as_of_basis not in _VALID_BASIS:
                return None, "as_of_basis_invalid"
            availability = {"column": as_of_col, "basis": as_of_basis}
        if grain is None and availability is None:
            return None, "empty_synthesis"    # abstention / nothing proposed -> skipped-loud
        out = {"grain": grain, "availability_time": availability,
               "table_role": s.get("table_role"), "primary_entity": s.get("primary_entity"),
               "event_or_snapshot": s.get("event_or_snapshot")}
        return json.dumps(out, sort_keys=True), "valid"
    return accept


def synthesize_tables(conn, client, items: list[BatchItem], *, columns_by_table, actor
                      ) -> dict[str, dict]:
    """Run the governed batch synthesis; return {table: synthesis_dict} for VALID results only.
    Validation is done INSIDE run_batched via the ref-aware accept — this function does no
    post-filtering (an INVALID synthesis never reaches here)."""
    accept = make_ref_accept(columns_by_table)
    resolved = run_batched(
        conn, client, short="table_synth", task="table_synth",
        prompt_id="overlay_table_synth_v1", schema_id="overlay_table_synth_batch",
        shared_metadata={}, items=items, out_key="synthesis",
        instruction=_INSTRUCTION, accept=accept, actor=actor,
        extract=lambda e: json.dumps(e.get("synthesis"), sort_keys=True), ref_aware=True,
    )
    return {table: json.loads(raw) for table, raw in resolved.items()}


_INSTRUCTION = (
    "For each table, identify: the grain (the minimal set of columns whose combination uniquely "
    "identifies one row) — RETURN AN EMPTY grain_columns list if you cannot determine it, do not "
    "guess; the as-of/availability column and its basis (posted_at|ingested_at); "
    "the primary business entity; the table role; and whether it is an event or snapshot table. "
    "Only name columns that appear in the provided column list."
)
