"""Task 12 — key-gated Anthropic LIVE canary for the projected structured-output wire schemas.

Every enrichment batch call ships a CANONICAL strict JSON Schema (the source of truth for validating
the model's RESPONSE, `DocumentSchemaRegistry.validate`) but sends a PROJECTED, Anthropic-compatible
subset on the WIRE (`schema_projection.project_for_anthropic`, applied inside `ClaudeLLM.call` via
`_wire_output_config`). The hermetic gold gate drives a scripted FakeLLM, so it never proves the
projected wire schema is one the real Anthropic structured-output API actually accepts — a projection
regression (e.g. a node the API rejects with HTTP 400) would fail EVERY live batch call closed while
CI stays green.

THIS canary closes that gap: for each of the four batch schemas this branch relies on, it registers
the canonical schema, builds a minimal real `LLMRequest` exactly the way `enrich_llm.audited_batch_call`
does (same `build_llm_inputs` / `RedactionResult` / `_generation_settings` / `output_schema`-from-the
-registry builders — the audit/egress/DB-write machinery around them is irrelevant to "does the wire
schema get a 400"), calls the REAL provider through `ClaudeLLM` (which projects the wire schema), and
asserts (a) NO schema-rejection 400 and (b) the live response validates against the CANONICAL schema.

Run it (needs a live key; skips cleanly without one — the only behaviour default CI / this env sees):

    ANTHROPIC_API_KEY=... uv run pytest -m eval tests/eval/test_anthropic_live_canary.py -q

`anthropic` is imported INSIDE the test body (after the skipif) so collection works without the SDK.
"""
from __future__ import annotations

import os

import pytest

from featuregen.documents.registry import DocumentSchemaRegistry
from featuregen.intake.llm import PROVIDER_OK, LLMRequest
from featuregen.intake.llm_claude import ClaudeConfig, build_claude_llm
from featuregen.intake.redaction import RedactionResult, build_llm_inputs
from featuregen.overlay.upload.enrich_llm import _generation_settings, register_enrichment_schemas

pytestmark = pytest.mark.eval

# The four projected wire schemas this branch relies on: the two Pass A batch shapes (concept/domain)
# and the two Pass B table-synthesis batch shapes (per-chunk summary + per-table synthesis). Each
# entry is a minimal, REAL catalog-metadata payload + a fixed instruction — enough content for the
# model to emit a structured response that the strict canonical schema then accepts.
_CANARY_INPUTS: dict[str, dict] = {
    "overlay_concept_batch": {
        "instruction": ("Classify each requested column into a single short business concept. "
                        "Return one {ref, concept} object per requested item, echoing its ref."),
        "shared": {},
        "items": [
            {"ref": "c1", "table": "accounts", "column": "current_balance", "type": "numeric"},
            {"ref": "c2", "table": "customers", "column": "email_address", "type": "text"},
        ],
    },
    "overlay_domain_batch": {
        "instruction": ("Classify each requested table into a single short business data domain. "
                        "Return one {ref, domain} object per requested item, echoing its ref."),
        "shared": {},
        "items": [
            {"ref": "d1", "table": "accounts", "columns": "current_balance, currency, opened_at"},
            {"ref": "d2", "table": "transactions", "columns": "txn_id, account_id, amount, posted_at"},
        ],
    },
    "overlay_table_synth_summary_batch": {
        "instruction": ("For each requested column chunk, summarize grain/id candidates, temporal/"
                        "as-of candidates, entity signals, and whether it looks event- or "
                        "snapshot-shaped. Return one {ref, summary} object per item, echoing its ref."),
        "shared": {},
        "items": [
            {"ref": "s1", "table": "transactions",
             "column_profiles": [
                 {"column": "txn_id", "type": "text"},
                 {"column": "account_id", "type": "text"},
                 {"column": "amount", "type": "numeric"},
                 {"column": "posted_at", "type": "timestamp"}]},
        ],
    },
    "overlay_table_synth_batch": {
        "instruction": ("For each requested table, propose grain columns, an as-of column and basis, "
                        "the primary entity, table role, and event-or-snapshot shape. Return one "
                        "{ref, synthesis} object per item, echoing its ref."),
        "shared": {},
        "items": [
            {"ref": "b1", "table": "transactions",
             "column_roster": ["txn_id:text", "account_id:text", "amount:numeric",
                               "posted_at:timestamp"]},
        ],
    },
}


def _build_request(reg: DocumentSchemaRegistry, schema_id: str, spec: dict) -> LLMRequest:
    """Build the LLMRequest exactly as `enrich_llm.audited_batch_call` does: reserved-keyed inputs via
    `build_llm_inputs`, the CANONICAL (unprojected) schema attached as `output_schema` (the wire
    projection happens inside `ClaudeLLM.call`), and env-driven pinned generation settings."""
    schema = reg.schema_for(schema_id, 1)
    assert schema is not None, f"{schema_id} not registered"
    catalog_metadata = {**spec["shared"], "items": [dict(it) for it in spec["items"]]}
    redaction = RedactionResult(text=spec["instruction"], redaction_version="metadata-only",
                                redacted_spans=(), disposition="ok")
    inputs = build_llm_inputs(redaction, catalog_metadata=catalog_metadata,
                              raw_input_classification="clean")
    return LLMRequest(task=f"{schema_id}_canary", prompt_id=f"{schema_id}-canary", prompt_version=1,
                      inputs=inputs, output_schema_id=schema_id, output_schema_version=1,
                      generation_settings=_generation_settings(), output_schema=schema)


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="live provider required")
@pytest.mark.parametrize("schema_id", list(_CANARY_INPUTS))
def test_anthropic_live_canary(db, monkeypatch, schema_id: str) -> None:
    import anthropic  # inside the body (after the skipif) so collection works without the SDK

    # Wire the REAL adapter; FEATUREGEN_LLM_PROVIDER=anthropic also makes `_generation_settings` pin
    # the true model/max_tokens/thinking/effort a live call requests (never model "test").
    monkeypatch.setenv("FEATUREGEN_LLM_PROVIDER", "anthropic")

    register_enrichment_schemas(db)          # idempotent; also asserts every schema projects clean
    reg = DocumentSchemaRegistry(db)
    req = _build_request(reg, schema_id, _CANARY_INPUTS[schema_id])

    client = build_claude_llm(ClaudeConfig.from_env())
    try:
        result = client.call(req)            # projects the wire schema via `_wire_output_config`
    except anthropic.APIStatusError as exc:  # ClaudeLLM.call maps these to a status; surface an escapee
        pytest.fail(f"{schema_id}: unexpected APIStatusError (HTTP {getattr(exc, 'status_code', '?')})")

    # (a) No schema-rejection 400: `ClaudeLLM.call` maps a 400 to PROVIDER_NON_RETRYABLE with an empty
    # body, so a PROVIDER_OK status proves the projected wire schema was accepted end to end.
    assert result.status == PROVIDER_OK, (
        f"{schema_id}: provider status {result.status!r} (a 400 schema rejection maps to "
        f"non_retryable with an empty body) — the projected wire schema was likely rejected: "
        f"{result.output}")
    # (b) The live response validates against the strict CANONICAL schema (the wire projection only
    # dropped constraints the canonical schema still enforces on the RESPONSE). Raises on mismatch.
    reg.validate(schema_id, 1, result.output)
