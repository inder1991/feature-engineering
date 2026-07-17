from __future__ import annotations

import hashlib
import json
import logging

from featuregen.intake.llm import LLMClient
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_evidence import (
    field_input_hash,
    read_active_field_evidence,
    record_field_evidence,
    stale_source_evidence,
)
from featuregen.overlay.object_identity import ObjectBinding, may_attach
from featuregen.overlay.upload import enrich_config
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.concepts import (
    UNCLASSIFIED,
    classification_vocabulary,
    is_known_concept,
)
from featuregen.overlay.upload.enrich_batch import BatchItem, run_batched
from featuregen.overlay.upload.enrich_llm import ENRICHMENT_RUN_ID, audited_enrich_call
from featuregen.overlay.upload.glossary_reader import GlossaryRecord, GlossaryUpload
from featuregen.overlay.upload.object_ref import normalize_ref, parse_ref
from featuregen.overlay.upload.sample_parser import strip_sample_values

logger = logging.getLogger(__name__)

_TASK = "overlay.enrich.concept"

# Cap on any single glossary-sidecar metadata value placed in an LLM request. Matches the per-value
# bound the metadata-only egress filter (`enrich_llm._item_egress_ok`) enforces, so a long business
# definition is trimmed to its leading meaning rather than silently excluding the whole column.
_MAX_META_LEN = 200
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


def concept_cache_key(row: CanonicalRow, rec: GlossaryRecord | None) -> str:
    """Concept CACHE key (#3). ``content_hash`` stays the DOWNSTREAM dict key (graph/ingest look
    concepts up by it — unchanged), but as a cache key it is sidecar-blind: the classifier ALSO
    receives the glossary metadata (``_concept_metadata`` — term, declared SQL type, domain,
    synonyms, BIAN/FIBO paths), so a re-upload that CORRECTS any of those while keeping the same
    definition would hit the stale entry. This key hashes the FULL classifier input instead —
    the canonical ``_concept_metadata`` payload (sorted keys; ``rec=None`` for a technical CSV
    yields the base names/types payload) plus the source (M5/M6: one source's entry is never
    reused for another's same-named column) and the prompt/schema/vocabulary identity
    (``_CONCEPT_CACHE_VERSION``) — so a corrected sidecar re-classifies and an unchanged
    re-upload still hits."""
    raw = json.dumps([row.source, _CONCEPT_CACHE_VERSION, _concept_metadata(row, rec)],
                     sort_keys=True)
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


def _norm(value: str) -> str:
    """Strip + lower-case, matching ``object_ref._norm`` — so a row's (table, column) matches a
    glossary record's normalized identity regardless of the source's casing."""
    return value.strip().lower()


def _records_by_tc(glossary: GlossaryUpload) -> dict[tuple[str, str], GlossaryRecord]:
    """Index a glossary's COLUMN sidecars by normalized (table, column). The flat ``CanonicalRow`` is
    schema-dropped (``public``-scoped), while ``GlossaryRecord.logical_ref`` is schema-PRESERVING, so
    a row cannot join a record on the ref string; (table, column) is the stable bridge. Table-level
    terms (no column) carry no per-column concept and are excluded."""
    out: dict[tuple[str, str], GlossaryRecord] = {}
    for rec in glossary.records:
        if rec.is_table:
            continue
        try:
            _source, _schema, table, column = parse_ref(rec.logical_ref)
        except ValueError:
            continue
        if column is None:
            continue
        out[(table, column)] = rec   # ref components are already normalized (lower-cased)
    return out


