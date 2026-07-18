from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from featuregen.aggregates.ids import mint_id
from featuregen.overlay import facts
from featuregen.overlay.catalog_changes import detect_catalog_changes
from featuregen.overlay.conflict_review import conflict_fingerprint, open_or_reopen_conflict
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_evidence import (
    field_input_hash,
    read_active_field_evidence,
    record_field_evidence,
    stale_source_evidence,
)
from featuregen.overlay.identity import fact_key, proposal_fingerprint
from featuregen.overlay.object_identity import ObjectBinding, may_attach
from featuregen.overlay.projection import OverlayProjection
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import append_overlay_event, load_fact
from featuregen.overlay.upload.brake import large_change_brake, resolution_brake
from featuregen.overlay.upload.canonical import (
    CanonicalRow,
    RowError,
    ValidationResult,
    validate_rows,
)
from featuregen.overlay.upload.enrich import (
    classify_domains,
    content_hash,
    draft_definitions,
    enrich_concepts,
    suppressed_definition_hashes,
)
from featuregen.overlay.upload.enrich_llm import consume_audit_degradations
from featuregen.overlay.upload.field_resolution import FIELD_POLICY_VERSION, resolve_and_project
from featuregen.overlay.upload.field_revalidation import flag_pending_revalidation
from featuregen.overlay.upload.glossary_reader import GlossaryRecord, GlossaryUpload, join_path
from featuregen.overlay.upload.graph import (
    _column_ref,
    _table_ref,
    add_column_row,
    build_graph,
    declared_type_by_ref,
    governed_join_proposal,
    governed_joins_enabled,
    parse_join_ref,
    rebuild_search_doc,
    schema_by_ref,
)
from featuregen.overlay.upload.ingestion_run import record_run_facts, record_run_objects
from featuregen.overlay.upload.join_drift import detect_governed_join_divergences
from featuregen.overlay.upload.object_ref import _norm, normalize_ref, parse_ref
from featuregen.overlay.upload.passc.projection import (
    list_approved_join_refs,
    project_confirmed_joins,
)
from featuregen.overlay.upload.readiness import ReadinessScopeType, compute_readiness
from featuregen.overlay.upload.review_queue import persist_quarantine
from featuregen.overlay.upload.sample_parser import ParsedProfile, reconcile_profile
from featuregen.overlay.upload.sanitize import redact_text
from featuregen.overlay.upload.source_profile import (
    FTR_GLOSSARY_PROFILE,
    SourceCapabilityProfile,
    strength_for,
)
from featuregen.overlay.upload.stage_report import (
    StageRecorder,
    record_skipped_downstream,
    record_stage,
)
from featuregen.overlay.upload.table_fact_projection import project_table_facts
from featuregen.overlay.upload.taxonomy_evidence import derive_concept_evidence
from featuregen.overlay.upload.upload_catalog import (
    UploadCatalog,
    ensure_upload_catalog_adapter,
    table_ref,
)
from featuregen.overlay.upload.upload_identity import MetadataConflict, classify_upload
from featuregen.projections.runner import projection_lag, run_projection
from featuregen.runtime.observability import counters

logger = logging.getLogger(__name__)


def table_synth_enabled() -> bool:
    """Feature switch for Pass B / table synthesis (default OFF). Orthogonal to the batch MODE
    (OVERLAY_ENRICH_TABLE_SYNTH_MODE), which only selects batch-vs-single execution WHEN the feature
    is on. Feature-off means Pass B never runs; mode=single does NOT mean the feature is off. Task 7
    gates the ingest call on this; Task 2 owns the definition so it exists before any consumer."""
    return os.environ.get("OVERLAY_TABLE_SYNTH", "0") == "1"


def pass_c_enabled() -> bool:
    """Feature switch for Pass C — deterministic governed join candidates (Phase 3A), default OFF.
    ON also implies the governed `joins_to` seam (`graph.governed_joins_enabled` reads this env
    directly to avoid an import cycle), so a declared join's raw edge is written display_only AND
    routed to an approved_join proposal — never stranded display-only."""
    return os.environ.get("OVERLAY_PASS_C", "0") == "1"


# #21 — per-call ceiling on the events one _drain_projection call may process. The drain runs
# INSIDE the upload's request transaction (the source advisory lock held throughout; the global-seq
# lock once facts are asserted), and the projection backlog is GLOBAL — unbounded, upload latency
# and lock hold time would scale with UNRELATED backlog, not this upload's size. 5000 (10 full
# batches) comfortably covers a normal upload's OWN events (2 per (re)asserted table fact plus a
# handful of governed-seam proposals — hundreds, not thousands), so the common case still reaches
# head and drift runs; a larger unrelated backlog leaves projection_lag > 0 and every call site's
# EXISTING lag guard then defers drift / re-projection to a later caught-up ingest (the background
# worker drains too) — a spent budget degrades into an already-tested skip path, never a new one.
_DRAIN_MAX_EVENTS = 5000


def _drain_projection(conn) -> None:
    """Run the overlay projection until caught up — or the #21 budget (_DRAIN_MAX_EVENTS) is spent.
    A single run_projection caps at 500 events and an upload emits 2 per (re)asserted fact, so one
    pass on a large upload leaves the dependency index stale when detect_catalog_changes reads it
    (false stale / missed drop). Each pass advances the checkpoint, so this terminates (a partial
    batch = caught up or poison-halted; a spent budget = stop early, the caller's projection_lag
    guard takes over)."""
    drained = 0
    while drained < _DRAIN_MAX_EVENTS:
        batch = min(500, _DRAIN_MAX_EVENTS - drained)
        applied = run_projection(conn, OverlayProjection(), batch=batch)
        drained += applied
        if applied < batch:   # short batch: caught up (or poison-halted) — nothing more to drain
            return


@dataclass(frozen=True, slots=True)
class IngestResult:
    status: str            # "ingested" | "held" | "rejected"
    reason: str | None
    asserted: int
    # Catalog OBJECTS this upload dropped/renamed/type-changed (the drift diff) — NOT a count of
    # facts staled; one changed object can stale zero or many facts (#30, was misnamed `staled`).
    changed_objects: int
    quarantined: int
    flagged: str | None = None   # a soft-gate note (e.g. first upload — review recommended)
    # MF-5 truthful counts (additive, `=0` defaults so every POSITIONAL reject/held constructor
    # site still builds). `asserted`/`changed_objects`/`quarantined` above conflate "127 nodes
    # stored" with "126 columns" and say nothing about edges, join candidates, or Pass B; these
    # tell the honest story. `objects_stored == tables + columns`; `containment_edges == columns`
    # (one `contains` edge per column); `facts_asserted` mirrors `asserted` (the Pass A count);
    # `join_candidates` is Pass C's discovered count (0 when Pass C is off); Pass B splits into
    # `proposed` (a synthesis with a grain or an as-of) + `abstained` (neither).
    objects_stored: int = 0
    tables: int = 0
    columns: int = 0
    containment_edges: int = 0
    facts_asserted: int = 0
    join_candidates: int = 0
    passb_proposed: int = 0
    passb_abstained: int = 0


def _enrichment_outcome(result: dict | None, expected: int, *, internal_failures: int = 0
                        ) -> tuple[str, str | None, dict]:
    """``(state, reason_code, detail)`` for a per-item stage (#22) — the honest account. ``None``
    (the stage's advisory except fired) is ``failed``; a non-empty expectation resolving NOTHING is
    ``failed``; SOME items unresolved — or the stage caught per-item failures INTERNALLY (the
    concept-evidence writes, batch discards, Pass B) — is ``partial``: an outer success is NOT
    evidence that every item succeeded. Counts ride in ``detail`` (never row data).

    A Pass B ABSTENTION (a parseable synthesis with no grain AND no as-of) is RESOLVED, not a failure
    — some tables genuinely have no single grain/as-of. Its count rides in ``detail["abstained"]``
    (present only when non-zero, like ``unresolved``). Only dict-valued stages (Pass B) can abstain;
    string-valued stages (concept/definition/domain) never match, so their detail is unchanged."""
    if result is None:
        return "failed", "exception", {"expected": expected}
    detail: dict = {"resolved": len(result), "expected": expected}
    unresolved = max(expected - len(result), 0)
    if unresolved:
        detail["unresolved"] = unresolved
    abstained = sum(1 for syn in result.values()
                    if isinstance(syn, dict)
                    and syn.get("grain") is None and syn.get("availability_time") is None)
    if abstained:
        detail["abstained"] = abstained
    if internal_failures:
        detail["internal_failures"] = internal_failures
    if expected and not result:
        return "failed", "no_items_resolved", detail
    if unresolved or internal_failures:
        return "partial", "items_failed", detail
    return "succeeded", None, detail


def _with_audit_degradations(detail: dict) -> dict:
    """Attach the count of durable llm_call audit writes that DEGRADED to the request connection
    during the LLM stage that just ran (#13 gap D) — the stage detail then carries the honest
    ``audit_degraded`` count instead of the degradation living only in a log line. Consuming at
    every stage boundary keeps the attribution per stage; a zero count leaves ``detail``
    untouched (byte-for-byte for the healthy path)."""
    degraded = consume_audit_degradations()
    if degraded:
        detail = {**detail, "audit_degraded": degraded}
    return detail


def _table_facts(rows: list[CanonicalRow]):
    """Yield (table, fact_type, value) for grain + availability_time facts."""
    by_table: dict[str, list[CanonicalRow]] = {}
    for r in rows:
        by_table.setdefault(r.table, []).append(r)
    for table, trows in by_table.items():
        grain_cols = [r.column for r in trows if r.is_grain]
        if grain_cols:
            yield table, "grain", {"columns": grain_cols, "is_unique": True}
        # At most ONE as_of row can reach here (#17): validate_rows quarantines ALL of a table's
        # as_of rows when it declares 2+, so this next() is never an order-dependent pick — the
        # ambiguity was surfaced to the reviewer, not silently resolved to whichever row came first.
        as_of_row = next((r for r in trows if r.as_of), None)
        if as_of_row:
            # Use the declared basis when valid; default to posted_at (M8 — no longer hard-coded).
            basis = as_of_row.as_of_basis if as_of_row.as_of_basis in (
                "posted_at", "ingested_at") else "posted_at"
            yield table, "availability_time", {"column": as_of_row.column, "basis": basis}


def _assert_fact(conn, source: str, table: str, fact_type: str, value: dict, *, actor,
                 origin_type: str = "upload") -> str | None:
    """Assert a fact, or RE-assert it when the upload changed its value or it is not currently
    VERIFIED. Skipping only-on-existence (the original bug) served a stale value forever (B1) and
    left a staled fact stuck unservable after the file was fixed (M1). We diff on the value: skip
    only when the stream is already VERIFIED with the identical value.

    Returns the assertion OUTCOME (run-provenance, design #3): ``None`` — skipped, the stream is
    already VERIFIED with the identical value; ``"asserted"`` — a first assertion, or a same-value
    re-assert of a non-VERIFIED stream (M1's staled-then-fixed fact: the CONTENT didn't change);
    ``"changed"`` — the value genuinely differs from the stream's last known value (the folded
    current value, or the prior_value a STALED/EXPIRED fold retired it to). Truthy exactly when
    the original bool was True, so `if _assert_fact(...)` call sites keep their behavior.

    #10 honest authority: the auto-confirm records `authority_basis=source_declared` + the ingest
    `origin_type` ("upload" | "connector" | "resolution") + the actor's REAL role_claims. It never
    fabricates a confirmer — an uploader (whatever their role) is not a data owner vouching for the
    fact; the fact is authoritative because the SOURCE declared it. Operationally identical: the
    fact folds/projects VERIFIED exactly as before."""
    fk = fact_key(table_ref(source, table), fact_type)
    stream = load_fact(conn, fk)
    outcome = "asserted"
    if stream:
        state = fold_overlay_state(stream)
        if state.status == "VERIFIED" and state.value == value:
            return None    # genuinely unchanged -> skip (cheap re-upload)
        prior = state.value if state.value is not None else state.prior_value
        if prior is not None and prior != value:
            outcome = "changed"
    # New fact, a changed value, or a non-VERIFIED (STALE/REVERIFY/REJECTED) stream -> (re)assert.
    base = stream[-1].stream_version if stream else 0
    draft = append_overlay_event(conn, fact_key=fk, type=facts.OVERLAY_FACT_PROPOSED,
        actor=actor, expected_version=base, payload={
            "catalog_object_ref": {"catalog_source": source, "object_kind": "table",
                                   "schema": "public", "table": table},
            "object_ref": f"public.{table}", "fact_type": fact_type,
            "proposed_value": value, "proposal_fingerprint": proposal_fingerprint(value),
            "proposed_by": actor.subject})
    append_overlay_event(conn, fact_key=fk, type=facts.OVERLAY_FACT_CONFIRMED,
        actor=actor, expected_version=base + 1, payload={
            "value": value, "authority_basis": facts.AUTHORITY_SOURCE_DECLARED,
            "origin_type": origin_type, "role_claims": list(actor.role_claims),
            "expires_at": None, "confirms_event_id": draft.event_id})
    return outcome


