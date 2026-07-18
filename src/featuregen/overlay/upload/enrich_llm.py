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
from featuregen.overlay.upload.sanitize import sanitize_definition
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
# they are never presumable-clean. Phase-2 Slice 1 makes the boundary FIELD-AWARE: the two curated
# DEFINITION fields (business_definition + the table-level table_definition) can EMBED raw sample
# values in prose, so they route through `sanitize.sanitize_definition` (sample-clause strip +
# fail-closed data-marker scan + PII redaction); every other free-text field is PII-only via
# `redaction.redact_free_text`. The deterministic scan (email/SSN/PAN/IBAN/phone/account/DOB/
# address) classifies + scrubs, and a REGISTERED IntentRedactor (`register_intent_redactor` — the
# NER seam redaction.py documents as the DEFERRED personal-NAMES closer) supersedes the default
# when present (sanitize_definition's PII step rides the same seam). A value that fails closed
# (redactor failure, or a definition the sanitizer blanks) blocks the ITEM (batch: excluded +
# audited; single: no dispatch).
_DEFINITION_META_KEYS = frozenset({"business_definition", "table_definition"})
# [F6] `synonyms` is prose emitted as list[str] (enrich.py) — a LIST of prose values, each
# PII-scanned per item and audited at an indexed path (`synonyms[0]`).
_LIST_PROSE_META_KEYS = frozenset({"synonyms"})
_SCALAR_PROSE_META_KEYS = frozenset({"term_name", "data_domain", "bian_path", "fibo_path"})
_PROSE_META_KEYS = _SCALAR_PROSE_META_KEYS | _LIST_PROSE_META_KEYS
_FREE_TEXT_META_KEYS = _DEFINITION_META_KEYS | _PROSE_META_KEYS


def _meta_field_kind(key: str) -> str:
    """The egress KIND of one free-text metadata key: ``definition`` (sample-strip + PII via
    `sanitize_definition`), ``prose`` (PII-only via `redact_free_text`), or ``list_of_prose``
    (per-item PII). A free-text key with NO declared kind is a hard error ([F6] fail closed) —
    a new key must be classified before anything under it can egress, never silently routed
    down the weaker prose path."""
    if key in _DEFINITION_META_KEYS:
        return "definition"
    if key in _LIST_PROSE_META_KEYS:
        return "list_of_prose"
    if key in _SCALAR_PROSE_META_KEYS:
        return "prose"
    raise ValueError(f"free-text metadata key {key!r} has no declared egress kind (fail closed)")


