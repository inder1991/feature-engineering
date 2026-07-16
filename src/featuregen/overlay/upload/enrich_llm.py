"""Overlay-owned audited LLM call for catalog enrichment.

The direct `client.call()` path works only against FakeLLM: a real adapter (ClaudeLLM) fails closed
without an attached output-schema, and going around `call_llm` skips the egress guard + audit record.
But `call_llm` itself is coupled to the SP-2 feature-contract aggregate (it emits LLM_CALL_RECORDED on
a feature_contract). Catalog enrichment is not a feature contract, so we COMPOSE the same governance
from the decoupled building blocks — registered output-schema, reserved input keys, `assert_llm_safe`,
`drive_structured_call`, `record_llm_call` — under our own run bucket.

Enrichment inputs carry schema METADATA (names/types) plus — for a GLOSSARY column — its curated
business-semantic sidecar; the "intent" is a fixed instruction, never uploader free text or data
values. The structural names/types are classified `clean`; the sidecar's FREE-TEXT values
(definitions/synonyms/taxonomy paths) are uploader-authored and therefore NEVER presumed clean —
each rides through `redact_free_text` (finding #19), so the classification on the wire is what the
scan actually established ('clean' only post-scan; 'contains_pii' + scrubbed spans otherwise).
"""
from __future__ import annotations

import logging
import os
from contextvars import ContextVar

import psycopg

from featuregen.config import get_settings
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.contracts.identity import identity_to_jsonb
from featuregen.documents.registry import DocumentSchemaRegistry
from featuregen.intake.llm import (
    STATUS_FAILED,
    LLMClient,
    LLMRequest,
    compute_input_hash,
    drive_structured_call,
    record_llm_call,
)
from featuregen.intake.llm_claude import ClaudeConfig
from featuregen.intake.redaction import (
    EgressViolation,
    RedactionResult,
    assert_llm_safe,
    build_llm_inputs,
    redact_free_text,
)
from featuregen.overlay.upload.enrich_batch import (
    EGRESS,
    BatchCallResult,
    BatchItem,
    BatchItemOutcome,
    validate_batch_results,
)
from featuregen.security.audit import record_security_event

logger = logging.getLogger(__name__)

_OWNER = "featuregen-overlay"
# The audit run bucket for catalog enrichment llm_call records. Exposed as ENRICHMENT_RUN_ID so the
# evidence layer can use it as the `producer_ref` on Pass A field_evidence — tying each proposal back
# to the immutable llm_call records recorded under this same run bucket.
ENRICHMENT_RUN_ID = "overlay-enrichment"
_RUN = ENRICHMENT_RUN_ID
_REDACTION_VERSION = "metadata-only"  # structural names/types only — nothing to redact. When the
# metadata carries glossary FREE-TEXT, _redact_free_text_meta supplies the ACTUAL redactor version
# instead (finding #19) — this constant is never stamped on a call whose free-text was scanned.


# Finding #19: glossary sidecar values (business definitions, synonyms, data domains, BIAN/FIBO
# taxonomy paths, the term name) are uploader-authored FREE TEXT — not structural names/types — so
# they are never presumable-clean. Each rides through `redaction.redact_free_text` before egress:
# the deterministic scan (email/SSN/PAN/IBAN/phone/account/DOB/address) classifies + scrubs, and a
# REGISTERED IntentRedactor (`register_intent_redactor` — the NER seam redaction.py documents as
# the DEFERRED personal-NAMES closer) supersedes the default when present. A value the redactor
# fails closed on blocks the item (batch: excluded + audited; single: no dispatch).
_FREE_TEXT_META_KEYS = frozenset({
    "term_name", "business_definition", "synonyms", "data_domain", "bian_path", "fibo_path",
})