def _propose_governed_joins(conn, rows: list[CanonicalRow], *, actor) -> None:
    """Route each declared `joins_to` into the governed approved_join path via `propose_fact`, behind
    OVERLAY_GOVERNED_JOINS=1 (the caller gates on `governed_joins_enabled()`).

    ADVISORY / fail-soft (spec §12.1): this NEVER aborts the upload. A malformed `joins_to` is
    skipped-loud with its parse diagnostic; a `propose_fact` failure is logged and counted.

    ADAPTER-GATED (Phase-1 dependency): `propose_fact` resolves `current_catalog_adapter()`, which the
    UPLOAD request path does not yet register (only the worker/deployment does). When no adapter is
    wired we skip-loud rather than crash — the display-only edge marking (graph.py) still happens, so
    turning the flag on is safe today; the actual proposal dispatch activates once the upload-context
    adapter lands. The flag is default-OFF, so production behaviour is unchanged."""
    # Imported lazily: propose_fact -> proposal_commands resolves the catalog adapter at import-use
    # time, and the pure builder/parser tests must import graph.py without pulling the command stack.
    from featuregen.contracts.envelopes import Command
    from featuregen.overlay.catalog import current_catalog_adapter
    from featuregen.overlay.commands import propose_fact

    try:
        current_catalog_adapter()
    except RuntimeError:
        counters.incr("overlay.governed_joins.skipped_no_adapter")
        logger.warning("OVERLAY_GOVERNED_JOINS is on but no catalog adapter is registered in the "
                       "upload flow — skipping approved_join proposals (Phase-1: wire the "
                       "upload-context adapter). Display-only edges are still marked.")
        return

    for r in rows:
        if not r.joins_to:
            continue
        ref = governed_join_proposal(r)
        if ref is None:
            counters.incr("overlay.governed_joins.skipped_malformed")
            logger.warning("skipping governed join for %s.%s: %s", r.table, r.column,
                           parse_join_ref(r.joins_to).diagnostic)
            continue
        value = {
            "from_ref": asdict(ref.from_ref),
            "to_ref": asdict(ref.to_ref),
            "column_pairs": [{"from_col": p.from_col, "to_col": p.to_col} for p in ref.column_pairs],
            "cardinality": ref.cardinality,
        }
        try:
            # Per-proposal savepoint (audit I-2): a DB-class fault inside propose_fact aborts the
            # transaction it runs in; the except below swallows the Python exception, so without a
            # ROLLBACK TO here the REQUEST tx would stay aborted and the next unguarded statement
            # in ingest would raise InFailedSqlTransaction, rolling back the Pass A facts.
            with conn.transaction():
                result = propose_fact(conn, Command(
                    "propose_fact", "overlay_fact", None,
                    {"ref": ref, "fact_type": "approved_join", "proposed_value": value},
                    actor, proposal_fingerprint(value)))
        except Exception:  # noqa: BLE001 — advisory: a proposal failure must never fail an upload
            counters.incr("overlay.governed_joins.propose_error")
            logger.warning("advisory governed-join proposal raised for %s.%s -> %s",
                           r.table, r.column, r.joins_to, exc_info=True)
            continue
        if not result.accepted:
            # A deny (e.g. a duplicate of an already-pending/verified join) is expected on re-upload —
            # advisory, not an error. Counted so the seam's activity is observable.
            counters.incr("overlay.governed_joins.propose_denied")
            logger.info("governed-join proposal for %s.%s not accepted: %s", r.table, r.column,
                        result.denied_reason)


# ── Pass C ingest wiring (Phase 3A Task 10, spec §7/§12): deterministic join candidates from
# upload metadata alone — NO LLM. Behind OVERLAY_PASS_C (default OFF); the caller savepoints. ──

def _taxonomy_leaf(path: str) -> str:
    """The LAST segment of a taxonomy path ('Customer Management/Customer Reference' ->
    'Customer Reference'). Comparisons downstream are case-insensitive; extraction is verbatim."""
    segments = [s.strip() for s in re.split(r"[/>]", path or "") if s.strip()]
    return segments[-1] if segments else ""


def _pass_c_columns(conn, catalog_source: str, rows: list[CanonicalRow], *,
                    concepts: dict[str, str] | None, glossary: GlossaryUpload | None) -> list:
    """Assemble one `ColMeta` per canonical row for Pass C blocking/scoring.

    Sourcing (spec §7): `object_ref` is the PUBLIC graph-node ref (`public.{table}.{column}` —
    build_graph flattens the graph to public scope, and `entity_of` reads graph_node by this key).
    `column_entity` is THE namespace gate: the graph entity first (`entity_of` — build_graph wrote
    the declared entity and re-applied human-CONFIRMED entity_suggestions moments ago), else the
    declared row entity, else "" (empty falls back to the same-identifier-concept + corroborator
    namespace path — safe). `table_entity` is LOW-STAKES (a different table_entity alone is never
    incompatible): the table's declared grain-column entity, else the table name. Glossary sidecar
    fields (term_name/term_type/synonyms/BIAN/FIBO leaves/domain) come from the `GlossaryRecord`
    matched by normalized (table, column); a technical upload (glossary=None) leaves them "" — Pass C
    still runs on name/concept/entity signals. `term_type` is supplied by the FTR glossary adapter
    (A1) so `is_join_key_eligible` can exclude Measures; other readers leave it ""."""
    from featuregen.overlay.upload.entity import entity_of
    from featuregen.overlay.upload.passc.identifiers import ColMeta

    concepts = concepts or {}
    records: dict[tuple[str, str], GlossaryRecord] = {}
    if glossary is not None:
        for rec in glossary.records:
            if rec.is_table:
                continue
            try:
                _src, _schema, table, column = parse_ref(rec.logical_ref)
            except ValueError:
                continue
            if column is None:
                continue
            records.setdefault((table, column), rec)   # parse_ref components are normalized

    grain_entity: dict[str, str] = {}
    for r in rows:
        if r.is_grain and r.entity:
            grain_entity.setdefault(r.table, r.entity)

    out = []
    for r in rows:
        object_ref = f"public.{r.table}.{r.column}"   # graph_node.object_ref rendering
        rec = records.get((_lc(r.table), _lc(r.column)))
        out.append(ColMeta(
            object_ref=object_ref, table=r.table, column=r.column, data_type=r.type,
            term_name=rec.term_name if rec else "",
            term_type=rec.term_type if rec else "",
            concept=concepts.get(content_hash(r)) or "",
            synonyms="|".join(rec.synonyms) if rec else "",
            bian_leaf=_taxonomy_leaf(rec.bian_path) if rec else "",
            fibo_leaf=_taxonomy_leaf(rec.fibo_path) if rec else "",
            table_entity=grain_entity.get(r.table) or r.table,
            column_entity=entity_of(conn, catalog_source, object_ref) or r.entity or "",
            data_domain=rec.domain if rec else "",
            is_grain=r.is_grain))
    return out


def _run_pass_c(conn, catalog_source: str, rows: list[CanonicalRow], *,
                concepts: dict[str, str] | None, glossary: GlossaryUpload | None) -> int:
    """Pass C: block + score this upload's columns, OWN THE CYCLE in the candidate ledger
    (clear-then-write: DELETE this source's rows, INSERT this cycle's strong + weak rows — a
    suppressed bucket is counted, never persisted), then propose the strong bucket through the
    governed approved_join path (`propose_join_candidates` grain-gates internally and stamps
    fact_key/proposed_event_id back onto the just-written ledger rows). Runs for glossary AND
    technical uploads. The caller wraps this in a savepoint + except (fail-soft).

    Returns the count of candidate pairs this cycle PERSISTED to the ledger (strong + weak;
    suppressed pairs are counted-not-persisted and excluded) — the MF-5 `join_candidates` count
    threaded onto IngestResult. 0 when nothing survived scoring."""
    from featuregen.overlay.upload.enrich_llm import _ENRICH_ACTOR
    from featuregen.overlay.upload.passc.candidates import block_candidates, score
    from featuregen.overlay.upload.passc.lifecycle import candidate_fingerprint, unordered_pair
    from featuregen.overlay.upload.passc.propose import propose_join_candidates

    cols = _pass_c_columns(conn, catalog_source, rows, concepts=concepts, glossary=glossary)
    snap = mint_id("psc")
    evidences = [score(pair, source_snapshot_id=snap) for pair in block_candidates(cols)]

    # SNAPSHOT each already-claimed pair's governed identity BEFORE the clear: `decide_action`'s
    # cross-cycle checks read the ledger row's PRIOR fact_key, so wiping it to NULL would make a
    # rival direction/cardinality on the SAME unordered pair invisible — a second contradictory
    # DRAFT instead of CONFLICT (whole-branch review, Important-1). Keyed by the ledger's PK form:
    # the SORTED (from_ref, to_ref) pair, exactly what `unordered_pair` yields.
    prior_claims: dict[tuple[str, str], tuple[str, str | None]] = {
        (row[0], row[1]): (row[2], row[3])
        for row in conn.execute(
            "SELECT from_ref, to_ref, fact_key, proposed_event_id"
            " FROM pass_c_candidate_evidence"
            " WHERE catalog_source = %s AND fact_key IS NOT NULL",
            (catalog_source,)).fetchall()}

    # Clear-then-write: a stale prior candidate must not linger past the cycle that stopped
    # producing it. The PROJECTOR never reads this ledger — facts survive the clear.
    conn.execute("DELETE FROM pass_c_candidate_evidence WHERE catalog_source = %s",
                 (catalog_source,))
    strong = []
    for ev in evidences:
        if ev.bucket == "suppressed":
            counters.incr("overlay.passc.candidates.suppressed")
            continue
        lo, hi = unordered_pair(ev)
        # A pair that existed before carries its prior fact_key/proposed_event_id forward (the
        # pair's governing claim survives the cycle); a brand-new pair starts unclaimed (NULL).
        prior_key, prior_event = prior_claims.get((lo, hi), (None, None))
        conn.execute(
            "INSERT INTO pass_c_candidate_evidence (catalog_source, candidate_id,"
            " candidate_fingerprint, from_ref, to_ref, fact_key, proposed_event_id, bucket,"
            " namespace_compatibility, lifecycle, evidence_json, source_snapshot_id,"
            " config_version, candidate_algorithm_version)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'weak', %s, %s, %s, %s)",
            (catalog_source, ev.candidate_id, candidate_fingerprint(ev), lo, hi, prior_key,
             prior_event, ev.bucket, ev.namespace_compatibility.value, json.dumps(asdict(ev)),
             ev.source_snapshot_id, ev.config_version, ev.candidate_algorithm_version))
        counters.incr(f"overlay.passc.candidates.{ev.bucket}")
        if ev.bucket == "strong":
            strong.append(ev)

    if strong:
        # Service proposer (four-eyes holds against the two human confirmers). Fail-soft inside.
        propose_join_candidates(conn, catalog_source, strong, actor=_ENRICH_ACTOR)

    # Persisted (non-suppressed) candidate pairs — the truthful `join_candidates` count (MF-5).
    return sum(1 for ev in evidences if ev.bucket != "suppressed")


# ── Glossary ingest wiring (spec §6.3 / §U). GUARDED: only runs for a glossary upload (a `glossary`
# sidecar is passed in); a non-glossary upload takes none of this and is byte-for-byte unchanged. ──

# The severity a glossary metadata disagreement (review #12 MetadataConflict) opens a conflict at.
_GLOSSARY_CONFLICT_SEVERITY = "metadata_conflict"

# The SOURCE fields whose change on a re-upload is MATERIAL enough to invalidate a prior human
# confirmation (spec §6.3). A glossary attests no physical type, so `definition` is its material axis.
_MATERIAL_FIELDS = frozenset({"definition"})

# The full set of fields each producer can assert for a glossary column. On a re-upload we reconcile
# these against the fields the NEW upload actually provides: a field the new upload NO LONGER asserts
# (present->absent) must have its prior ACTIVE rows STALED, else a dropped value stays load-bearing
# (Task-10 Important-3). Kept in sync with `_write_glossary_source_evidence` / `_parser_evidence`.
_SOURCE_FIELDS: tuple[str, ...] = ("definition", "domain", "business_term", "bian_path", "fibo_path")
_PARSER_FIELDS: tuple[str, ...] = ("logical_representation", "semantic_type")
# The full set of behavioural fields TAXONOMY can DERIVE from a concept (see `derive_concept_evidence`).
# `additivity` is conditional (skipped for an `n/a` concept), so a reclassification to a non-additive
# concept emits no additivity — its prior ACTIVE row must be reconciled present->absent (Important-3).
_TAXONOMY_FIELDS: tuple[str, ...] = (
    "additivity", "temporal_role", "sensitivity_floor", "leakage_anchor")

# A `keep_input_hash` that can never equal a real per-field input hash (always a 64-char sha256 hex
# digest — this contains non-hex chars), so `stale_source_evidence(..., keep_input_hash=_STALE_ALL)`
# stales EVERY active row for the given producer+field — used to retire a field the new upload dropped
# entirely (absent->stale). Plain ASCII (PostgreSQL text rejects NUL bytes in a bound parameter).
_STALE_ALL = "__field_absent_from_upload__"


