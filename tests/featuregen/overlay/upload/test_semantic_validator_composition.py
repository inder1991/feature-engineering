"""Repair-seam Task 2 — the audited wrapper composes a CALLER'S semantic validator, under a
governed failure contract.

Before this task ``drive_audited_structured_call`` hardcoded its validator to
``reg.validate(schema_id, schema_version, output)``. A caller with rules the JSON Schema cannot
express (the closed use-case taxonomy, "exactly one primary") had to check AFTER the call — outside
the bounded repair loop — so the model was never asked to fix an answer the platform then threw
away whole.

What is under test here is the SEAM, not any caller (the recognizer arrives in Task 3):

1. schema FIRST, then semantics — the registry stays the fail-closed floor, and a schema-invalid
   body never reaches the caller's validator at all;
2. only a typed ``SchemaValidationError`` from the semantic validator enters repair, so a semantic
   doubt is indistinguishable-in-kind from a structural one and the SAME §9.2 budget governs both;
3. any OTHER exception is an AUDITED technical failure — the provider was called and billed, so the
   ``llm_call`` row is written before anything is returned and nothing escapes the seam;
4. the repair complaint on the wire is a CLOSED, VALUE-FREE CODE by construction — not by author
   promise: a validator that attests prose (or leaks the value it rejected) is scrubbed back to a
   value-free constant here;
5. B1 — the post-repair body is exposed on the SEMANTIC arm only, and only once its audit row
   exists. Schema-invalid, provider-failed, egress-blocked and audit-degraded expose nothing, and
   the output-only view (``audited_structured_call``) still returns ``None`` for every one of them.
"""
from __future__ import annotations

import ast
import inspect
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from featuregen.contracts import AttestedSchemaValidationError, SchemaValidationError
from featuregen.intake.llm import (
    PROVIDER_OK,
    PROVIDER_REFUSAL,
    FakeLLM,
    FakeResponse,
    LLMRequest,
    LLMResult,
)
from featuregen.intake.llm_claude import _wire_prompt
from featuregen.intake.redaction import INPUT_KEY_REPAIR_ERRORS
from featuregen.overlay.upload import enrich_llm
from featuregen.overlay.upload.dispatch_audit import DispatchAuditContext
from featuregen.overlay.upload.enrich_llm import (
    FAILURE_KIND_AUDIT_DEGRADED,
    FAILURE_KIND_EGRESS_BLOCKED,
    FAILURE_KIND_PROVIDER_FAILED,
    FAILURE_KIND_SCHEMA_INVALID,
    FAILURE_KIND_SEMANTIC_INVALID,
    FAILURE_KIND_VALIDATOR_FAULT,
    audited_structured_call,
    drive_audited_structured_call,
    register_enrichment_schemas,
)

_META = {"table": "accounts", "column": "balance", "type": "numeric"}
_INSTRUCTION = "Classify the concept of this column."

# The one value the closed vocabulary knows. Everything else is a SEMANTIC failure the JSON Schema
# (`{"concept": {"type": "string"}}`) happily accepts — which is the whole point of the seam.
_KNOWN = "monetary_amount"

# A distinctive rejected value. Every value-freedom assertion greps the WHOLE outbound request for
# this string, so a leak through any channel (repair errors, instruction, metadata) is caught.
_REJECTED = "zz-placeholder-sentinel-zz"


def _reject_unknown(output: Mapping[str, Any]) -> None:
    """A well-behaved semantic validator: a typed failure carrying a CLOSED CODE."""
    if output.get("concept") != _KNOWN:
        raise AttestedSchemaValidationError(
            f"concept {output.get('concept')!r} is not in the closed vocabulary",
            llm_safe_reason="UNKNOWN_CONCEPT")


def _reject_unattested(output: Mapping[str, Any]) -> None:
    """A validator in `recognition.py`'s CURRENT shape: a plain SchemaValidationError whose MESSAGE
    embeds the offending value (the message is an in-process artefact and must never reach a wire)."""
    if output.get("concept") != _KNOWN:
        raise SchemaValidationError(f"concept {output.get('concept')!r} is not in the vocabulary")


