from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable

from featuregen.intake.llm import LLMClient
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_decision import (
    FieldDecisionEventType,
    read_field_decisions,
    record_field_decision,
)
from featuregen.overlay.field_evidence import (
    canonical_hash,
    field_input_hash,
    read_active_field_evidence,
    record_field_evidence,
    stale_source_evidence,
)
from featuregen.overlay.object_identity import ObjectBinding, may_attach
from featuregen.overlay.upload import enrich_config
from featuregen.overlay.upload.attest.concept_critic import (
    ConceptCriticItemV1,
    ConceptDisposition,
    critique_concept_batch,
)
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.concepts import (
    UNCLASSIFIED,
    classification_vocabulary,
    is_known_concept,
)
from featuregen.overlay.upload.concepts import concept as concept_record
from featuregen.overlay.upload.dispatch_audit import DispatchAuditContext
from featuregen.overlay.upload.enrich_batch import BatchItem, run_batched
from featuregen.overlay.upload.enrich_llm import (
    ENRICHMENT_RUN_ID,
    MAX_DEFINITION_LEN,
    audited_enrich_call,
)
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
_SYN_TASK = "overlay.enrich.synonyms"
_UNIT_TASK = "overlay.enrich.unit"
_SUMMARY_TASK = "overlay.enrich.summary"

# Cap on ONE column's drafted synonym list — a short comma-separated line of aliases, not prose.
_MAX_SYNONYMS_LEN = 200

# Larger bound for a SANITIZED business definition specifically. The 200-char default cut every real
# definition mid-sentence; sanitized definitions are the intended metadata payload, so allow a bigger
# but still-bounded window with word-boundary truncation. Second boundary remains the batch token budget.
# DRY: the value is the single `MAX_DEFINITION_LEN` shared with the egress cap (`enrich_llm`) and Pass
# B's descriptor bound (`table_synth`); `_MAX_DEFINITION_LEN` stays as the historical private alias.
_MAX_DEFINITION_LEN = MAX_DEFINITION_LEN


def bounded_definition(text: str, limit: int) -> str:
    """Trim `text` to <= `limit` chars on a word boundary (prefer a sentence end within the window)."""
    if len(text) <= limit:
        return text
    window = text[:limit]
    cut = window.rfind(". ")
    if cut >= limit // 2:          # a sentence break in the back half → keep whole sentences
        return window[:cut + 1]
    sp = window.rfind(" ")
    return window[:sp] if sp > 0 else window

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
# v2 (#3): the concept cache key moved from `content_hash` to `concept_cache_key` (full classifier
# metadata — term/declared type/domain/synonyms/BIAN/FIBO — not just the sidecar-blind definition).
# The version bump versions pre-existing v1 rows out explicitly (intentional one-time re-key) rather
# than leaving them orphaned under a same-version key.
_CONCEPT_CACHE_VERSION = f"concept:v2:{_vocab_fingerprint()}"
_DEFINITION_CACHE_VERSION = "definition:v1"
# The summary is written FROM the metadata, so the key folds in the metadata it was written from —
# an enriched payload (a new source_attributes column, a corrected term_name) must re-draft rather
# than serve a summary written from less.
_SUMMARY_CACHE_VERSION = "summary:v1"
# v2 (E1a T3): a domain result is no longer a bare table-domain string but the TWO-LEVEL envelope
# (`_accept_domain_result`) — the table default plus the column overrides. The bump versions the v1
# bare-string rows out rather than leaving them to decode as a domain with no overrides.
_DOMAIN_CACHE_VERSION = "domain:v2"


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
    "enrichment_summary": "summary",
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
          catalog_metadata: dict, out_key: str, instruction: str, actor,
          dispatch_audit: DispatchAuditContext | None = None,
          cacheable_metadata_keys: tuple[str, ...] = ()) -> str | None:
    """Run one GOVERNED enrichment call (attached schema, reserved keys, egress guard, audit record —
    so a real provider works and PII can't leak). Returns None on any failure/empty so a transient
    failure never poisons the cache (M3). ``dispatch_audit`` (C5-T5) threads the ingestion-run
    attribution context to the single seam; ``None`` is byte-identical. ``cacheable_metadata_keys``
    (vocab-caching) marks a large static shared prefix (the concept vocabulary) so the adapter caches
    it instead of re-billing it per call; ``()`` (definition/domain) is byte-identical."""
    return audited_enrich_call(
        conn, client, task=task, prompt_id=prompt_id, schema_id=schema_id,
        catalog_metadata=catalog_metadata, out_key=out_key, instruction=instruction, actor=actor,
        dispatch_audit=dispatch_audit, cacheable_metadata_keys=cacheable_metadata_keys)


def _column_subject(row: CanonicalRow) -> dict:
    """One ``llm_dispatch_subject`` mapping (C5-T5) for the COLUMN a Pass A item enriches: the
    upload's schema-less evidence identity (``normalize_ref`` public-flattens, matching the graph),
    with ``object_ref`` the source-local path (the ``graph_node.object_ref`` convention) and
    ``field_names`` the one column this item is about. Attribution strings only — never row data."""
    logical_ref = normalize_ref(row.source, None, row.table, row.column)
    return {"catalog_source": row.source, "object_ref": logical_ref.split("::", 1)[1],
            "logical_ref": logical_ref, "field_names": [row.column]}


def _table_subject(source: str, table: str, columns: list[str]) -> dict:
    """The TABLE-grain subject (C5-T5) for a per-table enrichment item (domain classification):
    ``field_names`` lists the column names the request carries for that table."""
    logical_ref = normalize_ref(source, None, table)
    return {"catalog_source": source, "object_ref": logical_ref.split("::", 1)[1],
            "logical_ref": logical_ref, "field_names": sorted(columns)}


def _single_ctx(ingestion_run_id: str | None, stage: str,
                subject: dict) -> DispatchAuditContext | None:
    """The per-call context for a SINGLE-mode enrichment call: this one subject under the run +
    stage. ``ingestion_run_id=None`` (a direct call with no run) yields ``None`` — no attribution,
    byte-for-byte today's behavior."""
    if ingestion_run_id is None:
        return None
    return DispatchAuditContext(ingestion_run_id=ingestion_run_id, stage=stage,
                                subjects=(subject,))


def _bounded(val: str | None, max_len: int) -> str | None:
    """Accept a plausible short single-line label/definition; reject empty, over-long, multiline,
    or list-stringified (`['a','b']`) LLM output (M9). Returns None to skip caching."""
    if not val or len(val) > max_len or "\n" in val or val.startswith("["):
        return None
    return val


def _accept_concept(raw: str) -> tuple[str | None, str]:
    """The concept response contract (spec C3), shared by BOTH batch and single mode (#5 — a single
    off-vocabulary response must not be coerced/counted resolved just because it took the un-batched
    path): the literal 'unclassified' is a real classification and IS cached; a known concept is
    cached; anything else is invalid -> NOT cached, NOT counted resolved (retried next ingest)."""
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


# Internal task/namespace identifiers the enrichment machinery uses. A domain response that ECHOES
# one of these — or is shaped like one — is prompt-echo garbage, not a business domain (07-17: the
# model returned its own task name and 'overlay.enrich.domain' was durably cached as a domain).
_INTERNAL_TASKS = frozenset({_TASK, _DEF_TASK, _DOMAIN_TASK})
_INTERNAL_NAMESPACE_RE = re.compile(r"^overlay[._]")
_DOTTED_ID_RE = re.compile(r"[a-z0-9]+(?:\.[a-z0-9]+)+")


def _is_task_echo(value: str) -> bool:
    """True if ``value`` is a prompt/task echo or an internal dotted identifier rather than a real
    business domain. Domains are OPEN-vocabulary, so this rejects ONLY our own machinery leaking
    through: an exact internal task id, the reserved ``overlay.``/``overlay_`` namespace, an embedded
    ``overlay.enrich``, or a bare dotted-lowercase token shaped like a task id (no whitespace). A
    legitimate label ('Compliance', 'banking_payments_transactions') matches none of these."""
    low = value.strip().lower()
    if low in _INTERNAL_TASKS:
        return True
    if "overlay.enrich" in low or _INTERNAL_NAMESPACE_RE.match(low):
        return True
    # A dotted-lowercase token that IS the whole (whitespace-free) value — task-id-shaped, not a domain.
    return " " not in low and "\t" not in low and _DOTTED_ID_RE.fullmatch(low) is not None


def _accept_domain(max_len: int):
    """Domain acceptor: a bounded short single-line label (reuses ``_accept_bounded``) that ALSO
    rejects a prompt/task echo or an internal dotted identifier (``_is_task_echo``). A rejected value
    is invalid -> treated as failure -> NOT cached (M3), same as any other reject. Domains stay
    open-vocabulary: no controlled list — only our own task/namespace identifiers are filtered out."""
    bounded = _accept_bounded(max_len)

    def _accept(raw: str) -> tuple[str | None, str]:
        value, reason = bounded(raw)
        if value is None:
            return None, reason
        if _is_task_echo(value):
            return None, "invalid_value"
        return value, "valid"
    return _accept


def _extract_domain_result(entry: dict) -> str:
    """Serialize ONE batch result's TWO-LEVEL domain payload — the table default plus the column
    OVERRIDES — into the single canonical string the batch harness carries per item (the same
    ``extract`` seam Pass B uses for its structured ``synthesis`` object)."""
    return json.dumps({"domain": str(entry.get("domain") or "").strip(),
                       "column_domains": entry.get("column_domains") or []}, sort_keys=True)


def _accept_domain_result(max_len: int):
    """Acceptor for the TWO-LEVEL domain result (E1a T3): the table's DEFAULT domain plus the
    columns whose domain DIFFERS from it, canonicalized into ONE envelope — the single string the
    batch harness caches and returns per item.

    Both seams land on the same shape: BATCH carries the overrides (its per-item schema has them),
    while the SINGLE seam (and the batch's single-item fallback) returns a BARE table-domain string
    from the flat schema, accepted here as the same envelope with NO overrides. An invalid table
    domain rejects the whole item (it is the context everything inherits) — and so does an override
    the gate cannot accept: a rejected label is indistinguishable from a provider blip, and dropping
    it on its own reads downstream as "the model WITHDREW this override" and retires the column's
    prior AI domain (KEEP is the safe default this branch commits to everywhere else, so the item
    resolves as a transient MISS instead). An override EQUAL to the table default is dropped —
    that IS a withdrawal: it is not an override, and writing evidence for it would fabricate a
    column-level assertion for what is really inheritance."""
    accept_label = _accept_domain(max_len)

    def _accept(raw: str) -> tuple[str | None, str]:
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = raw            # a bare label from the flat single/fallback schema
        if not isinstance(payload, dict):
            payload = {"domain": raw}
        table_domain, reason = accept_label(str(payload.get("domain") or "").strip())
        if table_domain is None:
            return None, reason
        proposed = payload.get("column_domains") or []
        if isinstance(proposed, dict):   # already-canonical envelope {column: domain}
            proposed = [{"column": c, "domain": d} for c, d in proposed.items()]
        overrides: dict[str, str] = {}
        for item in proposed:
            if not isinstance(item, dict):
                continue
            column = _norm(str(item.get("column") or ""))
            if not column:
                continue                 # an unnamed override identifies nothing — skip it
            value, reason = accept_label(str(item.get("domain") or "").strip())
            if value is None:
                return None, reason      # unusable label -> the ITEM is a transient miss (KEEP)
            if value != table_domain:
                overrides[column] = value
        return json.dumps({"domain": table_domain, "column_domains": overrides},
                          sort_keys=True), "valid"
    return _accept