def _lc(value: str) -> str:
    """Strip + lower-case a ref component (matches object_ref._norm) so a public-scoped CanonicalRow's
    (table, column) matches a schema-preserving logical_ref's already-normalized components."""
    return value.strip().lower()


def _schema_by_table(glossary: GlossaryUpload | None) -> dict[str, str]:
    """Map each glossary table's NORMALIZED name to the real (non-public) schema its column decisions
    are keyed under (``parse_ref(rec.logical_ref)[1]``). Pass B keys its advisory table ref +
    ``resolve_and_project`` refs under this schema so ``readiness`` (schema-aware) sees ONE
    ``(schema, table)`` pair per physical table instead of a phantom public twin that double-counts
    the structural requirements. Empty for a non-glossary upload -> ``normalize_ref`` falls back to
    ``public`` (correct: technical columns are public and write no glossary column decisions)."""
    out: dict[str, str] = {}
    if glossary is None:
        return out
    for rec in glossary.records:
        try:
            _src, schema, table, _col = parse_ref(rec.logical_ref)
        except ValueError:
            continue
        out.setdefault(table, schema)
    return out


def _cross_schema_conflicts(conn, catalog_source: str, rows: list[CanonicalRow],
                            glossary: GlossaryUpload, *,
                            skip_indexes: set[int]) -> list[RowError]:
    """The cross-upload schema fence (round-4 #4): RowErrors for every incoming row that would
    silently RE-ATTRIBUTE an existing ``public.table.column`` graph node to a DIFFERENT schema —
    empty when the upload is safe. The operational key is single-schema until Delivery C, so a
    schema change on the same public-flattened identity is a silent identity rewrite; fail closed
    and hold the WHOLE upload instead.

    A conflict is an incoming column whose lowercased ``(table, column)`` matches an existing
    column node where EITHER the stored ``schema_name`` is non-NULL and differs (case-insensitive)
    from the incoming schema, OR the stored ``schema_name`` IS NULL — a legacy/unverifiable node
    (built before schema preservation, or by a schema-less upload) that a new schema must not
    silently claim (the legacy-NULL policy). The fence keys on the TABLE node too (R5-4): an
    incoming table whose existing ``public.<table>`` node carries a differing (or NULL — same
    legacy policy) ``schema_name`` conflicts even when NO column names overlap — comparing only
    column nodes let a new schema with disjoint columns silently replace the table's identity.
    Every affected row (each row is affected by at most ONE error — the more specific column-level
    one wins — keeping the quarantine's ``(catalog_source, row_index)`` PK safe) is indexed by its
    position in the ORIGINAL upload (disjoint from validate's quarantined indexes — those rows are
    not conflict candidates — and from the reader-level quarantine, which starts AT ``len(rows)``)
    and stamped ``adapter="ftr"``: a schema conflict cannot be repaired inline (resolving the row
    would bypass this very fence) — the fix is re-uploading a corrected file. Schema-less records
    (the generic glossary reader) contribute nothing, so only schema-carrying uploads can ever
    hold."""
    incoming: dict[tuple[str, str], str] = {}
    incoming_tables: dict[str, str] = {}   # normalized table -> declared schema (first wins)
    for rec in glossary.records:
        if not rec.schema:
            continue
        try:
            _src, _schema, table, column = parse_ref(rec.logical_ref)
        except ValueError:
            continue
        incoming_tables.setdefault(table, rec.schema)
        if column is not None:
            incoming[(table, column)] = rec.schema
    if not incoming_tables:
        return []
    existing = conn.execute(
        "SELECT object_ref, schema_name FROM graph_node "
        "WHERE catalog_source = %s AND kind = 'column'", (catalog_source,)).fetchall()
    conflicts: dict[tuple[str, str], str | None] = {}   # (table, column) -> existing schema_name
    for object_ref, schema_name in existing:
        parts = object_ref.split(".")   # "public.table.column" — validate_rows rejects dotted names
        if len(parts) != 3:
            continue
        declared = incoming.get((parts[1], parts[2]))
        if declared is None:
            continue
        if schema_name is None or _lc(schema_name) != _lc(declared):
            conflicts[(parts[1], parts[2])] = schema_name
    existing_tables = conn.execute(
        "SELECT object_ref, schema_name FROM graph_node "
        "WHERE catalog_source = %s AND kind = 'table'", (catalog_source,)).fetchall()
    table_conflicts: dict[str, str | None] = {}   # table -> existing table-node schema_name
    for object_ref, schema_name in existing_tables:
        parts = object_ref.split(".")   # "public.table"
        if len(parts) != 2:
            continue
        declared_table = incoming_tables.get(parts[1])
        if declared_table is None:
            continue
        if schema_name is None or _lc(schema_name) != _lc(declared_table):
            table_conflicts[parts[1]] = schema_name
    if not conflicts and not table_conflicts:
        return []

    def _attribution(existing_schema: str | None) -> str:
        return (f"already exists under schema {existing_schema!r}"
                if existing_schema is not None
                else "already exists with no attested schema (legacy — unverifiable)")

    errors: list[RowError] = []
    for i, r in enumerate(rows):
        if i in skip_indexes:
            continue
        key = (_lc(r.table), _lc(r.column))
        if key in conflicts:
            errors.append(RowError(
                i, f"schema conflict — public.{key[0]}.{key[1]} "
                   f"{_attribution(conflicts[key])}; cannot re-attribute to {incoming[key]!r} "
                   f"while the operational key is single-schema (Delivery C)",
                r, adapter="ftr"))
        elif key[0] in table_conflicts:
            errors.append(RowError(
                i, f"schema conflict — table public.{key[0]} "
                   f"{_attribution(table_conflicts[key[0]])}; cannot re-attribute to "
                   f"{incoming_tables[key[0]]!r} while the operational key is single-schema "
                   f"(Delivery C)",
                r, adapter="ftr"))
    return errors


def _schema_preserving_ref_map(glossary: GlossaryUpload) -> dict[str, str]:
    """Map each column record's PUBLIC-FLATTENED ref (the key ``classify_upload`` emits conflicts under,
    via ``normalize_ref(source, None, table, column)``) to its SCHEMA-PRESERVING ``rec.logical_ref``
    (the key evidence/decisions use). Lets ``_open_glossary_conflicts`` open a conflict under the SAME
    identity the object's evidence uses instead of the schema-forced-public row key (Task-10 Minor-5)."""
    out: dict[str, str] = {}
    for rec in glossary.records:
        if rec.is_table:
            continue
        try:
            rec_source, _schema, table, column = parse_ref(rec.logical_ref)
        except ValueError:
            continue
        if column is None:
            continue
        out[normalize_ref(rec_source, None, table, column)] = rec.logical_ref
    return out


def _open_glossary_conflicts(
    conn, conflicts: list[MetadataConflict], *, ref_map: dict[str, str], now: datetime | None
) -> None:
    """Open (or reopen) one ``conflict_review`` item per metadata disagreement (review #12). Fail-soft
    + savepointed: a conflict-open failure logs and is contained, never aborting the upload.

    ``ref_map`` reconciles each conflict's public-flattened ``logical_ref`` to the schema-preserving one
    the same object's evidence/decisions key on (Task-10 Minor-5), so the conflict and its evidence
    never diverge on identity. A ref with no sidecar record falls back to the row key unchanged."""
    for c in conflicts:
        logical_ref = ref_map.get(c.logical_ref, c.logical_ref)
        try:
            with conn.transaction():
                fingerprint = conflict_fingerprint(
                    logical_ref, c.field, c.competing_value_hashes, FIELD_POLICY_VERSION
                )
                open_or_reopen_conflict(
                    conn, fingerprint=fingerprint, logical_ref=logical_ref, field_name=c.field,
                    severity=_GLOSSARY_CONFLICT_SEVERITY, competing_evidence_ids=(),
                    competing_value_hashes=c.competing_value_hashes, now=now,
                )
        except Exception:  # noqa: BLE001 — advisory: a conflict-open failure never aborts the upload
            logger.warning("advisory conflict-review open failed for %s.%s",
                           logical_ref, c.field, exc_info=True)


def _write_producer_field(conn, *, logical_ref: str, field_name: str, value: object,
                          producer: EvidenceProducer, strength: AssertionStrength,
                          producer_ref: str, snapshot_id: str, material: object) -> int:
    """Write ONE per-field proposal with PRODUCER-SCOPED staleness + snapshot reuse (spec §5.1, review
    must-fix #7). Returns the number of the producer's prior ACTIVE rows this staled (a differing input
    superseded). NEVER touches other producers' rows — in particular a source/parser/taxonomy write can
    never stale HUMAN evidence.

    * stale the producer's own ACTIVE rows for the field whose ``input_hash`` differs from this upload's
      (a CHANGED input supersedes -> STALE);
    * an UNCHANGED input (an ACTIVE row with the same ``input_hash`` already exists) is REUSED — not
      re-written — even though ``source_snapshot_id`` advanced."""
    input_hash = field_input_hash(logical_ref=logical_ref, field_name=field_name, material=material)
    staled = stale_source_evidence(
        conn, logical_ref=logical_ref, field_name=field_name,
        producer=producer, keep_input_hash=input_hash,
    )
    reused = any(
        e.producer == EvidenceProducer(producer).value and e.input_hash == input_hash
        for e in read_active_field_evidence(conn, logical_ref, field_name)
    )
    if not reused:
        record_field_evidence(
            conn, logical_ref=logical_ref, field_name=field_name, proposed_value=value,
            producer=producer, strength=strength, producer_ref=producer_ref,
            source_snapshot_id=snapshot_id, input_hash=input_hash,
        )
    return staled


def _stale_absent_fields(
    conn, *, logical_ref: str, producer: EvidenceProducer, all_fields: tuple[str, ...],
    present: set[str],
) -> set[str]:
    """Stale a producer's prior ACTIVE rows for every field the NEW upload NO LONGER asserts
    (``all_fields - present``) — a present->absent field must not leave a load-bearing value behind
    (Task-10 Important-3). PRODUCER-SCOPED (never touches human/taxonomy evidence). Returns the set of
    fields that actually had ≥1 row staled (so the caller can treat a dropped MATERIAL field as a change)."""
    staled_fields: set[str] = set()
    for field_name in all_fields:
        if field_name in present:
            continue
        n = stale_source_evidence(
            conn, logical_ref=logical_ref, field_name=field_name,
            producer=producer, keep_input_hash=_STALE_ALL,
        )
        if n > 0:
            staled_fields.add(field_name)
    return staled_fields


def _write_glossary_source_evidence(
    conn, *, logical_ref: str, rec: GlossaryRecord, snapshot_id: str
) -> bool:
    """Write SOURCE evidence for a glossary term (column or table ``logical_ref``) at the profile's
    per-field strength (definition + bian/fibo/term ATTESTED, domain PROPOSED). Returns whether the
    term's MATERIAL changed vs the
    prior upload — a material field whose prior source proposal was staled EITHER because its value
    changed (present->present) OR because the new upload dropped it entirely (present->absent)."""
    material_changed = False
    present: set[str] = set()
    for field_name, value in (("definition", rec.definition), ("domain", rec.domain),
                              ("business_term", rec.term_name), ("bian_path", rec.bian_path),
                              ("fibo_path", rec.fibo_path)):
        if not value:
            continue
        present.add(field_name)
        staled = _write_producer_field(
            conn, logical_ref=logical_ref, field_name=field_name, value=value,
            producer=EvidenceProducer.SOURCE,
            strength=strength_for(FTR_GLOSSARY_PROFILE, field_name),
            producer_ref=snapshot_id, snapshot_id=snapshot_id, material=value,
        )
        if field_name in _MATERIAL_FIELDS and staled > 0:
            material_changed = True
    # Reconcile absent fields: a field the prior upload asserted but this one dropped is staled here;
    # a MATERIAL field going present->absent is itself a material change (clearing a definition must
    # flag a prior human confirmation pending-revalidation, not silently keep it load-bearing).
    dropped = _stale_absent_fields(
        conn, logical_ref=logical_ref, producer=EvidenceProducer.SOURCE,
        all_fields=_SOURCE_FIELDS, present=present,
    )
    if dropped & _MATERIAL_FIELDS:
        material_changed = True
    return material_changed