def _redact_free_text_meta(metadata: dict) -> tuple[dict | None, list[dict], str | None]:
    """Route every glossary free-text value in `metadata` (top-level keys + each column_profiles
    descriptor's business_definition) through `redact_free_text`. Returns
    ``(redacted_metadata, span_records, redaction_version)``:

    * ``redacted_metadata`` — the metadata with scrubbed free-text, or ``None`` when any value
      failed closed (the caller must not egress the item);
    * ``span_records`` — ``{"key", "type", "start", "end"}`` per scrubbed span (types/positions,
      NEVER values) for the llm_call ``input_redaction`` audit field;
    * ``redaction_version`` — the redactor version that scanned the free-text, or ``None`` when
      the metadata carried no free-text at all (pure structural names/types)."""
    out = dict(metadata)
    spans: list[dict] = []
    version: str | None = None

    def _one(text: str, key: str) -> str | None:          # None ⟹ fail closed
        nonlocal version
        res = redact_free_text(text)
        version = version or res.redaction_version
        if res.text is None:
            return None
        spans.extend({"key": key, **dict(s)} for s in res.redacted_spans)
        return res.text

    for key in sorted(_FREE_TEXT_META_KEYS & out.keys()):
        val = out[key]
        if isinstance(val, str):
            redacted = _one(val, key)
            if redacted is None:
                return None, spans, version
            out[key] = redacted
        elif isinstance(val, list):
            new_list = []
            for v in val:
                nv = _one(v, key) if isinstance(v, str) else v
                if nv is None:
                    return None, spans, version
                new_list.append(nv)
            out[key] = new_list
    profiles = out.get("column_profiles")
    if isinstance(profiles, list):
        new_profiles = []
        for desc in profiles:
            if isinstance(desc, dict) and isinstance(desc.get("business_definition"), str):
                nv = _one(desc["business_definition"], "column_profiles.business_definition")
                if nv is None:
                    return None, spans, version
                desc = {**desc, "business_definition": nv}
            new_profiles.append(desc)
        out["column_profiles"] = new_profiles
    if version is None:
        return metadata, [], None                          # no free-text — metadata untouched
    return out, spans, version


def _generation_settings() -> dict:
    """Generation settings for the audit record + idempotency key, read from the SAME env that
    configures the client (ClaudeConfig.from_env). So a real ClaudeLLM is audited as
    anthropic/<model> WITH the settings the adapter actually applies — model + max_tokens +
    thinking + effort (#24), which the adapter also treats as pinned — never just provider/model
    (and never the old hard-coded {"provider":"fake","model":"test"}, which made a production
    Claude call request model "test"). Defaults to fake/test with no provider set."""
    provider = os.environ.get("FEATUREGEN_LLM_PROVIDER", "fake")
    if provider == "anthropic":
        cfg = ClaudeConfig.from_env()
        return {"provider": "anthropic", "model": cfg.model, "max_tokens": cfg.max_tokens,
                "thinking": cfg.thinking, "effort": cfg.effort}
    return {"provider": "fake", "model": "test"}


def _audit_egress_block(conn, *, task: str, actor, reason: str) -> None:
    """A blocked egress is a security event (content was about to reach the LLM) — record it on the
    tamper-evident chain, not just the log (the redaction contract requires hard failures be audited).
    Best-effort: an audit failure (e.g. HMAC key unset) must never turn a safe block into a crash."""
    if actor is None:
        return
    try:
        record_security_event(conn, event_type="EGRESS_BLOCKED", actor=actor,
                              attempted_action=f"llm.{task}", decision="denied",
                              reason=reason or "egress guard blocked")
    except Exception:  # noqa: BLE001 — never let an audit failure mask the (correct) egress block
        logger.exception("failed to record EGRESS_BLOCKED security event for %s", task)


# #13 gap D: how many durable llm_call audit writes DEGRADED to the request connection in this
# request context. A ContextVar, not a module global: FastAPI runs each sync request in its own
# copied context, so counts can never bleed across concurrent requests. Incremented ONLY on the
# genuine degrade (DSN configured but the fresh connection failed) — the DSN-less harness writing
# on the request conn is the designed test path, not a degradation.
_AUDIT_DEGRADED: ContextVar[int] = ContextVar("overlay_llm_audit_degraded", default=0)


def consume_audit_degradations() -> int:
    """Return-and-reset the count of durable llm_call audit writes that degraded to the request
    connection since the last consume (#13 gap D). Ingest calls this at each enrichment stage
    boundary so the stage that carried the degraded call reports it in its detail."""
    n = _AUDIT_DEGRADED.get()
    if n:
        _AUDIT_DEGRADED.set(0)
    return n