def _reject_leaking(output: Mapping[str, Any]) -> None:
    """A MISBEHAVING validator: it attests a reason that carries the rejected value. The seam must
    refuse the attestation rather than trust it — value-freedom is structural here, not a promise."""
    if output.get("concept") != _KNOWN:
        raise AttestedSchemaValidationError(
            "leaky", llm_safe_reason=f"the concept {output.get('concept')!r} is unknown")


class _Recorder:
    """LLMClient wrapper that keeps every physical request, so the tests can assert on the ACTUAL
    prompt sent (including the repair turn), not on an inferred one."""

    def __init__(self, inner: FakeLLM) -> None:
        self._inner = inner
        self.requests: list[LLMRequest] = []

    def call(self, request: LLMRequest) -> LLMResult:
        self.requests.append(request)
        return self._inner.call(request)


_TASK = "test.repairseam.semantic"


def _client(*bodies: dict, task: str = _TASK, provider_status: str = PROVIDER_OK) -> FakeLLM:
    # FakeLLM repeats the LAST response once its sequence is exhausted, so a single body drives the
    # whole repair budget and a two-body script drives "invalid then fixed".
    return FakeLLM(script={task: [FakeResponse(output=b, provider_status=provider_status)
                                  for b in bodies]})


def _drive(db, client, *, task: str = _TASK, instruction: str = _INSTRUCTION, **kwargs):
    register_enrichment_schemas(db)
    return drive_audited_structured_call(
        db, client, task=task, prompt_id="overlay_concept_v1", schema_id="overlay_concept",
        catalog_metadata=_META, instruction=instruction, **kwargs)


def _llm_call_row(db, ref: str) -> dict:
    row = db.execute(
        "SELECT task, validation_result, repair_attempts, raw_output FROM llm_call "
        "WHERE llm_call_ref = %s", (ref,)).fetchone()
    assert row is not None, "no durable llm_call row for a call the provider actually served"
    return {"task": row[0], "validation_result": row[1], "repair_attempts": row[2],
            "raw_output": row[3]}


# ---- 1. a semantic failure is a doubt the model is asked to fix ------------------------------


def test_a_semantic_failure_is_repaired_not_returned(db) -> None:
    """The live incident's shape, at the seam: turn 1 is schema-valid but semantically wrong, turn 2
    is right. The caller gets turn 2 — and the round trip is recorded, so nobody has to guess
    whether the model was ever asked."""
    client = _client({"concept": _REJECTED}, {"concept": _KNOWN})
    res = _drive(db, client, validate_semantics=_reject_unknown)

    assert res.output == {"concept": _KNOWN}      # the REPAIRED body, never the first answer
    assert res.failure_kind is None
    assert res.last_schema_valid_semantic_invalid_output is None
    assert res.provider_calls == 2                # the repair really was a second provider request

    row = _llm_call_row(db, res.llm_call_ref)
    assert row["validation_result"]["result"] == "repaired"
    assert [a["class"] for a in row["repair_attempts"]] == ["repair"]
    assert row["repair_attempts"][0]["reason"] == "UNKNOWN_CONCEPT"


def test_the_semantic_validator_shares_the_schema_repair_budget(db) -> None:
    """§9.2's budget governs BOTH kinds: a body that never satisfies the semantics costs exactly
    1 + DEFAULT_REPAIR_BUDGET provider calls and then fails closed — a semantic failure can no more
    loop forever than a structural one."""
    client = _client({"concept": _REJECTED})
    res = _drive(db, client, validate_semantics=_reject_unknown)

    assert res.output is None
    assert res.provider_calls == 3                # 1 + 2 repairs
    row = _llm_call_row(db, res.llm_call_ref)
    assert [a["class"] for a in row["repair_attempts"]] == ["repair", "repair"]
    assert row["validation_result"]["result"] == "failed_into_clarification"


# ---- 2. the registry schema is still the floor -----------------------------------------------


