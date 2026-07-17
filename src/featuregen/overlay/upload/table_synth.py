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
import logging

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.enrich_batch import BatchItem, run_batched
from featuregen.overlay.upload.enrich_llm import _MAX_COLUMN_PROFILES
from featuregen.overlay.upload.sample_parser import strip_sample_values
from featuregen.runtime.observability import counters

logger = logging.getLogger(__name__)


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
        if not isinstance(s, dict):
            return None, "not_object"   # "null"/"[]"/"\"x\"" parse fine but can't .get(...)
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
        # Availability is DECOUPLED from grain: a bad as-of (a column the table lacks, or a basis
        # outside the lag-free enum) drops ONLY the availability — it must NEVER discard an otherwise
        # VALID grain proposal. Coupling them silently lost a real grain to a single hallucinated
        # as-of column; the grain still proposes and the bad as-of is logged/counted, not returned as
        # a whole-item rejection. Both absent still abstains (empty_synthesis) below.
        availability = None
        if as_of_col is not None:
            if as_of_col in cols and as_of_basis in _VALID_BASIS:
                availability = {"column": as_of_col, "basis": as_of_basis}
            else:
                counters.incr("overlay.table_synth.availability.dropped_bad_as_of")
                logger.info("table_synth dropped a bad as-of for %r (col=%r basis=%r) — keeping grain",
                            ref, as_of_col, as_of_basis)
        if grain is None and availability is None:
            return None, "empty_synthesis"    # abstention / nothing proposed -> skipped-loud
        out = {"grain": grain, "availability_time": availability,
               "table_role": s.get("table_role"), "primary_entity": s.get("primary_entity"),
               "event_or_snapshot": s.get("event_or_snapshot")}
        return json.dumps(out, sort_keys=True), "valid"
    return accept


def make_summary_accept(columns_by_ref: dict[str, set[str]]):
    """A ref-aware accept for the PHASE-1 chunk-summary task (#1). `ref` is a chunk id; validate the
    serialized `summary` and FILTER its candidate columns to those actually in THAT chunk (a summary
    is advisory input to phase 2, not a governed fact — a stray hallucinated column drops silently, it
    must never lose the whole chunk's summary and thereby fail the table). Only unparseable / non-object
    raw is rejected; everything else normalizes to a bounded, egress-safe summary."""
    def accept(raw: str, ref: str) -> tuple[str | None, str]:
        cols = columns_by_ref.get(ref, set())
        try:
            s = json.loads(raw)
        except (ValueError, TypeError):
            return None, "unparseable"
        if not isinstance(s, dict):
            return None, "not_object"
        grain = [c for c in (s.get("grain_candidates") or [])
                 if isinstance(c, str) and c in cols][:32]
        temporal = [c for c in (s.get("temporal_candidates") or [])
                    if isinstance(c, str) and c in cols][:32]
        entity = [e for e in (s.get("entity_signals") or []) if isinstance(e, str)][:16]
        kind = s.get("event_or_snapshot")
        if kind not in ("event", "snapshot", None):
            kind = None
        out = {"grain_candidates": grain, "temporal_candidates": temporal,
               "entity_signals": entity, "event_or_snapshot": kind}
        return json.dumps(out, sort_keys=True), "valid"
    return accept


def synthesize_tables(conn, client, items: list[BatchItem], *, columns_by_table, actor
                      ) -> dict[str, dict]:
    """Run the governed batch synthesis; return {table: synthesis_dict} for VALID results only.
    Validation is done INSIDE run_batched via the ref-aware accept — this function does no
    post-filtering (an INVALID synthesis never reaches here).

    Wide tables (#1): an item whose ``column_profiles`` exceeds ``_MAX_COLUMN_PROFILES`` cannot egress
    as one giant item, so it is routed through the TWO-PHASE path (phase-1 per-chunk summaries -> a
    single phase-2 synthesis over the summaries + a complete roster). NARROW tables (``<=64`` profiles)
    keep today's single-call fast path byte-for-byte. A wide table that fails to summarize every chunk,
    or whose synthesis is invalid, simply never appears in the returned dict — the caller then reports
    the honest partial/failed outcome (no phantom "resolved").

    NOTE: the batch-mode config (``OVERLAY_ENRICH_TABLE_SYNTH_MODE`` / ``mode("table_synth")``) is
    intentionally NOT consulted here. Pass B is BATCH-ONLY: a ref_aware task has no single-call
    seam (run_batched skips the single fallback for ref_aware), so there is no "single" execution
    path a mode switch could select. Only the FEATURE switch (``OVERLAY_TABLE_SYNTH``,
    ``ingest.table_synth_enabled``) gates Pass B."""
    narrow = [it for it in items
              if len(it.metadata.get("column_profiles") or []) <= _MAX_COLUMN_PROFILES]
    wide = [it for it in items
            if len(it.metadata.get("column_profiles") or []) > _MAX_COLUMN_PROFILES]
    resolved: dict[str, dict] = {}
    if narrow:
        # Today's exact path: one synthesis batch over the full profiles (fast path, byte-for-byte).
        resolved.update(_run_synthesis(conn, client, narrow, columns_by_table=columns_by_table,
                                       actor=actor, instruction=_INSTRUCTION))
    if wide:
        resolved.update(_synthesize_wide_tables(conn, client, wide,
                                                columns_by_table=columns_by_table, actor=actor))
    return resolved