def _concept_metadata(row: CanonicalRow, rec: GlossaryRecord | None) -> dict:
    """The metadata-only concept-enrichment input for a column. Always names/types (M4: NEVER the
    uploader's free-text definition on a technical row). For a GLOSSARY column (``rec`` present) it
    ALSO carries the business-semantic sidecar — term, business definition, synonyms/aliases, data
    domain, BIAN/FIBO paths — so the classifier reasons over meaning, not just the physical name.
    Free-text values are bounded to ``_MAX_META_LEN`` to stay within the metadata-only egress filter."""
    meta: dict = {"table": row.table, "column": row.column, "type": row.type}
    if rec is not None:
        # R5-5: the FTR adapter keeps the OPERATIONAL row type UNKNOWN_TYPE (a business glossary is
        # not the physical-type authority), but the file's DECLARED SQL type is real classifier
        # signal. It rides the already-allowlisted `type` key — a bounded structural token the
        # adapter validated (`^[a-z0-9 _()]+$`, ≤64), never free text — so the classifier sees
        # "varchar"/"double", not the useless operational "unknown". CanonicalRow.type is untouched.
        if rec.declared_type:
            meta["type"] = rec.declared_type[:_MAX_META_LEN]
        # `business_definition` (NOT `definition`) is deliberate: the plain `definition` key stays
        # forbidden by the egress filter so a technical upload's free text can never egress (M4); the
        # curated glossary definition rides through under this distinct, unambiguous key.
        #
        # DATA-LEAK BACKSTOP (whole-branch review CRITICAL): an FTR business definition EMBEDS raw
        # customer sample VALUES in prose ("...representative values such as 3708484836801; 15:07:08
        # ..."). `strip_sample_values` EXCISES that clause before it egresses, so the classifier sees
        # the business meaning but never a raw value (the redaction PII backstop misses most of them).
        for key, val in (("term_name", rec.term_name),
                         ("business_definition", strip_sample_values(rec.definition)),
                         ("data_domain", rec.domain), ("bian_path", rec.bian_path),
                         ("fibo_path", rec.fibo_path)):
            if val:
                meta[key] = val[:_MAX_META_LEN]
        if rec.synonyms:
            meta["synonyms"] = [s[:_MAX_META_LEN] for s in rec.synonyms]
    return meta


def _write_concept_evidence(conn, *, resolved: dict[str, str], by_hash: dict[str, CanonicalRow],
                            meta_by_hash: dict[str, dict],
                            rec_by_tc: dict[tuple[str, str], GlossaryRecord],
                            bindings: dict[str, ObjectBinding] | None,
                            source_snapshot_id: str) -> int:
    """Write one ``field_evidence`` proposal per glossary column classified THIS run (spec §5.1),
    ROUTED THROUGH producer-scoped staleness + snapshot reuse (whole-branch review Important-2 — the
    LLM producer must not bypass the machinery every other producer goes through).

    ``producer=LLM`` / ``strength=PROPOSED``; ``producer_ref`` is the enrichment run bucket (ties the
    proposal to its immutable llm_call records), ``producer_item_ref`` the batch item ref (content
    hash), ``producer_configuration_hash`` the vocabulary fingerprint. C3: an ``unclassified`` (or any
    non-known) value is NOT a proposal — no evidence. Only ATTACHABLE columns (``may_attach`` on the
    Task-2 binding, when supplied) get evidence.

    Producer-scoped staleness (mirrors ``ingest._write_producer_field``): before writing, the LLM's OWN
    prior ACTIVE ``concept`` rows whose ``input_hash`` differs from this run's are STALED (a reclassifying
    re-upload supersedes the old row instead of accumulating a second live one -> no resolver
    ``_CONFLICT`` NULLing the concept); an UNCHANGED input (same ``input_hash`` already ACTIVE) is REUSED,
    not re-written. NEVER touches another producer's rows. Fail-soft + txn-safe: each item is savepointed
    so a single failure logs and is contained, never aborting enrichment or poisoning the caller's txn.

    Returns the number of CONTAINED per-item write failures so the caller's stage report (#22) can
    say ``partial`` — the swallowed except below must never be laundered into an outer success."""
    failures = 0
    for h, concept in resolved.items():
        if concept == UNCLASSIFIED or not is_known_concept(concept):
            continue   # C3: unclassified / invalid is not a proposal
        row = by_hash[h]
        rec = rec_by_tc.get((_norm(row.table), _norm(row.column)))
        if rec is None:
            continue   # not a glossary column term — no schema-preserving identity to key on
        if bindings is not None:
            binding = bindings.get(normalize_ref(row.source, None, row.table, row.column))
            if binding is None or not may_attach(binding):
                continue   # attachable columns only
        material = meta_by_hash.get(h, {"table": row.table, "column": row.column, "type": row.type})
        input_hash = field_input_hash(logical_ref=rec.logical_ref, field_name="concept",
                                      material=material)
        try:
            with conn.transaction():   # savepoint: contain a failed write without poisoning the txn
                # Stale the LLM's own prior ACTIVE concept rows with a DIFFERENT input (a reclassify),
                # keeping any row that matches this run's input (unchanged -> reuse).
                stale_source_evidence(
                    conn, logical_ref=rec.logical_ref, field_name="concept",
                    producer=EvidenceProducer.LLM, keep_input_hash=input_hash)
                reused = any(
                    e.producer == EvidenceProducer.LLM.value and e.input_hash == input_hash
                    for e in read_active_field_evidence(conn, rec.logical_ref, "concept"))
                if not reused:
                    record_field_evidence(
                        conn, logical_ref=rec.logical_ref, field_name="concept",
                        proposed_value=concept, producer=EvidenceProducer.LLM,
                        strength=AssertionStrength.PROPOSED, producer_ref=ENRICHMENT_RUN_ID,
                        producer_item_ref=h, producer_configuration_hash=_vocab_fingerprint(),
                        source_snapshot_id=source_snapshot_id, input_hash=input_hash)
        except Exception:  # noqa: BLE001 — advisory: an evidence-write failure never aborts enrichment
            failures += 1
            logger.warning("advisory concept field_evidence write failed for %s", rec.logical_ref,
                           exc_info=True)
    return failures