def _parse_domain_result(raw: str) -> tuple[str, dict[str, str]]:
    """Decode one canonical domain envelope into ``(table_domain, {column: override})``."""
    try:
        payload = json.loads(raw)
    except ValueError:
        return raw, {}
    if not isinstance(payload, dict):
        return raw, {}
    overrides = payload.get("column_domains")
    return str(payload.get("domain") or ""), overrides if isinstance(overrides, dict) else {}


# ── E4a T2: the MEASURE ANNOTATION accept gate ────────────────────────────────────────────────────
# A unit is a short measurement TOKEN, never prose — "AED", "fils", "%", "bps", "days", "shares",
# "transactions", "USD per share". 32 chars is generous for every real one and far too small for a
# sentence, so a model that starts explaining itself is rejected rather than stored as a unit.
_MAX_UNIT_LEN = 32
# Starts with a letter/digit/% (never punctuation or a list-stringified `['a','b']`) and continues
# with the characters real units use. Deliberately NOT a closed vocabulary: units are open (every
# commodity, rate basis and count noun is one), so a closed list would reject legitimate values
# while buying no safety — the safety comes from the value being EVIDENCE the resolver never reads.
_UNIT_TOKEN_RE = re.compile(r"[A-Za-z0-9%][A-Za-z0-9 %/().,_+-]*")
# currency, by contrast, IS a closed shape: ISO-4217 is exactly three ASCII letters. Anything else
# ("dollars", "AED/USD", "n/a") is not a currency code and is dropped. The value is normalized to
# upper case so `AED`/`aed` are one proposal, not two. A hard closed LIST of the ~180 live codes was
# considered and rejected: it would have to be maintained against ISO revisions and would silently
# drop a legitimate exotic code, while the shape gate already excludes everything that is not
# code-shaped — and a human confirms the value before it can ever become load-bearing (Task 3).
_CURRENCY_CODE_RE = re.compile(r"[A-Za-z]{3}")


def _accept_unit(raw: str) -> tuple[str | None, str]:
    """Accept ONE unit token (bounded, single-line, token-shaped, not a prompt/task echo)."""
    value = _bounded(raw.strip(), _MAX_UNIT_LEN)
    if value is None or not _UNIT_TOKEN_RE.fullmatch(value) or _is_task_echo(value):
        return None, "invalid_value"
    return value, "valid"


def _accept_currency(raw: str) -> str | None:
    """The ISO-4217-SHAPED currency code, upper-cased — or ``None`` for anything else."""
    value = raw.strip()
    return value.upper() if _CURRENCY_CODE_RE.fullmatch(value) else None


def _extract_unit_result(entry: dict) -> str:
    """Serialize ONE batch result's measure annotation (unit + optional currency) into the single
    canonical string the batch harness carries per item (the ``extract`` seam ``domain`` uses)."""
    return json.dumps({"unit": str(entry.get("unit") or "").strip(),
                       "currency": str(entry.get("currency") or "").strip()}, sort_keys=True)


def _accept_unit_result(raw: str) -> tuple[str | None, str]:
    """Acceptor for the measure-annotation envelope. BATCH carries ``{unit, currency}``; the flat
    SINGLE seam (and the batch's single-item fallback) returns a BARE unit string, accepted here as
    the same envelope with no currency.

    PER-FIELD SALVAGE, deliberately asymmetric: an unusable UNIT rejects the whole item (it is the
    question the stage exists to answer, and an item with no unit is not a result), while an
    off-shape CURRENCY drops only the currency — losing a good unit because the model wrote
    "dirhams" instead of "AED" would be a worse outcome than storing the unit alone."""
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = raw                       # a bare unit from the flat single/fallback schema
    if not isinstance(payload, dict):
        payload = {"unit": payload}
    unit, reason = _accept_unit(str(payload.get("unit") or ""))
    if unit is None:
        return None, reason
    currency = _accept_currency(str(payload.get("currency") or ""))
    return json.dumps({"unit": unit, "currency": currency or ""}, sort_keys=True), "valid"


def _parse_unit_result(raw: str) -> tuple[str, str]:
    """Decode one measure-annotation envelope into ``(unit, currency)``; currency may be ``""``."""
    try:
        payload = json.loads(raw)
    except ValueError:
        return raw, ""
    if not isinstance(payload, dict):
        return raw, ""
    return str(payload.get("unit") or ""), str(payload.get("currency") or "")


# The taxonomy's own definition of a MEASURE: a concept that carries an AGGREGATION behaviour.
# `additivity` is "n/a" for every identifier, date, code, flag and free-text concept and is set only
# where the registry says the column holds a quantity that can be summed / averaged / taken latest
# (monetary_flow additive, monetary_stock semi_additive, monetary_rate / price / dpd / count
# non_additive, …). Reading the target set off the registry keeps it a DATA question, not a second
# hand-maintained list that could drift from the taxonomy.
_MEASURE_ADDITIVITY = frozenset({"additive", "semi_additive", "non_additive"})


def _is_measure_concept(name: str) -> bool:
    """True iff ``name`` is a known concept the taxonomy treats as a MEASURE (see above)."""
    rec = concept_record(name) if name else None
    return rec is not None and rec.additivity in _MEASURE_ADDITIVITY


def _unit_targets(rows: list[CanonicalRow], concepts: dict[str, str] | None) -> set[str]:
    """The content hashes this run EXPECTS a measure annotation for — a column where a unit is
    MEANINGFUL and the file declares none:

    * its assigned concept is a MEASURE (``_is_measure_concept``) — asking a customer id or a
      settlement status for its unit is the unanswerable question E4a Task 1 removed, and drafting
      one would put AI noise on a column that can never carry a unit;
    * the upload DECLARES no ``unit`` for it. The AI fills a blank; it never contests a declared
      value (a source proposal and a competing LLM one would resolve to a display CONFLICT).

    ONE definition of the target set, shared by all three readers (the drafter's selection, ingest's
    honest expected count, and the evidence reconciler's keep-set) — drift between them is
    asymmetric and dangerous: a reconciler narrower than the drafter would silently RETIRE a live
    target on a transient miss. An unclassified column (no concept, or the concept stage failed) is
    NOT a target: with no measure signal there is nothing to be confident about."""
    concepts = concepts or {}
    return {h for r in rows
            if not r.unit and _is_measure_concept(concepts.get((h := content_hash(r)), ""))}


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
        meta_defn = strip_sample_values(rec.definition)
        if meta_defn:
            # The sanitized business definition is the payload we WANT the classifier to see, so give
            # it the larger word-bounded window instead of the 200-char default that cut it mid-sentence.
            meta["business_definition"] = bounded_definition(meta_defn, _MAX_DEFINITION_LEN)
        for key, val in (("term_name", rec.term_name), ("data_domain", rec.domain),
                         ("bian_path", rec.bian_path), ("fibo_path", rec.fibo_path)):
            if val:
                meta[key] = val[:_MAX_META_LEN]
        if rec.synonyms:
            meta["synonyms"] = [s[:_MAX_META_LEN] for s in rec.synonyms]
        # Every column of the uploader's own file this platform has no first-class slot for. These
        # are the columns that used to be dropped: `attribute_category`, `security_classification`,
        # the PCI/AML/KYC and `pi_*` governance flags. A mapping file's columns describe COLUMNS —
        # meaning, not customer rows — and when a source auto-fills its description column by bucket
        # (12 of CIB's columns share one sentence) they are the only per-column signal that varies.
        # Bounded + capped at the reader; PII-scanned on egress as list-of-prose like `synonyms`.
        if rec.source_attributes:
            meta["source_attributes"] = [a[:_MAX_META_LEN] for a in rec.source_attributes]
    return meta