def _run_synthesis(conn, client, items: list[BatchItem], *, columns_by_table, actor, instruction
                   ) -> dict[str, dict]:
    """The governed phase-2 synthesis batch (shared by the narrow fast path and the wide path): SAME
    task/schema/accept/result-shape — only the item metadata (full profiles vs summaries+roster) and
    the instruction differ. Returns {table: synthesis_dict} for VALID results only."""
    accept = make_ref_accept(columns_by_table)
    resolved = run_batched(
        conn, client, short="table_synth", task="table_synth",
        prompt_id="overlay_table_synth_v1", schema_id="overlay_table_synth_batch",
        shared_metadata={}, items=items, out_key="synthesis",
        instruction=instruction, accept=accept, actor=actor,
        extract=lambda e: json.dumps(e.get("synthesis"), sort_keys=True), ref_aware=True,
    )
    return {table: json.loads(raw) for table, raw in resolved.items()}


def _chunk_profiles(profiles: list[dict]) -> list[list[dict]]:
    """Deterministic consecutive chunks of the table's profiles (stable column order preserved), each
    ``<=_MAX_COLUMN_PROFILES`` so every chunk item passes the per-item egress cap."""
    return [profiles[i:i + _MAX_COLUMN_PROFILES]
            for i in range(0, len(profiles), _MAX_COLUMN_PROFILES)]


def _synthesize_wide_tables(conn, client, wide_items: list[BatchItem], *, columns_by_table, actor
                            ) -> dict[str, dict]:
    """Two-phase synthesis for tables wider than the egress cap (#1).

    Phase 1: split each wide table into consecutive ``<=64``-profile chunks and SUMMARIZE each chunk
    (no fact output) — every chunk item is egress-safe. Phase 2: for each table whose chunks ALL
    summarized, run ONE synthesis over its chunk summaries + a compact complete roster (names/types).
    A table missing any chunk summary is dropped (never partially synthesized) so the caller reports it
    honestly as unresolved."""
    chunk_items: list[BatchItem] = []
    chunk_refs_by_table: dict[str, list[str]] = {}
    columns_by_ref: dict[str, set[str]] = {}
    roster_by_table: dict[str, list[str]] = {}
    for it in wide_items:
        table = it.ref
        profiles = it.metadata.get("column_profiles") or []
        # Complete roster: `name:type` only — small, egress-safe, and enough for phase-2 grounding.
        roster_by_table[table] = [f"{d.get('column', '')}:{d.get('type', '')}"[:200]
                                  for d in profiles]
        refs: list[str] = []
        for idx, chunk in enumerate(_chunk_profiles(profiles)):
            ref = f"{table}#chunk{idx}"
            refs.append(ref)
            columns_by_ref[ref] = {d.get("column") for d in chunk if d.get("column")}
            chunk_items.append(BatchItem(ref=ref,
                                         metadata={"table": table, "column_profiles": chunk}))
        chunk_refs_by_table[table] = refs

    summaries = run_batched(
        conn, client, short="table_synth", task="table_synth_summary",
        prompt_id="overlay_table_synth_summary_v1", schema_id="overlay_table_synth_summary_batch",
        shared_metadata={}, items=chunk_items, out_key="summary",
        instruction=_SUMMARY_INSTRUCTION, accept=make_summary_accept(columns_by_ref), actor=actor,
        extract=lambda e: json.dumps(e.get("summary"), sort_keys=True), ref_aware=True,
    )

    phase2_items: list[BatchItem] = []
    for table, refs in chunk_refs_by_table.items():
        if not all(r in summaries for r in refs):
            # An incomplete summary set for a wide table -> no synthesis (never a partial/guessed one).
            counters.incr("overlay.table_synth.wide.incomplete_summaries")
            logger.info("table_synth wide %r summarized %d/%d chunks — no synthesis (honest miss)",
                        table, sum(r in summaries for r in refs), len(refs))
            continue
        chunk_summaries = [json.loads(summaries[r]) for r in refs]
        phase2_items.append(BatchItem(ref=table, metadata={
            "table": table, "chunk_summaries": chunk_summaries,
            "column_roster": roster_by_table[table]}))
    if not phase2_items:
        return {}
    return _run_synthesis(conn, client, phase2_items, columns_by_table=columns_by_table,
                          actor=actor, instruction=_SYNTH_WIDE_INSTRUCTION)


