"""Pass B — per-table input assembler (spec §15.2).

`assemble_table_items` consumes the Task-3 metadata views (`column_view.build_table_views` — one
`TableMetadataView` per table, each column a `ColumnMetadataView` with the sidecar already
bound-and-fenced) and emits one `BatchItem` per table whose metadata carries each column's
egress-safe descriptor plus the table-level `table_definition` when the view has one. The
descriptor keeps `operational_type` and `declared_type` as TWO fields — the declared type is a
HINT from the glossary, never a confirmation of the physical type. Pass B later proposes
grain/availability as human-gated typed-fact proposals and table_role/primary_entity as advisory
field evidence; the assembler does no propose logic.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from featuregen.overlay.upload import enrich_config, table_vocab
from featuregen.overlay.upload.enrich_batch import BatchItem, run_batched
from featuregen.overlay.upload.enrich_llm import _MAX_COLUMN_PROFILES
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.taxonomy.dimensions import known_entities
from featuregen.runtime.observability import counters

if TYPE_CHECKING:
    from datetime import datetime

    from featuregen.overlay.upload.column_view import ColumnMetadataView, TableMetadataView

logger = logging.getLogger(__name__)


def _descriptor(view: ColumnMetadataView) -> dict:
    """Egress-safe per-column descriptor from the Task-3 view: `{column, operational_type,
    declared_type, concept?, business_definition?, term_type?, domain?, process_path?,
    semantic_type?}`. NEVER a conflated `type` key — `operational_type` is the row's physical type
    (stays `unknown` under a glossary upload until confirmed) and `declared_type` is the
    glossary-DECLARED SQL type (a hint; blank for a technical upload). Both always present so the
    synthesizer sees the distinction even when one is blank.

    M4 still holds by construction: the view sources `business_definition` ONLY from the curated
    sidecar meaning or the Pass-A draft (never the uploader's raw `r.definition` cell), bounded to
    the 600 egress window; the field-aware egress seam (`_redact_free_text_meta`) re-sanitizes it
    (sample-clause strip + PII) at dispatch. Facets are bounded structural tokens (200 cap)."""
    desc: dict = {"column": view.column,
                  "operational_type": (view.operational_type or "")[:200],
                  "declared_type": (view.declared_type or "")[:200]}
    if view.concept:
        desc["concept"] = view.concept
    if view.business_definition:
        desc["business_definition"] = view.business_definition
    for key, val in (("term_type", view.term_type), ("domain", view.domain),
                     ("process_path", view.process_path),
                     ("semantic_type", view.semantic_type)):
        if val:
            desc[key] = val[:200]
    return desc


def assemble_table_items(views: dict[str, TableMetadataView]) -> list[BatchItem]:
    """One BatchItem per table view; metadata is `{table, column_profiles, table_definition?}` —
    `table_definition` ONLY when the view carries one (the [F8] schema fence already ran in
    `build_table_views`, so a mismatched table term never reaches this seam). Each profile is the
    dual-type descriptor above and the assembled metadata is admissible under the metadata-only
    egress contract (`enrich_llm._item_egress_ok`). Sidecar attachment/withholding, Pass-A joins,
    and normalization all happened in the view builder — the assembler only projects."""
    items: list[BatchItem] = []
    for table, view in views.items():
        metadata: dict = {"table": table,
                          "column_profiles": [_descriptor(c) for c in view.columns]}
        if view.table_definition:
            metadata["table_definition"] = view.table_definition
        items.append(BatchItem(ref=table, metadata=metadata))
    return items


_VALID_BASIS = {"posted_at", "ingested_at"}  # lag-free bases only (event_time_plus_lag needs lag_hours)

# The five per-table disposition fields — the per-run record set is TOTAL over them: every table
# that reaches `make_ref_accept` gets exactly one record per field, and every assembled table that
# never resolves gets the same five as `not_evaluated` ([F12]). ONE constant so the accept, the
# totalizer, and their tests can never drift.
DISPOSITION_FIELDS = ("grain", "availability_time", "table_role", "primary_entity",
                      "event_or_snapshot")


def add_not_evaluated(dispositions: list[dict], table: str) -> None:
    """[F12] totality for a table that NEVER REACHED per-field validation (egress-excluded,
    provider-failed, timed out, whole-rejected raw, or simply missing from the batch result —
    ``run_batched`` returns resolved refs only): append the full five-field record set with status
    ``not_evaluated`` so the per-run disposition shape stays uniform/TOTAL. ``not_evaluated`` is
    DISTINCT from ``abstained`` — abstained means the model was asked and offered nothing for an
    EVALUATED table; not_evaluated means validation never saw the table at all."""
    for field in DISPOSITION_FIELDS:
        dispositions.append({"table": table, "field": field, "status": "not_evaluated",
                             "reason": None, "prior_value_staled": False})


def make_ref_accept(columns_by_table: dict[str, set[str]], *,
                    dispositions: list[dict] | None = None):
    """A ref-aware accept for `validate_batch_results(..., ref_aware=True)`. `ref` is the table name;
    validate the serialized `synthesis` against THAT table's real columns and map a valid result onto
    the FACT_VALUE_SCHEMAS shapes (grain `{columns, is_unique}` / availability `{column, basis}`).

    Slice 2: every field is validated INDEPENDENTLY — an invalid field drops THAT FIELD ONLY (the
    table still resolves; only unparseable / non-object raw whole-rejects). The `table_role` vocab
    is enforced HERE, not as a schema enum ([F1] — `reg.validate` would fail the WHOLE synthesis on
    one off-vocab role, destroying this per-field salvage). Every resolved synthesis appends a
    disposition record to `dispositions` for ALL FIVE fields ([F12] — TOTAL):
    ``{"table", "field", "status", "reason", "prior_value_staled": False}`` with
    ``status in {accepted, abstained, dropped_invalid}``; an absent advisory field == abstained.
    ``prior_value_staled`` is set later by the staling seam, never here."""
    disp = dispositions if dispositions is not None else []

    def _put(ref: str, field: str, status: str, reason: str | None = None) -> None:
        disp.append({"table": ref, "field": field, "status": status, "reason": reason,
                     "prior_value_staled": False})

    def accept(raw: str, ref: str) -> tuple[str | None, str]:
        cols = columns_by_table.get(ref, set())
        back = {c.lower(): c for c in cols}   # normalized -> CANONICAL table spelling
        try:
            s = json.loads(raw)
        except (ValueError, TypeError):
            return None, "unparseable"
        if not isinstance(s, dict):
            return None, "not_object"   # "null"/"[]"/"\"x\"" parse fine but can't .get(...)

        # ── grain: a real list[str], case-folded for duplicates/membership, mapped BACK to the
        # table's canonical spelling. Any violation drops the GRAIN ONLY — the other fields keep
        # their own verdicts. `is_unique=True` is the CLAIM being proposed (these columns are
        # asserted to identify a row), NOT empirical proof — there is no profiling in Phase 2; human
        # confirmation IS the uniqueness attestation. An empty/absent grain_columns == the model
        # ABSTAINING (MF-3), never a reject.
        rg = s.get("grain_columns")
        grain = None
        if rg is None or rg == []:
            _put(ref, "grain", "abstained")
        elif not isinstance(rg, list) or not all(isinstance(c, str) for c in rg):
            _put(ref, "grain", "dropped_invalid", "grain_invalid_shape")
        else:
            fold = [c.strip().lower() for c in rg]
            if len(fold) != len(set(fold)):
                _put(ref, "grain", "dropped_invalid", "grain_duplicate")
            elif len(rg) > table_vocab.MAX_GRAIN_COLS:
                _put(ref, "grain", "dropped_invalid", "grain_over_bound")
            elif any(f not in back for f in fold):
                _put(ref, "grain", "dropped_invalid", "grain_col_not_in_table")
            else:
                grain = {"columns": [back[f] for f in fold], "is_unique": True}
                _put(ref, "grain", "accepted")

        # ── availability: DECOUPLED from grain — a bad as-of (a column the table lacks, or a basis
        # outside the lag-free enum) drops ONLY the availability, never an otherwise-valid grain.
        # [F13]: the column is case-folded and emitted in the CANONICAL table spelling (same map as
        # grain); the basis is strip/lower-matched into `_VALID_BASIS`.
        availability = None
        aoc, aob = s.get("as_of_column"), s.get("as_of_basis")
        if aoc is None:
            _put(ref, "availability_time", "abstained")
        else:
            col = back.get(aoc.strip().lower()) if isinstance(aoc, str) else None
            basis = aob.strip().lower() if isinstance(aob, str) else None
            if col is not None and basis in _VALID_BASIS:
                availability = {"column": col, "basis": basis}
                _put(ref, "availability_time", "accepted")
            else:
                _put(ref, "availability_time", "dropped_invalid",
                     "basis_not_allowed" if col is not None else "as_of_col_not_in_table")
                counters.incr("overlay.table_synth.availability.dropped_bad_as_of")
                logger.info("table_synth dropped a bad as-of for %r (col=%r basis=%r) — keeping grain",
                            ref, aoc, aob)

        # ── advisory fields: strip/lower-normalized, vocab/registry-gated, each with its own
        # disposition. [F13]: a NON-EMPTY event_or_snapshot that normalizes to None is OFF-VOCAB
        # (dropped_invalid), not an abstention.
        reos = s.get("event_or_snapshot")
        eos = table_vocab.normalize_event_or_snapshot(reos)
        if eos is not None:
            _put(ref, "event_or_snapshot", "accepted")
        elif isinstance(reos, str) and reos != "":
            _put(ref, "event_or_snapshot", "dropped_invalid", "event_or_snapshot_off_vocab")
        else:
            _put(ref, "event_or_snapshot", "abstained")

        rr = s.get("table_role")
        role = table_vocab.normalize_table_role(rr, event_or_snapshot=eos)
        if rr and role is None:
            _put(ref, "table_role", "dropped_invalid", "role_off_vocab")
        else:
            _put(ref, "table_role", "accepted" if role else "abstained")

        ent = s.get("primary_entity")
        ent = ent.strip().lower() if isinstance(ent, str) else None
        if ent and ent not in known_entities():
            _put(ref, "primary_entity", "dropped_invalid", "entity_not_registered")
            ent = None
        else:
            _put(ref, "primary_entity", "accepted" if ent else "abstained")

        # A parseable synthesis with neither grain nor availability is a VALID ABSTENTION (some tables
        # genuinely have no single grain / as-of) — retain the surviving advisory fields and propose
        # zero grain/availability facts. Only unparseable / non-object raw (above) is a failure.
        out = {"grain": grain, "availability_time": availability,
               "table_role": role, "primary_entity": ent, "event_or_snapshot": eos}
        return json.dumps(out, sort_keys=True), ("valid" if (grain or availability) else "abstained")
    return accept


def make_summary_accept(columns_by_ref: dict[str, set[str]]):
    """A ref-aware accept for the PHASE-1 chunk-summary task (#1). `ref` is a chunk id; validate the
    serialized `summary` and FILTER its candidate columns to those actually in THAT chunk (a summary
    is advisory input to phase 2, not a governed fact — a stray hallucinated column drops silently, it
    must never lose the whole chunk's summary and thereby fail the table). Only unparseable / non-object
    raw is rejected; everything else normalizes to a bounded, egress-safe summary."""
    def accept(raw: str, ref: str) -> tuple[str | None, str]:
        cols = columns_by_ref.get(ref, set())
        back = {c.lower(): c for c in cols}   # normalized -> CANONICAL chunk spelling (Slice 2)

        def _known(names) -> list[str]:
            # Same normalization as make_ref_accept: case-fold, match against the chunk's real
            # columns, emit the CANONICAL spelling; a stray/off-chunk candidate drops silently
            # (a summary is advisory phase-1 input, never a governed fact). Deduped post-fold.
            out: list[str] = []
            for c in names or []:
                if isinstance(c, str):
                    hit = back.get(c.strip().lower())
                    if hit is not None and hit not in out:
                        out.append(hit)
            return out[:32]

        try:
            s = json.loads(raw)
        except (ValueError, TypeError):
            return None, "unparseable"
        if not isinstance(s, dict):
            return None, "not_object"
        grain = _known(s.get("grain_candidates"))
        temporal = _known(s.get("temporal_candidates"))
        entity = [e for e in (s.get("entity_signals") or []) if isinstance(e, str)][:16]
        kind = table_vocab.normalize_event_or_snapshot(s.get("event_or_snapshot"))
        out = {"grain_candidates": grain, "temporal_candidates": temporal,
               "entity_signals": entity, "event_or_snapshot": kind}
        return json.dumps(out, sort_keys=True), "valid"
    return accept


def _table_dispatch_subject(catalog_source: str, schema: str | None, table: str,
                            columns: list[str]) -> dict:
    """One ``llm_dispatch_subject`` mapping (C5-T5) for a Pass B table item: the table's
    schema-aware evidence identity (the SAME ``schema_by_table`` schema its fact proposals key
    under) with ``field_names`` = the column names this physical request carries. Attribution
    strings only — never row data."""
    logical_ref = normalize_ref(catalog_source, schema, table)
    return {"catalog_source": catalog_source, "object_ref": logical_ref.split("::", 1)[1],
            "logical_ref": logical_ref, "field_names": sorted(columns)}


def _dispatch_subjects_for(items: list[BatchItem], *, catalog_source: str | None,
                           schema_by_table: dict[str, str] | None) -> dict[str, dict] | None:
    """The ``{ref: subject}`` mapping for a synthesis batch (C5-T5): one TABLE subject per item,
    ``field_names`` drawn from whichever roster the item carries (full ``column_profiles`` on the
    narrow/summary path, the compact ``column_roster`` on the wide phase-2 path). ``None`` when the
    caller supplied no ``catalog_source`` (a direct/test call) — no subjects to attribute."""
    if catalog_source is None:
        return None
    sbt = schema_by_table or {}
    out: dict[str, dict] = {}
    for it in items:
        descs = it.metadata.get("column_profiles") or it.metadata.get("column_roster") or []
        cols = [d.get("column") for d in descs if isinstance(d, dict) and d.get("column")]
        table = it.metadata.get("table") or it.ref
        out[it.ref] = _table_dispatch_subject(catalog_source, sbt.get(table), table, cols)
    return out


def synthesize_tables(conn, client, items: list[BatchItem], *, columns_by_table, actor,
                      dispositions: list[dict] | None = None,
                      ingestion_run_id: str | None = None,
                      catalog_source: str | None = None,
                      schema_by_table: dict[str, str] | None = None,
                      stats: dict | None = None) -> dict[str, dict]:
    """Run the governed batch synthesis; return {table: synthesis_dict} for VALID results only.
    Validation is done INSIDE run_batched via the ref-aware accept — this function does no
    post-filtering (an INVALID synthesis never reaches here).

    ``dispositions`` (Slice-2 Task 3) is the caller's per-run collector, threaded into BOTH
    execution paths' ref-aware accepts (`make_ref_accept`), which append the five per-field
    records for every table they validate. Mutated IN PLACE (the caller keeps the same list it
    later hands to `_propose_table_facts` for the [F9] staling flips and totalizes via
    `add_not_evaluated`); ``None`` (a direct caller) collects nothing.

    Wide tables (#1): an item whose ``column_profiles`` exceeds ``_MAX_COLUMN_PROFILES`` cannot egress
    as one giant item, so it is routed through the TWO-PHASE path (phase-1 per-chunk summaries -> a
    single phase-2 synthesis over the summaries + a complete roster). NARROW tables (``<=64`` profiles)
    keep today's single-call fast path byte-for-byte. A wide table synthesizes over whatever chunk
    summaries LANDED (the roster is complete regardless); only a table with ZERO chunk summaries,
    or whose synthesis is invalid, simply never appears in the returned dict — the caller then reports
    the honest partial/failed outcome (no phantom "resolved").

    NOTE: the batch-mode config (``OVERLAY_ENRICH_TABLE_SYNTH_MODE`` / ``mode("table_synth")``) is
    intentionally NOT consulted here. Pass B is BATCH-ONLY: a ref_aware task has no single-call
    seam (run_batched skips the single fallback for ref_aware), so there is no "single" execution
    path a mode switch could select. Only the FEATURE switch (``OVERLAY_TABLE_SYNTH``,
    ``ingest.table_synth_enabled``) gates Pass B.

    ``stats`` (optional out-param — return shape unchanged): accumulates ``not_attempted``, the count
    of TABLES the budget/deadline cutoff skipped WITHOUT dispatching their synthesis (narrow path +
    wide phase-2), so the caller labels a truncated Pass B ``truncated`` rather than ``items_failed``.

    C5-T5 — ``ingestion_run_id`` + ``catalog_source`` (+ ``schema_by_table``, the Pass-B fact-key
    schema map): with a run id, every Pass B dispatch (chunk summaries AND syntheses) is pre-audited
    and attributed to the run + its TABLE subjects under stage ``pass_b``. ``ingestion_run_id=None``
    (every direct/test caller) is byte-for-byte today's behavior."""
    narrow = [it for it in items
              if len(it.metadata.get("column_profiles") or []) <= _MAX_COLUMN_PROFILES]
    wide = [it for it in items
            if len(it.metadata.get("column_profiles") or []) > _MAX_COLUMN_PROFILES]
    resolved: dict[str, dict] = {}
    if narrow:
        # Today's exact path: one synthesis batch over the full profiles (fast path, byte-for-byte).
        resolved.update(_run_synthesis(conn, client, narrow, columns_by_table=columns_by_table,
                                       actor=actor, instruction=_INSTRUCTION,
                                       dispositions=dispositions,
                                       ingestion_run_id=ingestion_run_id,
                                       catalog_source=catalog_source,
                                       schema_by_table=schema_by_table, stats=stats))
    if wide:
        resolved.update(_synthesize_wide_tables(conn, client, wide,
                                                columns_by_table=columns_by_table, actor=actor,
                                                dispositions=dispositions,
                                                ingestion_run_id=ingestion_run_id,
                                                catalog_source=catalog_source,
                                                schema_by_table=schema_by_table, stats=stats))
    return resolved


def _run_synthesis(conn, client, items: list[BatchItem], *, columns_by_table, actor, instruction,
                   dispositions: list[dict] | None = None,
                   ingestion_run_id: str | None = None,
                   catalog_source: str | None = None,
                   schema_by_table: dict[str, str] | None = None,
                   stats: dict | None = None) -> dict[str, dict]:
    """The governed phase-2 synthesis batch (shared by the narrow fast path and the wide path): SAME
    task/schema/accept/result-shape — only the item metadata (full profiles vs summaries+roster) and
    the instruction differ. Returns {table: synthesis_dict} for VALID results only.

    Ships the Pass B Slice-2 contract via the Task-1 version seam: **prompt v3** (the code-side
    `table_role` vocab is enumerated in the instruction) over the **unchanged canonical v2
    schema**. [F1]: `table_role` is deliberately NOT a schema enum — `reg.validate` rejects the
    WHOLE synthesis on one schema violation, so a strict role enum would lose a valid grain to one
    off-vocab role; the vocab is enforced per-field in `make_ref_accept` instead.

    ``dispositions`` threads the caller's per-run collector into the ref-aware accept, which
    appends the five per-field records for every table it validates (retries never duplicate: a
    resolved ref is excluded from every retry/split chunk, and only a parseable-dict raw — which
    always resolves — appends records)."""
    accept = make_ref_accept(columns_by_table, dispositions=dispositions)
    batch_report: dict = {}   # honest-labeling: run_batched reports budget/deadline not_attempted
    resolved = run_batched(
        conn, client, short="table_synth", task="table_synth",
        prompt_id="overlay_table_synth_v3", schema_id="overlay_table_synth_batch",
        prompt_version=3, schema_version=2,
        shared_metadata={}, items=items, out_key="synthesis",
        instruction=instruction, accept=accept, actor=actor,
        extract=lambda e: json.dumps(e.get("synthesis"), sort_keys=True), ref_aware=True,
        deadline_s=enrich_config.stage_deadline_s(),   # MF-4 — bound the source-lock hold
        report=batch_report,
        ingestion_run_id=ingestion_run_id, dispatch_stage="pass_b",
        dispatch_subjects=_dispatch_subjects_for(items, catalog_source=catalog_source,
                                                 schema_by_table=schema_by_table),
    )
    # ACCUMULATE (narrow + wide-phase-2 both funnel here): these table-granular refs are exactly
    # Pass B's expected unit, so a truncated synthesis surfaces as the stage's `not_attempted`.
    if stats is not None and batch_report.get("not_attempted"):
        stats["not_attempted"] = stats.get("not_attempted", 0) + batch_report["not_attempted"]
    return {table: json.loads(raw) for table, raw in resolved.items()}


def _chunk_profiles(profiles: list[dict]) -> list[list[dict]]:
    """Deterministic consecutive chunks of the table's profiles (stable column order preserved), each
    ``<=_MAX_COLUMN_PROFILES`` so every chunk item passes the per-item egress cap."""
    return [profiles[i:i + _MAX_COLUMN_PROFILES]
            for i in range(0, len(profiles), _MAX_COLUMN_PROFILES)]


def _roster_entry(desc: dict) -> dict:
    """One STRUCTURED wide-roster entry `{column, operational_type, declared_type}` from a
    per-column descriptor (the wide path holds assembled items, so the descriptor — which carries
    exactly these keys from the view — is the projection source). Structured, never the old
    `name:type` flat string: a column name may itself contain `:`/`/`, which the flat form
    conflated irrecoverably. Values are bounded to the default per-value egress cap."""
    return {"column": (desc.get("column") or "")[:200],
            "operational_type": (desc.get("operational_type") or "")[:200],
            "declared_type": (desc.get("declared_type") or "")[:200]}


def _synthesize_wide_tables(conn, client, wide_items: list[BatchItem], *, columns_by_table, actor,
                            dispositions: list[dict] | None = None,
                            ingestion_run_id: str | None = None,
                            catalog_source: str | None = None,
                            schema_by_table: dict[str, str] | None = None,
                            stats: dict | None = None) -> dict[str, dict]:
    """Two-phase synthesis for tables wider than the egress cap (#1). ``dispositions`` threads to
    the PHASE-2 synthesis only (whose refs are the table names); the phase-1 chunk summaries are
    advisory input keyed by chunk ref and record no per-field dispositions.

    Phase 1: split each wide table into consecutive ``<=64``-profile chunks and SUMMARIZE each chunk
    (no fact output) — every chunk item is egress-safe. Phase 2: for each table with AT LEAST ONE
    chunk summary, run ONE synthesis over whatever chunk summaries LANDED + a compact complete roster
    of STRUCTURED ``{column, operational_type, declared_type}`` entries + the table's
    ``table_definition`` (when the assembled item carried one). The roster is built from ALL profiles,
    so a budget-skipped chunk summary (advisory phase-2 input, not a governed fact) never starves the
    synthesis — the ``overlay.table_synth.wide.partial_summaries`` counter records the shortfall. Only
    a table with ZERO landed summaries is dropped (``wide.zero_summaries``) so the caller reports it
    honestly as unresolved."""
    chunk_items: list[BatchItem] = []
    chunk_refs_by_table: dict[str, list[str]] = {}
    columns_by_ref: dict[str, set[str]] = {}
    roster_by_table: dict[str, list[dict]] = {}
    table_def_by_table: dict[str, str] = {}
    for it in wide_items:
        table = it.ref
        profiles = it.metadata.get("column_profiles") or []
        # Complete roster: STRUCTURED {column, operational_type, declared_type} entries — small,
        # egress-safe, and enough for phase-2 grounding without conflating a `:`-containing name.
        roster_by_table[table] = [_roster_entry(d) for d in profiles]
        # The table-level definition rides the ASSEMBLED item's metadata; the rebuilt phase-2 item
        # must carry it forward explicitly or the wide path silently drops it.
        table_def = it.metadata.get("table_definition")
        if table_def:
            table_def_by_table[table] = table_def
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
        # Slice-2 stamp: prompt v3 / canonical schema v2, matching the synthesis call so one Pass B
        # run never egresses under two contract generations. The summary TEXT is unchanged at v3
        # (it emits no table_role, so there is no vocab to enumerate) — the bump identifies the
        # Slice-2 contract, mirroring the Slice-1 v2-aliases-v1 schema precedent.
        prompt_id="overlay_table_synth_summary_v3", schema_id="overlay_table_synth_summary_batch",
        prompt_version=3, schema_version=2,
        shared_metadata={}, items=chunk_items, out_key="summary",
        instruction=_SUMMARY_INSTRUCTION, accept=make_summary_accept(columns_by_ref), actor=actor,
        extract=lambda e: json.dumps(e.get("summary"), sort_keys=True), ref_aware=True,
        deadline_s=enrich_config.stage_deadline_s(),   # MF-4 — bound the source-lock hold
        # C5-T5: each chunk item attributes to its TABLE (the real catalog object), field_names =
        # the chunk's columns — a wide table's summary dispatches stay subject-attributed.
        ingestion_run_id=ingestion_run_id, dispatch_stage="pass_b",
        dispatch_subjects=_dispatch_subjects_for(chunk_items, catalog_source=catalog_source,
                                                 schema_by_table=schema_by_table),
    )

    phase2_items: list[BatchItem] = []
    for table, refs in chunk_refs_by_table.items():
        present = [r for r in refs if r in summaries]
        if not present:
            # ZERO chunks summarized for a wide table -> no synthesis (never a guessed one): the
            # phase-2 call would have no advisory grounding at all. Only this all-missing case drops
            # the table; the caller then reports it honestly as unresolved.
            counters.incr("overlay.table_synth.wide.zero_summaries")
            logger.info("table_synth wide %r summarized 0/%d chunks — no synthesis (honest miss)",
                        table, len(refs))
            continue
        if len(present) < len(refs):
            # SOME chunks summarized: proceed to phase 2 on the LANDED summaries. This is SAFE
            # because the phase-2 item carries the COMPLETE column_roster built from ALL profiles
            # (above), independent of the summaries — a chunk summary is explicitly advisory phase-2
            # input, not a governed fact. Dropping the whole (e.g. 126-col) table because one chunk's
            # summary call was budget-skipped would stamp all five dispositions not_evaluated and
            # fail Pass B, so we synthesize over whatever summaries arrived.
            counters.incr("overlay.table_synth.wide.partial_summaries")
            logger.info("table_synth wide %r summarized %d/%d chunks — synthesizing on the partial "
                        "summaries (roster is complete)", table, len(present), len(refs))
        chunk_summaries = [json.loads(summaries[r]) for r in present]
        metadata: dict = {"table": table, "chunk_summaries": chunk_summaries,
                          "column_roster": roster_by_table[table]}
        if table in table_def_by_table:
            metadata["table_definition"] = table_def_by_table[table]
        phase2_items.append(BatchItem(ref=table, metadata=metadata))
    if not phase2_items:
        return {}
    # `stats` threads to PHASE-2 only (table-granular refs = Pass B's `not_attempted` unit). Phase-1
    # summary truncation is chunk-granular; a wholly-unsummarized wide table already surfaces as
    # unresolved (`wide.zero_summaries`), so it is deliberately not recounted here.
    return _run_synthesis(conn, client, phase2_items, columns_by_table=columns_by_table,
                          actor=actor, instruction=_SYNTH_WIDE_INSTRUCTION,
                          dispositions=dispositions, ingestion_run_id=ingestion_run_id,
                          catalog_source=catalog_source, schema_by_table=schema_by_table,
                          stats=stats)


_TYPE_FIELDS_NOTE = (
    "Each column profile carries TWO type fields: operational_type is the observed physical type "
    "(it stays 'unknown' until operationally confirmed — an empty or unknown value means the "
    "physical type is NOT established) and declared_type is the glossary-DECLARED SQL type, a HINT "
    "from documentation, not a confirmation of the physical type. Never treat declared_type as the "
    "operational type. When present, table_definition is the curated business definition of the "
    "whole table. "
)

# Prompt v3 ([F1]): the accepted table_role values are enumerated in the PROMPT (and enforced
# per-field in `make_ref_accept`) — never as an enum on the canonical response schema, which would
# whole-reject a synthesis over one off-vocab role.
_ROLE_VOCAB_NOTE = (
    "table_role MUST be one of: " + ", ".join(table_vocab.TABLE_ROLE_ENUM) +
    " (any other value is discarded); event_or_snapshot MUST be event or snapshot. "
)

_INSTRUCTION = (
    _TYPE_FIELDS_NOTE +
    "For each table, identify: the grain (the minimal set of columns whose combination uniquely "
    "identifies one row) — RETURN AN EMPTY grain_columns list if you cannot determine it, do not "
    "guess; the as-of/availability column and its basis (posted_at|ingested_at); "
    "the primary business entity; the table role; and whether it is an event or snapshot table. "
    + _ROLE_VOCAB_NOTE +
    "Only name columns that appear in the provided column list."
)

_SUMMARY_INSTRUCTION = (
    _TYPE_FIELDS_NOTE +
    "For each column CHUNK, SUMMARIZE the columns to support a LATER whole-table synthesis — DO NOT "
    "propose a table grain here. Identify: candidate grain/identifier columns (columns that could help "
    "uniquely identify a row), temporal/as-of columns (event or load timestamps), entity signals "
    "(the business entities these columns describe), and whether the chunk looks like event or "
    "snapshot data. Only name columns that appear in the provided column list."
)

_SYNTH_WIDE_INSTRUCTION = (
    _TYPE_FIELDS_NOTE +
    "This is a WIDE table presented as per-chunk SUMMARIES (each with candidate grain/id columns, "
    "temporal/as-of columns, entity signals, and an event/snapshot hint) PLUS the table's COMPLETE "
    "column roster (each entry an object {column, operational_type, declared_type} — the same two "
    "type fields described above). Using the summaries and the roster, identify for the WHOLE "
    "table: the grain (the minimal set of columns whose combination uniquely identifies one row) — "
    "RETURN AN EMPTY grain_columns list if you cannot determine it, do not guess; the as-of/availability "
    "column and its basis (posted_at|ingested_at); the primary business entity; the table role; and "
    "whether it is an event or snapshot table. " + _ROLE_VOCAB_NOTE +
    "Only name columns that appear in the column roster."
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


def _mark_staled(dispositions: list[dict] | None, table: str, field: str, *,
                 status_if_missing: str) -> None:
    """Set ``prior_value_staled=True`` on the ``{table, field}`` disposition record ([F9] — driven
    by the staled COUNT in BOTH directions: a present value superseding older LLM rows AND a
    dropped/absent field retiring them). Finds the record ``make_ref_accept`` appended for this run
    (searched newest-first); a caller that never threaded the collector through the accept gets an
    appended record with ``status_if_missing`` so the flag is never silently lost."""
    if dispositions is None:
        return
    for rec in reversed(dispositions):
        if rec.get("table") == table and rec.get("field") == field:
            rec["prior_value_staled"] = True
            return
    dispositions.append({"table": table, "field": field, "status": status_if_missing,
                         "reason": None, "prior_value_staled": True})


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
                         schema_by_table: dict[str, str] | None = None,
                         dispositions: list[dict] | None = None,
                         now: datetime | None = None,
                         source_uploader: str | None = None) -> None:
    """Route Pass B grain/availability candidates into governed PROPOSED-only facts and advisory
    table-field evidence. Fail-soft (never aborts the upload). Skips QUIETLY only when a stronger
    active claim governs the key (VERIFIED / a pending proposal); otherwise lets propose_fact
    adjudicate re-proposal after a terminal state, logging any denial as a conflict diagnostic.

    ``actor`` MUST be the service actor (``_ENRICH_ACTOR``) so a human confirmer later satisfies
    four-eyes. ``source_uploader`` is the uploading HUMAN principal's subject (or None): the
    grain/availability operands are shaped by the uploader's own file, so the proposal records that
    principal and ``confirm_fact`` bars them from confirming it single-handedly (program-audit F10
    — the same M-7 SOURCE-provenance rule as the semantic-binding surface). ``source_snapshot_id``
    keys producer-scoped staleness for the advisory evidence (a NOT-NULL column).

    ``schema_by_table`` maps a NORMALIZED table name to the real (non-public) schema its glossary
    column decisions are keyed under. The advisory table-field evidence MUST be keyed under that SAME
    schema so ``readiness`` (schema-aware) sees ONE ``(schema, table)`` pair per physical table — a
    schema-forced-public advisory ref otherwise manufactures a phantom ``(public, table)`` twin that
    double-counts the grain/availability/join requirements and makes a bare TABLE subset ambiguous.
    Empty / absent (a non-glossary technical upload) falls back to ``public``, which is correct —
    technical columns are public and write no glossary column decisions. NOTE: the grain/availability
    FACT stays keyed under the always-public ``table_ref`` (below); only the advisory field evidence
    ref is schema-aligned.

    ``dispositions`` is the SAME per-run collector list ``make_ref_accept`` appended to during
    validation: [F9] flips ``prior_value_staled=True`` on the matching ``{table, field}`` record
    whenever this pass ACTUALLY staled prior LLM rows — a present value superseding an older one
    AND a dropped/absent field retiring them (both driven by the staled COUNT, decoupled from the
    clear-gate below).

    STALE-VALUE LIFECYCLE (Slice-2 Task 2): an advisory field the new synthesis NO LONGER carries
    gets its prior LLM evidence producer-scope staled here; when NO active evidence remains for the
    field (no human/source confirmation keeping it alive), ``stale_and_clear_field`` records a
    STALED decision (supersedes read from the durable decision log, [F2]) and CLEARS the flat
    ``graph_node`` display column. This runs BEFORE the caller's ``resolve_and_project`` in the
    SAME transaction (the ingest Pass B savepoint), which then SKIPS the evidence-less field —
    the clear is never re-projected away.

    ``now`` is the ingest round's threaded decision timestamp, passed through to
    ``stale_and_clear_field`` so the STALED decision carries the SAME ``now`` as the round's
    sibling RESOLVED decisions (the monotonic-ordering contract of ``read_field_decisions``).
    ``None`` (a direct caller) keeps the prior wall-clock behavior."""
    # Imported lazily (mirrors _propose_governed_joins): propose_fact resolves the catalog adapter
    # at import-use time, and the pure assembler/accept tests must import this module without
    # pulling the command stack (or ingest, which imports table_synth lazily in the Pass B block).
    from featuregen.contracts.envelopes import Command
    from featuregen.overlay.catalog import current_catalog_adapter
    from featuregen.overlay.commands import propose_fact
    from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
    from featuregen.overlay.field_evidence import stale_source_evidence
    from featuregen.overlay.identity import proposal_fingerprint
    from featuregen.overlay.upload.enrich_llm import ENRICHMENT_RUN_ID
    from featuregen.overlay.upload.field_resolution import (
        _active_field_names,
        stale_and_clear_field,
    )
    from featuregen.overlay.upload.ingest import _STALE_ALL, _write_producer_field
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
                # `source_uploader` (when a human uploaded) is the F10 four-eyes provenance.
                args: dict[str, object] = {"ref": ref, "fact_type": fact_type,
                                           "proposed_value": value}
                if source_uploader:
                    args["source_uploader"] = source_uploader
                result = propose_fact(conn, Command(
                    "propose_fact", "overlay_fact", None, args,
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
                staled = _write_producer_field(
                    conn, logical_ref=logical_ref, field_name=field_name, value=v,
                    producer=EvidenceProducer.LLM, strength=AssertionStrength.PROPOSED,
                    producer_ref=ENRICHMENT_RUN_ID, snapshot_id=source_snapshot_id, material=v)
                if staled > 0:
                    # [F9] present-replaces-older: the accepted value superseded prior LLM rows.
                    _mark_staled(dispositions, table, field_name, status_if_missing="accepted")
                continue
            # Dropped/absent advisory field (the stale-value lifecycle): retire ALL of the LLM's
            # prior ACTIVE rows for this field — _STALE_ALL can never equal a real input_hash, so
            # every LLM row stales; other producers' rows (human/source) are NEVER touched.
            n = stale_source_evidence(
                conn, logical_ref=logical_ref, field_name=field_name,
                producer=EvidenceProducer.LLM, keep_input_hash=_STALE_ALL)
            if n > 0:
                # [F9] DECOUPLED from the clear-gate: the LLM rows WERE staled even when a human
                # confirmation below keeps the field alive and blocks the clear.
                _mark_staled(dispositions, table, field_name, status_if_missing="abstained")
                if field_name not in _active_field_names(conn, logical_ref):
                    # NO producer's evidence remains: resolve_and_project would SKIP this field
                    # (it iterates active field names), leaving the prior display visible. Record
                    # the STALED decision + clear the display NOW — same transaction, BEFORE the
                    # caller's resolve_and_project, so the clear is never re-projected away.
                    stale_and_clear_field(
                        conn, source=source, logical_ref=logical_ref, field_name=field_name,
                        now=now)