def _write_concept_evidence(conn, *, resolved: dict[str, str], by_hash: dict[str, CanonicalRow],
                            meta_by_hash: dict[str, dict],
                            rec_by_tc: dict[tuple[str, str], GlossaryRecord],
                            bindings: dict[str, ObjectBinding] | None,
                            source_snapshot_id: str,
                            cache_hit_hashes: frozenset[str] = frozenset()) -> int:
    """Write one ``field_evidence`` proposal per glossary column classified THIS run (spec §5.1),
    ROUTED THROUGH producer-scoped staleness + snapshot reuse (whole-branch review Important-2 — the
    LLM producer must not bypass the machinery every other producer goes through).

    ``resolved`` (#6) now carries BOTH a hash classified by a FRESH LLM call this run AND a cache
    HIT reused from a prior run's cached classification — a HIT still needs its evidence to EXIST
    (self-heals a prior write that failed and left the concept ungoverned), and re-writing an
    already-present, input-unchanged proposal is a safe no-op below (the ``reused`` check), so this
    never duplicates or stales a still-current concept. ``cache_hit_hashes`` identifies which of
    ``resolved`` came from the cache rather than a call this run.

    ``producer=LLM`` / ``strength=PROPOSED``; ``producer_ref`` is the enrichment run bucket (ties the
    proposal to its immutable llm_call records — for a cache HIT this is the bucket the ORIGINAL call
    was recorded under, since the classification itself is unchanged), ``producer_item_ref`` the batch
    item ref (content hash), prefixed ``cache:`` for a HIT so the audit trail honestly shows no
    provider call backed THIS run's write. ``producer_configuration_hash`` the vocabulary fingerprint.
    C3: an ``unclassified`` (or any non-known) value is NOT a proposal — no evidence. Only ATTACHABLE
    columns (``may_attach`` on the Task-2 binding, when supplied) get evidence.

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
        item_ref = f"cache:{h}" if h in cache_hit_hashes else h
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
                        producer_item_ref=item_ref, producer_configuration_hash=_vocab_fingerprint(),
                        source_snapshot_id=source_snapshot_id, input_hash=input_hash)
        except Exception:  # noqa: BLE001 — advisory: an evidence-write failure never aborts enrichment
            failures += 1
            logger.warning("advisory concept field_evidence write failed for %s", rec.logical_ref,
                           exc_info=True)
    return failures


# A `keep_input_hash` that can never equal a real per-field input hash (always a 64-char sha256 hex
# digest — this contains non-hex chars), so `stale_source_evidence(..., keep_input_hash=_STALE_ALL)`
# stales EVERY active row for the given producer+field. The SAME sentinel value `ingest._STALE_ALL`
# uses; duplicated (not imported) because `ingest` imports THIS module — importing back would cycle.
_STALE_ALL = "__field_absent_from_upload__"


def stale_all_llm_field_evidence(conn, *, logical_ref: str, field_name: str) -> int:
    """Retire ALL of the LLM's ACTIVE evidence for one ``(logical_ref, field_name)``; return the rows
    staled. PRODUCER-SCOPED — human / taxonomy / source rows are never touched."""
    return stale_source_evidence(conn, logical_ref=logical_ref, field_name=field_name,
                                 producer=EvidenceProducer.LLM, keep_input_hash=_STALE_ALL)


def _write_llm_field_evidence(conn, *, field_name: str, items: dict[str, str],
                              ref_of: Callable[[str], tuple[str, str, object] | None],
                              source_snapshot_id: str,
                              valid_fn: Callable[[str], bool] | None = None,
                              producer_configuration_hash: str | None = None,
                              bindings: dict[str, ObjectBinding] | None = None) -> int:
    """Write one ``llm/proposed`` ``field_evidence`` row per item of ``items`` (E1a); return the
    number of CONTAINED per-item failures so the caller's stage report can say ``partial``.

    SUPERSEDE-AND-REWRITE, unconditionally: prior ACTIVE LLM evidence for the field is staled and a
    fresh row written. No unchanged-detection and no result cache — that reuse is a DEFERRED
    optimization (and is why this is a separate, simpler writer than ``_write_concept_evidence``,
    which keeps its own).

    TWO IDENTITIES, never collapsed: ``ref_of(key)`` returns ``(evidence_ref, binding_ref,
    material)`` — attachability is checked against ``bindings[binding_ref]`` (the PUBLIC-flattened
    ref a binding is keyed by), while the hash/stale/write all key on ``evidence_ref`` (the
    schema-preserving storage identity). Collapsing them silently skips every non-``public``-schema
    column: the binding lookup misses, the item is skipped, and the run reports zero failures.

    FAIL-SOFT per item, wrapping the WHOLE body: a throw anywhere (``ref_of``, hashing, the write)
    logs, counts one failure, and moves on — one bad item never aborts the batch. A SKIP (invalid
    value, ``ref_of`` returning ``None``, an unattachable binding) is not a failure and is not
    counted."""
    failures = 0
    for key, value in items.items():
        try:
            if valid_fn is not None and not valid_fn(value):
                continue                       # skip — not a proposal, not a failure
            resolved = ref_of(key)
            if resolved is None:
                continue
            evidence_ref, binding_ref, material = resolved
            if bindings is not None:
                binding = bindings.get(binding_ref)          # PUBLIC-flattened attachability lookup
                if binding is None or not may_attach(binding):
                    continue                   # attachable columns only
            input_hash = field_input_hash(logical_ref=evidence_ref, field_name=field_name,
                                          material=material)
            with conn.transaction():   # savepoint: contain a failed write without poisoning the txn
                stale_all_llm_field_evidence(conn, logical_ref=evidence_ref, field_name=field_name)
                record_field_evidence(
                    conn, logical_ref=evidence_ref, field_name=field_name, proposed_value=value,
                    producer=EvidenceProducer.LLM, strength=AssertionStrength.PROPOSED,
                    producer_ref=ENRICHMENT_RUN_ID, producer_item_ref=str(key),
                    producer_configuration_hash=producer_configuration_hash,
                    source_snapshot_id=source_snapshot_id, input_hash=input_hash)
        except Exception:  # noqa: BLE001 — advisory: one item's failure never aborts the rest
            failures += 1
            logger.warning("advisory %s field_evidence write failed for item %s", field_name, key,
                           exc_info=True)
    return failures


def _reconcile_llm_field_evidence(conn, *, field_name: str, retire_refs: set[str]) -> None:
    """Retire prior ACTIVE LLM evidence for the columns this run must DROP — deliberately withheld
    (sanitizer-suppressed / egress-blocked) or no longer a target. A suppressed field must not keep
    asserting an old AI value. A TRANSIENT failure's ref is deliberately NOT in ``retire_refs``, so
    good data survives a provider blip."""
    for evidence_ref in retire_refs:
        stale_all_llm_field_evidence(conn, logical_ref=evidence_ref, field_name=field_name)


def _active_llm_field_refs(conn, *, source: str, field_name: str) -> set[str]:
    """The refs in ONE catalog source that currently carry ACTIVE LLM evidence for a field — the
    universe reconciliation reasons over (an upload is a whole-source replacement, so a ref absent
    from it is genuinely gone). Scoped with ``split_part`` rather than ``LIKE``: a source name may
    legitimately contain ``_``, a LIKE wildcard, which would reach into a NEIGHBOURING catalog."""
    rows = conn.execute(
        "SELECT DISTINCT logical_ref FROM field_evidence "
        "WHERE field_name = %s AND producer = %s AND lifecycle = 'active' "
        "AND split_part(logical_ref, '::', 1) = %s",
        (field_name, EvidenceProducer.LLM.value, _norm(source))).fetchall()
    return {r[0] for r in rows}


def _write_definition_evidence(conn, *, source: str, rows: list[CanonicalRow],
                               definitions: dict[str, str], glossary: GlossaryUpload,
                               concepts: dict[str, str] | None,
                               bindings: dict[str, ObjectBinding] | None,
                               source_snapshot_id: str) -> int:
    """Promote the LLM's drafted definitions (E1a Task 2) out of the display-only
    ``graph_node.definition`` into governed ``llm/proposed`` ``field_evidence``, so asset-detail can
    honestly show the AI as their author. Returns the CONTAINED per-item failure count, which the
    caller MUST propagate into its stage report (``partial``/``items_failed``) — losing metadata
    while reporting success is the bug that count exists to prevent.

    GLOSSARY columns only: the evidence keys on the record's SCHEMA-preserving ``logical_ref``,
    while attachability is checked at the row's PUBLIC-flattened ref — the two identities
    ``_write_llm_field_evidence`` never collapses. A blank/whitespace draft is not a proposal.
    ``concepts`` rides in the input material because the drafter's own cache key folds in the
    assigned concept (``_def_cache_key``) — a concept change legitimately re-drafts, so the
    evidence's ``input_hash`` must see the same input the draft was made from (T2-M1).

    Task 2b — RECONCILE THE TARGET UNIVERSE, not just the successes: a column that DROPS OUT of a run
    must stop asserting yesterday's AI value. Everything in this source that still carries active LLM
    ``definition`` evidence is retired EXCEPT the refs this run wrote and the refs it still EXPECTED
    to draft (blank + not sanitizer-suppressed). So a withheld (suppressed) column and one that is no
    longer a target (source-provided definition, dropped from the upload, no longer a glossary term)
    are RETIRED, while a miss on a still-expected target — a provider blip, and equally an egress
    block or an accept-gate rejection, which the drafter cannot distinguish from one — is KEPT. That
    conflation is deliberate: KEEP is the safe default; AI evidence is never retired on ambiguity."""
    by_hash = {content_hash(r): r for r in rows}
    rec_by_tc = _records_by_tc(glossary)
    concepts = concepts or {}

    def ref_of(h: str) -> tuple[str, str, object] | None:
        row = by_hash.get(h)
        if row is None:
            return None
        rec = rec_by_tc.get((_norm(row.table), _norm(row.column)))
        if rec is None:
            return None   # not a glossary column term — no schema-preserving identity to key on
        return (rec.logical_ref, normalize_ref(row.source, None, row.table, row.column),
                {"table": row.table, "column": row.column, "type": row.type,
                 "concept": concepts.get(h, "")})

    failures = _write_llm_field_evidence(
        conn, field_name="definition", items=definitions, ref_of=ref_of,
        source_snapshot_id=source_snapshot_id, valid_fn=lambda v: bool(v and v.strip()),
        producer_configuration_hash=None, bindings=bindings)

    # The set difference is computed here (never handed a ref written this run — that would silently
    # stale the fresh evidence): KEEP = written this run ∪ still an expected target. The expected set
    # comes from the SAME `_definition_targets` the drafter selects with — a reconciler that narrowed
    # relative to the drafter would silently RETIRE a live target on a transient miss.
    keep = {ref[0] for h in set(definitions) | _definition_targets(rows, glossary)
            if (ref := ref_of(h)) is not None}
    _reconcile_llm_field_evidence(
        conn, field_name="definition",
        retire_refs=_active_llm_field_refs(conn, source=source, field_name="definition") - keep)
    return failures


def _write_summary_evidence(conn, *, source: str, rows: list[CanonicalRow],
                            summaries: dict[str, str], glossary: GlossaryUpload,
                            bindings: dict[str, ObjectBinding] | None,
                            source_snapshot_id: str,
                            extras_by_hash: dict[str, dict] | None = None) -> int:
    """Promote drafted summaries into governed ``llm/proposed`` ``field_evidence`` under
    ``ai_summary``. Returns the CONTAINED per-item failure count for the caller's stage report.

    Mirrors :func:`_write_definition_evidence` but the RECONCILIATION is simpler for one reason: the
    target universe is EVERY column, so a column can only drop out by leaving the upload entirely.
    Keep = written this run ∪ still present, which retires a genuinely removed column and never
    retires a live one on a transient provider miss (KEEP stays the safe default — AI evidence is
    never retired on ambiguity).
    """
    by_hash = {content_hash(r): r for r in rows}
    rec_by_tc = _records_by_tc(glossary)
    extras_by_hash = extras_by_hash or {}

    def ref_of(h: str) -> tuple[str, str, object] | None:
        row = by_hash.get(h)
        if row is None:
            return None
        rec = rec_by_tc.get((_norm(row.table), _norm(row.column)))
        if rec is None:
            return None   # not a glossary column term — no schema-preserving identity to key on
        # The evidence material must be the SAME metadata the summary was drafted from — the ONE
        # `summary_payload` builder, with the SAME extras the tail drafter passed — never a
        # {table, column, type} stub. The input_hash is what decides whether existing evidence is
        # still current: keyed on less than the drafting input, correcting a term_name (or the
        # dossier getting richer) leaves the hash unchanged, so a redraft that fails transiently
        # leaves the OLD summary active as though it still described the column.
        return (rec.logical_ref, normalize_ref(row.source, None, row.table, row.column),
                summary_payload(row, rec, extras_by_hash.get(h)))

    failures = _write_llm_field_evidence(
        conn, field_name="ai_summary", items=summaries, ref_of=ref_of,
        source_snapshot_id=source_snapshot_id, valid_fn=lambda v: bool(v and v.strip()),
        producer_configuration_hash=None, bindings=bindings)
    keep = {ref[0] for h in set(summaries) | _summary_targets(rows, glossary)
            if (ref := ref_of(h)) is not None}
    _reconcile_llm_field_evidence(
        conn, field_name="ai_summary",
        retire_refs=_active_llm_field_refs(conn, source=source, field_name="ai_summary") - keep)
    return failures


def _table_refs(glossary: GlossaryUpload) -> dict[str, str]:
    """Normalized table name -> the SCHEMA-PRESERVING ref of its TABLE node — the identity
    table-grained evidence stores under. Built from the glossary's own records (a table term names
    its table; a column term attests its table's schema too), so an FTR table under a real schema is
    never written to a ``public`` twin. First declaration wins, mirroring ``graph.schema_by_ref``."""
    out: dict[str, str] = {}
    for rec in glossary.records:
        try:
            source, schema, table, _column = parse_ref(rec.logical_ref)
        except ValueError:
            continue
        out.setdefault(table, normalize_ref(source, schema, table))
    return out


def _declared_domain_tables(glossary: GlossaryUpload) -> set[str]:
    """Normalized names of the tables whose glossary TABLE term DECLARES a data domain of its own."""
    out: set[str] = set()
    for rec in glossary.records:
        if not rec.is_table or not rec.domain:
            continue
        try:
            _source, _schema, table, _column = parse_ref(rec.logical_ref)
        except ValueError:
            continue
        out.add(table)
    return out


def _write_domain_evidence(conn, *, source: str, rows: list[CanonicalRow],
                           domains: dict[str, str],
                           column_domains: dict[tuple[str, str], str],
                           glossary: GlossaryUpload,
                           bindings: dict[str, ObjectBinding] | None,
                           source_snapshot_id: str) -> int:
    """Promote the LLM's TWO-LEVEL domain classification (E1a T3) into governed ``llm/proposed``
    ``domain`` ``field_evidence``. Returns the CONTAINED per-item failure count, which the caller
    MUST propagate into its stage report (``partial``/``items_failed``).

    TWO LEVELS, HONESTLY SEPARATED:
    * the TABLE's domain — the default context — is evidence on the TABLE node, at its
      schema-preserving ref. A table has no per-column attachability question, so no binding gate
      applies (``bindings=None``).
    * a COLUMN's domain is evidence on the COLUMN node ONLY where it OVERRIDES that default (keyed
      on the glossary record's schema-preserving ref, attachability checked at the row's
      public-flattened one — the two identities the writer never collapses). A column that INHERITS
      gets NO evidence: inheritance is a read-time relationship, and fabricating a column-level row
      for it would assert an authorship the classifier never claimed.

    THE AI FILLS A BLANK, IT NEVER CONTESTS A CURATED VALUE (R3's rule for definitions, applied to
    domain): a table or column whose glossary sidecar DECLARES a ``data_domain`` is NOT an AI target
    at either level. That is not politeness — the source's own proposal and a competing LLM one
    resolve to a display CONFLICT, which BLANKS the curated domain the catalog was showing.

    RECONCILIATION by disposition, over BOTH levels (the target universe, not just the successes).
    Every ref in this source still carrying active LLM ``domain`` evidence is retired EXCEPT: every
    table in this upload that is still a target (classified this run, or MISSED — a transient miss
    KEEPS, the safe default), the overrides written this run, and the still-target columns of a table
    whose classification missed (its overrides are unknown this run, not withdrawn). So a table that
    dropped out of the source, one that GAINED a curated domain, and a column the classifier no
    longer singles out are all RETIRED.

    Scoped safely (T3 review): ``domain`` LLM evidence has exactly ONE writer — this one. Pass B
    (``table_synth``) also writes ``llm/proposed`` evidence at TABLE refs, but for ``table_role`` /
    ``primary_entity`` / ``event_or_snapshot``; the ``field_name='domain'`` filter excludes it. The
    glossary sidecar's own ``data_domain`` is SOURCE-produced and human corrections are HUMAN — both
    invisible to this producer-scoped retirement."""
    table_refs = _table_refs(glossary)
    rec_by_tc = _records_by_tc(glossary)
    declared_tables = _declared_domain_tables(glossary)
    columns_by_table: dict[str, list[str]] = {}
    for r in rows:
        columns_by_table.setdefault(_norm(r.table), []).append(_norm(r.column))

    def table_ref_of(table: str) -> tuple[str, str, object] | None:
        """The table's evidence identity, or None when it is not an AI domain target."""
        ref = table_refs.get(_norm(table))
        if ref is None or _norm(table) in declared_tables:
            return None   # no glossary record names it, or its term declares the domain itself
        # The classification's own input: the table and the columns the request carried.
        return (ref, ref, {"table": _norm(table),
                           "columns": sorted(columns_by_table.get(_norm(table), []))})

    def column_ref_of(key: str) -> tuple[str, str, object] | None:
        """The column's evidence identity, or None when it is not an AI domain target."""
        table, _sep, column = key.partition(".")
        rec = rec_by_tc.get((table, column))
        if rec is None or rec.domain:
            return None   # not a glossary column term, or its sidecar declares the domain itself
        return (rec.logical_ref, normalize_ref(source, None, table, column),
                {"table": table, "column": column, "table_domain": domains.get(table, "")})

    overrides = {f"{table}.{column}": value
                 for (table, column), value in column_domains.items()}
    failures = _write_llm_field_evidence(
        conn, field_name="domain", items=dict(domains), ref_of=table_ref_of,
        source_snapshot_id=source_snapshot_id, valid_fn=lambda v: bool(v and v.strip()),
        bindings=None)
    failures += _write_llm_field_evidence(
        conn, field_name="domain", items=overrides, ref_of=column_ref_of,
        source_snapshot_id=source_snapshot_id, valid_fn=lambda v: bool(v and v.strip()),
        bindings=bindings)

    # KEEP = every still-target table in this upload (written OR transient-missed) ∪ the overrides
    # written this run ∪ every still-target column of a table whose classification MISSED (its
    # overrides are unknown, not withdrawn). Computed through the SAME `ref_of`s the writes used, so
    # a ref written this run is never handed to retirement and a non-target is never kept alive.
    missed = {t for t in columns_by_table if t not in {_norm(d) for d in domains}}
    # The classified tables join the keep set explicitly: `table_ref_of` consults the glossary's
    # `table_refs`, not `columns_by_table`, so a `domains` key with no rows would be WRITTEN above
    # and then immediately retired here (unreachable from ingest, which always passes `vr.good` —
    # but `_write_domain_evidence` is module-level and callers pass the two independently).
    keep = {ref[0] for t in set(columns_by_table) | {_norm(d) for d in domains}
            if (ref := table_ref_of(t)) is not None}
    keep |= {ref[0] for k in overrides if (ref := column_ref_of(k)) is not None}
    keep |= {ref[0] for (table, column) in rec_by_tc
             if table in missed and (ref := column_ref_of(f"{table}.{column}")) is not None}
    _reconcile_llm_field_evidence(
        conn, field_name="domain",
        retire_refs=_active_llm_field_refs(conn, source=source, field_name="domain") - keep)
    return failures