def _write_glossary_parser_evidence(
    conn, *, logical_ref: str, logical_representation: str, semantic_type: str,
    declared_type: str, column: str, snapshot_id: str
) -> None:
    """Write PARSER evidence (logical_representation / semantic_type @ parser:supported) from the SAFE
    facets the reader CARRIED on the record — captured by the deterministic sample parser at read time,
    BEFORE any sample-clause stripping (Task 7 / review #4: the FTR adapter sanitizes the definition,
    so re-parsing it here would find nothing and silently drop the evidence). An empty facet (``""``)
    is ABSENT: nothing is written, and no facets at all is a gap, never a failure.

    Before writing, the carried facets are reconciled against the record's ``declared_type`` and
    ``column`` name (MF-1): the sample-shape classifier sees neither, so an epoch/timestamp integer or
    a point-less decimal can be asserted as an ``identifier`` at the OPERATIONAL parser:supported tier.
    :func:`reconcile_profile` WITHHOLDS a contradicted field (sets it to None) rather than asserting a
    wrong operational value — a withheld field is then simply ABSENT and staled like any unparsed one."""
    reconciled = reconcile_profile(
        ParsedProfile(logical_representation=logical_representation or None,
                      semantic_type=semantic_type or None, computational_type=None,
                      sample_values=(), diagnostic=None),
        declared_type=declared_type, column=column or "",
    )
    if reconciled.diagnostic:
        logger.info("glossary parser evidence reconciled for %s: %s", logical_ref,
                    reconciled.diagnostic)
    logical_representation = reconciled.logical_representation or ""
    semantic_type = reconciled.semantic_type or ""
    present: set[str] = set()
    for field_name, value in (("logical_representation", logical_representation),
                              ("semantic_type", semantic_type)):
        if not value:
            continue
        present.add(field_name)
        _write_producer_field(
            conn, logical_ref=logical_ref, field_name=field_name, value=value,
            producer=EvidenceProducer.PARSER, strength=AssertionStrength.SUPPORTED,
            producer_ref=snapshot_id, snapshot_id=snapshot_id, material=value,
        )
    # Reconcile absent parser fields: an edited upload that drops its sample-profile facet leaves
    # the prior logical_representation/semantic_type ACTIVE + load-bearing unless we stale it here.
    _stale_absent_fields(
        conn, logical_ref=logical_ref, producer=EvidenceProducer.PARSER,
        all_fields=_PARSER_FIELDS, present=present,
    )
    if not present:
        logger.info("glossary record carried no sample-profile facets for %s", logical_ref)


def _write_glossary_taxonomy_evidence(
    conn, *, logical_ref: str, row: CanonicalRow, concepts: dict[str, str], snapshot_id: str
) -> None:
    """Write TAXONOMY-derived behavioural evidence for a column whose concept was classified this run
    (§3.2 strength propagation: derived at PROPOSED from the llm/proposed concept). An unknown /
    unclassified concept derives nothing.

    Present->absent reconciliation (Important-3): after writing the derived triples, stale the
    TAXONOMY producer's prior ACTIVE rows for every derivable field this run did NOT emit — a re-upload
    reclassifying an additive concept to a non-additive one emits no ``additivity``, so the prior
    ``additivity='additive'`` row must be STALED, else ``resolve_and_project`` re-projects the wrong
    aggregation semantics. Mirrors the SOURCE/PARSER reconciliation; PRODUCER-SCOPED (taxonomy only)."""
    concept = concepts.get(content_hash(row))
    present: set[str] = set()
    if concept:
        for field_name, value, strength in derive_concept_evidence(
                concept, AssertionStrength.PROPOSED):
            present.add(field_name)
            _write_producer_field(
                conn, logical_ref=logical_ref, field_name=field_name, value=value,
                producer=EvidenceProducer.TAXONOMY, strength=strength,
                producer_ref=snapshot_id, snapshot_id=snapshot_id, material=concept,
            )
    _stale_absent_fields(
        conn, logical_ref=logical_ref, producer=EvidenceProducer.TAXONOMY,
        all_fields=_TAXONOMY_FIELDS, present=present,
    )


def _flag_human_confirmed_revalidation(conn, *, logical_ref: str, snapshot_id: str,
                                       now: datetime | None) -> None:
    """When a column's MATERIAL changed, flag every field carrying HUMAN-confirmed evidence PENDING
    revalidation (spec §6.3). The human evidence is NOT staled — the flag blocks its load-bearing
    effect (via active_disqualifiers_for) until a human re-confirms."""
    rows = conn.execute(
        "SELECT DISTINCT field_name FROM field_evidence "
        "WHERE logical_ref = %s AND producer = 'human' AND strength = 'confirmed' "
        "AND lifecycle = 'active'",
        (logical_ref,),
    ).fetchall()
    for (field_name,) in rows:
        flag_pending_revalidation(
            conn, logical_ref=logical_ref, field_name=field_name,
            reason="source re-upload changed the column's material (definition/type); the human "
                   "confirmation must be revalidated",
            source_snapshot_id=snapshot_id, now=now,
        )


def _project_semantic_terms(conn, *, source: str, object_ref: str, rec: GlossaryRecord) -> None:
    """Project the record's semantic text — term name, synonyms, BIAN/FIBO/process paths, related
    terms — onto the graph node's ``semantic_terms`` and rebuild its ``search_doc`` (Task 8), so a
    search for a business synonym or a taxonomy token finds the column/table.

    ``semantic_terms`` is a SEARCH-PROJECTION column (index material, like ``search_doc`` itself),
    NOT an evidence-resolved authority field, so a direct UPDATE is correct here — contrast
    definition/domain, which must flow through ``resolve_and_project``. Re-redacted via
    ``redact_text`` as defense-in-depth: the FTR adapter already redacted every field at parse time
    (idempotent there), but this also covers any generic path that reaches here unredacted. An
    empty/blanked result leaves the node's freshly-inserted NULL. ``rebuild_search_doc`` is called
    EXPLICITLY because ``resolve_and_project``'s ``_project_display`` rebuild is conditional on a
    doc-bearing display column changing — this ref might otherwise keep a doc without the terms."""
    text = join_path(
        [rec.term_name, *rec.synonyms, rec.bian_path, rec.fibo_path, rec.process_path,
         *rec.related_terms], sep=" ")
    clean, _redaction_version = redact_text(text)
    if not clean:
        return
    conn.execute(
        "UPDATE graph_node SET semantic_terms = %s WHERE catalog_source = %s AND object_ref = %s",
        (clean, source, object_ref))
    rebuild_search_doc(conn, source, object_ref)


def _ingest_glossary_evidence(conn, *, source: str, rows: list[CanonicalRow],
                              glossary: GlossaryUpload, bindings: dict[str, ObjectBinding],
                              concepts: dict[str, str] | None, snapshot_id: str,
                              now: datetime | None, stats: dict | None = None) -> int:
    """Attach the glossary's per-field evidence (source / parser / taxonomy), flag human-confirmation
    revalidation on a material change, project ``semantic_terms`` into search (Task 8), then
    resolve-and-project + a readiness diagnostic (spec §6.3).

    GUARDED to ATTACHABLE columns only (Task-2 ``may_attach`` binding). Every stage is savepointed and
    fail-soft by the failure-class table: a per-column / per-stage failure logs a warning and is
    contained; the upload's FACTS (already asserted) and raw graph are never rolled back. LLM concept
    evidence is written INSIDE ``enrich_concepts`` (Task 6); this never re-writes it.

    TABLE-level terms (round-4 #5) take a dedicated pass after the column loop: a 2-part record has
    no ``CanonicalRow`` and no ``classify_upload`` binding, so its SOURCE evidence is written at the
    schema-preserving TABLE ref and the ref joins the same ``resolve_and_project`` set — the
    projection (``_graph_key`` maps a column-less ref to the ``public.<table>`` node) is the ONLY
    thing that fills the table node's definition/domain, never a direct UPDATE. A table term whose
    schema disagrees with its columns' is skipped (the columns are authoritative — #5 tail).

    Returns the number of CONTAINED failures (per-column evidence/revalidation writes + the
    resolve/project pass; not the log-only readiness diagnostic) so the caller's stage report can
    say ``partial`` instead of laundering them under an outer success (#22). A ``stats`` dict, when
    passed, threads out non-failure counts the stage detail should SURFACE — currently
    ``table_schema_mismatch_skipped`` (a table term skipped because its schema disagrees with its
    columns'): visible in the run manifest, but not a failure (the upload still succeeds)."""
    contained_failures = 0
    rows_by_tc = {(_lc(r.table), _lc(r.column)): r for r in rows}
    attachable_refs: list[str] = []
    for rec in glossary.records:
        if rec.is_table:
            continue
        try:
            rec_source, _schema, table, column = parse_ref(rec.logical_ref)
        except ValueError:
            continue
        if column is None:
            continue
        # Attachability is decided on the PUBLIC-scoped binding key classify_upload emits (the flat
        # graph is public-scoped); evidence is keyed by the schema-preserving logical_ref (as Task 6).
        binding = bindings.get(normalize_ref(rec_source, None, table, column))
        if binding is None or not may_attach(binding):
            continue
        row = rows_by_tc.get((table, column))
        if row is None:
            continue  # deduped / quarantined out of the validated set -> no evidence to attach
        logical_ref = rec.logical_ref

        material_changed = False
        try:
            with conn.transaction():
                material_changed = _write_glossary_source_evidence(
                    conn, logical_ref=logical_ref, rec=rec, snapshot_id=snapshot_id)
        except Exception:  # noqa: BLE001 — evidence-write failure: warn + continue (facts intact)
            contained_failures += 1
            logger.warning("advisory glossary SOURCE evidence failed for %s", logical_ref,
                           exc_info=True)
        try:
            with conn.transaction():
                _write_glossary_parser_evidence(
                    conn, logical_ref=logical_ref,
                    logical_representation=rec.logical_representation,
                    semantic_type=rec.semantic_type,
                    declared_type=rec.declared_type, column=column or "",
                    snapshot_id=snapshot_id)
        except Exception:  # noqa: BLE001
            contained_failures += 1
            logger.warning("advisory glossary PARSER evidence failed for %s", logical_ref,
                           exc_info=True)
        if concepts is not None:
            try:
                with conn.transaction():
                    _write_glossary_taxonomy_evidence(
                        conn, logical_ref=logical_ref, row=row, concepts=concepts,
                        snapshot_id=snapshot_id)
            except Exception:  # noqa: BLE001
                contained_failures += 1
                logger.warning("advisory glossary TAXONOMY evidence failed for %s", logical_ref,
                               exc_info=True)
        if material_changed:
            try:
                with conn.transaction():
                    _flag_human_confirmed_revalidation(
                        conn, logical_ref=logical_ref, snapshot_id=snapshot_id, now=now)
            except Exception:  # noqa: BLE001
                contained_failures += 1
                logger.warning("advisory revalidation flag failed for %s", logical_ref,
                               exc_info=True)
        try:
            with conn.transaction():
                _project_semantic_terms(
                    conn, source=source, object_ref=_column_ref(table, column), rec=rec)
        except Exception:  # noqa: BLE001 — search projection failure: warn + continue (facts intact)
            contained_failures += 1
            logger.warning("advisory semantic_terms projection failed for %s", logical_ref,
                           exc_info=True)
        attachable_refs.append(logical_ref)

    # Dedicated TABLE-term pass (see docstring): mirror the column loop's savepointed, fail-soft
    # shape — SOURCE evidence + revalidation-on-material-change, then membership in the resolve set.
    column_schemas: dict[str, set[str]] = {}   # normalized table -> its column records' schemas
    for rec in glossary.records:
        if rec.is_table:
            continue
        try:
            _rec_source, schema, table, column = parse_ref(rec.logical_ref)
        except ValueError:
            continue
        if column is not None:
            column_schemas.setdefault(table, set()).add(schema)
    for rec in glossary.records:
        if not rec.is_table:
            continue
        try:
            _rec_source, schema, table, column = parse_ref(rec.logical_ref)
        except ValueError:
            continue
        if column is not None:
            continue
        declared = column_schemas.get(table)
        if declared and declared != {schema}:
            # #5 tail: the columns are authoritative for the schema — attaching the table term's
            # evidence under a disagreeing identity would split one physical table across two refs.
            # Not a failure (the upload still succeeds), but the skip is surfaced in the stage
            # detail via `stats` so a reviewer can see the table evidence was dropped.
            logger.warning(
                "glossary table term %s declares schema %r but its columns declare %s — columns "
                "are authoritative; table evidence skipped", rec.logical_ref, schema,
                sorted(declared))
            if stats is not None:
                stats["table_schema_mismatch_skipped"] = (
                    stats.get("table_schema_mismatch_skipped", 0) + 1)
            continue
        material_changed = False
        try:
            with conn.transaction():
                material_changed = _write_glossary_source_evidence(
                    conn, logical_ref=rec.logical_ref, rec=rec, snapshot_id=snapshot_id)
        except Exception:  # noqa: BLE001 — evidence-write failure: warn + continue (facts intact)
            contained_failures += 1
            logger.warning("advisory glossary SOURCE evidence failed for %s", rec.logical_ref,
                           exc_info=True)
        if material_changed:
            try:
                with conn.transaction():
                    _flag_human_confirmed_revalidation(
                        conn, logical_ref=rec.logical_ref, snapshot_id=snapshot_id, now=now)
            except Exception:  # noqa: BLE001
                contained_failures += 1
                logger.warning("advisory revalidation flag failed for %s", rec.logical_ref,
                               exc_info=True)
        try:
            with conn.transaction():
                _project_semantic_terms(
                    conn, source=source, object_ref=_table_ref(table), rec=rec)
        except Exception:  # noqa: BLE001 — search projection failure: warn + continue (facts intact)
            contained_failures += 1
            logger.warning("advisory semantic_terms projection failed for %s", rec.logical_ref,
                           exc_info=True)
        attachable_refs.append(rec.logical_ref)

    if not attachable_refs:
        return contained_failures
    try:
        with conn.transaction():
            resolve_and_project(conn, source=source, logical_refs=attachable_refs, now=now)
    except Exception:  # noqa: BLE001 — resolver failure: continue with the raw graph (degraded)
        contained_failures += 1
        logger.warning("advisory resolve_and_project failed for %r — graph left with raw nodes "
                       "(degraded)", source, exc_info=True)
    try:
        # SAVEPOINTED (mirrors resolve_and_project above): a DB-level error inside compute_readiness
        # aborts the request transaction, and the bare except alone would swallow the Python error yet
        # leave the tx poisoned — the next unconditional statement (persist_quarantine's DELETE) would
        # then raise InFailedSqlTransaction and roll back the WHOLE upload (facts + graph lost, 500).
        # The savepoint contains the abort so this advisory diagnostic can never fail the upload.
        with conn.transaction():
            readiness = compute_readiness(conn, source=source, scope=ReadinessScopeType.CATALOG)
            logger.info("glossary ingest readiness for %r: status=%s blocking=%d review=%d",
                        source, readiness.operational_status, len(readiness.blocking_requirements),
                        len(readiness.review_requirements))
    except Exception:  # noqa: BLE001
        logger.warning("advisory readiness diagnostic failed for %r", source, exc_info=True)
    return contained_failures