def _record_llm_call_durable(conn, **record_kwargs) -> None:
    """Finding #20: by the time this runs, content has ALREADY egressed to the provider — so the
    immutable llm_call record must NOT share the upload transaction's fate (a later graph/DB
    failure in the same request would erase the evidence that data left the system). Mirror of the
    api.deps.audit_access_denied separate-connection pattern: gated on the production DSN being
    configured (unset ⟹ tests / no-DB harness, where a separately-committing connection would
    pollute the rolled-back test DB — same reasoning as that gate), and connecting with
    ``get_settings().dsn`` — the FULL configured DSN, exactly as audit_access_denied does. NOT
    ``conn.info.dsn``: psycopg3's ConnectionInfo.dsn strips the password, so in a password-auth
    deployment that connect fails and the record silently degrades to the request conn.

    The fresh connection performs ONE bare INSERT (record_llm_call) and NEVER takes an advisory
    lock — it cannot re-acquire the upload's ``pg_advisory_xact_lock`` and self-deadlock the way a
    second chain-locking security-audit connection did (program-audit I-3). NOT used for
    ``_audit_egress_block``: record_security_event's tamper-evident chain lock is exactly that
    hazard, so egress-block events stay on the request conn.

    Best-effort fallback: if the separate connection cannot be opened/committed, the record is
    written on the request conn — transactional evidence beats none — and the failure is logged."""
    dsn = get_settings().dsn
    if dsn:
        try:
            with psycopg.connect(dsn) as audit_conn:  # own tx, committed on `with` exit
                record_llm_call(audit_conn, **record_kwargs)
            return
        except Exception:  # noqa: BLE001 — degraded audit must never fail the (done) provider call
            logger.exception(
                "durable llm_call audit write failed; falling back to the request connection")
            # #13 gap D: the degradation is COUNTED (per request context) so the enrichment stage
            # that carried this call can report audit_degraded instead of a log-only trace.
            _AUDIT_DEGRADED.set(_AUDIT_DEGRADED.get() + 1)
    record_llm_call(conn, **record_kwargs)

