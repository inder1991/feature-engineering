"""Overlay-owned audited LLM call for catalog enrichment.

The direct `client.call()` path works only against FakeLLM: a real adapter (ClaudeLLM) fails closed
without an attached output-schema, and going around `call_llm` skips the egress guard + audit record.
But `call_llm` itself is coupled to the SP-2 feature-contract aggregate (it emits LLM_CALL_RECORDED on
a feature_contract). Catalog enrichment is not a feature contract, so we COMPOSE the same governance
from the decoupled building blocks — registered output-schema, reserved input keys, `assert_llm_safe`,
`drive_structured_call`, `record_llm_call` — under our own run bucket.

Enrichment inputs carry schema METADATA only (names/types); the "intent" is a fixed instruction, never
uploader free text or data values — so it is classified `clean` and passes the egress guard.
"""
from __future__ import annotations

import logging
import os

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
from featuregen.intake.redaction import (
    EgressViolation,
    RedactionResult,
    assert_llm_safe,
    build_llm_inputs,
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
_RUN = "overlay-enrichment"          # the audit run bucket for catalog enrichment llm_call records
_REDACTION_VERSION = "metadata-only"  # inputs are names/types only — nothing to redact


def _generation_settings() -> dict:
    """Provider/model for the audit record + idempotency key, read from the SAME env that configures
    the client (ClaudeConfig.from_env). So a real ClaudeLLM is audited as anthropic/<model> and
    requests its configured model — NOT the old hard-coded {"provider":"fake","model":"test"}, which
    made a production Claude call request model "test". Defaults to fake/test with no provider set."""
    provider = os.environ.get("FEATUREGEN_LLM_PROVIDER", "fake")
    if provider == "anthropic":
        return {"provider": "anthropic",
                "model": os.environ.get("FEATUREGEN_LLM_MODEL", "claude-opus-4-8")}
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

    redaction = RedactionResult(text=instruction, redaction_version=_REDACTION_VERSION,
                                redacted_spans=(), disposition="ok")
    inputs = build_llm_inputs(redaction, catalog_metadata=catalog_metadata,
                              raw_input_classification="clean")
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
    record_llm_call(
        conn, run_id=_RUN, request=req, input_hash=compute_input_hash(req.inputs),
        redaction_version=_REDACTION_VERSION, input_redaction={},
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


# Only metadata may egress per item (Global Constraint). Any other key (e.g. a free-text definition
# or a data value) means the item is excluded pre-egress and audited (spec C9 per-item egress).
_ITEM_META_ALLOWED = frozenset({"table", "column", "type", "columns", "concept"})


def _item_egress_ok(metadata: dict) -> bool:
    if any(k not in _ITEM_META_ALLOWED for k in metadata):
        return False
    for v in metadata.values():
        if isinstance(v, list):
            if not all(isinstance(x, str) and len(x) <= 200 for x in v):
                return False
        elif not isinstance(v, str) or len(v) > 200:
            return False
    return True


def audited_batch_call(conn, client: LLMClient, *, task: str, prompt_id: str, schema_id: str,
                       shared_metadata: dict, items: list[BatchItem], out_key: str, instruction: str,
                       accept, actor: IdentityEnvelope | None = None) -> BatchCallResult:
    """One GOVERNED batch call (spec C4/C9): per-item egress filter -> batch-level egress guard ->
    schema-validated array call -> one immutable llm_call with a per-item outcome summary. Returns a
    BatchCallResult whose outcomes classify every requested ref (via validate_batch_results)."""
    actor = actor or _ENRICH_ACTOR
    excluded = [it for it in items if not _item_egress_ok(it.metadata)]
    included = [it for it in items if _item_egress_ok(it.metadata)]
    egress_outcomes = [BatchItemOutcome(it.ref, EGRESS, None, (EGRESS,)) for it in excluded]
    for _it in excluded:
        _audit_egress_block(conn, task=task, actor=actor, reason="item metadata not metadata-only")

    if not included:
        return BatchCallResult(tuple(egress_outcomes), 0, 0, 0)

    reg = DocumentSchemaRegistry(conn)
    schema = reg.schema_for(schema_id, 1)
    if schema is None:
        register_enrichment_schemas(conn)
        schema = reg.schema_for(schema_id, 1)

    catalog_metadata = {**shared_metadata,
                        "items": [{"ref": it.ref, **it.metadata} for it in included]}
    redaction = RedactionResult(text=instruction, redaction_version=_REDACTION_VERSION,
                                redacted_spans=(), disposition="ok")
    inputs = build_llm_inputs(redaction, catalog_metadata=catalog_metadata,
                              raw_input_classification="clean")
    req = LLMRequest(task=task, prompt_id=prompt_id, prompt_version=1, inputs=inputs,
                     output_schema_id=schema_id, output_schema_version=1,
                     generation_settings=_generation_settings(), output_schema=schema)

    try:
        assert_llm_safe(req)                      # batch-level egress backstop (spec C9)
    except EgressViolation as exc:
        logger.warning("egress guard blocked batch %s (schema %s); no dispatch", task, schema_id)
        _audit_egress_block(conn, task=task, actor=actor, reason=str(exc))
        missing = validate_batch_results(included, [], out_key, accept)
        return BatchCallResult(tuple(egress_outcomes) + tuple(missing), 0, 0, 0)

    outcome = drive_structured_call(client, req, lambda o: reg.validate(schema_id, 1, o))
    # A repair-exhausted / truncated batch (STATUS_FAILED) carries an UNVERIFIED body — do not harvest
    # it. Treat it as empty so validate_batch_results marks every requested ref MISSING and the
    # orchestrator's fallback ladder recovers it. Mirrors audited_structured_call returning None on
    # STATUS_FAILED: otherwise a truncated definition/domain value (validated only by `accept`) would
    # be cached durably and never retried (whole-branch review, BLOCKING).
    results = (outcome.output.get("results", [])
               if outcome.status != STATUS_FAILED and isinstance(outcome.output, dict) else [])
    item_outcomes = validate_batch_results(included, results, out_key, accept)

    summary = {"requested": [it.ref for it in included],
               "outcomes": {o.ref: o.status for o in item_outcomes}}
    cost = dict(outcome.cost_metadata or {})
    record_llm_call(conn, run_id=_RUN, request=req, input_hash=compute_input_hash(req.inputs),
                    redaction_version=_REDACTION_VERSION, input_redaction={},
                    raw_output={"output": outcome.output,
                                "self_reported_scores": outcome.self_reported_scores},
                    validation_result=outcome.validation_result,
                    repair_attempts=list(outcome.repair_attempts), latency_ms=None,
                    cost_metadata={**cost, "batch": summary}, created_by=identity_to_jsonb(actor))

    return BatchCallResult(
        outcomes=tuple(egress_outcomes) + tuple(item_outcomes), provider_calls=1,
        input_tokens=int(cost.get("input_tokens", 0)), output_tokens=int(cost.get("output_tokens", 0)))