def test_the_registry_schema_still_fails_closed_first(db) -> None:
    """A SCHEMA-invalid body behaves identically with and without a semantic validator — and the
    semantic validator never even sees it. If composition had put the caller first, a semantic
    validator would be reading unvalidated structure."""
    seen: list[dict] = []

    def _semantics(output: Mapping[str, Any]) -> None:
        seen.append(dict(output))

    body = {"concept": 5}                         # `concept` is declared a string
    t_without, t_with = "test.repairseam.schema.without", "test.repairseam.schema.with"
    without = _drive(db, _client(body, task=t_without), task=t_without)
    with_ = _drive(db, _client(body, task=t_with), task=t_with, validate_semantics=_semantics)

    assert without.output is with_.output is None
    assert without.failure_kind == with_.failure_kind == FAILURE_KIND_SCHEMA_INVALID
    assert without.provider_calls == with_.provider_calls == 3
    assert seen == []                             # never handed a body the schema refused

    rows = [_llm_call_row(db, r.llm_call_ref) for r in (without, with_)]
    assert rows[0]["validation_result"] == rows[1]["validation_result"]
    assert rows[0]["repair_attempts"] == rows[1]["repair_attempts"]
    # and the complaint is the schema layer's own value-free pointer, unchanged by composition
    assert rows[0]["repair_attempts"][0]["reason"] == "$.concept: failed 'type'"


# ---- 3. an unexpected exception is audited, never escaped -------------------------------------


@pytest.mark.parametrize("boom", [
    lambda output: (_ for _ in ()).throw(KeyError("candidates")),
    lambda output: (_ for _ in ()).throw(ValueError("not a mapping")),
    lambda output: (_ for _ in ()).throw(TypeError("unsubscriptable")),
])
def test_a_validator_raising_an_unexpected_error_is_audited_not_escaped(db, boom) -> None:
    """A bug in a caller's validator is a PLATFORM failure, not a model failure. It may not escape
    the seam before ``_record_llm_call_durable`` — the provider has already been called and billed,
    and an unaudited escape loses exactly that fact. It also may not be re-prompted: no repair
    budget is spent asking a model to fix our own crash."""
    res = _drive(db, _client({"concept": _KNOWN}), validate_semantics=boom)

    assert res.output is None                     # nothing unvalidated is handed back
    assert res.failure_kind == FAILURE_KIND_VALIDATOR_FAULT
    assert res.last_schema_valid_semantic_invalid_output is None
    assert res.provider_calls == 1                # no repair burned on our own bug
    assert res.llm_call_ref                       # the billed call IS accounted for

    row = _llm_call_row(db, res.llm_call_ref)
    assert row["validation_result"]["result"] == "validator_fault"
    assert row["raw_output"]["output"] == {"concept": _KNOWN}   # the body we paid for is retained


def test_a_validator_fault_returns_none_through_the_output_only_view(db) -> None:
    register_enrichment_schemas(db)
    task = "test.repairseam.fault.view"
    out = audited_structured_call(
        db, _client({"concept": _KNOWN}, task=task), task=task,
        prompt_id="overlay_concept_v1", schema_id="overlay_concept", catalog_metadata=_META,
        instruction=_INSTRUCTION,
        validate_semantics=lambda output: (_ for _ in ()).throw(KeyError("boom")))
    assert out is None


# ---- 4. the repair prompt carries a code, never the value --------------------------------------


@pytest.mark.parametrize("validator,expected_reason", [
    (_reject_unknown, "UNKNOWN_CONCEPT"),
    # a plain SchemaValidationError has no attested reason and no jsonschema cause to rebuild from
    (_reject_unattested, "the result did not satisfy the semantic contract"),
    # an attested reason that is not a closed code is REFUSED — the seam does not trust the promise
    (_reject_leaking, "the result did not satisfy the semantic contract"),
])
def test_repair_prompts_never_contain_the_invalid_value(db, validator, expected_reason) -> None:
    recorder = _Recorder(_client({"concept": _REJECTED}))
    res = _drive(db, recorder, validate_semantics=validator)

    assert res.output is None
    assert len(recorder.requests) == 3            # the repair turns really happened
    repairs = [r for r in recorder.requests if INPUT_KEY_REPAIR_ERRORS in r.inputs]
    assert len(repairs) == 2

    for request in recorder.requests:
        # the WHOLE outbound payload, and the rendered wire prompt the provider adapter builds
        assert _REJECTED not in json.dumps(request.inputs, ensure_ascii=False)
        _system, user_content = _wire_prompt(request)
        assert _REJECTED not in user_content
    # the complaint the model is actually given — accumulating across the two repair turns
    assert [r.inputs[INPUT_KEY_REPAIR_ERRORS] for r in repairs] == [
        [expected_reason], [expected_reason, expected_reason]]

    # and the same value-free text is what the audit row stores
    row = _llm_call_row(db, res.llm_call_ref)
    assert {a["reason"] for a in row["repair_attempts"]} == {expected_reason}