# Structural output schemas for the three enrichment tasks (single string field each).
_SCHEMAS: dict[tuple[str, int], dict] = {
    ("overlay_concept", 1): {"type": "object", "additionalProperties": False,
                             "properties": {"concept": {"type": "string"}}, "required": ["concept"]},
    ("overlay_definition", 1): {"type": "object", "additionalProperties": False,
                                "properties": {"definition": {"type": "string"}},
                                "required": ["definition"]},
    ("overlay_domain", 1): {"type": "object", "additionalProperties": False,
                            "properties": {"domain": {"type": "string"}}, "required": ["domain"]},
    ("overlay_entity", 1): {"type": "object", "additionalProperties": False,
                            "properties": {"entity": {"type": "string"}}, "required": ["entity"]},
    ("overlay_contract", 1): {"type": "object", "additionalProperties": False,
                              "properties": {"definition": {"type": "string"}},
                              "required": ["definition"]},
    ("overlay_critique", 1): {"type": "object", "additionalProperties": False,
                              "properties": {"findings": {"type": "array",
                                                          "items": {"type": "string"}}},
                              "required": ["findings"]},
    # Batch array output-schemas (spec C18 — bounded arrays; `maxItems` is a generous backstop, app
    # validation enforces the real per-batch cap). One {ref, <out_key>} object per requested item.
    ("overlay_concept_batch", 1): {
        "type": "object", "additionalProperties": False,
        "properties": {"results": {"type": "array", "minItems": 0, "maxItems": 256,
            "items": {"type": "object", "additionalProperties": False,
                      "properties": {"ref": {"type": "string", "maxLength": 128},
                                     "concept": {"type": "string", "maxLength": 128}},
                      "required": ["ref", "concept"]}}},
        "required": ["results"]},
    ("overlay_definition_batch", 1): {
        "type": "object", "additionalProperties": False,
        "properties": {"results": {"type": "array", "minItems": 0, "maxItems": 256,
            "items": {"type": "object", "additionalProperties": False,
                      "properties": {"ref": {"type": "string", "maxLength": 128},
                                     "definition": {"type": "string", "maxLength": 500}},
                      "required": ["ref", "definition"]}}},
        "required": ["results"]},
    ("overlay_domain_batch", 1): {
        "type": "object", "additionalProperties": False,
        "properties": {"results": {"type": "array", "minItems": 0, "maxItems": 256,
            "items": {"type": "object", "additionalProperties": False,
                      "properties": {"ref": {"type": "string", "maxLength": 256},
                                     "domain": {"type": "string", "maxLength": 64}},
                      "required": ["ref", "domain"]}}},
        "required": ["results"]},
    # Table-synthesis (Pass B) output schemas. `_batch` is an array of per-item {ref, synthesis}
    # objects (batch harness treats `synthesis` as one structured out-key); the flat sibling is the
    # `_single_fallback` shape. `event_time_plus_lag` is intentionally EXCLUDED from as_of_basis:
    # FACT_VALUE_SCHEMAS mandates a `lag_hours` when basis == event_time_plus_lag (facts.py), and
    # Pass B has no way to infer a lag, so such a proposal would always be denied by
    # validate_fact_value. Phase 2 offers only the two lag-free bases; adding event_time_plus_lag
    # would require a lag_hours field end-to-end (out of scope).
    ("overlay_table_synth_batch", 1): {
        "type": "object", "additionalProperties": False,
        "properties": {"results": {"type": "array", "minItems": 0, "maxItems": 256,
            "items": {"type": "object", "additionalProperties": False,
                "properties": {
                    "ref": {"type": "string", "maxLength": 256},
                    "synthesis": {"type": "object", "additionalProperties": False,
                        "properties": {
                            "grain_columns": {"type": "array", "maxItems": 16,
                                              "items": {"type": "string", "maxLength": 128}},
                            "as_of_column": {"type": ["string", "null"], "maxLength": 128},
                            "as_of_basis": {"type": ["string", "null"],
                                            "enum": ["posted_at", "ingested_at", None]},
                            "primary_entity": {"type": ["string", "null"], "maxLength": 128},
                            "table_role": {"type": ["string", "null"], "maxLength": 64},
                            "event_or_snapshot": {"type": ["string", "null"],
                                                  "enum": ["event", "snapshot", None]},
                        }, "required": ["grain_columns"]}},
                "required": ["ref", "synthesis"]}}},
        "required": ["results"]},
    ("overlay_table_synth", 1): {
        "type": "object", "additionalProperties": False,
        "properties": {
            "grain_columns": {"type": "array", "maxItems": 16,
                              "items": {"type": "string", "maxLength": 128}},
            "as_of_column": {"type": ["string", "null"], "maxLength": 128},
            "as_of_basis": {"type": ["string", "null"],
                            "enum": ["posted_at", "ingested_at", None]},
            "primary_entity": {"type": ["string", "null"], "maxLength": 128},
            "table_role": {"type": ["string", "null"], "maxLength": 64},
            "event_or_snapshot": {"type": ["string", "null"],
                                  "enum": ["event", "snapshot", None]},
        }, "required": ["grain_columns"]},
    # Feature-assist output schemas (M6 — routed through the audited seam). Permissive object shapes:
    # the value is the LLM's proposal that the deterministic layer then grounds/validates.
    ("feature_ideas", 1): {"type": "object", "additionalProperties": True,
                           "properties": {"features": {"type": "array"}}},
    ("feature_recipe", 1): {"type": "object", "additionalProperties": True},
    ("leakage", 1): {"type": "object", "additionalProperties": True,
                     "properties": {"leaks": {"type": "array"}}},
    ("feature_set_rec", 1): {"type": "object", "additionalProperties": True},
    # LLM-2 candidate critic (SP-12 item 5): {"issues": [{"name","issue"}]} — advisory quality/fit notes.
    ("feature_candidate_critique", 1): {"type": "object", "additionalProperties": True,
                                        "properties": {"issues": {"type": "array"}}},
    # Intent-recognition (Phase-1A): the closed-shape recognition body. Structure only — the closed-
    # taxonomy semantics (id in registry, primary is a leaf) are a post-pass in recognizer.recognize.
    ("use_case_recognition", 1): {
        "type": "object", "additionalProperties": False,
        "properties": {
            "status": {"type": "string",
                       "enum": ["classified", "ambiguous", "unscoped", "technical_failure"]},
            "candidates": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "use_case_id": {"type": "string"},
                    "relationship": {"type": "string", "enum": ["primary", "secondary"]},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "evidence_spans": {"type": "array", "items": {"type": "string"}},
                    "rationale": {"type": "string"}},
                "required": ["use_case_id", "relationship", "confidence", "evidence_spans",
                             "rationale"]}},
            # Phase-2B optional intent dimensions. STRUCTURAL only (array-of-string / string|null) —
            # the closed-vocabulary semantics are a per-dimension, non-fatal post-pass
            # (recognition.normalize_dimensions), so a value outside the vocab never fails the call.
            "modelling_contexts": {"type": "array", "items": {"type": "string"}},
            "target_entity": {"type": ["string", "null"]},
            "ambiguity_note": {"type": ["string", "null"]}},
        "required": ["status", "candidates"]},
}