def _write_synonym_evidence(conn, *, source: str, rows: list[CanonicalRow],
                            synonyms: dict[str, str], glossary: GlossaryUpload,
                            bindings: dict[str, ObjectBinding] | None,
                            source_snapshot_id: str) -> int:
    """Store the LLM's drafted synonyms (E1a T4) as FIRST-CLASS ``llm/proposed`` ``semantic_terms``
    ``field_evidence``. Returns the CONTAINED per-item failure count, which the caller MUST propagate
    into its stage report (``partial``/``items_failed``).

    NOT search-only and NOT human-confirmed: an AI synonym may be the ONLY semantic signal that
    selects a column — it needs no corroboration and passes no new gate. What makes that safe is that
    the PROVENANCE travels WITH the term (producer ``llm``, strength ``proposed`` on the evidence
    row), so each downstream consumer applies its own policy. The MERGE onto
    ``graph_node.semantic_terms`` happens in ``ingest._project_semantic_terms``, AFTER ``build_graph``
    (which delete+recreates the nodes an enrich-time write would land on).

    GLOSSARY columns only, and the two identities are never collapsed: attachability is checked at
    the row's PUBLIC-flattened ref, the evidence is stored at the record's SCHEMA-preserving one.

    RECONCILIATION by disposition: EVERY column in this upload is a synonym target (synonyms are
    additive — unlike a definition or a domain there is no curated value for the AI to contest, so
    nothing is ever deliberately withheld). Retirement therefore covers exactly the refs that are no
    longer targets — a column dropped from the source, or no longer a glossary term — while a
    transient miss on a still-present column KEEPS its prior terms (the safe default).

    Scoped safely: ``semantic_terms`` LLM evidence has exactly ONE writer — this one. The glossary's
    own term/synonym/taxonomy text is a graph SEARCH PROJECTION, not ``field_evidence``, and Pass B
    (``table_synth``) writes its ``llm/proposed`` rows for other field names at TABLE refs, so the
    ``field_name='semantic_terms'`` + producer filter cannot reach another writer's rows."""
    by_hash = {content_hash(r): r for r in rows}
    rec_by_tc = _records_by_tc(glossary)

    def ref_of(h: str) -> tuple[str, str, object] | None:
        row = by_hash.get(h)
        if row is None:
            return None
        rec = rec_by_tc.get((_norm(row.table), _norm(row.column)))
        if rec is None:
            return None   # not a glossary column term — no schema-preserving identity to key on
        return (rec.logical_ref, normalize_ref(row.source, None, row.table, row.column),
                {"table": row.table, "column": row.column, "type": row.type})

    failures = _write_llm_field_evidence(
        conn, field_name="semantic_terms", items=synonyms, ref_of=ref_of,
        source_snapshot_id=source_snapshot_id, valid_fn=lambda v: bool(v and v.strip()),
        bindings=bindings)

    # KEEP = every glossary column still in this upload (written this run OR transient-missed);
    # computed through the SAME `ref_of` the write used, so a fresh row is never handed to retirement.
    keep = {ref[0] for h in by_hash if (ref := ref_of(h)) is not None}
    _reconcile_llm_field_evidence(
        conn, field_name="semantic_terms",
        retire_refs=_active_llm_field_refs(conn, source=source,
                                           field_name="semantic_terms") - keep)
    return failures


