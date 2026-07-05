import pytest

from featuregen.contracts import IdentityEnvelope
from featuregen.contracts.identity import identity_to_jsonb
from featuregen.idgen import new_run_id
from featuregen.intake.llm import (
    STATUS_OK,
    LLMRequest,
    find_llm_call,
    read_llm_call,
    record_llm_call,
)


def service_actor() -> IdentityEnvelope:
    """A service principal for llm_call records (relocated from the retired intake _helpers)."""
    return IdentityEnvelope(
        subject="service:overlay", actor_kind="service", authenticated=True, auth_method="mtls",
        role_claims=("overlay",), source_of_authority="platform", attestation="overlay-service")


def _req(gen=None):
    return LLMRequest(
        task="structure_intent", prompt_id="intake.v1", prompt_version=1,
        inputs={"redacted_intent": "count declined auths", "catalog_metadata": {},
                "raw_input_classification": "clean", "redaction_version": "default-redactor@1"},
        output_schema_id="TEST_STRUCT", output_schema_version=1,
        generation_settings=gen or {"provider": "fake", "model": "fake-1", "max_tokens": 1024},
    )


def _record(db, run_id, req):
    return record_llm_call(
        db, run_id=run_id, request=req, input_hash="hash-abc",
        redaction_version="default-redactor@1", input_redaction={"redacted_spans": []},
        raw_output={"output": {"entity": "customer"}, "self_reported_scores": {}},
        validation_result={"result": STATUS_OK}, repair_attempts=[],
        latency_ms=3, cost_metadata={"input_tokens": 40}, created_by=identity_to_jsonb(service_actor()),
    )


def test_record_and_read_round_trip(db):
    run_id = new_run_id()
    ref = _record(db, run_id, _req())
    assert ref.startswith("llmc_")
    rec = read_llm_call(db, ref)
    assert rec.run_id == run_id and rec.task == "structure_intent"
    assert rec.provider == "fake" and rec.model == "fake-1"
    assert rec.redacted_input["redacted_intent"] == "count declined auths"   # replayable
    assert rec.raw_output["output"] == {"entity": "customer"}
    assert rec.validation_result == {"result": STATUS_OK}


def test_populated_repair_attempts_round_trip(db):
    # The 0510 change made repair_attempts a LIST of {attempt,class,reason} records (jsonb).
    # Assert a NON-empty list survives the jsonb round-trip with order + dict contents intact.
    run_id = new_run_id()
    attempts = [
        {"attempt": 1, "class": "invalid", "reason": "schema_mismatch"},
        {"attempt": 2, "class": "invalid", "reason": "..."},
    ]
    ref = record_llm_call(
        db, run_id=run_id, request=_req(), input_hash="hash-abc",
        redaction_version="default-redactor@1", input_redaction={"redacted_spans": []},
        raw_output={"output": {"entity": "customer"}, "self_reported_scores": {}},
        validation_result={"result": STATUS_OK}, repair_attempts=attempts,
        latency_ms=3, cost_metadata={"input_tokens": 40}, created_by=identity_to_jsonb(service_actor()),
    )
    rec = read_llm_call(db, ref)
    assert rec.repair_attempts == attempts


def test_read_unknown_raises(db):
    with pytest.raises(KeyError):
        read_llm_call(db, "llmc_nope")


def test_find_matches_full_identity(db):
    run_id = new_run_id()
    req = _req()
    ref = _record(db, run_id, req)
    hit = find_llm_call(
        db, run_id=run_id, task=req.task, input_hash="hash-abc",
        provider="fake", model="fake-1", prompt_id="intake.v1", prompt_version=1,
        output_schema_id="TEST_STRUCT", output_schema_version=1,
        redaction_version="default-redactor@1", generation_settings=req.generation_settings,
    )
    assert hit is not None and hit.llm_call_ref == ref


def test_find_misses_on_any_identity_change(db):
    run_id = new_run_id()
    _record(db, run_id, _req())
    # a changed generation setting (max_tokens) must NOT reuse the stale record
    miss = find_llm_call(
        db, run_id=run_id, task="structure_intent", input_hash="hash-abc",
        provider="fake", model="fake-1", prompt_id="intake.v1", prompt_version=1,
        output_schema_id="TEST_STRUCT", output_schema_version=1,
        redaction_version="default-redactor@1",
        generation_settings={"provider": "fake", "model": "fake-1", "max_tokens": 2048},
    )
    assert miss is None
    # a changed model likewise misses
    assert find_llm_call(
        db, run_id=run_id, task="structure_intent", input_hash="hash-abc",
        provider="fake", model="fake-2", prompt_id="intake.v1", prompt_version=1,
        output_schema_id="TEST_STRUCT", output_schema_version=1,
        redaction_version="default-redactor@1",
        generation_settings={"provider": "fake", "model": "fake-1", "max_tokens": 1024},
    ) is None