# Fallback service identity for when no real actor is threaded in. authenticated=False — a
# fabricated authenticated identity is forbidden outside sanctioned auth modules; production threads
# the real (authenticated) upload actor from ingest.
_ENRICH_ACTOR = IdentityEnvelope(
    subject="featuregen-overlay-enrichment", actor_kind="service",
    authenticated=False, auth_method="internal", role_claims=())


def register_enrichment_schemas(conn) -> None:
    """Register the enrichment output-schemas so the audited call can resolve/validate them.
    Idempotent (register_schema upserts). Called at overlay bootstrap."""
    reg = DocumentSchemaRegistry(conn)
    for (name, ver), schema in _SCHEMAS.items():
        reg.register_schema(name, ver, schema, _OWNER)


def audited_structured_call(conn, client: LLMClient, *, task: str, prompt_id: str, schema_id: str,
                            catalog_metadata: dict, instruction: str,
                            actor: IdentityEnvelope | None = None) -> dict | None:
    """Run one governed metadata-only call and return the VALIDATED output dict, or None on any egress
    block / non-success. Attaches the registered output-schema (so a real provider does NOT fail closed),
    runs the egress guard, and records one immutable llm_call. The single audited seam for every overlay
    LLM node — enrichment, contract authoring/refine, and contract critique."""
    actor = actor or _ENRICH_ACTOR
    reg = DocumentSchemaRegistry(conn)
    schema = reg.schema_for(schema_id, 1)
    if schema is None:                      # self-register on first use (idempotent) so a real
        register_enrichment_schemas(conn)   # provider never fails closed for lack of a schema.
        schema = reg.schema_for(schema_id, 1)

    # #19: glossary free-text in the metadata is scanned/scrubbed BEFORE the payload is built —
    # the classification below is what the scan established, never a hardcoded "clean".
    safe_metadata, spans, free_text_version = _redact_free_text_meta(dict(catalog_metadata))
    if safe_metadata is None:
        logger.warning("free-text redaction failed closed for %s (schema %s); no dispatch",
                       task, schema_id)
        _audit_egress_block(conn, task=task, actor=actor,
                            reason="glossary free-text redaction failed closed")
        return None                       # hard fail closed — no dispatch, no cache
    redaction_version = free_text_version or _REDACTION_VERSION
    redaction = RedactionResult(text=instruction, redaction_version=redaction_version,
                                redacted_spans=(), disposition="ok")
    inputs = build_llm_inputs(redaction, catalog_metadata=safe_metadata,
                              raw_input_classification="contains_pii" if spans else "clean")
    req = LLMRequest(
        task=task, prompt_id=prompt_id, prompt_version=1, inputs=inputs,
        output_schema_id=schema_id, output_schema_version=1,
        generation_settings=_generation_settings(),   # from env — NOT a hard-coded fake/test
        output_schema=schema)

    try:
        assert_llm_safe(req)              # §9.4 egress backstop
    except EgressViolation as exc:
        logger.warning("egress guard blocked %s (schema %s); no dispatch", task, schema_id)
        _audit_egress_block(conn, task=task, actor=actor, reason=str(exc))
        return None                       # hard fail closed — no dispatch, no cache

    outcome = drive_structured_call(
        client, req, lambda output: reg.validate(schema_id, 1, output))
    _record_llm_call_durable(   # #20: egress evidence survives an upload-transaction rollback
        conn, run_id=_RUN, request=req, input_hash=compute_input_hash(req.inputs),
        redaction_version=redaction_version,
        input_redaction={"redacted_spans": spans} if spans else {},
        raw_output={"output": outcome.output, "self_reported_scores": outcome.self_reported_scores},
        validation_result=outcome.validation_result, repair_attempts=list(outcome.repair_attempts),
        latency_ms=None, cost_metadata=outcome.cost_metadata, created_by=identity_to_jsonb(actor))

    if outcome.status == STATUS_FAILED:
        logger.warning("enrichment call %s (schema %s) failed: %s", task, schema_id,
                       outcome.validation_result)
        return None                       # provider/repair failure -> don't cache
    return outcome.output if isinstance(outcome.output, dict) else None