def _redact_free_text_meta(metadata: dict) -> tuple[dict | None, list[dict], list[dict], str | None]:
    """Route every glossary free-text value in `metadata` (top-level keys + each column_profiles
    descriptor's business_definition) through its FIELD-KIND sanitizer (`_meta_field_kind`).
    Returns ``(redacted_metadata, pii_spans, sample_audits, redaction_version)``:

    * ``redacted_metadata`` — the metadata with sanitized free-text, or ``None`` when any value
      failed closed (the caller must not egress the item): a prose value the redactor returned
      ``None`` for, or a definition `sanitize_definition` BLANKED (``suspected_unhandled`` marker
      or ``pii_redaction_failed``);
    * ``pii_spans`` — ``{"key", "type", "start", "end"}`` per scrubbed PII span (types/positions,
      NEVER values) for ``input_redaction["redacted_spans"]``. Definition fields contribute their
      `sanitize_definition` spans at the same granularity ([F3]); list items are keyed at their
      indexed path (``synonyms[0]``);
    * ``sample_audits`` — ``{"path", "sanitizer_version", "state", "removed_count"}`` per
      DEFINITION field processed (prose fields never emit one) for
      ``input_redaction["sample_strip"]``;
    * ``redaction_version`` — the redactor/sanitizer version that scanned the free-text, or
      ``None`` when the metadata carried no free-text at all (pure structural names/types)."""
    out = dict(metadata)
    pii_spans: list[dict] = []
    sample_audits: list[dict] = []
    version: str | None = None

    def _definition(text: str, path: str) -> str | None:  # None ⟹ fail closed (blanked field)
        nonlocal version
        d = sanitize_definition(text)
        version = version or d.redaction_version or d.sanitizer_version
        sample_audits.append({"path": path, "sanitizer_version": d.sanitizer_version,
                              "state": d.state, "removed_count": d.removed})
        if d.reason:
            # The sanitizer blanked the field (unhandled data marker, or its PII redaction failed
            # closed): nothing provably safe — block the whole item, matching the prose contract.
            return None
        # [F3]: the definition's PII spans keep reaching input_redaction["redacted_spans"] at the
        # same granularity as prose fields — the sample audit above is IN ADDITION, not instead.
        pii_spans.extend({"key": path, **dict(s)} for s in d.redacted_spans)
        return d.clean

    def _prose(text: str, path: str) -> str | None:        # None ⟹ fail closed
        nonlocal version
        res = redact_free_text(text)
        version = version or res.redaction_version
        if res.text is None:
            return None
        pii_spans.extend({"key": path, **dict(s)} for s in res.redacted_spans)
        return res.text

    for key in sorted(_FREE_TEXT_META_KEYS & out.keys()):
        kind = _meta_field_kind(key)                       # ValueError on an unclassified key
        scrub = _definition if kind == "definition" else _prose
        val = out[key]
        if isinstance(val, str):
            redacted = scrub(val, key)
            if redacted is None:
                return None, pii_spans, sample_audits, version
            out[key] = redacted
        elif isinstance(val, list):
            new_list = []
            for i, v in enumerate(val):
                nv = scrub(v, f"{key}[{i}]") if isinstance(v, str) else v
                if nv is None:
                    return None, pii_spans, sample_audits, version
                new_list.append(nv)
            out[key] = new_list
    profiles = out.get("column_profiles")
    if isinstance(profiles, list):
        new_profiles = []
        for desc in profiles:
            if isinstance(desc, dict) and isinstance(desc.get("business_definition"), str):
                nv = _definition(desc["business_definition"],
                                 "column_profiles.business_definition")
                if nv is None:
                    return None, pii_spans, sample_audits, version
                desc = {**desc, "business_definition": nv}
            new_profiles.append(desc)
        out["column_profiles"] = new_profiles
    if version is None:
        return metadata, [], [], None                      # no free-text — metadata untouched
    return out, pii_spans, sample_audits, version


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
    # Batch array output-schemas (spec C18). NO `minItems`/`maxItems` on ANY array: the Anthropic
    # structured-output API rejects array `maxItems` with HTTP 400 ("For 'array' type, property
    # 'maxItems' is not supported"), which would fail EVERY batch call closed. The real per-batch
    # count cap is code-enforced by `validate_batch_results` (extra/duplicate refs against the
    # REQUESTED ref-set), and the input side is bounded by `chunk_items` / `_MAX_COLUMN_PROFILES`, so
    # dropping the schema cap loses no real bound. One {ref, <out_key>} object per requested item.
    ("overlay_concept_batch", 1): {
        "type": "object", "additionalProperties": False,
        "properties": {"results": {"type": "array",
            "items": {"type": "object", "additionalProperties": False,
                      "properties": {"ref": {"type": "string", "maxLength": 128},
                                     "concept": {"type": "string", "maxLength": 128}},
                      "required": ["ref", "concept"]}}},
        "required": ["results"]},
    ("overlay_definition_batch", 1): {
        "type": "object", "additionalProperties": False,
        "properties": {"results": {"type": "array",
            "items": {"type": "object", "additionalProperties": False,
                      "properties": {"ref": {"type": "string", "maxLength": 128},
                                     "definition": {"type": "string", "maxLength": 500}},
                      "required": ["ref", "definition"]}}},
        "required": ["results"]},
    ("overlay_domain_batch", 1): {
        "type": "object", "additionalProperties": False,
        "properties": {"results": {"type": "array",
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
        "properties": {"results": {"type": "array",
            "items": {"type": "object", "additionalProperties": False,
                "properties": {
                    "ref": {"type": "string", "maxLength": 256},
                    "synthesis": {"type": "object", "additionalProperties": False,
                        "properties": {
                            "grain_columns": {"type": "array",
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
            "grain_columns": {"type": "array",
                              "items": {"type": "string", "maxLength": 128}},
            "as_of_column": {"type": ["string", "null"], "maxLength": 128},
            "as_of_basis": {"type": ["string", "null"],
                            "enum": ["posted_at", "ingested_at", None]},
            "primary_entity": {"type": ["string", "null"], "maxLength": 128},
            "table_role": {"type": ["string", "null"], "maxLength": 64},
            "event_or_snapshot": {"type": ["string", "null"],
                                  "enum": ["event", "snapshot", None]},
        }, "required": ["grain_columns"]},
    # Table-synthesis PHASE 1 (#1 — wide tables): per-column-CHUNK summary. NO fact output — a compact
    # digest of one <=_MAX_COLUMN_PROFILES chunk (candidate grain/id + temporal/as-of columns, entity
    # signals, event/snapshot hint) that later feeds the SINGLE per-table synthesis (phase 2) alongside
    # a complete roster, so a table wider than the egress cap is never sent as one giant profile list.
    # Batch-only (ref_aware, no single seam), so only the `_batch` shape exists.
    ("overlay_table_synth_summary_batch", 1): {
        "type": "object", "additionalProperties": False,
        "properties": {"results": {"type": "array",
            "items": {"type": "object", "additionalProperties": False,
                "properties": {
                    "ref": {"type": "string", "maxLength": 256},
                    "summary": {"type": "object", "additionalProperties": False,
                        "properties": {
                            "grain_candidates": {"type": "array",
                                                 "items": {"type": "string", "maxLength": 128}},
                            "temporal_candidates": {"type": "array",
                                                    "items": {"type": "string", "maxLength": 128}},
                            "entity_signals": {"type": "array",
                                               "items": {"type": "string", "maxLength": 128}},
                            "event_or_snapshot": {"type": ["string", "null"],
                                                  "enum": ["event", "snapshot", None]},
                        }, "required": []}},
                "required": ["ref", "summary"]}}},
        "required": ["results"]},
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
    Idempotent (register_schema upserts). Called at overlay bootstrap.

    Fail-closed provider-compat guard (Phase-1 hardening): before touching the DB, assert every
    canonical schema PROJECTS to an Anthropic-compatible wire schema. A node that survives the
    projection still provider-incompatible (e.g. a bare `{maxLength}` leaving an empty node) raises
    ValueError HERE — at bootstrap — instead of failing every live structured-output call closed."""
    from featuregen.intake.schema_projection import (
        assert_schemas_provider_compatible,
        project_for_anthropic,
    )
    assert_schemas_provider_compatible(
        [(name, project_for_anthropic(schema)) for (name, _v), schema in _SCHEMAS.items()])
    reg = DocumentSchemaRegistry(conn)
    for (name, ver), schema in _SCHEMAS.items():
        reg.register_schema(name, ver, schema, _OWNER)


def audited_structured_call(conn, client: LLMClient, *, task: str, prompt_id: str, schema_id: str,
                            catalog_metadata: dict, instruction: str,
                            actor: IdentityEnvelope | None = None,
                            prompt_version: int = 1, schema_version: int = 1) -> dict | None:
    """Run one governed metadata-only call and return the VALIDATED output dict, or None on any egress
    block / non-success. Attaches the registered output-schema (so a real provider does NOT fail closed),
    runs the egress guard, and records one immutable llm_call. The single audited seam for every overlay
    LLM node — enrichment, contract authoring/refine, and contract critique.

    ``prompt_version``/``schema_version`` pin the request's contract (default ``1`` — byte-for-byte the
    v1 behavior): the resolved output-schema, the stamped request versions, and the validation all use
    them, so a versioned enrichment call cannot silently egress under the v1 contract."""
    actor = actor or _ENRICH_ACTOR
    reg = DocumentSchemaRegistry(conn)
    schema = reg.schema_for(schema_id, schema_version)
    if schema is None:                      # self-register on first use (idempotent) so a real
        register_enrichment_schemas(conn)   # provider never fails closed for lack of a schema.
        schema = reg.schema_for(schema_id, schema_version)

    # #19: glossary free-text in the metadata is scanned/scrubbed BEFORE the payload is built —
    # the classification below is what the scan established, never a hardcoded "clean".
    safe_metadata, spans, sample_audits, free_text_version = _redact_free_text_meta(
        dict(catalog_metadata))
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
        task=task, prompt_id=prompt_id, prompt_version=prompt_version, inputs=inputs,
        output_schema_id=schema_id, output_schema_version=schema_version,
        generation_settings=_generation_settings(),   # from env — NOT a hard-coded fake/test
        output_schema=schema)

    try:
        assert_llm_safe(req)              # §9.4 egress backstop
    except EgressViolation as exc:
        logger.warning("egress guard blocked %s (schema %s); no dispatch", task, schema_id)
        _audit_egress_block(conn, task=task, actor=actor, reason=str(exc))
        return None                       # hard fail closed — no dispatch, no cache

    outcome = drive_structured_call(
        client, req, lambda output: reg.validate(schema_id, schema_version, output))
    _record_llm_call_durable(   # #20: egress evidence survives an upload-transaction rollback
        conn, run_id=_RUN, request=req, input_hash=compute_input_hash(req.inputs),
        redaction_version=redaction_version,
        input_redaction=({"redacted_spans": spans, "sample_strip": sample_audits}
                         if (spans or sample_audits) else {}),
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
                        actor: IdentityEnvelope | None = None,
                        prompt_version: int = 1, schema_version: int = 1) -> str | None:
    """Single-string convenience over `audited_structured_call`: returns the trimmed `out_key` field, or
    None on any egress block / non-success / empty output (so the caller never caches a failure).
    ``prompt_version``/``schema_version`` (default ``1``) thread straight to the structured seam."""
    out = audited_structured_call(
        conn, client, task=task, prompt_id=prompt_id, schema_id=schema_id,
        catalog_metadata=catalog_metadata, instruction=instruction, actor=actor,
        prompt_version=prompt_version, schema_version=schema_version)
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
    "term_name", "business_definition", "table_definition",
    "synonyms", "data_domain", "bian_path", "fibo_path",
    "column_profiles",
    # Pass B phase-2 (#1 — wide tables): the per-chunk summaries (bounded structured digests) and the
    # complete column ROSTER (short `name:type` strings). Both are MEANING/structure, not data values,
    # and are bounded field-by-field below — the phase-2 item carries these INSTEAD of a giant
    # column_profiles list, so a >64-col table's synthesis input still passes the per-item egress cap.
    "chunk_summaries", "column_roster",
})

# The ONLY keys a per-column descriptor may carry, each a short scalar. `definition` is deliberately
# ABSENT — a technical free-text definition can never ride this seam; a curated meaning rides as
# `business_definition` (already stripped of sample values upstream). The role fields
# (identifier_role/temporal_role/semantic_type/entity) come from Pass A evidence and sharpen grain
# proposals (an identifier-role column is grain-eligible; a temporal-role column is as-of-eligible).
# The FTR-sidecar facets (term_type/domain/process_path — MF-2) come from the GlossaryRecord so the
# synthesizer reasons over the column's business classification, not just its physical name/type;
# they are bounded structural tokens (the default 200 cap via `_max_len_for`), never data values.
_COLUMN_PROFILE_KEYS = frozenset({
    "column", "type", "concept", "business_definition",
    "identifier_role", "temporal_role", "semantic_type", "entity",
    "term_type", "domain", "process_path",
})
_MAX_COLUMN_PROFILES = 64

# A phase-2 chunk-summary (#1) carries ONLY column-name lists + an event/snapshot enum — bounded,
# egress-safe, and column-name-shaped (never a data value). `event_or_snapshot` is the lone scalar
# (nullable enum); the three `*_candidates`/`entity_signals` keys are short string lists. A summary
# with any other key (or a non-list/oversized value) is rejected pre-egress.
_CHUNK_SUMMARY_LIST_KEYS = frozenset({
    "grain_candidates", "temporal_candidates", "entity_signals",
})
_CHUNK_SUMMARY_KEYS = _CHUNK_SUMMARY_LIST_KEYS | {"event_or_snapshot"}
# A wide table produces ceil(ncols/_MAX_COLUMN_PROFILES) chunk summaries; the generous cap backstops a
# pathological column count without unbounding the phase-2 payload.
_MAX_CHUNK_SUMMARIES = 256


# The single source of truth for the SANITIZED business-definition length bound (DRY): re-exported by
# `enrich` (as `MAX_DEFINITION_LEN`, with the private `_MAX_DEFINITION_LEN` alias) and consumed by
# `table_synth._descriptor` — so `enrich.bounded_definition`'s window, the metadata-only egress cap,
# and Pass B's descriptor bound can never drift apart. Defined HERE (not in `enrich`) because `enrich`
# imports `enrich_llm`, so this module is the cycle-free home for the shared constant.
MAX_DEFINITION_LEN = 600

# Per-value egress length cap. Every scalar is capped at 200 EXCEPT the two sanitized DEFINITION
# fields — the intended metadata payload — which get a larger (still-bounded) window so a real
# definition is not cut mid-sentence before it egresses. `business_definition` matches
# `enrich.bounded_definition`'s bound (same constant); [F7] gives the table-level
# `table_definition` the SAME 600 window (it previously inherited the 200 default).
_MAX_LEN_DEFAULT = 200
_MAX_LEN_BY_KEY = {"business_definition": MAX_DEFINITION_LEN,
                   "table_definition": MAX_DEFINITION_LEN}


def _max_len_for(key: str) -> int:
    return _MAX_LEN_BY_KEY.get(key, _MAX_LEN_DEFAULT)


def _column_profile_shape_ok(desc: object) -> bool:
    if not isinstance(desc, dict):
        return False
    if any(k not in _COLUMN_PROFILE_KEYS for k in desc):
        return False
    return all(isinstance(v, str) for v in desc.values())


def _column_profile_len_ok(desc: dict) -> bool:
    return all(len(v) <= _max_len_for(k) for k, v in desc.items() if isinstance(v, str))


def _column_profile_ok(desc: object) -> bool:
    return _column_profile_shape_ok(desc) and _column_profile_len_ok(desc)


def _chunk_summary_shape_ok(summary: object) -> bool:
    if not isinstance(summary, dict):
        return False
    if any(k not in _CHUNK_SUMMARY_KEYS for k in summary):
        return False
    for k, v in summary.items():
        if k == "event_or_snapshot":
            if v is not None and not isinstance(v, str):
                return False
        elif not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            return False
    return True


def _chunk_summary_len_ok(summary: dict) -> bool:
    for k, v in summary.items():
        if k == "event_or_snapshot":
            if isinstance(v, str) and len(v) > 64:
                return False
        elif isinstance(v, list) and any(isinstance(x, str) and len(x) > 128 for x in v):
            return False
    return True


def _chunk_summary_ok(summary: object) -> bool:
    return _chunk_summary_shape_ok(summary) and _chunk_summary_len_ok(summary)


def _item_shape_ok(metadata: dict) -> bool:
    """[F7] SHAPE/allowlist half of the per-item egress gate — runs BEFORE `_redact_free_text_meta`:
    allowlisted keys only, correct value types + list structure, count caps. Per-value LENGTH is
    deliberately NOT checked here: a long RAW definition may sanitize (sample-clause strip) to
    within its bound, so the length gate (`_item_len_ok`) runs AFTER sanitization instead — the
    old combined pre-redaction gate excluded such items before the sanitizer could shorten them."""
    if any(k not in _ITEM_META_ALLOWED for k in metadata):
        return False
    for k, v in metadata.items():
        if k == "column_profiles":
            if not isinstance(v, list) or len(v) > _MAX_COLUMN_PROFILES:
                return False
            if not all(_column_profile_shape_ok(d) for d in v):
                return False
        elif k == "chunk_summaries":
            if not isinstance(v, list) or len(v) > _MAX_CHUNK_SUMMARIES:
                return False
            if not all(_chunk_summary_shape_ok(s) for s in v):
                return False
        elif isinstance(v, list):
            if not all(isinstance(x, str) for x in v):
                return False
        elif not isinstance(v, str):
            return False
    return True


def _item_len_ok(metadata: dict) -> bool:
    """[F7] Per-value LENGTH half of the per-item egress gate — the batch seam runs it AFTER
    `_redact_free_text_meta`, on the SANITIZED item, so the bound applies to what would actually
    egress (a stripped definition that now fits passes; one still over-bound is excluded +
    audited on the same egress path)."""
    for k, v in metadata.items():
        if k == "column_profiles":
            if isinstance(v, list) and not all(
                    _column_profile_len_ok(d) for d in v if isinstance(d, dict)):
                return False
        elif k == "chunk_summaries":
            if isinstance(v, list) and not all(
                    _chunk_summary_len_ok(s) for s in v if isinstance(s, dict)):
                return False
        elif isinstance(v, list):
            if not all(len(x) <= _max_len_for(k) for x in v if isinstance(x, str)):
                return False
        elif isinstance(v, str) and len(v) > _max_len_for(k):
            return False
    return True


def _item_egress_ok(metadata: dict) -> bool:
    """The FULL per-item egress contract (shape AND length) — the single predicate assemblers
    (`table_synth`) validate against. The batch seam applies the two halves at different times
    ([F7]): shape before sanitization, length after."""
    return _item_shape_ok(metadata) and _item_len_ok(metadata)


def audited_batch_call(conn, client: LLMClient, *, task: str, prompt_id: str, schema_id: str,
                       shared_metadata: dict, items: list[BatchItem], out_key: str, instruction: str,
                       accept, actor: IdentityEnvelope | None = None,
                       extract=None, ref_aware: bool = False,
                       prompt_version: int = 1, schema_version: int = 1) -> BatchCallResult:
    """One GOVERNED batch call (spec C4/C9): per-item egress filter -> batch-level egress guard ->
    schema-validated array call -> one immutable llm_call with a per-item outcome summary. Returns a
    BatchCallResult whose outcomes classify every requested ref (via validate_batch_results).

    ``prompt_version``/``schema_version`` pin the request's contract (default ``1`` — byte-for-byte the
    v1 behavior): output-schema resolution, the stamped request versions, and validation all use them."""
    actor = actor or _ENRICH_ACTOR
    # [F7] SHAPE gate only pre-redaction: an item with a forbidden key / wrong structure never
    # reaches the sanitizer, but a merely-long definition is NOT excluded here — sanitization may
    # bring it under its length bound, so the length gate runs after `_redact_free_text_meta`.
    excluded = [it for it in items if not _item_shape_ok(it.metadata)]
    included = [it for it in items if _item_shape_ok(it.metadata)]
    egress_outcomes = [BatchItemOutcome(it.ref, EGRESS, None, (EGRESS,)) for it in excluded]
    for _it in excluded:
        _audit_egress_block(conn, task=task, actor=actor, reason="item metadata not metadata-only")

    # #19: per-item free-text scan/scrub (spec C9 grain — a fail-closed value excludes ITS item,
    # never the batch). The classification below is what the scan established, never a hardcoded
    # "clean"; scrubbed span types/positions + per-definition sample-strip audits are recorded on
    # the llm_call audit row. Span/audit records of an item that is ultimately EXCLUDED never ride
    # the record — input_redaction describes only what actually egressed.
    safe_items: list[BatchItem] = []
    all_spans: list[dict] = []
    all_sample_audits: list[dict] = []
    free_text_version: str | None = None
    for it in included:
        meta, spans, sample_audits, version = _redact_free_text_meta(it.metadata)
        free_text_version = free_text_version or version
        if meta is None:
            egress_outcomes.append(BatchItemOutcome(it.ref, EGRESS, None, (EGRESS,)))
            _audit_egress_block(conn, task=task, actor=actor,
                                reason="glossary free-text redaction failed closed")
            continue
        if not _item_len_ok(meta):
            # [F7] LENGTH gate on the SANITIZED item: a value still over its egress bound after
            # sample-strip/PII redaction is excluded + audited on the same egress path.
            egress_outcomes.append(BatchItemOutcome(it.ref, EGRESS, None, (EGRESS,)))
            _audit_egress_block(conn, task=task, actor=actor,
                                reason="sanitized item metadata over egress length bound")
            continue
        safe_items.append(BatchItem(it.ref, meta))
        all_spans.extend({"ref": it.ref, **s} for s in spans)
        all_sample_audits.extend({"ref": it.ref, **a} for a in sample_audits)
    included = safe_items

    if not included:
        return BatchCallResult(tuple(egress_outcomes), 0, 0, 0)

    reg = DocumentSchemaRegistry(conn)
    schema = reg.schema_for(schema_id, schema_version)
    if schema is None:
        register_enrichment_schemas(conn)
        schema = reg.schema_for(schema_id, schema_version)

    catalog_metadata = {**shared_metadata,
                        "items": [{"ref": it.ref, **it.metadata} for it in included]}
    redaction_version = free_text_version or _REDACTION_VERSION
    redaction = RedactionResult(text=instruction, redaction_version=redaction_version,
                                redacted_spans=(), disposition="ok")
    inputs = build_llm_inputs(redaction, catalog_metadata=catalog_metadata,
                              raw_input_classification="contains_pii" if all_spans else "clean")
    req = LLMRequest(task=task, prompt_id=prompt_id, prompt_version=prompt_version, inputs=inputs,
                     output_schema_id=schema_id, output_schema_version=schema_version,
                     generation_settings=_generation_settings(), output_schema=schema)

    try:
        assert_llm_safe(req)                      # batch-level egress backstop (spec C9)
    except EgressViolation as exc:
        logger.warning("egress guard blocked batch %s (schema %s); no dispatch", task, schema_id)
        _audit_egress_block(conn, task=task, actor=actor, reason=str(exc))
        missing = validate_batch_results(included, [], out_key, accept,
                                         extract=extract, ref_aware=ref_aware)
        return BatchCallResult(tuple(egress_outcomes) + tuple(missing), 0, 0, 0)

    outcome = drive_structured_call(client, req,
                                    lambda o: reg.validate(schema_id, schema_version, o))
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
        input_redaction=({"redacted_spans": all_spans, "sample_strip": all_sample_audits}
                         if (all_spans or all_sample_audits) else {}),
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
