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

logger = logging.getLogger(__name__)

_OWNER = "featuregen-overlay"
_RUN = "overlay-enrichment"          # the audit run bucket for catalog enrichment llm_call records
_REDACTION_VERSION = "metadata-only"  # inputs are names/types only — nothing to redact

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
        generation_settings={"provider": "fake", "model": "test"},
        output_schema=schema)

    try:
        assert_llm_safe(req)              # §9.4 egress backstop
    except EgressViolation:
        logger.warning("egress guard blocked %s (schema %s); no dispatch", task, schema_id)
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