# ---- 5. B1 — the post-repair body, exposed on the semantic arm ONLY ---------------------------


def test_the_invalid_body_is_exposed_only_on_the_semantic_arm(db) -> None:
    """Task 4 partitions a body whose candidates are individually judgeable — it can only do that if
    the body survives repair exhaustion. It is exposed here, and ONLY here."""
    body = {"concept": _REJECTED}
    res = _drive(db, _client(body), validate_semantics=_reject_unknown)

    assert res.output is None                     # still NOT a validated result
    assert res.failure_kind == FAILURE_KIND_SEMANTIC_INVALID
    assert res.last_schema_valid_semantic_invalid_output == body
    assert res.llm_call_ref                       # exposed only WITH its audit row

    # the output-only view is unchanged: None for every invalid result, no second channel
    register_enrichment_schemas(db)
    view_task = "test.repairseam.expose.view"
    assert audited_structured_call(
        db, _client(body, task=view_task), task=view_task, prompt_id="overlay_concept_v1",
        schema_id="overlay_concept", catalog_metadata=_META, instruction=_INSTRUCTION,
        validate_semantics=_reject_unknown) is None


def test_a_schema_invalid_result_exposes_nothing(db) -> None:
    res = _drive(db, _client({"concept": 5}), validate_semantics=_reject_unknown)
    assert res.failure_kind == FAILURE_KIND_SCHEMA_INVALID
    assert res.last_schema_valid_semantic_invalid_output is None


def test_a_provider_failure_after_a_semantic_failure_exposes_nothing(db) -> None:
    """The sharp case: turn 1 IS a schema-valid, semantically-invalid body — but the repair turn is
    refused, so the call's terminal cause is the provider, not the body. A stale body from an
    earlier turn must not ride out as if it were the answer that failed."""
    client = FakeLLM(script={_TASK: [
        FakeResponse(output={"concept": _REJECTED}),
        FakeResponse(output={}, provider_status=PROVIDER_REFUSAL)]})
    res = _drive(db, client, validate_semantics=_reject_unknown)

    assert res.output is None
    assert res.failure_kind == FAILURE_KIND_PROVIDER_FAILED
    assert res.last_schema_valid_semantic_invalid_output is None


def test_a_schema_invalid_last_turn_after_a_semantic_one_exposes_nothing(db) -> None:
    """The other stale-body trap, from the opposite side: turn 1 is semantically invalid, and the
    repair comes back MALFORMED. The exposed body must describe the FINAL answer or nothing — an
    invalid body from two turns ago is not what the call ended on."""
    task = "test.repairseam.stale"
    client = FakeLLM(script={task: [FakeResponse(output={"concept": _REJECTED}),
                                    FakeResponse(output={"concept": 5})]})
    res = _drive(db, client, task=task, validate_semantics=_reject_unknown)

    assert res.failure_kind == FAILURE_KIND_SCHEMA_INVALID
    assert res.last_schema_valid_semantic_invalid_output is None


def test_an_egress_blocked_result_exposes_nothing(db) -> None:
    """Blocked BEFORE dispatch: there is no body, no provider call and (at the enrichment default)
    no llm_call row — so there is nothing to expose and nothing to account for."""
    task = "test.repairseam.egress"
    res = _drive(db, _client({"concept": _KNOWN}, task=task), task=task,
                 instruction="Classify this column for analyst@example.com.",
                 validate_semantics=_reject_unknown)

    assert res.output is None
    assert res.failure_kind == FAILURE_KIND_EGRESS_BLOCKED
    assert res.last_schema_valid_semantic_invalid_output is None
    assert res.llm_call_ref is None and res.provider_calls == 0