def _write_unit_evidence(conn, *, source: str, rows: list[CanonicalRow],
                         units: dict[str, str], currencies: dict[str, str],
                         concepts: dict[str, str] | None, glossary: GlossaryUpload,
                         bindings: dict[str, ObjectBinding] | None,
                         source_snapshot_id: str) -> int:
    """Store the LLM's drafted measure annotation (E4a T2) as ``llm/proposed`` ``unit`` /
    ``currency`` ``field_evidence``. Returns the CONTAINED per-item failure count, which the caller
    MUST propagate into its stage report (``partial``/``items_failed``).

    EVIDENCE ONLY — AND THAT IS THE WHOLE SAFETY DESIGN. The feature gauntlet reads ``unit`` and
    ``currency`` from ``graph_node`` (``feature_assist._column_meta``, a plain SELECT), and
    ``field_policies._MEASURE_ANNOTATION`` keeps BOTH its ``display_rule`` and its
    ``operational_rule`` at ``_SOURCE_OR_HUMAN``. The LLM is therefore excluded from resolution, the
    resolver has no display projection for these fields at all, and ``build_graph`` populates
    ``graph_node.unit`` from the FILE. An AI proposal consequently CANNOT reach the column the
    gauntlet reads, so it cannot clear ``UNIT_CONSISTENT``/``CURRENCY_CONSISTENT``. The hole is
    designed out, not guarded — and pinned by
    ``test_ai_unit_proposal.test_an_ai_proposed_unit_does_not_clear_the_unit_consistent_safety_check``.

    TARGETS: ``_unit_targets`` (a MEASURE concept whose file declares no unit). ``currency`` is
    written on the same items MINUS any column whose file already declares one — the AI fills a
    blank at both levels and never contests a declared value.

    GLOSSARY columns only, and the two identities are never collapsed: attachability is checked at
    the row's PUBLIC-flattened ref, the evidence is stored at the record's SCHEMA-preserving one.

    RECONCILIATION by disposition, per field: every ref in this source still carrying active LLM
    ``unit`` (resp. ``currency``) evidence is retired EXCEPT the refs written this run and the refs
    that are STILL targets. So a column that gained a source-declared unit, was reclassified to a
    non-measure concept, or dropped out of the upload is RETIRED, while a transient miss on a
    still-live target KEEPS its prior proposal (the safe default this branch takes everywhere).

    Scoped safely: ``unit``/``currency`` LLM evidence has exactly ONE writer — this one. A technical
    CSV's declared unit is SOURCE-produced and a correction is HUMAN; both are invisible to this
    producer-scoped retirement."""
    by_hash = {content_hash(r): r for r in rows}
    rec_by_tc = _records_by_tc(glossary)
    concepts = concepts or {}
    targets = _unit_targets(rows, concepts)

    def _ref_of(h: str, *, field: str) -> tuple[str, str, object] | None:
        row = by_hash.get(h)
        if row is None or h not in targets:
            return None   # not a measure, or the file declares the unit itself
        if field == "currency" and row.currency:
            return None   # the file declares the currency — never contested
        rec = rec_by_tc.get((_norm(row.table), _norm(row.column)))
        if rec is None:
            return None   # not a glossary column term — no schema-preserving identity to key on
        return (rec.logical_ref, normalize_ref(row.source, None, row.table, row.column),
                {"table": row.table, "column": row.column, "type": row.type,
                 "concept": concepts.get(h, "")})

    def unit_ref_of(h: str) -> tuple[str, str, object] | None:
        return _ref_of(h, field="unit")

    def currency_ref_of(h: str) -> tuple[str, str, object] | None:
        return _ref_of(h, field="currency")

    failures = _write_llm_field_evidence(
        conn, field_name="unit", items=units, ref_of=unit_ref_of,
        source_snapshot_id=source_snapshot_id, valid_fn=lambda v: bool(v and v.strip()),
        bindings=bindings)
    failures += _write_llm_field_evidence(
        conn, field_name="currency", items=currencies, ref_of=currency_ref_of,
        source_snapshot_id=source_snapshot_id, valid_fn=lambda v: bool(v and v.strip()),
        bindings=bindings)

    # KEEP = written this run ∪ still an expected target, computed through the SAME `ref_of` the
    # write used (so a ref written this run is never handed to retirement, and a non-target is never
    # kept alive). Per field — a column that declares a currency but no unit keeps its AI unit while
    # its AI currency is retired.
    for field_name, ref_of, drafted in (("unit", unit_ref_of, units),
                                        ("currency", currency_ref_of, currencies)):
        keep = {ref[0] for h in set(drafted) | targets if (ref := ref_of(h)) is not None}
        _reconcile_llm_field_evidence(
            conn, field_name=field_name,
            retire_refs=_active_llm_field_refs(conn, source=source,
                                               field_name=field_name) - keep)
    return failures


def _record_concept_critique_decision(conn, *, logical_ref: str, disposition: str,
                                      conflict_codes: tuple[str, ...]) -> None:
    """Append the critic's REPLACEMENT decision to the column's field-decision trail — the
    machinery behind ``concept_decision_id`` — so the audit answers "why did this column's concept
    change" from the COLUMN, not only from one run's stage detail.

    A ``STALED`` event with the critic's closed reason codes (``concept_critic_<disposition>`` plus
    the deterministic conflict codes), superseding the latest non-retired decision. STALED is the
    honest lifecycle word: the critic retired the LLM's evidence, and a retired-latest decision is
    exactly what keeps ``is_feature_eligible`` fail-closed for the field. A REVISED column's fresh
    proposal then resolves normally on top (a later RESOLVED event); a REFUTED one stays retired —
    the abstain supersedes the wrong identifier instead of protecting it."""
    # Lazy import: ``field_resolution`` imports ``graph``, which imports THIS module for
    # ``content_hash`` — a module-level import would cycle (the ``_concept_grounding`` pattern).
    from featuregen.overlay.upload.field_resolution import (
        FIELD_POLICY_VERSION,
        RESOLVER_VERSION,
    )
    retired = {FieldDecisionEventType.REJECTED.value, FieldDecisionEventType.STALED.value,
               FieldDecisionEventType.SUPERSEDED.value}
    decisions = read_field_decisions(conn, logical_ref, "concept")   # oldest-first
    supersedes = next(
        (d.decision_event_id for d in reversed(decisions) if d.event_type not in retired),
        None,
    )
    record_field_decision(
        conn, logical_ref=logical_ref, field_name="concept",
        event_type=FieldDecisionEventType.STALED,
        selected_evidence_ids=[], evidence_set_hash=canonical_hash([]),
        display_value_hash=None, load_bearing_value_hash=None,
        conflict_status=f"concept_critic_{disposition}",
        reason_codes=[f"concept_critic_{disposition}", *conflict_codes],
        field_policy_version=FIELD_POLICY_VERSION, resolver_version=RESOLVER_VERSION,
        actor_ref=None, supersedes_event_id=supersedes)


def _apply_concept_critic(conn, client: LLMClient | None, *, result: dict[str, str],
                          by_hash: dict[str, CanonicalRow], meta_by_hash: dict[str, dict],
                          rec_by_tc: dict[tuple[str, str], GlossaryRecord], actor) -> dict:
    """The Pass-A ACCEPTANCE hook (ingestion-richness Task 2 step 5): run the refute-oriented
    concept critic over this run's IDENTIFIER-group assignments and apply its dispositions to
    ``result`` in place. Non-identifier assignments pass through byte-for-byte untouched.

    The re-derivation rule, exactly as the plan states it — a ``refuted`` identifier assignment
    never persists as the current suggestion; the column's concept resolves, in order:

    1. the critic's revise-pass result if accepted (``result[h]`` becomes the revision, whose
       fresh ``llm/proposed`` evidence is then written by ``_write_concept_evidence``);
    2. else the non-identifier abstain — ``result[h]`` becomes the literal ``unclassified``
       (a real classification the evidence writer deliberately never proposes);
    3. NEVER silent retention of a previously-stored wrong identifier: for every evicted
       assignment the LLM's ACTIVE ``concept`` evidence at the ref is staled (producer-scoped —
       human/source rows are untouched), so a prior run's wrong value cannot re-resolve, and the
       replacement decision (with the critic's conflict codes as its reason) is appended to the
       column's field-decision trail.

    DB-first, dict-after: all staling/decision writes happen inside the caller's savepoint BEFORE
    ``result`` is mutated, so a rolled-back savepoint leaves the in-memory classification exactly
    as the classifier produced it — never a half-applied correction.

    The classifier CACHE is deliberately not touched: it stores the classifier's own answer, and
    the critic's replay store (content-addressed on the same inputs) makes re-criticising a cache
    HIT on the next upload free. Returns the stage-detail report
    ``{items, accepted, revised, refuted, abstained, conflicts}``."""
    items: dict[str, ConceptCriticItemV1] = {}
    ref_to_hash: dict[str, str] = {}
    for h, concept_name in result.items():
        registered = concept_record(concept_name)
        if registered is None or registered.group != "identifier":
            continue                       # non-identifier fields pass through unchanged
        row = by_hash.get(h)
        if row is None:
            continue
        rec = rec_by_tc.get((_norm(row.table), _norm(row.column)))
        # Evidence and decisions key on the glossary's SCHEMA-PRESERVING ref; a technical column
        # (no sidecar) falls back to its public-flattened identity. The DEFINITION handed to the
        # critic is the already-sanitized, bounded glossary business definition — a technical
        # upload's uploader-authored free text stays out of every prompt (the M4 rule), so those
        # items carry None and are judged on name/type shape alone.
        evidence_ref = (rec.logical_ref if rec is not None
                        else normalize_ref(row.source, None, row.table, row.column))
        meta = meta_by_hash.get(h, {})
        items[evidence_ref] = ConceptCriticItemV1(
            logical_ref=evidence_ref,
            column_name=row.column,
            declared_type=str(meta.get("type") or row.type or "") or None,
            definition=meta.get("business_definition"),
            proposed_concept=concept_name,
        )
        ref_to_hash[evidence_ref] = h
    report: dict = {"items": len(items), "accepted": 0, "revised": 0, "refuted": 0,
                    "abstained": 0, "conflicts": {}}
    if not items:
        return report
    # The replay identity of THIS catalog's content: a byte-identical re-upload replays every
    # stored critique for free, while any content change re-asks the affected questions.
    catalog_revision = hashlib.sha256(
        json.dumps(sorted(by_hash)).encode("utf-8")).hexdigest()[:16]
    outcomes = critique_concept_batch(
        conn, client, list(items.values()), catalog_revision=catalog_revision, actor=actor)
    corrections: list[tuple[str, str]] = []
    for ref, outcome in outcomes.items():
        h = ref_to_hash.get(ref)
        if h is None:
            continue
        if outcome.disposition is ConceptDisposition.ACCEPTED:
            report["accepted"] += 1
            continue
        if outcome.disposition is ConceptDisposition.ABSTAINED:
            report["abstained"] += 1       # refute-oriented: no refutation -> proposal stands
            continue
        if outcome.conflict_codes:
            report["conflicts"][ref] = list(outcome.conflict_codes)
        stale_all_llm_field_evidence(conn, logical_ref=ref, field_name="concept")
        _record_concept_critique_decision(
            conn, logical_ref=ref, disposition=outcome.disposition.value,
            conflict_codes=outcome.conflict_codes)
        if (outcome.disposition is ConceptDisposition.REVISED
                and outcome.resolved_concept is not None):
            report["revised"] += 1
            corrections.append((h, outcome.resolved_concept))
        else:
            report["refuted"] += 1
            corrections.append((h, UNCLASSIFIED))
    for h, corrected in corrections:       # in-memory only after every DB write succeeded
        result[h] = corrected
    return report


