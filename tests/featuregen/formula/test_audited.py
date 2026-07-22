"""Child-1 Task 3: the audited-call wrapper the formula author/critic call through.

`audited_formula_call` is the ONE seam formula code makes governed provider calls through. It
delegates to the overlay's audited boundary (egress sanitizing + schema-registry validation +
durable fresh-connection llm_call audit + bounded repair/retry) — never re-implementing it, never
touching `record_llm_call` directly — and surfaces the FULL disposition: the validated output,
the immutable `llm_call_ref` recorded under the AUTHORING run bucket (not the enrichment bucket),
the physical provider-call count, and provider usage. An egress-BLOCKED call yields no output but
is still audited (a content-free llm_call row records that the block happened).
"""
import dataclasses
import json

import pytest

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.documents.registry import DocumentSchemaRegistry
from featuregen.formula.audited import AuditedCallResult, audited_formula_call
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.enrich_llm import ENRICHMENT_RUN_ID

_SCHEMA = {"type": "object", "additionalProperties": False,
           "properties": {"answer": {"type": "string"}}, "required": ["answer"]}

# Service identity mirroring enrich_llm's fallback actor shape: authenticated=False — a fabricated
# authenticated identity is forbidden outside sanctioned auth modules.
_ACTOR = IdentityEnvelope(subject="formula-author-test", actor_kind="service",
                          authenticated=False, auth_method="internal", role_claims=())

_META = {"table": "accounts", "column": "balance", "type": "numeric"}
_RUN = "formula-authoring-run-t3"


def _call(db, client, *, run_id=_RUN, instruction="Propose one formula.", metadata=_META):
    DocumentSchemaRegistry(db).register_schema("formula_task_out", 1, _SCHEMA,
                                               "featuregen-formula")
    return audited_formula_call(
        db, client, authoring_run_id=run_id, task="formula.author",
        prompt_id="formula_author_v1", schema_id="formula_task_out",
        instruction=instruction, catalog_metadata=metadata, actor=_ACTOR)


def test_governed_call_returns_output_and_ref_under_authoring_run(db):
    client = FakeLLM(script={"formula.author": FakeResponse(output={"answer": "sum(balance)"})})
    res = _call(db, client)
    assert res.output == {"answer": "sum(balance)"}
    assert res.llm_call_ref
    row = db.execute("SELECT run_id, task FROM llm_call WHERE llm_call_ref = %s",
                     (res.llm_call_ref,)).fetchone()
    assert row == (_RUN, "formula.author")
    # the authoring call is bucketed under ITS run — never leaked into the enrichment bucket
    n = db.execute("SELECT count(*) FROM llm_call WHERE run_id = %s",
                   (ENRICHMENT_RUN_ID,)).fetchone()[0]
    assert n == 0


def test_egress_blocked_call_returns_no_output_but_audited_ref(db):
    # An empty-script FakeLLM raises KeyError if dispatched — so this test also proves the
    # provider was NEVER called for a blocked payload.
    client = FakeLLM(script={})
    res = _call(db, client,
                instruction="Contact jane.doe@example.com about this formula.")
    assert res.output is None
    assert res.provider_calls == 0
    assert res.usage == {}
    # the block itself is audited: a real llm_call row under the authoring run bucket
    assert res.llm_call_ref
    row = db.execute(
        "SELECT run_id, validation_result, redacted_input, raw_output"
        "  FROM llm_call WHERE llm_call_ref = %s",
        (res.llm_call_ref,)).fetchone()
    assert row[0] == _RUN
    assert row[1]["result"] == "egress_blocked"
    assert row[1]["reason"]                       # the guard's reason (a label, never content)
    # the blocked content itself must NEVER be persisted on the audit row (it failed egress)
    assert "example.com" not in json.dumps(row[2]) + json.dumps(row[3])


def test_provider_calls_and_usage_populated(db):
    client = FakeLLM(script={"formula.author": FakeResponse(
        output={"answer": "ok"},
        cost_metadata={"input_tokens": 11, "output_tokens": 7})})
    res = _call(db, client)
    assert res.provider_calls == 1
    assert res.usage.get("input_tokens") == 11
    assert res.usage.get("output_tokens") == 7


def test_result_is_a_frozen_slotted_dataclass():
    assert dataclasses.is_dataclass(AuditedCallResult)
    res = AuditedCallResult(output=None, llm_call_ref=None, provider_calls=0, usage={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        res.output = {}
    assert not hasattr(res, "__dict__")           # slots=True