def test_an_audit_degraded_result_exposes_nothing(db, monkeypatch) -> None:
    """C5-T6: the logical outcome audit did not commit, so the result is discarded. A body whose
    linkage failed is a body nobody can account for — the semantic arm does not override that."""
    monkeypatch.setattr(enrich_llm, "link_llm_call", lambda **_kwargs: False)
    task = "test.repairseam.degraded"
    res = _drive(db, _client({"concept": _REJECTED}, task=task), task=task,
                 validate_semantics=_reject_unknown,
                 dispatch_audit=DispatchAuditContext(
                     ingestion_run_id=None, stage="enrichment",
                     subjects=[{"catalog_source": "deposits", "object_ref": "public.accounts",
                                "logical_ref": "accounts", "field_names": ["balance"]}]))

    assert res.output is None
    assert res.failure_kind == FAILURE_KIND_AUDIT_DEGRADED
    assert res.last_schema_valid_semantic_invalid_output is None
    assert res.llm_call_ref                       # the llm_call IS durable; only the linkage failed


# ---- 6. every existing call site is byte-identical ---------------------------------------------

#: The ONLY call sites allowed to pass ``validate_semantics``. An enumerable exemption, so
#: `grep` answers "who validates semantics inside the repair loop" — and so adding the first real
#: consumer (Task 3's recognizer) is a deliberate, reviewed line rather than a silent widening.
_SEMANTIC_SEAM_CONSUMERS = {
    # the output-only projection, which must pass the parameter THROUGH to be a true projection
    ("src/featuregen/overlay/upload/enrich_llm.py", "drive_audited_structured_call"),
}


def _call_sites() -> list[tuple[str, int, str, set[str]]]:
    """Every (module, line, callee, kwargs) invoking either wrapper, found by AST — never a grep
    count. Revision 1 of the plan quoted a raw grep LINE count as a caller count and was wrong by
    more than a factor of two; revision 2's replacement was wrong by one."""
    root = Path(__file__).resolve().parents[4] / "src"
    sites: list[tuple[str, int, str, set[str]]] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in ("audited_structured_call", "drive_audited_structured_call"):
                sites.append((str(path.relative_to(root.parent)), node.lineno, name,
                              {kw.arg for kw in node.keywords if kw.arg}))
    return sites


def test_every_existing_call_site_is_byte_identical(db) -> None:
    sig = inspect.signature(drive_audited_structured_call)
    param = sig.parameters["validate_semantics"]
    assert param.default is None
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert inspect.signature(audited_structured_call).parameters["validate_semantics"].default is None

    sites = _call_sites()
    seam = "src/featuregen/overlay/upload/enrich_llm.py"
    callers = {module for module, _line, _name, _kw in sites} - {seam}
    # 13 distinct caller modules / 14 call sites outside the seam module itself (the plan said 12).
    assert len(callers) == 13, sorted(callers)
    assert len([s for s in sites if s[0] != seam]) == 14

    passing = {(module, name) for module, _line, name, kwargs in sites
               if "validate_semantics" in kwargs}
    assert passing == _SEMANTIC_SEAM_CONSUMERS

    # ...and behaviourally: with the parameter defaulted, a body no semantic rule would accept is
    # returned unchanged, and the dispatched request is identical to an explicit `None`.
    absurd = {"concept": _REJECTED}
    task = "test.repairseam.default"
    default_rec = _Recorder(_client(absurd, task=task))
    explicit_rec = _Recorder(_client(absurd, task=task))
    default_res = _drive(db, default_rec, task=task)
    explicit_res = _drive(db, explicit_rec, task=task, validate_semantics=None)

    assert default_res.output == explicit_res.output == absurd
    assert default_res.failure_kind is explicit_res.failure_kind is None
    assert len(default_rec.requests) == len(explicit_rec.requests) == 1
    assert default_rec.requests[0] == explicit_rec.requests[0]
    assert INPUT_KEY_REPAIR_ERRORS not in default_rec.requests[0].inputs