def enrich_concepts(conn, rows: list[CanonicalRow], client: LLMClient, actor=None, *,
                    glossary: GlossaryUpload | None = None,
                    bindings: dict[str, ObjectBinding] | None = None,
                    source_snapshot_id: str | None = None,
                    stats: dict | None = None,
                    ingestion_run_id: str | None = None) -> dict[str, str]:
    """Classify each column into a controlled concept; returns {content_hash: concept} (unchanged).

    Glossary carry-forward (guarded — non-glossary uploads are UNCHANGED): when ``glossary`` is given,
    each glossary column's concept input ALSO carries its business-semantic sidecar (see
    ``_concept_metadata``), and — in BOTH single and batch modes (Important-2: single is the default)
    — every attachable glossary column with a known concept writes an item-level ``concept``
    ``field_evidence`` proposal through producer-scoped staleness (see ``_write_concept_evidence``):
    a fresh classification this run, AND (#6) a cache HIT reused from a prior run — so a HIT REPAIRS
    a prior failed evidence write instead of leaving it missing forever. ``source_snapshot_id`` is
    required to write evidence (a NOT-NULL column); absent it, enrichment still runs and returns
    concepts, just without the evidence side-effect.

    ``stats`` (#22, optional out-param — the return shape is unchanged): when given, receives
    ``evidence_write_failures``, the count of per-item evidence-write failures the stage CONTAINED
    internally, plus ``not_attempted`` (batch mode), the count of items the budget/deadline cutoff
    skipped WITHOUT dispatch — so the caller's stage report can say ``partial`` (``items_failed`` or
    the distinct ``truncated``) instead of a laundered success. It also receives
    ``concept_critic`` — the acceptance hook's report (``_apply_concept_critic``; ``{"failed":
    True}`` when the contained critic faulted) — which ingest records as the
    ``enrich_concept_critic`` stage.

    ``ingestion_run_id`` (C5-T5): the durable run this enrichment serves — with it, EVERY LLM
    dispatch this stage issues (batch chunks, retries, single fallbacks, single mode) is pre-audited
    and attributed to the run + the exact column subjects it enriches (stage ``enrich_concept``).
    ``None`` (a direct call with no run) dispatches unattributed — byte-for-byte today."""
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
    hit_hashes = frozenset(result)   # #6: cache HITS this run — their evidence still needs repair

    # Metadata for EVERY row, not just cache MISSES (#6): a MISS needs it as the LLM input, and BOTH
    # a MISS classified this run AND a HIT reused from cache need it as the evidence input material
    # (a HIT's material must match what a fresh classification would have used, so its input_hash
    # lines up with an already-active row -> reused, not duplicated).
    meta_by_hash = {h: _concept_metadata(r, _rec_of(r)) for h, r in by_hash.items()}
    miss_hashes = [h for h in by_hash if h not in result]
    resolved: dict[str, str] = {}   # {content_hash: concept} classified THIS run (a MISS only)

    if enrich_config.mode("concept") == "batch":
        misses = [BatchItem(h, meta_by_hash[h]) for h in miss_hashes]
        batch_report: dict = {}   # honest-labeling: run_batched reports budget/deadline not_attempted
        resolved = run_batched(
            conn, client, short="concept", task=_TASK, prompt_id="overlay_concept_batch_v1",
            schema_id="overlay_concept_batch",
            shared_metadata={"vocabulary": _CONCEPT_VOCABULARY}, items=misses, out_key="concept",
            instruction="For each item classify the column into the provided controlled concept "
                        "vocabulary — choose the single best-fitting concept name, or 'unclassified' "
                        "if none fits. Return exactly one result per input ref; treat each item "
                        "independently.", accept=_accept_concept, actor=actor,
            deadline_s=enrich_config.stage_deadline_s(),   # MF-4 — bound the source-lock hold
            report=batch_report,
            ingestion_run_id=ingestion_run_id, dispatch_stage="enrich_concept",
            dispatch_subjects=({h: _column_subject(by_hash[h]) for h in miss_hashes}
                               if ingestion_run_id is not None else None))
        if stats is not None:
            stats["not_attempted"] = batch_report.get("not_attempted", 0)
        for h, concept in resolved.items():
            _cache_put(conn, "enrichment_concept", key_of[h], concept, _CONCEPT_CACHE_VERSION)
            result[h] = concept
    else:
        for h in miss_hashes:                              # single mode
            # Metadata only (names/types + the glossary sidecar for a glossary column) — NEVER the
            # uploader's free-text definition on a technical row (M4 egress risk).
            raw = _call(conn, client, _TASK, "overlay_concept_v1", "overlay_concept",
                        {**meta_by_hash[h], "vocabulary": _CONCEPT_VOCABULARY}, "concept",
                        "Classify this column into the provided controlled concept vocabulary — choose "
                        "the single best-fitting concept name, or 'unclassified' if none fits.", actor,
                        _single_ctx(ingestion_run_id, "enrich_concept", _column_subject(by_hash[h])),
                        # vocab-caching: the vocabulary is the static shared prefix on this call too.
                        cacheable_metadata_keys=("vocabulary",))
            if raw is None:
                continue   # failure/empty -> don't cache; retry next ingest (M3)
            # #5: single mode enforces the IDENTICAL response contract as batch (_accept_concept) — a
            # known concept or the literal 'unclassified' is accepted, cached, and counted resolved;
            # an off-vocabulary/invalid response is REJECTED outright (never coerced to UNCLASSIFIED,
            # never counted resolved) so a stage report can't paper over every provider response being
            # invalid, and a later retry can still succeed (mirrors batch's _accept_concept exactly).
            concept, _reason = _accept_concept(raw)
            if concept is None:
                continue
            _cache_put(conn, "enrichment_concept", key_of[h], concept, _CONCEPT_CACHE_VERSION)
            result[h] = concept
            resolved[h] = concept

    # ── Pass-A ACCEPTANCE (ingestion-richness Task 2): the refute-oriented concept critic runs
    # over EVERY identifier-group assignment this run produced — fresh classifications AND cache
    # HITS (a hit re-proposing yesterday's wrong identifier must be evicted, not grandfathered) —
    # BEFORE any evidence is written, so a refuted assignment never persists as the current
    # suggestion. Savepointed + advisory like every enrichment side-effect: a critic fault
    # degrades to un-criticised concepts and an honest ``failed`` stage, never a lost upload.
    critic_report: dict = {"failed": True}
    try:
        with conn.transaction():
            critic_report = _apply_concept_critic(
                conn, client, result=result, by_hash=by_hash, meta_by_hash=meta_by_hash,
                rec_by_tc=rec_by_tc, actor=actor)
    except Exception:  # noqa: BLE001 — advisory: the critic never aborts concept enrichment
        logger.warning("advisory concept critic failed", exc_info=True)
    if stats is not None:
        stats["concept_critic"] = critic_report

    # Item-level LLM concept evidence (glossary only) — written in BOTH modes (Important-2), and now
    # for a cache HIT too (#6): a HIT's evidence must be (re)written so a prior failed write self-heals
    # on the very next upload instead of leaving graph_node.concept populated with no supporting
    # field_evidence forever. `_write_concept_evidence`'s input_hash reuse check makes this a safe
    # no-op when the evidence already exists and is unchanged — no duplicate/stale rows.
    if glossary is not None and source_snapshot_id is not None:
        # Values come from the corrected ``result`` (never the pre-critic ``resolved``): a REVISED
        # column's evidence must carry the revision, a REFUTED one is ``unclassified`` and writes
        # nothing (C3) — its old evidence was already staled by the acceptance hook.
        evidence_targets = {h: result[h] for h in set(resolved) | hit_hashes}
        failures = _write_concept_evidence(
            conn, resolved=evidence_targets, by_hash=by_hash, meta_by_hash=meta_by_hash,
            rec_by_tc=rec_by_tc, bindings=bindings, source_snapshot_id=source_snapshot_id,
            cache_hit_hashes=hit_hashes)
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


def _definition_targets(rows: list[CanonicalRow],
                        glossary: GlossaryUpload | None) -> set[str]:
    """The content hashes this run EXPECTS an AI definition for: a column with NO declared definition
    (R3 — a drafted one only ever fills a blank), MINUS the sanitizer-SUPPRESSED blanks (R5-3 —
    suppressed is not missing).

    ONE definition of the target set, shared by all three readers of it (T2b review finding): the
    drafter's own selection, ingest's honest expected count for the stage report, and the evidence
    reconciler's keep-set. Drift between them is asymmetric and dangerous — a reconciler narrower
    than the drafter would silently RETIRE a live target on a transient miss."""
    return ({content_hash(r) for r in rows if not r.definition}
            - suppressed_definition_hashes(rows, glossary))


def _summary_targets(rows: list[CanonicalRow],
                     glossary: GlossaryUpload | None = None) -> set[str]:
    """EVERY column. Deliberately not `_definition_targets`.

    The definition drafter only fills a blank ("a drafted one only ever fills a blank"), which is
    right for a field that carries SOURCE authority — a draft must never displace a declared value.
    The summary carries no such authority and lives in its own field, so it has no reason to defer to
    a declared description, and every reason not to: a source whose description column is filled by
    BUCKET (CIB: 47 distinct descriptions over 111 columns, one sentence covering 12) has a
    description present for every column and a useful one for almost none.

    Targeting every column also avoids inventing a "is this description good enough?" threshold,
    which nobody can set correctly and which would silently change what gets written as sources vary.
    """
    del glossary   # accepted for signature symmetry with _definition_targets; nothing is excluded
    return {content_hash(r) for r in rows}