_INSTRUCTION = (
    "For each table, identify: the grain (the minimal set of columns whose combination uniquely "
    "identifies one row) — RETURN AN EMPTY grain_columns list if you cannot determine it, do not "
    "guess; the as-of/availability column and its basis (posted_at|ingested_at); "
    "the primary business entity; the table role; and whether it is an event or snapshot table. "
    "Only name columns that appear in the provided column list."
)

_SUMMARY_INSTRUCTION = (
    "For each column CHUNK, SUMMARIZE the columns to support a LATER whole-table synthesis — DO NOT "
    "propose a table grain here. Identify: candidate grain/identifier columns (columns that could help "
    "uniquely identify a row), temporal/as-of columns (event or load timestamps), entity signals "
    "(the business entities these columns describe), and whether the chunk looks like event or "
    "snapshot data. Only name columns that appear in the provided column list."
)

_SYNTH_WIDE_INSTRUCTION = (
    "This is a WIDE table presented as per-chunk SUMMARIES (each with candidate grain/id columns, "
    "temporal/as-of columns, entity signals, and an event/snapshot hint) PLUS the table's COMPLETE "
    "column roster (each entry `name:type`). Using the summaries and the roster, identify for the WHOLE "
    "table: the grain (the minimal set of columns whose combination uniquely identifies one row) — "
    "RETURN AN EMPTY grain_columns list if you cannot determine it, do not guess; the as-of/availability "
    "column and its basis (posted_at|ingested_at); the primary business entity; the table role; and "
    "whether it is an event or snapshot table. Only name columns that appear in the column roster."
)


# The folded fact states in which a Pass B proposal is SKIPPED QUIETLY — a stronger/active claim
# already governs this key: VERIFIED (a declared/structural or human-confirmed fact — Pass B must
# never contest it), or a still-pending proposal/partial (DRAFT / PARTIALLY_CONFIRMED — already in
# the queue; DRAFT is the folded literal for a pending proposal, state.py). All OTHER states
# (REJECTED / REVERIFY / STALE / empty) are handed to propose_fact, which adjudicates: it duplicate-
# denies an identical pending fingerprint, sticky-denies a re-proposed rejected fingerprint, and
# ALLOWS a genuinely new value after a terminal state. We never skip on raw stream existence (that
# would suppress every future proposal once a stream existed, even after rejection/expiry).
_SKIP_QUIET_STATES = frozenset({"VERIFIED", "DRAFT", "PARTIALLY_CONFIRMED"})

# The advisory table-level fields Pass B records as LLM field evidence (never governed facts).
_ADVISORY_TABLE_FIELDS = ("table_role", "primary_entity", "event_or_snapshot")


def _active_skip_state(conn, ref, fact_type) -> str | None:
    from featuregen.overlay.identity import fact_key
    from featuregen.overlay.state import fold_overlay_state
    from featuregen.overlay.store import load_fact

    stream = load_fact(conn, fact_key(ref, fact_type))
    if not stream:
        return None
    status = fold_overlay_state(stream).status
    return status if status in _SKIP_QUIET_STATES else None


