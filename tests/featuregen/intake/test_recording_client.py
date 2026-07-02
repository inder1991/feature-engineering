import json

from tests.featuregen.intake._helpers import service_actor

from featuregen.documents.registry import DocumentSchemaRegistry
from featuregen.events.registry import event_registry
from featuregen.events.store import load_stream
from featuregen.identity.build import build_human_identity
from featuregen.idgen import new_run_id
from featuregen.intake.candidates import (
    CANDIDATES_OUTPUT_SCHEMA_ID,
    CANDIDATES_OUTPUT_SCHEMA_VERSION,
    CANDIDATES_PROMPT_ID,
    CANDIDATES_PROMPT_VERSION,
    RecordingLLMClient,
    StubCandidateGenerator,
)
from featuregen.intake.events import register_sp2_event_types
from featuregen.intake.llm import LLMRequest, LLMResult, read_llm_call
from featuregen.intake.redaction import DefaultIntentRedactor, register_intent_redactor

OWNER = build_human_identity(subject="user:raj", role_claims=("data_scientist",))


class _Inner:
    def call(self, request):
        return LLMResult(output={"candidates": []}, self_reported_scores={},
                         call_ref="inner_ref_ignored", status="ok")


def test_recording_client_binds_run_context_and_routes_through_call_llm(db, monkeypatch):
    seen = {}

    def fake_call_llm(conn, client, request, *, run_id, actor):
        seen.update(conn=conn, client=client, request=request, run_id=run_id, actor=actor)
        # call_llm returns the LLMResult carrying the REAL, event-sourced llm_call ref
        return LLMResult(output=request.inputs, self_reported_scores={}, call_ref="llmc_real",
                         status="ok")

    monkeypatch.setattr("featuregen.intake.candidates.call_llm", fake_call_llm)

    inner = _Inner()
    rec = RecordingLLMClient(conn=db, inner=inner, run_id="run_hyp", actor=OWNER)
    req = LLMRequest(
        task="generate_candidates",
        prompt_id=CANDIDATES_PROMPT_ID,
        prompt_version=CANDIDATES_PROMPT_VERSION,
        inputs={"allowed_concepts": ["mcc"]},
        output_schema_id=CANDIDATES_OUTPUT_SCHEMA_ID,
        output_schema_version=CANDIDATES_OUTPUT_SCHEMA_VERSION,
        generation_settings={},
    )
    res = rec.call(req)

    assert res.call_ref == "llmc_real"          # the event-sourced ref, not the inner client's
    assert seen["conn"] is db                    # bound conn passed to call_llm
    assert seen["client"] is inner               # the inner provider client is the one recorded
    assert seen["request"] is req                # the exact request forwarded unchanged (pure bridge)
    assert seen["run_id"] == "run_hyp"           # bound run context
    assert seen["actor"] is OWNER                # bound actor identity


# ── Task-6.3 integration: the full StubCandidateGenerator → RecordingLLMClient → call_llm path ──────
# The bridge is generic (forwards unchanged, above); the egress reconciliation lives in the generator,
# which builds the reserved LLM-safe request. These tests exercise the REAL call_llm envelope.

# A lenient structured-output schema call_llm validates the generation pass against (§9.1).
_CANDIDATES_OUT_SCHEMA = {
    "type": "object",
    "required": ["candidates"],
    "properties": {"candidates": {"type": "array"}},
    "additionalProperties": True,
}

_CANDS = {"candidates": [
    {"definition_text": "count of distinct MCCs, last 30d minus prior 30d",
     "rationale": "category churn precedes financial distress",
     "calculation_method": {"kind": "rolling_aggregate", "aggregation": "distinct_count",
                            "window": "30d", "filter": {"concept": "merchant_category_code"}}},
]}

_DRAFT = {"intake_mode": "hypothesis", "proposed_feature_name": "abrupt_category_shift",
          "target": "higher credit risk", "feature_semantics": {},
          "raw_input_classification": "clean"}
_CATALOG = {"concepts": ["merchant_category_code"]}
_DOMAIN = {"allowed_concepts": ["merchant_category_code"]}


class _CapturingInner:
    """An LLMClient double that records every request it is asked to dispatch and returns a valid
    structured output — so a test can prove exactly what (redacted) payload reached the provider."""

    def __init__(self, output):
        self._output = output
        self.seen = []

    def call(self, request):
        self.seen.append(request)
        return LLMResult(
            output=dict(self._output), self_reported_scores={}, call_ref="", status="ok"
        )