def _summary_cache_key(row_hash: str, metadata: dict) -> str:
    """Keyed on the row AND the metadata the summary is written from, so enriching the payload
    re-drafts instead of serving a summary written from less information."""
    raw = json.dumps([row_hash, metadata], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def summary_payload(row: CanonicalRow, rec: GlossaryRecord | None,
                    extras: dict | None = None) -> dict:
    """The summary drafting payload AND its evidence material — ONE builder (richness Task 3 Step
    6c), so the ``input_hash`` always hashes exactly what was drafted from and the cache key
    auto-redrafts whenever the payload gets richer.

    Base = :func:`_concept_metadata` (deliberately UNTOUCHED — it is also the concept classifier's
    input and cache key) plus the sidecar fields the classifier payload never carried
    (``term_type`` / ``process_path`` / ``related_terms`` — the Step-6b lost fields). ``extras``
    is the ingest tail's enriched dossier slice (concept, party_role, grain/table roles, AI
    synonyms, a fallback domain) — short strings / lists of strings only, every value bounded to
    the egress cap; an empty value is never fabricated into the payload. An extras key never
    displaces a base (file-side) key: the file's curated metadata always wins."""
    meta = _concept_metadata(row, rec)
    if rec is not None:
        for key, val in (("term_type", rec.term_type), ("process_path", rec.process_path)):
            if val:
                meta[key] = val[:_MAX_META_LEN]
        if rec.related_terms:
            meta["related_terms"] = [t[:_MAX_META_LEN] for t in rec.related_terms]
    for key, val in sorted((extras or {}).items()):
        if not val or key in meta:
            continue
        meta[key] = ([v[:_MAX_META_LEN] for v in val] if isinstance(val, (list, tuple))
                     else str(val)[:_MAX_META_LEN])
    return meta


def draft_summaries(conn, rows: list[CanonicalRow], client: LLMClient, actor=None,
                    *, glossary: GlossaryUpload | None = None,
                    concepts: dict[str, str] | None = None,
                    ingestion_run_id: str | None = None,
                    stats: dict | None = None,
                    extras_by_hash: dict[str, dict] | None = None) -> dict[str, str]:
    """One plain-English summary per column, written from that column's full metadata.
    Returns ``{content_hash: summary}``.

    NEVER touches ``definition``: the source keeps its own text at its own authority, and a summary
    that overwrote it would both lose the specifics AND downgrade a source-attested fact to an
    llm-proposed one. The two coexist.

    The payload is :func:`summary_payload` — the classifier's full metadata plus the Step-6b
    sidecar fields, plus ``extras_by_hash`` (Step 6c): the ingest tail's enriched dossier slice
    per content hash (concept, party_role, grain/table roles, AI synonyms). The tail caller passes
    it so the ONE summary per ingest is a synthesis of everything the platform resolved, not a
    paraphrase of the file; the cache key hashes the payload, so a richer payload re-drafts by
    construction. ``_write_summary_evidence`` takes the SAME extras so the evidence material is
    exactly the drafting input."""
    rec_by_tc = _records_by_tc(glossary) if glossary is not None else {}
    extras_by_hash = extras_by_hash or {}

    def _rec_of(row: CanonicalRow) -> GlossaryRecord | None:
        return rec_by_tc.get((_norm(row.table), _norm(row.column)))

    targets = _summary_targets(rows, glossary)
    by_hash = {h: r for r in rows if (h := content_hash(r)) in targets}
    meta_by_hash = {h: summary_payload(r, _rec_of(r), extras_by_hash.get(h))
                    for h, r in by_hash.items()}
    key_of = {h: _summary_cache_key(h, meta_by_hash[h]) for h in by_hash}
    cached = _cache_get(conn, "enrichment_summary", list(key_of.values()), _SUMMARY_CACHE_VERSION)
    result = {h: cached[key_of[h]] for h in by_hash if key_of[h] in cached}

    misses = sorted((h for h in by_hash if h not in result), key=lambda h: (by_hash[h].table, h))
    if not misses:
        return result
    items = [BatchItem(h, meta_by_hash[h]) for h in misses]
    batch_report: dict = {}
    resolved = run_batched(
        conn, client, short="summary", task=_SUMMARY_TASK,
        prompt_id="overlay_summary_batch_v1", schema_id="overlay_summary_batch",
        shared_metadata={}, items=items, out_key="summary",
        instruction="Write ONE plain-English sentence describing EACH column, for a data catalog a "
                    "banker will read. Use that item's own term_name, type and attributes. The "
                    "supplied business_definition may be a CATEGORY label shared by many columns — "
                    "if so, say what THIS column specifically is, do not repeat it. Treat each item "
                    "independently; never reuse another item's facts; return exactly one result per "
                    "input ref.",
        accept=_accept_bounded(400), actor=actor,
        deadline_s=enrich_config.stage_deadline_s(), report=batch_report,
        ingestion_run_id=ingestion_run_id, dispatch_stage="enrich_summary",
        dispatch_subjects=({h: _column_subject(by_hash[h]) for h in misses}
                           if ingestion_run_id is not None else None))
    if stats is not None:
        stats["not_attempted"] = batch_report.get("not_attempted", 0)
    for h, text in resolved.items():
        _cache_put(conn, "enrichment_summary", key_of[h], text, _SUMMARY_CACHE_VERSION)
        result[h] = text
    return result


def draft_definitions(conn, rows: list[CanonicalRow], client: LLMClient, actor=None,
                      *, concepts: dict[str, str] | None = None,
                      glossary: GlossaryUpload | None = None,
                      ingestion_run_id: str | None = None,
                      stats: dict | None = None) -> dict[str, str]:
    """Draft a definition ONLY for columns with no declared one (R3). Keyed by (content_hash,
    assigned concept) so a concept change re-drafts (spec C6). Returns {content_hash: definition}.

    R5-3 (``glossary`` given): a sanitizer-SUPPRESSED blank (``definition_suppressed`` on the
    sidecar) is skipped — suppressed is not missing; it stays empty pending review, never silently
    LLM-drafted. Non-glossary callers are byte-for-byte unchanged.

    ``stats`` (optional out-param — return shape unchanged): in batch mode, receives
    ``not_attempted``, the count of items the budget/deadline cutoff skipped WITHOUT dispatch, so the
    caller's stage report labels a truncated run ``truncated`` rather than ``items_failed``.

    ``ingestion_run_id`` (C5-T5): with it, every dispatch is attributed to the run + the exact
    column subjects drafted (stage ``enrich_definition``); ``None`` is byte-for-byte today."""
    concepts = concepts or {}
    targets = _definition_targets(rows, glossary)   # blank, minus the suppressed (R3 + R5-3)
    blank = {h: r for r in rows if (h := content_hash(r)) in targets}
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
        batch_report: dict = {}   # honest-labeling: run_batched reports budget/deadline not_attempted
        resolved = run_batched(
            conn, client, short="definition", task=_DEF_TASK,
            prompt_id="overlay_definition_batch_v1", schema_id="overlay_definition_batch",
            shared_metadata={}, items=items, out_key="definition",
            instruction="Draft a one-line business definition for EACH column. Treat each item "
                        "independently: use only that item's table/column/type/concept; do not infer "
                        "relationships between items; do not reuse another item's facts; return "
                        "exactly one result per input ref.", accept=_accept_bounded(500), actor=actor,
            deadline_s=enrich_config.stage_deadline_s(),   # MF-4 — bound the source-lock hold
            report=batch_report,
            ingestion_run_id=ingestion_run_id, dispatch_stage="enrich_definition",
            dispatch_subjects=({h: _column_subject(blank[h]) for h in misses}
                               if ingestion_run_id is not None else None))
        if stats is not None:
            stats["not_attempted"] = batch_report.get("not_attempted", 0)
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
                                 actor,
                                 _single_ctx(ingestion_run_id, "enrich_definition",
                                             _column_subject(row))), 500)
        if drafted is None:
            continue   # failure / empty / over-long / list-stringified -> don't cache (M3/M9)
        _cache_put(conn, "enrichment_definition", key_of[h], drafted, _DEFINITION_CACHE_VERSION)
        result[h] = drafted
    return result