def audited_enrich_call(conn, client: LLMClient, *, task: str, prompt_id: str, schema_id: str,
                        catalog_metadata: dict, out_key: str, instruction: str,
                        actor: IdentityEnvelope | None = None) -> str | None:
    """Single-string convenience over `audited_structured_call`: returns the trimmed `out_key` field, or
    None on any egress block / non-success / empty output (so the caller never caches a failure)."""
    out = audited_structured_call(
        conn, client, task=task, prompt_id=prompt_id, schema_id=schema_id,
        catalog_metadata=catalog_metadata, instruction=instruction, actor=actor)
    if not out:
        return None
    val = str(out.get(out_key, "")).strip()
    return val or None


# Only metadata may egress per item (Global Constraint). Any other key (e.g. a data value, or a
# TECHNICAL upload's uploader-authored `definition` free text — M4 PII risk) means the item is
# excluded pre-egress and audited (spec C9 per-item egress). Besides the structural keys
# (table/column/type/columns/concept), a GLOSSARY column carries curated business-semantic metadata
# from its sidecar — the business term, its curated business definition, synonyms/aliases, data
# domain, and BIAN/FIBO taxonomy paths. These are MEANING (semantics about the column), not raw data
# values, so they pass the per-item egress filter under DISTINCT keys — deliberately NOT the plain
# `definition` key (which stays forbidden, so a technical free-text definition can never ride through
# this seam). Being MEANING does not make them CLEAN: each free-text value is then scanned/scrubbed
# by `_redact_free_text_meta` (finding #19), and the batch-level `assert_llm_safe` scan still
# applies on top.
_ITEM_META_ALLOWED = frozenset({
    "table", "column", "type", "columns", "concept",
    "term_name", "business_definition", "synonyms", "data_domain", "bian_path", "fibo_path",
    "column_profiles",
})

# The ONLY keys a per-column descriptor may carry, each a short scalar. `definition` is deliberately
# ABSENT — a technical free-text definition can never ride this seam; a curated meaning rides as
# `business_definition` (already stripped of sample values upstream). The role fields
# (identifier_role/temporal_role/semantic_type/entity) come from Pass A evidence and sharpen grain
# proposals (an identifier-role column is grain-eligible; a temporal-role column is as-of-eligible).
_COLUMN_PROFILE_KEYS = frozenset({
    "column", "type", "concept", "business_definition",
    "identifier_role", "temporal_role", "semantic_type", "entity",
})
_MAX_COLUMN_PROFILES = 64


def _column_profile_ok(desc: object) -> bool:
    if not isinstance(desc, dict):
        return False
    if any(k not in _COLUMN_PROFILE_KEYS for k in desc):
        return False
    return all(isinstance(v, str) and len(v) <= 200 for v in desc.values())


def _item_egress_ok(metadata: dict) -> bool:
    if any(k not in _ITEM_META_ALLOWED for k in metadata):
        return False
    for k, v in metadata.items():
        if k == "column_profiles":
            if not isinstance(v, list) or len(v) > _MAX_COLUMN_PROFILES:
                return False
            if not all(_column_profile_ok(d) for d in v):
                return False
        elif isinstance(v, list):
            if not all(isinstance(x, str) and len(x) <= 200 for x in v):
                return False
        elif not isinstance(v, str) or len(v) > 200:
            return False
    return True