def _propose_table_facts(conn, source: str, syntheses: dict[str, dict], *, actor,
                         source_snapshot_id: str,
                         schema_by_table: dict[str, str] | None = None) -> None:
    """Route Pass B grain/availability candidates into governed PROPOSED-only facts and advisory
    table-field evidence. Fail-soft (never aborts the upload). Skips QUIETLY only when a stronger
    active claim governs the key (VERIFIED / a pending proposal); otherwise lets propose_fact
    adjudicate re-proposal after a terminal state, logging any denial as a conflict diagnostic.

    ``actor`` MUST be the service actor (``_ENRICH_ACTOR``) so a human confirmer later satisfies
    four-eyes. ``source_snapshot_id`` keys producer-scoped staleness for the advisory evidence (a
    NOT-NULL column).

    ``schema_by_table`` maps a NORMALIZED table name to the real (non-public) schema its glossary
    column decisions are keyed under. The advisory table-field evidence MUST be keyed under that SAME
    schema so ``readiness`` (schema-aware) sees ONE ``(schema, table)`` pair per physical table — a
    schema-forced-public advisory ref otherwise manufactures a phantom ``(public, table)`` twin that
    double-counts the grain/availability/join requirements and makes a bare TABLE subset ambiguous.
    Empty / absent (a non-glossary technical upload) falls back to ``public``, which is correct —
    technical columns are public and write no glossary column decisions. NOTE: the grain/availability
    FACT stays keyed under the always-public ``table_ref`` (below); only the advisory field evidence
    ref is schema-aligned."""
    # Imported lazily (mirrors _propose_governed_joins): propose_fact resolves the catalog adapter
    # at import-use time, and the pure assembler/accept tests must import this module without
    # pulling the command stack (or ingest, which imports table_synth lazily in the Pass B block).
    from featuregen.contracts.envelopes import Command
    from featuregen.overlay.catalog import current_catalog_adapter
    from featuregen.overlay.commands import propose_fact
    from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
    from featuregen.overlay.identity import proposal_fingerprint
    from featuregen.overlay.upload.enrich_llm import ENRICHMENT_RUN_ID
    from featuregen.overlay.upload.ingest import _write_producer_field
    from featuregen.overlay.upload.object_ref import normalize_ref
    from featuregen.overlay.upload.upload_catalog import table_ref

    # defense-in-depth: ingest_upload self-ensures the adapter (ensure_upload_catalog_adapter at
    # entry), so this is unreachable in the normal flow — it fail-softs a direct/future caller.
    try:
        current_catalog_adapter()
    except RuntimeError:
        counters.incr("overlay.table_synth.skipped_no_adapter")
        logger.warning("OVERLAY_TABLE_SYNTH on but no catalog adapter registered — skipping.")
        return

    for table, syn in syntheses.items():
        ref = table_ref(source, table)
        for fact_type in ("grain", "availability_time"):
            value = syn.get(fact_type)
            if value is None:
                continue
            skip_state = _active_skip_state(conn, ref, fact_type)
            if skip_state is not None:
                # a stronger/active claim governs this key — Pass B does not contest it
                counters.incr(f"overlay.table_synth.{fact_type}.skipped_{skip_state.lower()}")
                continue
            try:
                # Command needs ALL 6 fields (envelopes.py); mirror _propose_governed_joins exactly.
                result = propose_fact(conn, Command(
                    "propose_fact", "overlay_fact", None,
                    {"ref": ref, "fact_type": fact_type, "proposed_value": value},
                    actor, proposal_fingerprint(value)))
                if result.accepted:
                    counters.incr(f"overlay.table_synth.{fact_type}.proposed")
                else:
                    # propose_fact adjudicated a deny (duplicate fingerprint, sticky-rejected, or a
                    # non-terminal race) — a conflict DIAGNOSTIC, not a silent drop.
                    counters.incr(f"overlay.table_synth.{fact_type}.denied")
                    logger.info("table_synth %s proposal denied for %s.%s: %s",
                                fact_type, source, table, result.denied_reason)
            except Exception:   # noqa: BLE001 — advisory: a proposal error never fails an upload
                counters.incr(f"overlay.table_synth.{fact_type}.error")
                logger.exception("table_synth %s proposal errored for %s.%s",
                                 fact_type, source, table)
        # Advisory table fields -> field evidence via the SAME helper Pass A uses
        # (_write_producer_field: producer-scoped staleness + snapshot reuse + the required
        # source_snapshot_id/input_hash args a bare record_field_evidence would miss).
        # RECOMMENDATION-ceilinged in Task 8. A write error here is contained by the caller's
        # Pass B savepoint+except (ingest wiring).
        schema = (schema_by_table or {}).get(table.strip().lower())
        logical_ref = normalize_ref(source, schema, table)
        for field_name in _ADVISORY_TABLE_FIELDS:
            v = syn.get(field_name)
            if v:
                _write_producer_field(
                    conn, logical_ref=logical_ref, field_name=field_name, value=v,
                    producer=EvidenceProducer.LLM, strength=AssertionStrength.PROPOSED,
                    producer_ref=ENRICHMENT_RUN_ID, snapshot_id=source_snapshot_id, material=v)