def classify_domains(conn, rows: list[CanonicalRow], client: LLMClient,
                     actor=None, *, ingestion_run_id: str | None = None,
                     stats: dict | None = None,
                     column_domains: dict[tuple[str, str], str] | None = None) -> dict[str, str]:
    """Classify each table's business domain (per-table), returning {table_name: domain} — the table
    DEFAULT, the context every one of its columns inherits.

    E1a T3 — TWO LEVELS: the classifier is asked for the table's domain PLUS only those columns
    whose domain DIFFERS from it. ``column_domains`` (optional out-param; the RETURN shape is
    deliberately unchanged, so every existing consumer is untouched) receives those OVERRIDES ONLY,
    keyed by normalized ``(table, column)``. A column absent from it INHERITS its table's domain —
    the classifier never restates the default per column, and nothing downstream may invent
    column-level evidence for an inherited value. Overrides ride the BATCH per-item schema; the flat
    single/fallback schema carries only the table domain, so that path yields a table default with
    no overrides (inheritance for every column — never a fabricated one).

    ``stats`` (optional out-param — return shape unchanged): in batch mode, receives
    ``not_attempted``, the count of tables the budget/deadline cutoff skipped WITHOUT dispatch, so
    the caller's stage report labels a truncated run ``truncated`` rather than ``items_failed``.

    ``ingestion_run_id`` (C5-T5): with it, every dispatch is attributed to the run + the TABLE
    subjects classified (stage ``enrich_domain``, ``field_names`` = the table's columns in the
    request); ``None`` is byte-for-byte today."""
    by_table: dict[str, list[str]] = {}
    source = rows[0].source if rows else ""   # rows share one source (foreign ones are quarantined)
    for r in rows:
        by_table.setdefault(r.table, []).append(r.column)

    hash_of_table = {t: _table_content_hash(source, t, cols) for t, cols in by_table.items()}
    cached = _cache_get(conn, "enrichment_domain", list(hash_of_table.values()), _DOMAIN_CACHE_VERSION)
    # {table: canonical two-level envelope} — one shape from BOTH modes and from the cache.
    envelopes = {t: cached[hash_of_table[t]] for t in by_table if hash_of_table[t] in cached}
    accept_domain = _accept_domain_result(64)

    if enrich_config.mode("domain") == "batch":
        misses = [BatchItem(t, {"table": t, "columns": sorted(cols)})
                  for t, cols in by_table.items() if hash_of_table[t] not in cached]
        batch_report: dict = {}   # honest-labeling: run_batched reports budget/deadline not_attempted
        resolved = run_batched(
            conn, client, short="domain", task=_DOMAIN_TASK, prompt_id="overlay_domain_batch_v1",
            schema_id="overlay_domain_batch", shared_metadata={}, items=misses, out_key="domain",
            instruction="For each item give the TABLE's business domain in `domain` — the default "
                        "context for all of its columns — and in `column_domains` list ONLY the "
                        "columns whose own domain DIFFERS from that table domain (omit every column "
                        "that shares it; an empty list is the normal answer). Return exactly one "
                        "result per input ref; treat each table independently.",
            accept=accept_domain, extract=_extract_domain_result, actor=actor,
            deadline_s=enrich_config.stage_deadline_s(),   # MF-4 — bound the source-lock hold
            report=batch_report,
            ingestion_run_id=ingestion_run_id, dispatch_stage="enrich_domain",
            dispatch_subjects=({t: _table_subject(source, t, cols)
                                for t, cols in by_table.items()}
                               if ingestion_run_id is not None else None))
        if stats is not None:
            stats["not_attempted"] = batch_report.get("not_attempted", 0)
        for table, envelope in resolved.items():
            _cache_put(conn, "enrichment_domain", hash_of_table[table], envelope,
                       _DOMAIN_CACHE_VERSION)
            envelopes[table] = envelope
    else:
        for table, cols in by_table.items():
            if table in envelopes:
                continue
            raw = _call(conn, client, _DOMAIN_TASK, "overlay_domain_v1", "overlay_domain",
                        {"table": table, "columns": sorted(cols)}, "domain",
                        "Classify this table's business domain.", actor,
                        _single_ctx(ingestion_run_id, "enrich_domain",
                                    _table_subject(source, table, cols)))
            if raw is None:
                continue   # provider failure / empty -> don't cache (M3)
            envelope, _reason = accept_domain(raw)
            if envelope is None:
                continue   # over-long / multiline / list-stringified / task-echo -> don't cache (M3/M9)
            _cache_put(conn, "enrichment_domain", hash_of_table[table], envelope,
                       _DOMAIN_CACHE_VERSION)
            envelopes[table] = envelope

    result: dict[str, str] = {}
    for table, envelope in envelopes.items():
        table_domain, overrides = _parse_domain_result(envelope)
        if not table_domain:
            continue
        result[table] = table_domain
        if column_domains is not None:
            for column, value in overrides.items():
                column_domains[(_norm(table), _norm(column))] = value
    return result


_SYN_INSTRUCTION = ("List the business SYNONYMS and common aliases for EACH column — the other names "
                    "a business user would search for it by. Return ONE comma-separated line per "
                    "item, terms only, no explanation. Treat each item independently: use only that "
                    "item's table/column/type/concept; return exactly one result per input ref.")


def draft_synonyms(conn, rows: list[CanonicalRow], client: LLMClient, actor=None,
                   *, concepts: dict[str, str] | None = None,
                   ingestion_run_id: str | None = None,
                   stats: dict | None = None) -> dict[str, str]:
    """Draft business synonyms for EVERY column; returns {content_hash: "term, term, term"} (E1a T4).

    Every column is a target — synonyms are ADDITIVE (they merge with the glossary's own terms), so
    unlike a definition there is no "only fill a blank" rule to apply. NO CACHE: E1a defers reuse, and
    the evidence writer supersedes-and-rewrites unconditionally.

    ``stats`` (optional out-param): in batch mode receives ``not_attempted``, the count the
    budget/deadline cutoff skipped WITHOUT dispatch, so the caller labels a truncated run
    ``truncated`` rather than ``items_failed``. ``ingestion_run_id`` (C5-T5) attributes every
    dispatch to the run + the column subjects drafted (stage ``enrich_synonyms``)."""
    concepts = concepts or {}
    by_hash = {content_hash(r): r for r in rows}
    accept = _accept_bounded(_MAX_SYNONYMS_LEN)

    if enrich_config.mode("synonyms") == "batch":
        # Group by table so table context is sent once; the prompt isolates items (anti-contamination).
        refs = sorted(by_hash, key=lambda h: (by_hash[h].table, h))
        items = [BatchItem(h, {"table": by_hash[h].table, "column": by_hash[h].column,
                               "type": by_hash[h].type,
                               **({"concept": concepts[h]} if concepts.get(h) else {})})
                 for h in refs]
        batch_report: dict = {}   # honest-labeling: run_batched reports budget/deadline not_attempted
        resolved = run_batched(
            conn, client, short="synonyms", task=_SYN_TASK,
            prompt_id="overlay_synonyms_batch_v1", schema_id="overlay_synonyms_batch",
            shared_metadata={}, items=items, out_key="synonyms",
            instruction=_SYN_INSTRUCTION, accept=accept, actor=actor,
            deadline_s=enrich_config.stage_deadline_s(),   # MF-4 — bound the source-lock hold
            report=batch_report,
            ingestion_run_id=ingestion_run_id, dispatch_stage="enrich_synonyms",
            dispatch_subjects=({h: _column_subject(by_hash[h]) for h in refs}
                               if ingestion_run_id is not None else None))
        if stats is not None:
            stats["not_attempted"] = batch_report.get("not_attempted", 0)
        return dict(resolved)

    result: dict[str, str] = {}
    for h, row in by_hash.items():                    # single mode — the per-item path
        raw = _call(conn, client, _SYN_TASK, "overlay_synonyms_v1", "overlay_synonyms",
                    {"table": row.table, "column": row.column, "type": row.type}, "synonyms",
                    _SYN_INSTRUCTION, actor,
                    _single_ctx(ingestion_run_id, "enrich_synonyms", _column_subject(row)))
        if raw is None:
            continue   # provider failure / empty
        drafted, _reason = accept(raw)
        if drafted is not None:
            result[h] = drafted
    return result


_UNIT_INSTRUCTION = (
    "For EACH item give the UNIT OF MEASURE the column's values are expressed in — the short token "
    "a data dictionary would print, never a sentence: a currency minor/major unit (AED, fils), a "
    "count noun (transactions, shares, days), a rate basis (%, bps) or a physical unit. When the "
    "column holds a MONETARY amount also give the ISO-4217 `currency` code (three letters, e.g. "
    "AED); omit `currency` entirely for a non-monetary measure. Treat each item independently: use "
    "only that item's table/column/type/concept; return exactly one result per input ref.")


def draft_units(conn, rows: list[CanonicalRow], client: LLMClient, actor=None,
                *, concepts: dict[str, str] | None = None,
                currencies: dict[str, str] | None = None,
                ingestion_run_id: str | None = None,
                stats: dict | None = None) -> dict[str, str]:
    """Draft the MEASURE ANNOTATION for the columns a unit is meaningful for and the file left blank
    (E4a T2); returns ``{content_hash: unit}``.

    THE POINT: a feature whose measure has no declared unit is created and flagged
    ``NEEDS_EXTERNAL_VALIDATION`` with a ``UNIT_CONSISTENT`` requirement that nobody could answer —
    the FTR export declares no unit and the LLM was never asked. It is asked here. What comes back
    is a PROPOSAL: it is stored as ``llm/proposed`` evidence (``_write_unit_evidence``), it cannot
    win field resolution, it never reaches ``graph_node.unit``, and it therefore cannot clear the
    requirement. A human confirms it (Task 3); the AI only ever drafts the answer.

    Targets are ``_unit_targets`` — the ONE definition the writer and ingest's expected count share.
    NO CACHE (like ``draft_synonyms``): the evidence writer supersedes-and-rewrites unconditionally,
    and reuse is a deferred optimization.

    ``currencies`` (optional out-param; the RETURN shape stays ``{hash: unit}`` so the stage report
    reads exactly like every other Pass A stage) receives ``{content_hash: ISO-4217 code}`` for the
    monetary measures only — a count or a percentage yields a unit with no currency, and nothing
    downstream may invent one for it.

    ``stats`` (optional out-param): in batch mode receives ``not_attempted``, the count the
    budget/deadline cutoff skipped WITHOUT dispatch, so the caller labels a truncated run
    ``truncated`` rather than ``items_failed``. ``ingestion_run_id`` (C5-T5) attributes every
    dispatch to the run + the exact column subjects drafted (stage ``enrich_unit``)."""
    concepts = concepts or {}
    targets = _unit_targets(rows, concepts)
    blank = {h: r for r in rows if (h := content_hash(r)) in targets}
    result: dict[str, str] = {}

    def _record(h: str, envelope: str) -> None:
        unit, currency = _parse_unit_result(envelope)
        if not unit:
            return
        result[h] = unit
        if currency and currencies is not None:
            currencies[h] = currency

    if enrich_config.mode("unit") == "batch":
        # Group by table so table context is sent once; the prompt isolates items (anti-contamination).
        refs = sorted(blank, key=lambda h: (blank[h].table, h))
        items = [BatchItem(h, {"table": blank[h].table, "column": blank[h].column,
                               "type": blank[h].type,
                               **({"concept": concepts[h]} if concepts.get(h) else {})})
                 for h in refs]
        batch_report: dict = {}   # honest-labeling: run_batched reports budget/deadline not_attempted
        resolved = run_batched(
            conn, client, short="unit", task=_UNIT_TASK, prompt_id="overlay_unit_batch_v1",
            schema_id="overlay_unit_batch", shared_metadata={}, items=items, out_key="unit",
            instruction=_UNIT_INSTRUCTION, accept=_accept_unit_result,
            extract=_extract_unit_result, actor=actor,
            deadline_s=enrich_config.stage_deadline_s(),   # MF-4 — bound the source-lock hold
            report=batch_report,
            ingestion_run_id=ingestion_run_id, dispatch_stage="enrich_unit",
            dispatch_subjects=({h: _column_subject(blank[h]) for h in refs}
                               if ingestion_run_id is not None else None))
        if stats is not None:
            stats["not_attempted"] = batch_report.get("not_attempted", 0)
        for h, envelope in resolved.items():
            _record(h, envelope)
        return result

    for h, row in blank.items():                      # single mode — the per-item path
        raw = _call(conn, client, _UNIT_TASK, "overlay_unit_v1", "overlay_unit",
                    {"table": row.table, "column": row.column, "type": row.type,
                     **({"concept": concepts[h]} if concepts.get(h) else {})}, "unit",
                    _UNIT_INSTRUCTION, actor,
                    _single_ctx(ingestion_run_id, "enrich_unit", _column_subject(row)))
        if raw is None:
            continue   # provider failure / empty
        envelope, _reason = _accept_unit_result(raw)
        if envelope is not None:
            _record(h, envelope)
    return result