def audited_batch_call(conn, client: LLMClient, *, task: str, prompt_id: str, schema_id: str,
                       shared_metadata: dict, items: list[BatchItem], out_key: str, instruction: str,
                       accept, actor: IdentityEnvelope | None = None,
                       extract=None, ref_aware: bool = False) -> BatchCallResult:
    """One GOVERNED batch call (spec C4/C9): per-item egress filter -> batch-level egress guard ->
    schema-validated array call -> one immutable llm_call with a per-item outcome summary. Returns a
    BatchCallResult whose outcomes classify every requested ref (via validate_batch_results)."""
    actor = actor or _ENRICH_ACTOR
    excluded = [it for it in items if not _item_egress_ok(it.metadata)]
    included = [it for it in items if _item_egress_ok(it.metadata)]
    egress_outcomes = [BatchItemOutcome(it.ref, EGRESS, None, (EGRESS,)) for it in excluded]
    for _it in excluded:
        _audit_egress_block(conn, task=task, actor=actor, reason="item metadata not metadata-only")

    # #19: per-item free-text scan/scrub (spec C9 grain — a fail-closed value excludes ITS item,
    # never the batch). The classification below is what the scan established, never a hardcoded
    # "clean"; scrubbed span types/positions are recorded on the llm_call audit row.
    safe_items: list[BatchItem] = []
    all_spans: list[dict] = []
    free_text_version: str | None = None
    for it in included:
        meta, spans, version = _redact_free_text_meta(it.metadata)
        free_text_version = free_text_version or version
        if meta is None:
            egress_outcomes.append(BatchItemOutcome(it.ref, EGRESS, None, (EGRESS,)))
            _audit_egress_block(conn, task=task, actor=actor,
                                reason="glossary free-text redaction failed closed")
            continue
        safe_items.append(BatchItem(it.ref, meta))
        all_spans.extend({"ref": it.ref, **s} for s in spans)
    included = safe_items

    if not included:
        return BatchCallResult(tuple(egress_outcomes), 0, 0, 0)

    reg = DocumentSchemaRegistry(conn)
    schema = reg.schema_for(schema_id, 1)
    if schema is None:
        register_enrichment_schemas(conn)
        schema = reg.schema_for(schema_id, 1)

    catalog_metadata = {**shared_metadata,
                        "items": [{"ref": it.ref, **it.metadata} for it in included]}
    redaction_version = free_text_version or _REDACTION_VERSION
    redaction = RedactionResult(text=instruction, redaction_version=redaction_version,
                                redacted_spans=(), disposition="ok")
    inputs = build_llm_inputs(redaction, catalog_metadata=catalog_metadata,
                              raw_input_classification="contains_pii" if all_spans else "clean")
    req = LLMRequest(task=task, prompt_id=prompt_id, prompt_version=1, inputs=inputs,
                     output_schema_id=schema_id, output_schema_version=1,
                     generation_settings=_generation_settings(), output_schema=schema)

    try:
        assert_llm_safe(req)                      # batch-level egress backstop (spec C9)
    except EgressViolation as exc:
        logger.warning("egress guard blocked batch %s (schema %s); no dispatch", task, schema_id)
        _audit_egress_block(conn, task=task, actor=actor, reason=str(exc))
        missing = validate_batch_results(included, [], out_key, accept,
                                         extract=extract, ref_aware=ref_aware)
        return BatchCallResult(tuple(egress_outcomes) + tuple(missing), 0, 0, 0)

    outcome = drive_structured_call(client, req, lambda o: reg.validate(schema_id, 1, o))
    # A repair-exhausted / truncated batch (STATUS_FAILED) carries an UNVERIFIED body — do not harvest
    # it. Treat it as empty so validate_batch_results marks every requested ref MISSING and the
    # orchestrator's fallback ladder recovers it. Mirrors audited_structured_call returning None on
    # STATUS_FAILED: otherwise a truncated definition/domain value (validated only by `accept`) would
    # be cached durably and never retried (whole-branch review, BLOCKING).
    results = (outcome.output.get("results", [])
               if outcome.status != STATUS_FAILED and isinstance(outcome.output, dict) else [])
    item_outcomes = validate_batch_results(included, results, out_key, accept,
                                           extract=extract, ref_aware=ref_aware)

    summary = {"requested": [it.ref for it in included],
               "outcomes": {o.ref: o.status for o in item_outcomes}}
    cost = dict(outcome.cost_metadata or {})
    _record_llm_call_durable(   # #20: egress evidence survives an upload-transaction rollback
        conn, run_id=_RUN, request=req, input_hash=compute_input_hash(req.inputs),
        redaction_version=redaction_version,
        input_redaction={"redacted_spans": all_spans} if all_spans else {},
        raw_output={"output": outcome.output,
                    "self_reported_scores": outcome.self_reported_scores},
        validation_result=outcome.validation_result,
        repair_attempts=list(outcome.repair_attempts), latency_ms=None,
        cost_metadata={**cost, "batch": summary}, created_by=identity_to_jsonb(actor))

    return BatchCallResult(
        outcomes=tuple(egress_outcomes) + tuple(item_outcomes),
        # #21: the ACTUAL provider requests the driver issued (initial + repairs + retries) — never
        # a hardcoded 1, so the caller's provider-call budget reflects reality.
        provider_calls=outcome.provider_calls,
        input_tokens=int(cost.get("input_tokens", 0)), output_tokens=int(cost.get("output_tokens", 0)))