def ingest_source_lock_key(catalog_source: str) -> int:
    """Stable 64-bit advisory-lock key serializing ingests of ONE ``catalog_source`` (#3).

    sha256 over a dedicated ``overlay_ingest:`` namespace, first 8 bytes big-endian signed — the
    exact derivation worker.py uses for its ``overlay_renewal`` / ``overlay_drift:{source}`` keys,
    under a DISTINCT prefix so this key space cannot collide with those (nor, practically, with the
    fixed constants: security-chain 7_000_007, migrations 6157423001, global-seq
    4_201_873_355_201_001). MUST stay stable across releases: two versions deriving different keys
    for the same source would stop excluding each other during a rolling deploy."""
    return int.from_bytes(
        hashlib.sha256(f"overlay_ingest:{catalog_source}".encode()).digest()[:8],
        "big", signed=True)


def ingest_upload(conn, catalog_source: str, rows: list[CanonicalRow], *,
                  actor, now: datetime | None = None, client=None,
                  profile: SourceCapabilityProfile | None = None,
                  glossary: GlossaryUpload | None = None,
                  stage_recorder: StageRecorder | None = None,
                  origin_type: str = "upload",
                  ingestion_run_id: str | None = None) -> IngestResult:
    # `origin_type` (#10): how this ingest entered the system — "upload" (default; the /uploads
    # route) or "connector" (the integrations sync route) — stamped as the source-declared
    # authority origin on every auto-confirmed fact. Matches the ingestion_run origin_type.
    # `ingestion_run_id` (design #3, the deferred provenance piece): the durable run the calling
    # route opened via `open_run` BEFORE this call. When set, the run's observed objects (every
    # table/column ref this upload builds), changed objects (the drift diff's drop/type_change/
    # rename refs) and asserted/changed facts are recorded as ingestion_run_object /
    # ingestion_run_fact associations — batched, fail-soft, on THIS connection (atomic with the
    # ingest). `None` (every direct caller) records NOTHING: byte-for-byte unchanged.
    # `stage_recorder` (#22) BUFFERS an honest per-stage outcome as each stage runs — it never
    # writes during ingest (the route flushes it alongside terminalize), never touches the return
    # value, and `None` (every direct caller / flag-off) is a no-op: byte-for-byte unchanged.
    # Every record goes through `record_stage`, which contains ANY recorder failure (defensive).
    ensure_upload_catalog_adapter()   # governed fact lifecycle needs an adapter (owner_of->None)
    # #3 — SERIALIZE same-source ingests. build_graph is DELETE-this-source-then-reinsert, so two
    # concurrent uploads of the SAME source would clobber each other's graph (last-writer-wins) and
    # let the drift snapshot diverge from the graph. A transaction-scoped, SOURCE-scoped advisory
    # lock taken ONCE, on the REQUEST connection, at the very top (before brake/snapshot/facts/
    # graph) blocks the second same-source ingest until the first commits; different sources hash
    # to different keys and never block each other. Auto-released at COMMIT/ROLLBACK (nothing to
    # clean up). DEADLOCK SAFETY (program-audit I-3 history): acquired nowhere else in the ingest
    # path, and enrich_llm's durable-audit connection deliberately takes NO advisory lock, so no
    # second connection can wait on the key this transaction holds.
    # LOCK-HOLD DURING ENRICHMENT (#4): the three Pass A LLM stages below run WHILE this lock is held
    # (they are between here and build_graph). Enrichment defaults to BATCH now (enrich_config), so a
    # wide file makes ~ceil(cols/40) concept calls instead of one-per-column — the lock hold shrinks
    # by ~30x on a 126-col file. RELEASING the lock across the LLM calls and re-acquiring is NOT a safe
    # small change and is deliberately NOT done: this is a transaction-scoped lock (only releases at
    # COMMIT/ROLLBACK — you cannot unlock it mid-tx without switching to session-level advisory locks),
    # and enrichment is NOT the tail of the ingest — facts are already asserted above and build_graph's
    # whole-source DELETE+rebuild still runs AFTER it. A concurrent same-source ingest slipping into a
    # release window could commit its own build_graph, then this tx's later build_graph would clobber
    # it (last-writer-wins) with the drift snapshot/facts computed before the window now inconsistent —
    # exactly the corruption this lock exists to prevent. A true lock-release-during-enrichment (or
    # hoisting enrichment's side effects entirely before the lock) is a separate concurrency-design
    # task; batch mode is the throughput mitigation for now.
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (ingest_source_lock_key(catalog_source),))
    # `profile` (spec §U) makes validation profile-aware: a glossary upload's `type="unknown"` rows
    # pass, while a technical upload (or the default `profile=None`) still requires a real type. A
    # glossary sidecar IMPLIES the glossary profile (a glossary attests no physical type), so default
    # it — otherwise the `unknown`-type rows would all quarantine and no evidence could attach.
    if glossary is not None and profile is None:
        profile = FTR_GLOSSARY_PROFILE
    # #13 gap A: each stage's record carries the instant it BEGAN. `stage_started` is re-captured
    # immediately before every stage that actually runs; marker records (disabled/not_applicable/
    # skipped_no_client/not_run) never started and pass none.
    stage_started = datetime.now(UTC)
    vr = validate_rows(rows, catalog_source, profile=profile)
    if glossary is not None and glossary.quarantined:
        # #9 — merge the READER-level quarantine (multi-schema fold collisions: the schema is dropped
        # from the CanonicalRow, so only the reader could detect them) into this upload's quarantine,
        # so the collisions land in the review queue beside validation failures. Index spaces are
        # disjoint by construction (reader indexes start at len(rows); validate's are < len(rows)),
        # so the quarantine_row (catalog_source, row_index) PK cannot conflict. An ALL-collisions
        # glossary leaves `rows` empty — validate's "empty upload" structural error would then mask
        # the real cause AND skip persistence; clear it so the all-quarantined path below persists
        # the collisions and rejects honestly. A structural error on a NON-empty `rows` (e.g. "no
        # row has a source") still rejects as before.
        vr = ValidationResult(good=vr.good,
                              quarantined=[*vr.quarantined, *glossary.quarantined],
                              structural_error=vr.structural_error if rows else None)
    if vr.structural_error:
        record_stage(stage_recorder, "validation", "failed", reason_code="structural_error",
                     detail={"quarantined": len(vr.quarantined)}, started_at=stage_started)
        # #33 consistency: a structural rejection WITH quarantine content (a glossary whose reader
        # quarantined rows was merged above) surfaces it like the held/all-quarantined paths below;
        # with none it leaves the prior queue untouched (nothing ingested, nothing new to review).
        if vr.quarantined:
            stage_started = datetime.now(UTC)
            persist_quarantine(conn, catalog_source, vr.quarantined)
            record_stage(stage_recorder, "quarantine", "succeeded",
                         detail={"rows": len(vr.quarantined)}, started_at=stage_started)
        # #13 gap B: the stage account stays COMPLETE — everything downstream honestly not_run.
        record_skipped_downstream(stage_recorder, reason_code="skipped_rejected",
                                  is_glossary=glossary is not None)
        return IngestResult("rejected", vr.structural_error, 0, 0, len(vr.quarantined))
    # ANY quarantined row makes validation `partial`, not `succeeded` — per-row failures are the
    # stage's own outcome even though the upload proceeds on the good rows (#22).
    record_stage(stage_recorder, "validation", "partial" if vr.quarantined else "succeeded",
                 detail={"good": len(vr.good), "quarantined": len(vr.quarantined)},
                 started_at=stage_started)

    if glossary is not None:
        # Cross-upload schema fence (round-4 #4) — BEFORE any side effect (no UploadCatalog, no
        # facts, no graph write): a schema-carrying upload that would re-attribute an existing
        # public-flattened column to a DIFFERENT schema holds the WHOLE upload fail-closed.
        # Rows validation already quarantined are skipped (they never reach the graph, so they
        # cannot re-attribute anything — and their RowError indexes stay collision-free).
        stage_started = datetime.now(UTC)
        conflict_errors = _cross_schema_conflicts(
            conn, catalog_source, rows, glossary,
            skip_indexes={e.row_index for e in vr.quarantined})
        if conflict_errors:
            reason = (f"schema conflict: {len(conflict_errors)} column(s) already attributed to a "
                      f"different (or unverifiable) schema — e.g. {conflict_errors[0].message}; "
                      f"upload held fail-closed, correct the file and re-upload")
            record_stage(stage_recorder, "brake", "deferred", reason_code="held",
                         detail={"reason": reason, "schema_conflicts": len(conflict_errors)},
                         started_at=stage_started)
            # Persist the conflicts BESIDE the upload's validation quarantine (one whole-source
            # refresh — persist_quarantine deletes everything first, so two calls would clobber),
            # and report the REAL total persisted for review — never len(rows).
            held_quarantine = [*vr.quarantined, *conflict_errors]
            stage_started = datetime.now(UTC)
            persist_quarantine(conn, catalog_source, held_quarantine)
            record_stage(stage_recorder, "quarantine", "succeeded",
                         detail={"rows": len(held_quarantine)}, started_at=stage_started)
            logger.warning("upload of %r held by the cross-schema fence: %s",
                           catalog_source, reason)
            record_skipped_downstream(stage_recorder, reason_code="skipped_upload_held",
                                      is_glossary=True)
            return IngestResult("held", reason, 0, 0, len(held_quarantine))

    upload = UploadCatalog(catalog_source, vr.good)
    stage_started = datetime.now(UTC)
    brake = large_change_brake(conn, catalog_source, upload)
    if brake.held:
        record_stage(stage_recorder, "brake", "deferred", reason_code="held",
                     detail={"reason": brake.reason}, started_at=stage_started)
        # persist the quarantine even when held, so a reviewer can see WHY this upload's rows failed
        # (was: returned before persist_quarantine -> the queue still showed the previous upload).
        # ONLY when non-empty (#33): a held upload did NOT ingest — the catalog still reflects the
        # prior upload — so a held-but-clean upload must not wipe the queue a reviewer is working
        # through (persist_quarantine's whole-source refresh deletes everything first).
        if vr.quarantined:
            stage_started = datetime.now(UTC)
            persist_quarantine(conn, catalog_source, vr.quarantined)
            record_stage(stage_recorder, "quarantine", "succeeded",
                         detail={"rows": len(vr.quarantined)}, started_at=stage_started)
        logger.warning("upload of %r held by the large-change brake: %s", catalog_source, brake.reason)
        # #13 gap B: report the stages the hold skipped as not_run — a complete, honest account.
        record_skipped_downstream(stage_recorder, reason_code="skipped_upload_held",
                                  is_glossary=glossary is not None)
        return IngestResult("held", brake.reason, 0, 0, len(vr.quarantined))
    record_stage(stage_recorder, "brake", "succeeded", started_at=stage_started)

    if not vr.good and vr.quarantined:
        # Every row quarantined -> nothing usable (a CSV whose headers never mapped to
        # table/column/type, or a glossary whose FQNs all failed to resolve — the rows still carry a
        # source, so the "no row has a source" structural error above does NOT catch this). Persist
        # the quarantine so the reviewer can see WHY each row failed (like the held path), and
        # return an HONEST non-success status instead of "ingested" with asserted=0. Crucially,
        # return BEFORE build_graph so a garbage upload NEVER wipes an existing graph (mirrors the
        # structural-error early-return above). After the brake, so a held upload still reports held.
        stage_started = datetime.now(UTC)
        persist_quarantine(conn, catalog_source, vr.quarantined)
        record_stage(stage_recorder, "quarantine", "succeeded",
                     detail={"rows": len(vr.quarantined)}, started_at=stage_started)
        # #13 gap B: the stage account stays COMPLETE — everything downstream honestly not_run.
        record_skipped_downstream(stage_recorder, reason_code="skipped_rejected",
                                  is_glossary=glossary is not None)
        return IngestResult(
            "rejected",
            f"no rows could be ingested — all {len(vr.quarantined)} quarantined "
            f"(check the file's headers include table/column/type, or that the FQNs resolve)",
            0, 0, len(vr.quarantined))

    stage_started = datetime.now(UTC)
    asserted = 0
    # Run-provenance collection (design #3): the fact keys this run (re)asserted, and the subset
    # whose VALUE genuinely changed. Collected regardless of flag so the loop stays one shape;
    # recorded (below, after the drift diff) ONLY when the route threaded an ingestion_run_id.
    asserted_fact_keys: list[str] = []
    changed_fact_keys: list[str] = []
    for table, fact_type, value in _table_facts(vr.good):
        outcome = _assert_fact(conn, catalog_source, table, fact_type, value, actor=actor,
                               origin_type=origin_type)
        if outcome:
            asserted += 1
            fk = fact_key(table_ref(catalog_source, table), fact_type)
            asserted_fact_keys.append(fk)
            if outcome == "changed":
                changed_fact_keys.append(fk)
    record_stage(stage_recorder, "fact_assertion", "succeeded", detail={"asserted": asserted},
                 started_at=stage_started)

    stage_started = datetime.now(UTC)
    _drain_projection(conn)   # catch up (bounded, #21) BEFORE the diff reads the dependency index
    drift_lagged = False
    if projection_lag(conn, "overlay") > 0:
        # The drain stopped short of head (poison-HALT or the #21 budget): the dependency index is
        # stale, so drift detection would stale NOTHING for a just-dropped/changed column yet still
        # advance the snapshot — laundering the change for a full TTL. Skip drift this upload (same
        # guard as the worker); it re-detects once the projection catches up. The upload's facts
        # still assert; the snapshot is NOT advanced.
        counters.incr("overlay.drift.skipped_projection_lag")
        logger.warning("overlay projection lags after ingest of %r — skipping catalog-change detection "
                       "to avoid laundering drift (re-runs when the projection catches up)", catalog_source)
        changes = []
        drift_lagged = True
    else:
        changes = detect_catalog_changes(conn, upload, actor=actor, now=now, open_reverify=False)
        _drain_projection(conn)
    # Changed catalog OBJECTS (drift kinds that retire something), not facts staled (#30): each such
    # change stales its dependent facts inside detect_catalog_changes, but this counts the objects.
    changed_objects = sum(1 for c in changes if c.kind in ("drop", "type_change", "rename"))
    if drift_lagged:
        # `lagged`, not `skipped`: the honest state is "deferred behind the projection — re-runs on
        # the next caught-up ingest", exactly what the guard above does. The drain itself RAN, so
        # the stage carries its start instant either way.
        record_stage(stage_recorder, "drift", "lagged", reason_code="projection_lag",
                     started_at=stage_started)
    else:
        record_stage(stage_recorder, "drift", "succeeded",
                     detail={"changed_objects": changed_objects}, started_at=stage_started)

    if ingestion_run_id is not None:
        # Run provenance (design #3, deferred piece): durable run↔object / run↔fact associations on
        # THIS connection, so they commit atomically with the ingest they describe. `observed` =
        # every table/column ref this upload builds (`upload.fingerprint()` — the exact refs
        # build_graph and the drift diff key on); `changed` objects = the drift diff's retiring
        # kinds (under drift_lagged `changes` is empty — deferred drift records no provenance
        # either, honestly). The recorders are fail-soft internally (own savepoint + log +
        # counter): a provenance failure can NEVER abort the ingest. `None` (direct callers)
        # records nothing — flag-off byte-for-byte.
        prov_at = now or datetime.now(UTC)
        record_run_objects(conn, ingestion_run_id, catalog_source,
                           upload.fingerprint().keys(), "observed", prov_at)
        record_run_objects(conn, ingestion_run_id, catalog_source,
                           [c.object_ref for c in changes
                            if c.kind in ("drop", "type_change", "rename")], "changed", prov_at)
        record_run_facts(conn, ingestion_run_id, asserted_fact_keys, "asserted", prov_at)
        record_run_facts(conn, ingestion_run_id, changed_fact_keys, "changed", prov_at)

    # ── Glossary path (GUARDED): a glossary upload passes its semantic sidecar; a non-glossary upload
    # (glossary=None) skips ALL of the below and is byte-for-byte unchanged. The ingestion-run id is
    # the source_snapshot_id that keys per-field evidence + staleness for THIS upload (review #5). ──
    is_glossary = glossary is not None
    snapshot_id = mint_id("ing") if is_glossary else None
    bindings: dict[str, ObjectBinding] | None = None
    if glossary is not None:
        stage_started = datetime.now(UTC)
        try:
            # Classify the RAW rows (not vr.good): validate_rows DEDUPs same-FQN rows that differ only
            # in the advisory `definition`, so a definition CONFLICT is invisible in vr.good. The raw
            # rows carry the disagreement classify_upload surfaces as a MetadataConflict (review #12).
            # Filter identity-less rows (glossary_reader emits table=""/column="" for an unresolvable
            # FQN): they collapse to `source::public.` and would manufacture a bogus definition
            # conflict against a ref that never gets evidence or a node (Task-10 Minor-4).
            identified = [r for r in rows if r.table and r.column]
            bindings, conflicts = classify_upload(identified)
            _open_glossary_conflicts(
                conn, conflicts, ref_map=_schema_preserving_ref_map(glossary), now=now)
            record_stage(stage_recorder, "glossary_classification", "succeeded",
                         detail={"conflicts": len(conflicts)}, started_at=stage_started)
        except Exception:  # noqa: BLE001 — advisory: identity/conflict classification never aborts
            logger.warning("advisory glossary identity/conflict classification failed for %r",
                           catalog_source, exc_info=True)
            bindings = {}
            record_stage(stage_recorder, "glossary_classification", "failed",
                         reason_code="exception", started_at=stage_started)
    else:
        record_stage(stage_recorder, "glossary_classification", "not_applicable")

    concepts = definitions = domains = None
    if client is None:
        # No LLM provider configured: the three enrichment stages honestly never ran (#22).
        for _stage in ("enrich_concept", "enrich_definition", "enrich_domain"):
            record_stage(stage_recorder, _stage, "skipped_no_client")
    else:
        # Three INDEPENDENT advisory failure domains (spec C1): a failure in one task must not
        # discard another's already-computed enrichment. Each degrades search, never the facts.
        #
        # SAVEPOINTED (Important-4, same class as the Task-10 compute_readiness fix ~l.489): each
        # enrichment call does UN-savepointed writes (cache put/get, llm_call + security-audit
        # records). A DB-class fault (serialization failure / timeout / constraint) is swallowed as a
        # Python exception by the bare except, yet leaves the REQUEST tx aborted — and the very next
        # UNGUARDED statement (`build_graph`'s `DELETE FROM graph_edge`) would then raise
        # InFailedSqlTransaction and roll back the already-asserted FACTS. The savepoint contains the
        # abort so a poisoned enrichment tx can never reach build_graph — enrichment degrades, facts hold.
        #
        # Each stage's report (#22) is computed from its RESULT against the expected item count —
        # never from "the try didn't raise": these stages swallow per-item failures internally
        # (a failed call is simply absent from the returned dict; a contained concept-evidence
        # write failure is threaded out via `stats`), so an outer success is not evidence.
        concept_stats: dict = {}
        consume_audit_degradations()   # #13 gap D: discard any stale count before the first stage
        stage_started = datetime.now(UTC)
        try:
            # Glossary carry-forward (Task 6): thread the sidecar + bindings + snapshot so Pass A
            # writes item-level LLM concept evidence. Non-glossary keeps the exact original call.
            with conn.transaction():
                concepts = (
                    enrich_concepts(conn, vr.good, client, actor, glossary=glossary,
                                    bindings=bindings, source_snapshot_id=snapshot_id,
                                    stats=concept_stats)
                    if is_glossary
                    else enrich_concepts(conn, vr.good, client, actor, stats=concept_stats)
                )
        except Exception:  # noqa: BLE001
            logger.warning("advisory concept enrichment failed for %r", catalog_source, exc_info=True)
        state, reason, detail = _enrichment_outcome(
            concepts, len({content_hash(r) for r in vr.good}),
            internal_failures=concept_stats.get("evidence_write_failures", 0))
        record_stage(stage_recorder, "enrich_concept", state, reason_code=reason,
                     detail=_with_audit_degradations(detail), started_at=stage_started)
        stage_started = datetime.now(UTC)
        try:
            with conn.transaction():
                # R5-3: the glossary sidecar lets draft_definitions SKIP sanitizer-suppressed
                # blanks (suppressed ≠ missing — never silently LLM-drafted); None for technical.
                definitions = draft_definitions(conn, vr.good, client, actor, concepts=concepts,
                                                glossary=glossary)
        except Exception:  # noqa: BLE001
            logger.warning("advisory definition enrichment failed for %r", catalog_source, exc_info=True)
        state, reason, detail = _enrichment_outcome(
            definitions,
            # Honest expected count (R5-3): suppressed blanks are deliberately NOT drafted, so they
            # must not be counted as unresolved items degrading the stage to "partial".
            len({content_hash(r) for r in vr.good if not r.definition}
                - suppressed_definition_hashes(vr.good, glossary)))
        record_stage(stage_recorder, "enrich_definition", state, reason_code=reason,
                     detail=_with_audit_degradations(detail), started_at=stage_started)
        stage_started = datetime.now(UTC)
        try:
            with conn.transaction():
                domains = classify_domains(conn, vr.good, client, actor)
        except Exception:  # noqa: BLE001
            logger.warning("advisory domain enrichment failed for %r", catalog_source, exc_info=True)
        state, reason, detail = _enrichment_outcome(domains, len({r.table for r in vr.good}))
        record_stage(stage_recorder, "enrich_domain", state, reason_code=reason,
                     detail=_with_audit_degradations(detail), started_at=stage_started)
    stage_started = datetime.now(UTC)
    # Additive schema preservation (round-4 #5): object_ref-keyed maps from the glossary's
    # schema-carrying records; None/empty for technical and generic-glossary uploads, whose
    # nodes keep schema_name/declared_type NULL — byte-for-byte unchanged.
    build_graph(conn, catalog_source, vr.good, concepts, definitions, domains,
                schemas=schema_by_ref(glossary), declared_types=declared_type_by_ref(glossary))
    record_stage(stage_recorder, "graph_persistence", "succeeded", started_at=stage_started)
    if governed_joins_enabled() or pass_c_enabled():
        # Governed seam (Task 7 / §12.1) — Pass C (Task 10) implies it: the raw 'joins' edges just
        # written are display-only; route each declared join into the governed approved_join path so
        # it is never stranded display-only. Advisory/fail-soft + adapter-gated. (The `or` is
        # belt-and-braces: governed_joins_enabled already fires under OVERLAY_PASS_C.)
        # OWN savepoint + except (audit I-2, the Pass C pattern below): a DB-class fault in the
        # seam must never poison the request tx and roll back Pass A facts + the graph — the seam
        # degrades to a warning and the upload always ingests.
        stage_started = datetime.now(UTC)
        try:
            with conn.transaction():
                _propose_governed_joins(conn, vr.good, actor=actor)
            record_stage(stage_recorder, "governed_joins", "succeeded",
                         started_at=stage_started)
        except Exception:  # noqa: BLE001 — advisory: the governed-join seam never fails an upload
            counters.incr("overlay.governed_joins.error")
            logger.warning("advisory governed-join seam failed for %r — Pass A facts + graph "
                           "intact", catalog_source, exc_info=True)
            record_stage(stage_recorder, "governed_joins", "failed", reason_code="exception",
                         started_at=stage_started)
    else:
        record_stage(stage_recorder, "governed_joins", "disabled")

    # MF-5: Pass C's discovered join-candidate count, threaded onto the truthful result. 0 when
    # Pass C is off (default) or a DB fault degrades it below (join_candidate_count stays 0).
    join_candidate_count = 0
    if pass_c_enabled():
        # Pass C (Phase 3A Task 10): deterministic join-candidate discovery — blocking/scoring from
        # upload metadata alone (NO LLM/client needed), the durable candidate ledger, and governed
        # approved_join proposals for the strong bucket. Runs for technical AND glossary uploads.
        # OWN savepoint + except (the Pass B pattern above): a DB abort in here must never poison
        # the request tx and roll back Pass A facts + the graph — Pass C degrades to a warning and
        # the upload always ingests.
        stage_started = datetime.now(UTC)
        try:
            with conn.transaction():
                join_candidate_count = _run_pass_c(
                    conn, catalog_source, vr.good, concepts=concepts, glossary=glossary)
            record_stage(stage_recorder, "pass_c", "succeeded", started_at=stage_started)
        except Exception:  # noqa: BLE001 — advisory: Pass C never fails an upload
            counters.incr("overlay.passc.error")
            logger.warning("advisory Pass C join-candidate pass failed for %r — Pass A facts + "
                           "graph intact", catalog_source, exc_info=True)
            record_stage(stage_recorder, "pass_c", "failed", reason_code="exception",
                         started_at=stage_started)
    else:
        record_stage(stage_recorder, "pass_c", "disabled")

    # MF-5: the Pass B syntheses drive the truthful proposed/abstained split at the success return.
    # Initialised empty so it is ALWAYS in scope — Pass B off, no client, or an advisory failure
    # before it binds all leave `syntheses` at {} (proposed == abstained == 0).
    syntheses: dict = {}
    if not table_synth_enabled():
        record_stage(stage_recorder, "pass_b", "disabled")
    elif client is None:
        record_stage(stage_recorder, "pass_b", "skipped_no_client")
    else:
        # Pass B (spec §15): governed table synthesis — grain/availability as PROPOSED-only,
        # human-gated facts; table_role/primary_entity/event_or_snapshot as advisory evidence.
        from featuregen.overlay.upload.enrich_llm import _ENRICH_ACTOR
        from featuregen.overlay.upload.table_synth import (
            _propose_table_facts,
            assemble_table_items,
            synthesize_tables,
        )
        stage_started = datetime.now(UTC)
        try:
            # TWO savepoints (exactly like the Pass A stages and the governed-join seam
            # above): a DB abort inside either must not
            # poison the request tx and roll back Pass A facts + the quarantine. The try/except
            # makes Pass B strictly advisory. The FIRST savepoint contains the LLM egress + its
            # IMMUTABLE record_llm_call security audit, RELEASED before the advisory stage starts —
            # so an advisory-stage failure can never roll back the record of what egressed. The
            # SECOND contains the advisory propose/projection writes.
            with conn.transaction():
                synth_snapshot = snapshot_id or mint_id("tsy")  # non-glossary uploads have snapshot_id=None
                # MF-2: thread the glossary semantic sidecar into Pass B, keyed by normalized
                # (table, column) — the SAME (table, column) bridge Pass C uses (341-353): the flat
                # CanonicalRow is schema-dropped, so it cannot join the schema-preserving logical_ref
                # string; (table, column) is the stable key. Table-level terms (no column) and
                # unparseable refs are skipped. Empty for a non-glossary technical upload -> unchanged.
                records: dict[tuple[str, str], GlossaryRecord] = {}
                if glossary is not None:
                    for rec in glossary.records:
                        if rec.is_table:
                            continue
                        try:
                            _src, _schema, t, c = parse_ref(rec.logical_ref)
                        except ValueError:
                            continue
                        if c is None:
                            continue
                        records.setdefault((t, c), rec)
                items = assemble_table_items(vr.good, concepts=concepts, definitions=definitions,
                                             records=records)
                cols = {t: {r.column for r in vr.good if r.table == t}
                        for t in {r.table for r in vr.good}}
                syntheses = synthesize_tables(conn, client, items, columns_by_table=cols,
                                              actor=actor)     # LLM-call attribution only
            with conn.transaction():
                # Key the advisory table ref + its projection under the SAME schema the glossary
                # columns use (a non-public schema for an FTR glossary; public for a technical
                # upload) so readiness sees ONE (schema, table) pair per physical table.
                schema_by_table = _schema_by_table(glossary)
                # Propose under the SERVICE actor so a human confirmer later satisfies four-eyes:
                _propose_table_facts(conn, catalog_source, syntheses, actor=_ENRICH_ACTOR,
                                     source_snapshot_id=synth_snapshot,
                                     schema_by_table=schema_by_table)
                # Project the advisory table fields' DISPLAY. resolve_and_project is otherwise
                # called ONLY over glossary COLUMN refs (_ingest_glossary_evidence); table refs need
                # this explicit call or table_role/primary_entity/event_or_snapshot never project
                # (a no-op until Task 8 registers their FieldPolicies).
                pass_b_table_refs = [
                    normalize_ref(catalog_source, schema_by_table.get(t.strip().lower()), t)
                    for t in sorted({r.table for r in vr.good})]
                resolve_and_project(conn, source=catalog_source, logical_refs=pass_b_table_refs,
                                    now=now)
            # Pass B swallows per-table failures internally (an unsynthesized table is simply
            # absent from `syntheses`), so the report compares against the assembled items (#22).
            state, reason, detail = _enrichment_outcome(syntheses, len(items))
            record_stage(stage_recorder, "pass_b", state, reason_code=reason,
                         detail=_with_audit_degradations(detail), started_at=stage_started)
        except Exception:  # noqa: BLE001 — advisory: Pass B never fails an upload; Pass A facts hold
            counters.incr("overlay.table_synth.error")
            logger.warning("advisory Pass B table synthesis failed for %r — Pass A facts + graph "
                           "intact", catalog_source, exc_info=True)
            record_stage(stage_recorder, "pass_b", "failed", reason_code="exception",
                         started_at=stage_started)

    if glossary is not None and bindings is not None and snapshot_id is not None:
        # Attach per-field evidence + revalidation + resolve/readiness on top of the built graph.
        # Belt-and-braces fail-soft: the helper savepoints every stage, and this outer guard makes a
        # stray failure a warning rather than a rollback of the already-committed facts + graph.
        stage_started = datetime.now(UTC)
        try:
            glossary_stats: dict = {}
            contained = _ingest_glossary_evidence(
                conn, source=catalog_source, rows=vr.good, glossary=glossary,
                bindings=bindings, concepts=concepts, snapshot_id=snapshot_id, now=now,
                stats=glossary_stats)
            # The helper CONTAINS per-column failures (savepoint + warning); `partial` surfaces
            # them instead of laundering the outer no-raise as success (#22). A table-term schema
            # mismatch is NOT a failure (the upload still succeeds) but IS surfaced in the detail so
            # a reviewer can see the table evidence was skipped rather than it vanishing to a log.
            skipped = glossary_stats.get("table_schema_mismatch_skipped", 0)
            detail: dict = {}
            if contained:
                detail["contained_failures"] = contained
            if skipped:
                detail["table_schema_mismatch_skipped"] = skipped
            record_stage(stage_recorder, "glossary_evidence",
                         "partial" if contained else "succeeded",
                         reason_code="items_failed" if contained else None,
                         detail=detail or None,
                         started_at=stage_started)
        except Exception:  # noqa: BLE001
            logger.warning("advisory glossary evidence wiring failed for %r — facts + graph intact",
                           catalog_source, exc_info=True)
            record_stage(stage_recorder, "glossary_evidence", "failed", reason_code="exception",
                         started_at=stage_started)
    else:
        record_stage(stage_recorder, "glossary_evidence", "not_applicable")

    # DRAIN before the end-of-ingest re-projections (full-chain e2e finding, 2026-07-15): the
    # governed seams above (_propose_governed_joins / Pass C / Pass B) appended OVERLAY_FACT_PROPOSED
    # events AFTER the last drain, so `projection_lag > 0` here whenever THIS upload proposed
    # anything — and the lag guards below would then skip BOTH re-projection blocks. build_graph
    # just wiped every edge/node, so the skip left a previously-VERIFIED approved_join's operational
    # edge deleted (and a Pass-B-confirmed grain flag cleared) until the NEXT caught-up ingest of
    # the source — feature construction went dark on any re-upload that discovered a new candidate.
    # Draining on this conn (the project_verified_join drain-then-project pattern) brings the read
    # model to head; the guards below now fire ONLY on a genuine poison-HALT or a spent #21 drain
    # budget (a huge unrelated global backlog), their real purpose.
    # Flag-off byte-for-byte safe: with the seams off nothing was appended since the line-749 drain,
    # so this pass processes zero events.
    stage_started = datetime.now(UTC)
    _drain_projection(conn)

    # SPECIALIZED_FACT bridge (Task 9): build_graph just wiped graph_node, so re-project any
    # already-CONFIRMED grain/as-of facts onto the fresh column nodes. UNCONDITIONAL (not
    # flag-gated) — a grain confirmed in a PRIOR cycle must survive a rebuild even when
    # OVERLAY_TABLE_SYNTH is off. The clear-then-set SPARES the columns THIS upload declares (their
    # file-declared is_grain/is_as_of is final and must survive a drift-STALEd governed fact, which
    # would otherwise resolve None and wipe a just-declared grain — a flag-off byte-for-byte break).
    declared_grain: dict[str, set[str]] = {}
    declared_as_of: dict[str, set[str]] = {}
    for r in vr.good:
        if r.is_grain:
            declared_grain.setdefault(r.table, set()).add(r.column)
        if r.as_of:
            declared_as_of.setdefault(r.table, set()).add(r.column)
    # ONE lag read drives BOTH the projection_drain report (did the drain above reach head?) and
    # the existing grain/as-of guard — nothing runs between them, so they cannot disagree (#22).
    table_projection_lagged = projection_lag(conn, "overlay") > 0
    # The drain above RAN in both states (lagged = it stopped short of head), so the stage
    # carries its start instant either way (#13 gap A).
    record_stage(stage_recorder, "projection_drain",
                 "lagged" if table_projection_lagged else "succeeded",
                 reason_code="projection_lag" if table_projection_lagged else None,
                 started_at=stage_started)
    if table_projection_lagged:
        # Under projection lag the overlay_fact_state read model resolve_fact reads is stale: the
        # clear-then-set could wipe a just-declared grain (a not-yet-projected confirm) or persist a
        # should-be-stale one. Skip entirely (mirrors the drift path above); build_graph's declared
        # flags stand and re-project once the projection catches up.
        counters.incr("overlay.table_fact_projection.skipped_projection_lag")
        logger.warning("overlay projection lags after ingest of %r — skipping grain/as-of "
                       "re-projection (re-runs when the projection catches up)", catalog_source)
        record_stage(stage_recorder, "table_fact_projection", "lagged",
                     reason_code="projection_lag")
    else:
        # Savepoint + except: a projection DB fault must never poison the request tx or roll back
        # facts/quarantine (this path must not be able to 500 a flag-off upload).
        stage_started = datetime.now(UTC)
        try:
            with conn.transaction():   # savepoint: a projection fault must not roll back facts
                project_table_facts(conn, source=catalog_source,
                                    tables=sorted({r.table for r in vr.good}),
                                    declared_grain=declared_grain, declared_as_of=declared_as_of,
                                    now=now)
            record_stage(stage_recorder, "table_fact_projection", "succeeded",
                         started_at=stage_started)
        except Exception:  # noqa: BLE001 — advisory: re-projection never fails an upload
            counters.incr("overlay.table_fact_projection.error")
            logger.warning("advisory grain/as-of re-projection failed for %r — facts intact",
                           catalog_source, exc_info=True)
            record_stage(stage_recorder, "table_fact_projection", "failed",
                         reason_code="exception", started_at=stage_started)

    # approved_join re-projection (Pass C Task 10, closing Task 8's loop): build_graph wiped EVERY
    # edge for this source, so a join VERIFIED in a PRIOR cycle must be re-projected from its FACT
    # (enumerated off the overlay substrate — NEVER the Pass-C ledger, which this cycle just
    # cleared, and never graph_edge itself). UNCONDITIONAL (not flag-gated), and byte-for-byte safe
    # when flag-off: the projector is DECLARED-SPARE (it only deletes/demotes fact-linked edges)
    # and a pure-declared catalog has zero approved_join facts, so it enumerates nothing and writes
    # nothing. Same projection-lag guard as the grain/as-of block above: resolve_fact reads the
    # overlay_fact_state read model, and a lagging model could serve a stale status — skip and let
    # the next caught-up ingest re-project (the async demotion hook covers reject/expiry latency).
    if projection_lag(conn, "overlay") > 0:
        counters.incr("overlay.passc.join_projection.skipped_projection_lag")
        logger.warning("overlay projection lags after ingest of %r — skipping approved-join "
                       "re-projection (re-runs when the projection catches up)", catalog_source)
        record_stage(stage_recorder, "join_projection", "lagged", reason_code="projection_lag")
    else:
        stage_started = datetime.now(UTC)
        try:
            with conn.transaction():   # savepoint: a projection fault must not roll back facts
                project_confirmed_joins(conn, source=catalog_source,
                                        pairs=list_approved_join_refs(conn, catalog_source))
            record_stage(stage_recorder, "join_projection", "succeeded",
                         started_at=stage_started)
        except Exception:  # noqa: BLE001 — advisory: join re-projection never fails an upload
            counters.incr("overlay.passc.join_projection.error")
            logger.warning("advisory approved-join re-projection failed for %r — facts intact",
                           catalog_source, exc_info=True)
            record_stage(stage_recorder, "join_projection", "failed", reason_code="exception",
                         started_at=stage_started)

    if governed_joins_enabled():
        # Governed-join DRIFT detection (advisory): a re-upload that RETARGETS or DROPS a joins_to
        # humans VERIFIED is surfaced as a governed_join_divergence row — NEVER a state change on
        # the fact/edge (no auto-demote; the old join stays operational until a human acts). MUST
        # run here, after the approved-join re-projection above: build_graph wiped every edge
        # mid-ingest, and only project_confirmed_joins restored the source's VERIFIED operational
        # edges the detector diffs against (under projection lag the block above skipped, the
        # detector sees zero VERIFIED joins and no-ops — it re-detects on the next caught-up
        # ingest). OWN savepoint + except (the _propose_governed_joins pattern): a DB-class fault
        # inside must never poison the request tx and roll back Pass A facts + the graph — the
        # detection degrades to a warning and the upload always ingests. Flag-off byte-for-byte:
        # this whole block is behind governed_joins_enabled().
        stage_started = datetime.now(UTC)
        try:
            with conn.transaction():
                detect_governed_join_divergences(conn, catalog_source, vr.good,
                                                 source_snapshot_id=snapshot_id, now=now)
            record_stage(stage_recorder, "join_drift", "succeeded", started_at=stage_started)
        except Exception:  # noqa: BLE001 — advisory: drift detection never fails an upload
            counters.incr("overlay.join_drift.error")
            logger.warning("advisory governed-join drift detection failed for %r — facts + graph "
                           "intact", catalog_source, exc_info=True)
            record_stage(stage_recorder, "join_drift", "failed", reason_code="exception",
                         started_at=stage_started)
    else:
        record_stage(stage_recorder, "join_drift", "disabled")

    stage_started = datetime.now(UTC)
    persist_quarantine(conn, catalog_source, vr.quarantined)
    record_stage(stage_recorder, "quarantine", "succeeded", detail={"rows": len(vr.quarantined)},
                 started_at=stage_started)
    flagged = (f"first upload of '{catalog_source}' ({len(vr.good)} objects) — review recommended"
               if brake.is_first_upload else None)
    # MF-5 truthful counts, computed from data already in scope. tables/columns come from the
    # VALIDATED rows (one graph node each -> objects_stored; one `contains` edge per column ->
    # containment_edges). Pass B abstained = a synthesis with NO grain AND NO as-of (matches
    # `_enrichment_outcome`); proposed = the rest. `syntheses` is {} unless Pass B ran.
    tables = len({r.table for r in vr.good})
    columns = len(vr.good)
    passb_abstained = sum(1 for syn in syntheses.values()
                          if syn.get("grain") is None and syn.get("availability_time") is None)
    passb_proposed = len(syntheses) - passb_abstained
    return IngestResult("ingested", None, asserted, changed_objects, len(vr.quarantined), flagged,
                        objects_stored=tables + columns, tables=tables, columns=columns,
                        containment_edges=columns, facts_asserted=asserted,
                        join_candidates=join_candidate_count, passb_proposed=passb_proposed,
                        passb_abstained=passb_abstained)