def enrich_concepts(conn, rows: list[CanonicalRow], client: LLMClient, actor=None, *,
                    glossary: GlossaryUpload | None = None,
                    bindings: dict[str, ObjectBinding] | None = None,
                    source_snapshot_id: str | None = None,
                    stats: dict | None = None) -> dict[str, str]:
    """Classify each column into a controlled concept; returns {content_hash: concept} (unchanged).

    Glossary carry-forward (guarded — non-glossary uploads are UNCHANGED): when ``glossary`` is given,
    each glossary column's concept input ALSO carries its business-semantic sidecar (see
    ``_concept_metadata``), and — in BOTH single and batch modes (Important-2: single is the default)
    — each newly-classified attachable glossary column writes an item-level ``concept``
    ``field_evidence`` proposal through producer-scoped staleness (see ``_write_concept_evidence``).
    ``source_snapshot_id`` is required to write evidence (a NOT-NULL column); absent it, enrichment
    still runs and returns concepts, just without the evidence side-effect.

    ``stats`` (#22, optional out-param — the return shape is unchanged): when given, receives
    ``evidence_write_failures``, the count of per-item evidence-write failures the stage CONTAINED
    internally, so the caller's stage report can say ``partial`` instead of a laundered success."""
    by_hash: dict[str, CanonicalRow] = {content_hash(r): r for r in rows}
    rec_by_tc = _records_by_tc(glossary) if glossary is not None else {}

    def _rec_of(row: CanonicalRow) -> GlossaryRecord | None:
        return rec_by_tc.get((_norm(row.table), _norm(row.column)))

    # #3: the cache is read/written by ``concept_cache_key`` (the FULL classifier input — glossary
    # sidecar included), NOT by ``content_hash``; the RETURNED dict stays keyed by content_hash for
    # downstream (graph/ingest — unchanged). Mirrors ``draft_definitions``'s ``key_of`` seam.
    key_of = {h: concept_cache_key(r, _rec_of(r)) for h, r in by_hash.items()}
    cached = _cache_get(conn, "enrichment_concept", list(key_of.values()), _CONCEPT_CACHE_VERSION)
    result = {h: cached[key_of[h]] for h in by_hash if key_of[h] in cached}

    # Metadata for every cache-MISS row — the LLM input AND (glossary) the evidence input material.
    meta_by_hash = {h: _concept_metadata(r, _rec_of(r)) for h, r in by_hash.items()
                    if h not in result}
    resolved: dict[str, str] = {}   # {content_hash: concept} classified THIS run (the evidence set)

    if enrich_config.mode("concept") == "batch":
        misses = [BatchItem(h, meta_by_hash[h]) for h in meta_by_hash]
        resolved = run_batched(
            conn, client, short="concept", task=_TASK, prompt_id="overlay_concept_batch_v1",
            schema_id="overlay_concept_batch",
            shared_metadata={"vocabulary": _CONCEPT_VOCABULARY}, items=misses, out_key="concept",
            instruction="For each item classify the column into the provided controlled concept "
                        "vocabulary — choose the single best-fitting concept name, or 'unclassified' "
                        "if none fits. Return exactly one result per input ref; treat each item "
                        "independently.", accept=_accept_concept, actor=actor)
        for h, concept in resolved.items():
            _cache_put(conn, "enrichment_concept", key_of[h], concept, _CONCEPT_CACHE_VERSION)
            result[h] = concept
    else:
        for h in meta_by_hash:                            # single mode — today's exact behaviour
            # Metadata only (names/types + the glossary sidecar for a glossary column) — NEVER the
            # uploader's free-text definition on a technical row (M4 egress risk).
            raw = _call(conn, client, _TASK, "overlay_concept_v1", "overlay_concept",
                        {**meta_by_hash[h], "vocabulary": _CONCEPT_VOCABULARY}, "concept",
                        "Classify this column into the provided controlled concept vocabulary — choose "
                        "the single best-fitting concept name, or 'unclassified' if none fits.", actor)
            if raw is None:
                continue   # failure/empty -> don't cache; retry next ingest (M3)
            # #22: only a REAL classification is durable — a known concept, or the literal
            # 'unclassified' (a legitimate "none fits" verdict). An UNKNOWN/off-vocabulary response
            # is still coerced to UNCLASSIFIED for THIS run (today's return behaviour) but is NOT
            # cached: caching the coercion would poison the cache permanently on a transient bad
            # response, where batch mode rejects unknowns for retry (_accept_concept).
            classified = raw == UNCLASSIFIED or is_known_concept(raw)
            concept = raw if classified else UNCLASSIFIED
            if classified:
                _cache_put(conn, "enrichment_concept", key_of[h], concept, _CONCEPT_CACHE_VERSION)
            result[h] = concept
            resolved[h] = concept

    # Item-level LLM concept evidence (glossary only) — written in BOTH modes now (Important-2).
    if glossary is not None and source_snapshot_id is not None:
        failures = _write_concept_evidence(
            conn, resolved=resolved, by_hash=by_hash, meta_by_hash=meta_by_hash,
            rec_by_tc=rec_by_tc, bindings=bindings, source_snapshot_id=source_snapshot_id)
        if stats is not None:
            stats["evidence_write_failures"] = failures
    return result