def _setup(db):
    register_sp2_event_types(event_registry())  # LLM_CALL_RECORDED@v1 (idempotent)
    DocumentSchemaRegistry(db).register_schema(
        CANDIDATES_OUTPUT_SCHEMA_ID, CANDIDATES_OUTPUT_SCHEMA_VERSION,
        _CANDIDATES_OUT_SCHEMA, owner="test",
    )
    register_intent_redactor(DefaultIntentRedactor())  # R10 seam the generator resolves


def test_full_path_records_llm_call_and_emits_event(db):
    _setup(db)
    run_id = new_run_id()
    inner = _CapturingInner(_CANDS)
    rec = RecordingLLMClient(conn=db, inner=inner, run_id=run_id, actor=service_actor())

    cands = StubCandidateGenerator(rec).generate(_DRAFT, _CATALOG, _DOMAIN)

    assert len(cands) == 1  # the generation pass yielded a candidate
    # exactly ONE immutable llm_call recorded on this run...
    rows = db.execute("SELECT llm_call_ref FROM llm_call WHERE run_id=%s", (run_id,)).fetchall()
    assert len(rows) == 1
    call_ref = rows[0][0]
    # ...the candidate's provenance points at the REAL event-sourced ref (never the inner "")
    assert cands[0].provenance["llm_call_refs"] == [call_ref]
    # ...and LLM_CALL_RECORDED is on the feature_contract aggregate (X3, run_id)
    recorded = [e for e in load_stream(db, "feature_contract", run_id) if e.type == "LLM_CALL_RECORDED"]
    assert len(recorded) == 1
    assert recorded[0].payload["llm_call_ref"] == call_ref
    assert recorded[0].payload["task"] == "generate_candidates"


def test_pii_draft_field_is_redacted_before_reaching_the_llm(db):
    _setup(db)
    run_id = new_run_id()
    inner = _CapturingInner(_CANDS)
    rec = RecordingLLMClient(conn=db, inner=inner, run_id=run_id, actor=service_actor())
    draft = {**_DRAFT,
             "target": "escalate to jane.doe@bank.example about higher credit risk",
             "raw_input_classification": "contains_pii"}

    cands = StubCandidateGenerator(rec).generate(draft, _CATALOG, _DOMAIN)

    assert cands  # still generated — the PII was scrubbed, not fatal
    # (a) the raw email NEVER reached the provider; only the redacted marker did (the no-PII backstop)
    assert inner.seen, "the LLM must be reached on the clean/redacted path"
    for req in inner.seen:
        assert "jane.doe@bank.example" not in json.dumps(req.inputs)
        assert "[REDACTED:EMAIL]" in req.inputs["redacted_intent"]
    # (b) the immutable, replayable llm_call record stores the REDACTED input (never raw PII)
    call_ref = db.execute("SELECT llm_call_ref FROM llm_call WHERE run_id=%s", (run_id,)).fetchone()[0]
    stored = read_llm_call(db, call_ref)
    assert "jane.doe@bank.example" not in json.dumps(stored.redacted_input)
    assert "[REDACTED:EMAIL]" in stored.redacted_input["redacted_intent"]


def test_unscanned_draft_fails_closed_no_call_no_record(db):
    _setup(db)
    run_id = new_run_id()
    inner = _CapturingInner(_CANDS)
    rec = RecordingLLMClient(conn=db, inner=inner, run_id=run_id, actor=service_actor())
    draft = {**_DRAFT, "raw_input_classification": "unscanned"}

    assert StubCandidateGenerator(rec).generate(draft, _CATALOG, _DOMAIN) == []
    assert inner.seen == []  # the provider is never reached on the fail-closed path
    assert db.execute("SELECT count(*) FROM llm_call WHERE run_id=%s", (run_id,)).fetchone()[0] == 0
    assert load_stream(db, "feature_contract", run_id) == []


def test_identical_generation_dedups_to_one_llm_call(db):
    _setup(db)
    run_id = new_run_id()
    inner = _CapturingInner(_CANDS)
    rec = RecordingLLMClient(conn=db, inner=inner, run_id=run_id, actor=service_actor())
    gen = StubCandidateGenerator(rec)

    gen.generate(_DRAFT, _CATALOG, _DOMAIN)
    gen.generate(_DRAFT, _CATALOG, _DOMAIN)  # identical → full-identity idempotent reuse (§9.3)

    assert len(inner.seen) == 1  # the provider is dispatched ONCE; the reuse never re-calls it
    assert db.execute("SELECT count(*) FROM llm_call WHERE run_id=%s", (run_id,)).fetchone()[0] == 1
    assert len([e for e in load_stream(db, "feature_contract", run_id)
                if e.type == "LLM_CALL_RECORDED"]) == 1