def _bool(v) -> bool:
    return v is True or (isinstance(v, str) and v.strip().lower() in ("true", "1", "yes"))


def _row_from_raw(raw: dict, catalog_source: str) -> CanonicalRow:
    """Rebuild a CanonicalRow from a quarantine `raw` dict merged with the reviewer's edits."""
    def s(k: str) -> str:
        return str(raw.get(k) or "")
    return CanonicalRow(
        source=s("source") or catalog_source, table=s("table"), column=s("column"), type=s("type"),
        is_grain=_bool(raw.get("is_grain")), as_of=_bool(raw.get("as_of")),
        as_of_basis=s("as_of_basis"), definition=s("definition"), sensitivity=s("sensitivity"),
        joins_to=s("joins_to"), cardinality=s("cardinality"), additivity=s("additivity"),
        unit=s("unit"), currency=s("currency"), entity=s("entity"))


def resolve_quarantine_row(conn, catalog_source: str, row_index: int, edits: dict, *,
                           actor, now: datetime | None = None) -> tuple[bool, str]:
    """Apply a reviewer's inline fix to a quarantined row: merge the edits onto the raw row, RE-RUN the
    real deterministic validation (validate_rows — never the client mock), and, if it now passes, its
    column isn't already in the catalog, and the cumulative resolved additions don't trip the
    source-level large-change brake (#4 — resolution is an ingestion path), add it to the source
    graph + reconcile its table's grain / point-in-time facts and drop it from the queue.
    Returns (resolved, reason).

    LIMITS (holds until the source is re-uploaded — the file stays the source of truth): a resolved
    column is added incrementally, so it is NOT recorded in the drift snapshot and a subsequent
    re-upload of the still-broken file rebuilds the graph WITHOUT it (the resolution is superseded).
    Fix the source file for durability."""
    row = conn.execute(
        "SELECT raw FROM quarantine_row WHERE catalog_source = %s AND row_index = %s",
        (catalog_source, row_index)).fetchone()
    if row is None:
        return False, "no such quarantined row"
    # A1 resolution #9: an FTR-adapter-quarantined row cannot be repaired inline — a resolved
    # CanonicalRow cannot reconstruct the glossary sidecar (schema, term_type, taxonomy, safe
    # facets, per-field evidence), so resolving it would graph a column stripped of exactly the
    # semantics the FTR upload exists to carry. Refuse BEFORE any validation/mutation; the durable
    # fix is re-uploading the corrected file (adapter-aware repair is future work).
    if row[0].get("_adapter") == "ftr":
        return False, ("This row came from an FTR glossary upload and cannot be fixed inline — "
                       "re-upload the corrected FTR file.")
    merged = {**row[0], **(edits or {})}
    vr = validate_rows([_row_from_raw(merged, catalog_source)], catalog_source)
    if vr.structural_error or vr.quarantined:
        return False, vr.structural_error or vr.quarantined[0].message   # still invalid — surface why
    good = vr.good[0]
    # #7: the SAME ref seam the main ingest path graphs under — `good` is already identity-normalized
    # by validate_rows (#1), so a case/space variant of an existing column resolves to that column's
    # ref and is refused below rather than re-added as a raw-cased twin node.
    c_ref = _column_ref(good.table, good.column)
    if conn.execute("SELECT 1 FROM graph_node WHERE catalog_source = %s AND object_ref = %s",
                    (catalog_source, c_ref)).fetchone() is not None:
        return False, f"{good.table}.{good.column} is already in the catalog"
    # Round-3 #4 (dismiss-proof): the live-sibling check below only inspects rows STILL in
    # quarantine_row, and dismiss_quarantine_row hard-DELETEs — so dismissing the pii-tagged member
    # of a conflict pair (as an apparent duplicate) and then resolving the untagged one found no
    # sibling and graphed a WORLD-READABLE node for a declared-PII column. Each conflict-quarantined
    # row therefore carries its OWN floor (validate_rows stamps the tags declared across the
    # conflicting duplicates; persist_quarantine stores it in `raw` as sensitivity_conflict_floor),
    # read from the STORED record — never from `merged` — so reviewer edits cannot strip it. Same
    # covering rule as the sibling check: '' sits below every tag and the tags are mutually
    # unordered role gates, so any recorded tag the resolution doesn't match refuses (fail-closed
    # MOST_RESTRICTIVE — a resolve can NEVER graph a node below the column's ever-declared floor).
    own_floor = sorted(
        {str(t).strip() for t in (row[0].get("sensitivity_conflict_floor") or ())}
        - {"", good.sensitivity})
    if own_floor:
        return False, (
            f"sensitivity conflict: {good.table}.{good.column} was declared with sensitivity "
            f"{', '.join(repr(t) for t in own_floor)} by a conflicting duplicate — recorded on "
            f"this row's own quarantine record, so dismissing the tagged row does not lift it; "
            f"resolving as '{good.sensitivity or 'untagged'}' would weaken the column's effective "
            "sensitivity — match the declared tag instead")
    # Round-3 #4: the whole-upload sensitivity-conflict invariant survives resolution. validate_rows
    # quarantines BOTH members when one (table, column) appears with disagreeing metadata (a 'pii'
    # copy + an untagged one) precisely so the untagged copy can't graph a world-readable node for a
    # PII column — but re-validating the ONE edited row in isolation forgot those siblings, and
    # resolving the untagged member alone re-opened exactly that leak. Mirror the validate_rows
    # conflict key (the same `_norm` its dedup uses; `good` is already normalized) against the OTHER
    # still-quarantined rows of this source, and refuse a resolution whose sensitivity fails to
    # cover a sibling's declared tag: '' sits below every tag, and 'pii'/'restricted' are distinct
    # read-scope role gates with no order between them, so ANY differing non-empty sibling tag
    # refuses (fail-closed MOST_RESTRICTIVE — a resolve can never lower the column's effective
    # sensitivity). Resolving the strictest member first still succeeds; the leftover siblings then
    # hit the already-in-the-catalog refusal above, so they can never weaken the node either (the
    # existing-node side of the invariant). Kept as defense in depth beside the own-record floor
    # above: it also covers rows queued BEFORE the floor was recorded (pre-migration quarantine).
    sibling_tags = sorted({
        str(raw.get("sensitivity") or "").strip()
        for (raw,) in conn.execute(
            "SELECT raw FROM quarantine_row WHERE catalog_source = %s AND row_index <> %s",
            (catalog_source, row_index)).fetchall()
        if _norm(str(raw.get("table") or "")) == good.table
        and _norm(str(raw.get("column") or "")) == good.column
    } - {"", good.sensitivity})
    if sibling_tags:
        return False, (
            f"sensitivity conflict: still-quarantined row(s) for {good.table}.{good.column} "
            f"declare sensitivity {', '.join(repr(t) for t in sibling_tags)}; resolving this row "
            f"as '{good.sensitivity or 'untagged'}' would weaken the column's effective "
            "sensitivity — resolve the tagged row (or match its tag) instead")
    # #17: a table asserts ONE availability basis. validate_rows quarantined ALL of a table's as_of
    # rows when 2+ were declared; resolving the FIRST picks the basis explicitly, but resolving a
    # SECOND as_of column would silently re-assert availability_time onto it (the same
    # last-writer-wins the validation fix removed, via the resolve path). Refuse it loudly — the
    # reviewer edits as_of off this row (or dismisses it) instead of flipping the chosen basis.
    if good.as_of:
        other = conn.execute(
            "SELECT column_name FROM graph_node WHERE catalog_source = %s AND table_name = %s "
            "AND kind = 'column' AND is_as_of = true AND column_name <> %s",
            (catalog_source, good.table, good.column)).fetchone()
        if other is not None:
            return False, (
                f"as_of conflict: {good.table} already has availability basis column "
                f"'{other[0]}', and a table has ONE availability basis — edit as_of off this "
                f"row (or dismiss it) rather than silently flipping the basis")
    # #4: resolution is an INGESTION path, so it takes the same source-level large-change brake an
    # upload does. Cumulative: every object added by resolution since the last successful upload
    # (graph minus the drift snapshot) counts alongside this row's, so an all-quarantined
    # wrong-source upload cannot be laundered into the catalog one resolved row at a time.
    brake = resolution_brake(
        conn, catalog_source, set(UploadCatalog(catalog_source, [good]).fingerprint().keys()))
    if brake.held:
        logger.warning("quarantine resolution for %r held by the large-change brake: %s",
                       catalog_source, brake.reason)
        return False, f"held by the large-change brake: {brake.reason}"
    # Round-3 #5: stamp the node with its OWN attestation instant (this resolution, `now`). Search
    # freshness is otherwise SOURCE-level (the drift watermark), and this row was never part of any
    # scan/snapshot — inheriting the watermark would present it as "fresh" under a scan that never
    # saw it, re-blessed by every later scan of the OTHER rows. With its own stamp it is fresh for
    # the SLA window after the human verified it, then honestly stale until a real re-upload of the
    # fixed file rebuilds the graph (which supersedes the resolution — see LIMITS above). The source
    # watermark itself is NEVER advanced here.
    add_column_row(conn, catalog_source, good, attested_at=now or datetime.now(UTC))
    if good.is_grain:
        # reconcile the table's grain fact with its FULL grain-column set (now incl. the added column),
        # or the uniqueness key stays silently wrong (a grain column added to the graph but not the fact).
        grain_cols = [r[0] for r in conn.execute(
            "SELECT column_name FROM graph_node WHERE catalog_source = %s AND table_name = %s "
            "AND kind = 'column' AND is_grain = true ORDER BY column_name",
            (catalog_source, good.table)).fetchall()]
        _assert_fact(conn, catalog_source, good.table, "grain",
                     {"columns": grain_cols, "is_unique": True}, actor=actor,
                     origin_type="resolution")
    if good.as_of:
        basis = good.as_of_basis if good.as_of_basis in ("posted_at", "ingested_at") else "posted_at"
        _assert_fact(conn, catalog_source, good.table, "availability_time",
                     {"column": good.column, "basis": basis}, actor=actor,
                     origin_type="resolution")
    conn.execute("DELETE FROM quarantine_row WHERE catalog_source = %s AND row_index = %s",
                 (catalog_source, row_index))
    return True, ""


def dismiss_quarantine_row(conn, catalog_source: str, row_index: int) -> bool:
    """Durably drop a quarantined row from the queue (holds until the source is re-uploaded)."""
    row = conn.execute(
        "DELETE FROM quarantine_row WHERE catalog_source = %s AND row_index = %s RETURNING row_index",
        (catalog_source, row_index)).fetchone()
    return row is not None