def suppressed_definition_hashes(rows: list[CanonicalRow],
                                 glossary: GlossaryUpload | None) -> set[str]:
    """Content hashes of rows whose BLANK definition is sanitizer-SUPPRESSED (R5-3): the uploader
    DECLARED one, but the adapter blanked it fail-closed (``GlossaryRecord.definition_suppressed``).
    Suppressed is NOT missing — LLM-drafting over it would land generated text in the graph with no
    governance decision, so these rows stay empty pending review. Shared by ``draft_definitions``
    (the skip) and ingest's ``enrich_definition`` stage report (the honest expected count)."""
    if glossary is None:
        return set()
    rec_by_tc = _records_by_tc(glossary)
    out: set[str] = set()
    for r in rows:
        if r.definition:
            continue
        rec = rec_by_tc.get((_norm(r.table), _norm(r.column)))
        if rec is not None and rec.definition_suppressed:
            out.add(content_hash(r))
    return out


def draft_definitions(conn, rows: list[CanonicalRow], client: LLMClient, actor=None,
                      *, concepts: dict[str, str] | None = None,
                      glossary: GlossaryUpload | None = None) -> dict[str, str]:
    """Draft a definition ONLY for columns with no declared one (R3). Keyed by (content_hash,
    assigned concept) so a concept change re-drafts (spec C6). Returns {content_hash: definition}.

    R5-3 (``glossary`` given): a sanitizer-SUPPRESSED blank (``definition_suppressed`` on the
    sidecar) is skipped — suppressed is not missing; it stays empty pending review, never silently
    LLM-drafted. Non-glossary callers are byte-for-byte unchanged."""
    concepts = concepts or {}
    blank = {content_hash(r): r for r in rows if not r.definition}
    for h in suppressed_definition_hashes(rows, glossary):
        blank.pop(h, None)   # R5-3: suppressed ≠ missing — never silently LLM-drafted
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
