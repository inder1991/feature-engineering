"""Phase-1A Task 2 — the LLM-only, fail-open use-case recognizer.

FakeLLM-scripted (keyed on ``RECOGNIZER_TASK``) exercises of ``recognize``'s outcome mapping: a clean
CLASSIFIED body, an unknown-id body that exhausts the repair budget, a provider refusal, and an
UNSCOPED body — asserting the recognizer folds every failure to ``UNSCOPED``/``TECHNICAL_FAILURE`` and
never raises, never surfaces an invalid id. Plus ``build_recognition_prompt`` offers the selectable
ids and never the non-selectable ``financial_crime`` domain parent. See
``docs/superpowers/plans/2026-07-09-phase1a-shadow-recognizer.md`` Task 2.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from featuregen.contracts.contract_versions import contract_owner
from featuregen.documents.registry import DocumentSchemaRegistry
from featuregen.intake.llm import (
    PROVIDER_MAX_TOKENS,
    PROVIDER_OK,
    PROVIDER_REFUSAL,
    FakeLLM,
    FakeResponse,
)
from featuregen.intake.redaction import INPUT_KEY_REPAIR_ERRORS
from featuregen.overlay.upload.contract.scope_records import (
    find_recognition_attempt,
    load_recognition_attempt,
    record_recognition_attempt,
)
from featuregen.overlay.upload.enrich_llm import (
    FAILURE_KIND_SCHEMA_INVALID,
    FAILURE_KIND_SEMANTIC_INVALID,
    AuditedStructuredResult,
    register_enrichment_schemas,
)
from featuregen.overlay.upload.taxonomy import recognizer as recognizer_module
from featuregen.overlay.upload.taxonomy.applicability import scope_from_recognition
from featuregen.overlay.upload.taxonomy.recognition import (
    MALFORMED_EVIDENCE_SPANS,
    MULTIPLE_PRIMARY_CANDIDATES,
    RECOGNITION_VALIDATOR_VERSION,
    TAXONOMY_VERSION,
    CandidateDrop,
    RecognitionDisposition,
    RecognitionStatus,
    unscoped_result,
)
from featuregen.overlay.upload.taxonomy.recognizer import (
    _OUTPUT_SCHEMA_VERSION,
    RECOGNITION_REQUEST_CONTRACT,
    RECOGNITION_REQUEST_VERSION,
    RECOGNIZER_TASK,
    recognition_request_hash,
    recognition_request_material,
    recognize,
    recognize_with_audit,
)
from featuregen.overlay.upload.taxonomy.recognizer_prompt import (
    PROMPT_VERSION,
    build_recognition_prompt,
)

# Real selectable leaves (see use_cases.py).
CHURN = "customer.relationship_attrition.churn"
DEPOSIT = "customer.relationship_attrition.deposit_attrition"

_MODEL = "claude-sonnet-5"


def _candidate(
    use_case_id: str,
    *,
    relationship: str = "primary",
    confidence: str = "high",
    evidence_spans: tuple[str, ...] = ("close their current account",),
    rationale: str = "clear attrition intent",
) -> dict[str, Any]:
    return {
        "use_case_id": use_case_id,
        "relationship": relationship,
        "confidence": confidence,
        "evidence_spans": list(evidence_spans),
        "rationale": rationale,
    }


def _fake(output: dict[str, Any], *, provider_status: str = PROVIDER_OK) -> FakeLLM:
    # R19 task-key form: keyed on request.task (== RECOGNIZER_TASK). FakeLLM repeats the last
    # response once its sequence is exhausted, so a single response also drives the repair loop.
    return FakeLLM(script={RECOGNIZER_TASK: FakeResponse(output=output, provider_status=provider_status)})


def test_classified_output_maps_to_classified_result(db) -> None:
    output = {
        "status": "classified",
        "candidates": [
            _candidate(CHURN, relationship="primary"),
            _candidate(DEPOSIT, relationship="secondary", confidence="medium"),
        ],
        "ambiguity_note": None,
    }
    result = recognize(
        db, _fake(output),
        redacted_hypothesis="will this customer close their current account next quarter?")

    assert result.status is RecognitionStatus.CLASSIFIED
    primaries = [c for c in result.candidates if c.relationship == "primary"]
    assert len(primaries) == 1
    assert primaries[0].use_case_id == CHURN
    assert primaries[0].confidence == "high"
    assert primaries[0].evidence_spans == ("close their current account",)
    # Version quintet fields this phase owns are stamped on the result.
    assert result.taxonomy_version == TAXONOMY_VERSION
    assert result.recognizer_model_id == _MODEL
    assert result.prompt_version == PROMPT_VERSION


def test_unknown_use_case_id_fails_open_never_invalid(db) -> None:
    # Since Task 1 an unknown id fails the SCHEMA (v2's closed enum), so it exhausts the seam's
    # repair budget and arrives here as an empty body; before that it passed the schema and died in
    # the closed-taxonomy post-pass. Either way the recognizer folds it open — never an invalid id,
    # never a raise. (`test_an_invented_id_now_reaches_the_seam_as_doubt…` pins the new route.)
    output = {
        "status": "classified",
        "candidates": [_candidate("customer.not_a_real_leaf", relationship="primary")],
    }
    result = recognize(db, _fake(output), redacted_hypothesis="something vague and unmapped")

    assert result.status in (RecognitionStatus.TECHNICAL_FAILURE, RecognitionStatus.UNSCOPED)
    assert result.candidates == ()
    assert all(c.use_case_id != "customer.not_a_real_leaf" for c in result.candidates)


def test_provider_refusal_is_technical_failure(db) -> None:
    result = recognize(
        db, _fake({}, provider_status=PROVIDER_REFUSAL), redacted_hypothesis="anything at all")
    assert result.status is RecognitionStatus.TECHNICAL_FAILURE
    assert result.candidates == ()


def test_unscoped_output_maps_to_unscoped(db) -> None:
    result = recognize(
        db, _fake({"status": "unscoped", "candidates": []}),
        redacted_hypothesis="let's explore what is interesting in the data")
    assert result.status is RecognitionStatus.UNSCOPED
    assert result.candidates == ()


def test_prompt_lists_selectable_ids_not_financial_crime() -> None:
    prompt = build_recognition_prompt()
    # A known selectable leaf is offered as a pick.
    assert "credit.early_warning" in prompt
    # financial_crime is the non-selectable domain parent — never offered as a selectable choice.
    assert "financial_crime" not in prompt


def test_prompt_enumerates_dimension_vocabularies() -> None:
    prompt = build_recognition_prompt()
    # The model is asked to ALSO return the two optional dimensions, from their closed lists.
    assert "modelling_contexts" in prompt
    assert "target_entity" in prompt
    assert "ifrs9" in prompt        # a modelling-context regime is enumerated
    assert "customer" in prompt     # a target-entity grain is enumerated


# ── per-dimension failure semantics through the full fail-open recognizer ─────────────────────────
def test_valid_dimensions_are_stamped_on_the_result(db) -> None:
    output = {
        "status": "classified",
        "candidates": [_candidate(CHURN, relationship="primary")],
        "ambiguity_note": None,
        "modelling_contexts": ["ifrs9"],
        "target_entity": "customer",
    }
    result = recognize(db, _fake(output), redacted_hypothesis="ifrs9 ecl per customer")
    assert result.status is RecognitionStatus.CLASSIFIED
    assert result.modelling_contexts == ("ifrs9",)
    assert result.target_entity == "customer"
    assert result.warnings == ()


def test_invalid_modelling_context_does_not_invalidate_use_case(db) -> None:
    # THE core guarantee: an invalid OPTIONAL dimension never invalidates a valid use-case recognition.
    output = {
        "status": "classified",
        "candidates": [_candidate(CHURN, relationship="primary")],
        "ambiguity_note": None,
        "modelling_contexts": ["invented"],
        "target_entity": "customer",
    }
    result = recognize(db, _fake(output), redacted_hypothesis="will they churn?")
    assert result.status is RecognitionStatus.CLASSIFIED
    assert [c.use_case_id for c in result.candidates if c.relationship == "primary"] == [CHURN]
    assert result.modelling_contexts == ()                    # the invalid context is dropped
    assert "UNKNOWN_MODELLING_CONTEXT" in result.warnings
    assert result.target_entity == "customer"                 # the valid entity is untouched


def test_invalid_target_entity_cleared_use_case_preserved(db) -> None:
    output = {
        "status": "classified",
        "candidates": [_candidate(CHURN, relationship="primary")],
        "ambiguity_note": None,
        "modelling_contexts": ["ifrs9"],
        "target_entity": "not_an_entity",
    }
    result = recognize(db, _fake(output), redacted_hypothesis="will they churn?")
    assert result.status is RecognitionStatus.CLASSIFIED
    assert result.modelling_contexts == ("ifrs9",)            # the valid context is untouched
    assert result.target_entity is None                       # the invalid entity is cleared
    assert "UNKNOWN_TARGET_ENTITY" in result.warnings


def test_invalid_primary_still_fails_even_with_valid_dimensions(db) -> None:
    # A valid optional dimension does NOT rescue an INVALID primary use-case — the whole recognition
    # is unscoped, carrying no confirmed dimensions.
    output = {
        "status": "classified",
        "candidates": [_candidate("customer.not_a_real_leaf", relationship="primary")],
        "modelling_contexts": ["ifrs9"],
        "target_entity": "customer",
    }
    result = recognize(db, _fake(output), redacted_hypothesis="unmapped and vague")
    assert result.status in (RecognitionStatus.TECHNICAL_FAILURE, RecognitionStatus.UNSCOPED)
    assert result.candidates == ()
    assert result.modelling_contexts == ()
    assert result.target_entity is None


# ── Task 0 (2026-08-15): the recognition REQUEST identity ───────────────────────────────────────


def test_the_request_hash_covers_every_leg_of_the_contract(monkeypatch) -> None:
    """One leg per line, each proved by changing ONLY it. This is the whole point of Task 0: the
    old key covered the redacted user input and nothing else, so a build that would now answer
    differently kept serving — and re-serving the id of — the old answer."""
    base = recognition_request_hash(input_content_hash="c" * 64)

    assert recognition_request_hash(input_content_hash="d" * 64) != base       # user input
    assert recognition_request_hash(
        input_content_hash="c" * 64, model_id="claude-opus-4-8") != base       # model

    def _leg(module: str, name: str, value: Any) -> str:
        monkeypatch.setattr(f"featuregen.overlay.upload.taxonomy.{module}.{name}", value)
        return recognition_request_hash(input_content_hash="c" * 64)

    assert _leg("recognizer", "build_recognition_prompt", lambda: "a different prompt") != base
    monkeypatch.undo()
    assert _leg("recognizer", "PROMPT_VERSION", "99") != base
    monkeypatch.undo()
    # The schema by CONTENT, not by version number (blocker B4: `register_schema` upserts, so one
    # version can mean two bodies on two deployments — the number alone is not an identity).
    assert _leg("recognizer", "canonical_output_schema",
                lambda _id, _v: {"type": "object"}) != base
    monkeypatch.undo()
    assert _leg("recognizer", "TAXONOMY_VERSION", "9.9.9") != base
    monkeypatch.undo()
    # "99", not "2": Task 4 bumped the real validator version TO "2", and a leg test that patches in
    # the value already there proves nothing.
    assert _leg("recognizer", "RECOGNITION_VALIDATOR_VERSION", "99") != base
    monkeypatch.undo()
    assert _leg("recognizer", "APPLICABILITY_MAPPING_VERSION", "9.9.9") != base
    monkeypatch.undo()
    monkeypatch.setattr(
        "featuregen.overlay.upload.taxonomy.recognizer.current_enrichment_generation_settings",
        lambda: {"provider": "fake", "model": "test", "max_tokens": 4096})
    assert recognition_request_hash(input_content_hash="c" * 64) != base       # generation controls


def test_the_two_hashes_answer_different_questions() -> None:
    """``input_content_hash`` keeps its own meaning — the redacted user input, and nothing else —
    and is carried INTO the request material rather than replaced by it. Neither absorbs the other:
    sealed-input verification must stay able to re-derive its hash from the input alone."""
    content = "e" * 64
    material = recognition_request_material(input_content_hash=content)
    assert material["input_content_hash"] == content
    assert recognition_request_hash(input_content_hash=content) != content


def test_the_request_hash_is_stable_across_calls() -> None:
    """No clock, no randomness, no set-ordering: an identity that drifted would make every re-run a
    new request and reinstate the double-spend Task 0 removes."""
    assert (recognition_request_hash(input_content_hash="f" * 64)
            == recognition_request_hash(input_content_hash="f" * 64))


def test_the_request_contract_version_is_registered() -> None:
    """``contract_hash_v1`` refuses an unregistered (name, version) — the loud failure that stops an
    ungoverned identity being minted. Asserted directly so the registration is not merely implied by
    the hash happening to work."""
    assert contract_owner(RECOGNITION_REQUEST_CONTRACT, RECOGNITION_REQUEST_VERSION) == (
        "featuregen.overlay.upload.taxonomy.recognizer")


# ── Task 1 (2026-08-15): the frozen output contract, ACTIVATED ──────────────────────────────────


class _RecordingClient:
    """An LLMClient that remembers the request it was handed. The dispatched schema version is not
    observable any other way — and it is the whole question here."""

    def __init__(self, inner: FakeLLM) -> None:
        self._inner = inner
        self.requests: list[Any] = []

    def call(self, request: Any) -> Any:
        self.requests.append(request)
        return self._inner.call(request)


def test_the_dispatched_call_is_held_to_the_frozen_v2_contract(db) -> None:
    """Activation is a CALL-SITE fact, not a constant. ``drive_audited_structured_call`` defaults
    ``schema_version=1``, so bumping ``_OUTPUT_SCHEMA_VERSION`` alone would have left every real
    dispatch enforced against v1 while the request identity, the audit row and the release gate all
    claimed v2 — the exact class of drift Task 0 built the identity hash to stop."""
    client = _RecordingClient(_fake({
        "status": "classified",
        "candidates": [_candidate(CHURN, relationship="primary")],
    }))
    result = recognize(db, client, redacted_hypothesis="will this customer leave?")

    assert result.status is RecognitionStatus.CLASSIFIED
    assert client.requests, "the recognizer never dispatched"
    request = client.requests[0]
    assert request.output_schema_version == _OUTPUT_SCHEMA_VERSION == 2
    enum = (request.output_schema["properties"]["candidates"]["items"]["properties"]
            ["use_case_id"]["enum"])
    assert len(enum) == 88 and CHURN in enum
    # The platform's own outcome is not on the wire for the model to claim.
    assert request.output_schema["properties"]["status"]["enum"] == [
        "classified", "ambiguous", "unscoped"]


def test_an_invented_id_now_reaches_the_seam_as_doubt_not_as_a_silent_failure(db) -> None:
    """The live incident's value, end to end. Under v1 the body validated and the invented id died
    in a post-call pass the seam never saw — ``repair_attempts: []``, one provider call, and the
    correct sibling discarded with it. Under v2 it is malformed STRUCTURE, so the seam re-prompts
    within its bounded budget. The disposition is still fail-open (Tasks 2-4 make the recovery
    honest); what changed is that the model is now ASKED to fix it."""
    client = _RecordingClient(_fake({
        "status": "classified",
        "candidates": [_candidate("x", relationship="primary", evidence_spans=("x",),
                                  rationale="placeholder")],
    }))
    result = recognize(db, client, redacted_hypothesis="predict churn in the next 90 days")

    assert result.status is RecognitionStatus.TECHNICAL_FAILURE
    assert result.candidates == ()
    assert len(client.requests) == 3, "the repair budget (2) was not spent on the invalid id"
    # And the re-prompt names the FIELD, never the value — this text is audited and re-egressed.
    repairs = client.requests[-1].inputs["_repair_errors"]     # accumulated, one per failed attempt
    assert set(repairs) == {"$.candidates[0].use_case_id: failed 'enum'"}
    assert not any("placeholder" in str(r) or "'x'" in str(r) for r in repairs)


def test_a_v1_answer_is_not_reused_as_the_answer_to_the_v2_question(db, monkeypatch) -> None:
    """The Task 0 interaction, proved rather than discovered. The schema enters the request identity
    by CONTENT and by version, so activating v2 re-keys every objective — which is correct: a
    different contract is a different question, and the stored v1 answer (produced under a schema
    that could not even refuse ``"x"``) is not its answer."""
    sealed = "a1" * 32
    v2_hash = recognition_request_hash(input_content_hash=sealed)

    monkeypatch.setattr(
        "featuregen.overlay.upload.taxonomy.recognizer._OUTPUT_SCHEMA_VERSION", 1)
    v1_hash = recognition_request_hash(input_content_hash=sealed)
    assert v1_hash != v2_hash

    # The stored v1 answer stays exactly where it is — found by its OWN identity, never by the new
    # one. (The re-run therefore asks the provider again, which is the point.)
    stored = record_recognition_attempt(
        db, intent_id="intent_schema_v2", input_hash=v1_hash, result=unscoped_result(
            "nothing in scope", model_id=_MODEL, prompt_version=PROMPT_VERSION),
        actor="ds1", input_content_hash=sealed, recognition_request_hash=v1_hash)
    found = find_recognition_attempt(
        db, intent_id="intent_schema_v2", recognition_request_hash=v1_hash)
    assert found is not None and found.recognition_id == stored
    assert find_recognition_attempt(
        db, intent_id="intent_schema_v2", recognition_request_hash=v2_hash) is None

    # And the SEALED-INPUT identity is untouched by the schema flip: the lineage trigger and
    # `load_recognition_input` join on it, so a legacy row's own identity cannot move underneath it.
    assert recognition_request_material(input_content_hash=sealed)["input_content_hash"] == sealed
    monkeypatch.undo()
    assert recognition_request_material(input_content_hash=sealed)["input_content_hash"] == sealed


# ── Task 3 (2026-08-15): the recognizer's OWN rules run INSIDE the repair loop ───────────────────

#: The live incident, recorded verbatim in ``llm_call.raw_output`` (run
#: ``grun_01M02SAZWKC6FJ4DPTY4WVCB12``, 13:20:17Z). TWO defects, and they fail on DIFFERENT arms:
#: the invented ``"x"`` id is malformed STRUCTURE under the frozen v2 enum (Task 1), and the two
#: ``"primary"`` candidates are a SEMANTIC failure no JSON Schema can express (this task).
_LIVE_INCIDENT_BODY: dict[str, Any] = {
    "status": "classified",
    "candidates": [
        {"use_case_id": CHURN, "relationship": "primary", "confidence": "high",
         "evidence_spans": ["predict churn in the next 90 days",
                            "customers whose transaction activity suddenly accelerates are about "
                            "to leave"],
         "rationale": ""},
        {"use_case_id": "x", "relationship": "primary", "confidence": "high",
         "evidence_spans": ["x"], "rationale": "placeholder"}],
    "modelling_contexts": [],
}

#: What the model returns when it is finally ASKED to fix its answer.
_CLEAN_BODY: dict[str, Any] = {
    "status": "classified",
    "candidates": [_candidate(CHURN, relationship="primary",
                              evidence_spans=("predict churn in the next 90 days",))],
    "modelling_contexts": [],
}

#: The body whose ONLY defect is the aggregate one: two VALID leaf ids, both ``primary``. The v2
#: schema accepts it (asserted, not assumed, below) — so if this drives repair, the caller's
#: semantics drove it and nothing else could have.
_DOUBLE_PRIMARY_BODY: dict[str, Any] = {
    "status": "classified",
    "candidates": [_candidate(CHURN, relationship="primary"),
                   _candidate(DEPOSIT, relationship="primary")],
}

_HYPOTHESIS = "customers whose transaction activity suddenly accelerates are about to leave"
_GOAL = "predict churn in the next 90 days"


def _script(*bodies: dict[str, Any]) -> FakeLLM:
    """FakeLLM over a SEQUENCE of turns (it repeats the last one once exhausted, so a single body
    drives the whole repair budget and a two-body script drives 'invalid, then fixed')."""
    return FakeLLM(script={RECOGNIZER_TASK: [FakeResponse(output=b, provider_status=PROVIDER_OK)
                                             for b in bodies]})


def _repair_reasons(client: _RecordingClient) -> list[str]:
    """The complaint text actually put on the wire in the LAST turn — read from the dispatched
    request, never inferred. This string is re-prompted to the provider AND persisted."""
    return list(client.requests[-1].inputs.get(INPUT_KEY_REPAIR_ERRORS, []))


def test_the_padded_body_from_the_live_incident_now_recognises(db) -> None:
    """The incident, end to end: the padded body is asked to be fixed, the fixed answer is what the
    user gets, and the correct candidate is no longer discarded with its placeholder sibling.

    Honest about the split: it is TASK 1 that routes THIS body into repair (the ``"x"`` fails the
    frozen enum), so this case would already pass before Task 3. It is pinned anyway because it is
    the incident, and because the acceptance of Task 3 is that the OTHER defect in the very same
    body — two primaries — is now repairable too (the next test isolates it)."""
    client = _RecordingClient(_script(_LIVE_INCIDENT_BODY, _CLEAN_BODY))
    audited = recognize_with_audit(db, client, redacted_hypothesis=_HYPOTHESIS,
                                   redacted_goal=_GOAL)

    assert audited.result.status is RecognitionStatus.CLASSIFIED
    assert [c.use_case_id for c in audited.result.candidates] == [CHURN]
    assert len(client.requests) == 2, "the model was not asked to fix the padded body"
    # The complaint names the failing field, never the model's text.
    wire = str(_repair_reasons(client))
    assert "placeholder" not in wire and "'x'" not in wire


def test_the_semantic_arm_alone_drives_repair(db) -> None:
    """The distinct contribution of Task 3, isolated. Both ids are real leaves and every band is in
    range, so the frozen v2 schema ACCEPTS this body — asserted directly against the registry, so
    the proof does not rest on reading the schema file. The only thing left that can drive a repair
    turn is ``validate_recognition_output`` running INSIDE the loop."""
    register_enrichment_schemas(db)
    DocumentSchemaRegistry(db).validate(
        "use_case_recognition", _OUTPUT_SCHEMA_VERSION, _DOUBLE_PRIMARY_BODY)   # no raise

    client = _RecordingClient(_script(_DOUBLE_PRIMARY_BODY, _CLEAN_BODY))
    audited = recognize_with_audit(db, client, redacted_hypothesis=_HYPOTHESIS,
                                   redacted_goal=_GOAL)

    assert audited.result.status is RecognitionStatus.CLASSIFIED
    assert [c.use_case_id for c in audited.result.candidates] == [CHURN]
    assert len(client.requests) == 2
    # A closed code — the model is told WHICH rule it broke, and nothing about the values it chose.
    assert _repair_reasons(client) == [MULTIPLE_PRIMARY_CANDIDATES]
    assert CHURN not in str(_repair_reasons(client))


def test_a_body_that_stays_invalid_after_repair_reaches_task_4(db, monkeypatch) -> None:
    """Repair exhausted on the SEMANTIC arm: the platform still invents no scope, and the final
    schema-valid body rides out on the seam result for Task 4 to partition. The seam call is SPIED
    (the real function runs) — the exposure is a fact about the recognizer's own call site."""
    seen: list[Any] = []
    real = recognizer_module.drive_audited_structured_call

    def _spy(*args: Any, **kwargs: Any) -> Any:
        outcome = real(*args, **kwargs)
        seen.append(outcome)
        return outcome

    monkeypatch.setattr(recognizer_module, "drive_audited_structured_call", _spy)
    client = _RecordingClient(_script(_DOUBLE_PRIMARY_BODY))       # never fixed
    audited = recognize_with_audit(db, client, redacted_hypothesis=_HYPOTHESIS,
                                   redacted_goal=_GOAL)

    assert audited.result.status is RecognitionStatus.TECHNICAL_FAILURE
    assert audited.result.candidates == ()          # no scope is invented from an unrepaired body
    assert len(client.requests) == 3                # 1 + DEFAULT_REPAIR_BUDGET
    assert len(seen) == 1
    assert seen[0].output is None                   # never a validated result
    assert seen[0].failure_kind == FAILURE_KIND_SEMANTIC_INVALID
    assert seen[0].last_schema_valid_semantic_invalid_output == _DOUBLE_PRIMARY_BODY


def test_repair_is_recorded_on_the_llm_call(db) -> None:
    """The incident's audit row said ``repair_attempts: []`` — the evidence that the model was never
    asked. It is not empty any more, and it names the closed code, not the value."""
    client = _RecordingClient(_script(_DOUBLE_PRIMARY_BODY, _CLEAN_BODY))
    audited = recognize_with_audit(db, client, redacted_hypothesis=_HYPOTHESIS,
                                   redacted_goal=_GOAL)

    row = db.execute(
        "SELECT validation_result, repair_attempts FROM llm_call WHERE llm_call_ref = %s",
        (audited.llm_call_ref,)).fetchone()
    assert row is not None
    validation_result, repair_attempts = row[0], row[1]
    assert repair_attempts, "the repair round-trip left no audit trace"
    assert [a["class"] for a in repair_attempts] == ["repair"]
    assert [a["reason"] for a in repair_attempts] == [MULTIPLE_PRIMARY_CANDIDATES]
    assert validation_result["result"] == "repaired"


def test_the_post_call_floor_still_refuses_a_body_the_seam_let_through(db, monkeypatch) -> None:
    """Belt and braces. The seam now runs the same rules inside the loop, so this can only fire if
    that wiring regresses — which is exactly why it stays: a repair that never succeeded must still
    never yield a scope. The seam is replaced (not spied) to manufacture the impossible."""
    monkeypatch.setattr(
        recognizer_module, "drive_audited_structured_call",
        lambda *_a, **_k: AuditedStructuredResult(
            output=dict(_DOUBLE_PRIMARY_BODY), llm_call_ref="llmc_floor", provider_calls=1,
            usage={}))
    audited = recognize_with_audit(db, _RecordingClient(_script(_CLEAN_BODY)),
                                   redacted_hypothesis=_HYPOTHESIS)

    assert audited.result.status is RecognitionStatus.TECHNICAL_FAILURE
    assert audited.result.candidates == ()
    # …and the note the user's row carries names the rule, never the model's text.
    assert MULTIPLE_PRIMARY_CANDIDATES in str(audited.result.ambiguity_note)


def test_the_recognizer_is_a_reviewed_consumer_of_the_semantic_seam(db) -> None:
    """Task 2 left an enumerable allow-list of call sites permitted to pass ``validate_semantics``.
    Task 3 adds ONE line to it. Asserted here as well as in the seam's own AST test so the
    recognizer's side of the contract is visible from the recognizer's own file."""
    source = Path(recognizer_module.__file__).read_text(encoding="utf-8")
    assert "validate_semantics=validate_recognition_output" in source


# ── Task 4 (2026-08-15): the partition, folded into the recognizer ──────────────────────────────
#
# What can actually REACH the partition is narrower than the frozen rule's list, and deliberately
# recorded here: since Task 1's frozen v2 enum, an unknown id, a non-leaf primary, an out-of-band
# confidence/relationship and a non-string evidence span are all malformed STRUCTURE, so the seam
# reports `schema_invalid` and exposes no body at all. A BLANK evidence span is the one
# candidate-local defect the schema accepts and these rules reject — which is why it is the sibling
# in the end-to-end cases below. The unit cases in test_recognition_contract.py cover the rest of
# the rule directly, where the schema is not in the way.

def _blank_span_sibling(use_case_id: str = DEPOSIT, *, relationship: str = "secondary"):
    return _candidate(use_case_id, relationship=relationship, evidence_spans=("   ",))


def _seam_spy(monkeypatch) -> list[Any]:
    """Wrap — never replace — the real seam call, so a test can read the disposition the recognizer
    actually received."""
    seen: list[Any] = []
    real = recognizer_module.drive_audited_structured_call

    def _spy(*args: Any, **kwargs: Any) -> Any:
        outcome = real(*args, **kwargs)
        seen.append(outcome)
        return outcome

    monkeypatch.setattr(recognizer_module, "drive_audited_structured_call", _spy)
    return seen


def test_a_junk_sibling_no_longer_costs_the_valid_answer(db) -> None:
    """The done-bar, end to end, on the arm where repair FAILED. The model was asked twice and never
    fixed its answer; the valid primary is still the user's scope, and the loss is named."""
    body = {"status": "classified",
            "candidates": [_candidate(CHURN, relationship="primary"), _blank_span_sibling()]}
    client = _RecordingClient(_script(body))            # never repaired
    audited = recognize_with_audit(db, client, redacted_hypothesis=_HYPOTHESIS,
                                   redacted_goal=_GOAL)

    assert len(client.requests) == 3                    # the repair budget WAS spent first
    assert audited.result.status is RecognitionStatus.CLASSIFIED
    assert [(c.use_case_id, c.relationship) for c in audited.result.candidates] == [
        (CHURN, "primary")]
    assert audited.result.dropped_candidates == (
        CandidateDrop(index=1, reason_code=MALFORMED_EVIDENCE_SPANS),)
    # The loss does NOT ride `warnings` — that field means "we couldn't map part of what you
    # described", which is a different thing and is what the UI already renders it as.
    assert audited.result.warnings == ()
    assert scope_from_recognition(audited.result).primary == CHURN


def test_the_live_incident_is_refused_by_the_aggregate_rule_not_rescued_by_partition(db) -> None:
    """Two primaries, end to end, with repair failing. The partition REFUSES: choosing between the
    model's two primaries is not recovery. This is the case Task 3's repair exists to rescue."""
    client = _RecordingClient(_script(_DOUBLE_PRIMARY_BODY))
    audited = recognize_with_audit(db, client, redacted_hypothesis=_HYPOTHESIS,
                                   redacted_goal=_GOAL)

    assert audited.result.status is RecognitionStatus.TECHNICAL_FAILURE
    assert audited.result.candidates == ()
    assert scope_from_recognition(audited.result).unscoped is True
    # …and it is NOT laundered into a one-primary scope by dropping the other candidate.
    assert CHURN not in [c.use_case_id for c in audited.result.candidates]


def test_nothing_valid_survives_is_unchanged(db) -> None:
    """Every candidate individually invalid: today's fail-open technical failure, untouched."""
    body = {"status": "classified",
            "candidates": [_blank_span_sibling(CHURN, relationship="primary"),
                           _blank_span_sibling()]}
    audited = recognize_with_audit(db, _RecordingClient(_script(body)),
                                   redacted_hypothesis=_HYPOTHESIS)

    assert audited.result.status is RecognitionStatus.TECHNICAL_FAILURE
    assert audited.result.candidates == ()
    assert audited.result.dropped_candidates == ()      # nothing recovered → nothing to report yet


def test_the_only_primary_being_invalid_does_not_promote_a_secondary(db) -> None:
    """The surviving candidate keeps the relationship the MODEL gave it. `classified` is what is
    given up — and a primary-less result grounds everything rather than narrowing on a guess."""
    body = {"status": "classified",
            "candidates": [_blank_span_sibling(CHURN, relationship="primary"),
                           _candidate(DEPOSIT, relationship="secondary")]}
    audited = recognize_with_audit(db, _RecordingClient(_script(body)),
                                   redacted_hypothesis=_HYPOTHESIS)

    assert audited.result.status is RecognitionStatus.AMBIGUOUS
    assert [(c.use_case_id, c.relationship) for c in audited.result.candidates] == [
        (DEPOSIT, "secondary")]
    assert audited.result.dropped_candidates == (
        CandidateDrop(index=0, reason_code=MALFORMED_EVIDENCE_SPANS),)
    scope = scope_from_recognition(audited.result)
    assert scope.primary is None and scope.unscoped is True


def test_only_the_semantic_arm_is_partitioned(db, monkeypatch) -> None:
    """A SCHEMA-invalid body carries no exposed body by the seam's own design, so there is nothing
    to partition and the disposition is unchanged. Asserted on the seam result the recognizer
    received, so it cannot pass by coincidence of the outcome."""
    seen = _seam_spy(monkeypatch)
    invented = {"status": "classified",
                "candidates": [_candidate("x", relationship="primary", evidence_spans=("x",))]}
    audited = recognize_with_audit(db, _RecordingClient(_script(invented)),
                                   redacted_hypothesis=_HYPOTHESIS)

    assert seen[0].failure_kind == FAILURE_KIND_SCHEMA_INVALID
    assert seen[0].last_schema_valid_semantic_invalid_output is None
    assert audited.result.status is RecognitionStatus.TECHNICAL_FAILURE
    assert audited.result.dropped_candidates == ()


def test_a_partitioned_result_persists_its_drops(db) -> None:
    """**Task 5 closed the loss Task 4 recorded.** Until migration 1071 there was no column for
    candidate loss, so a result read back from storage carried `dropped_candidates == ()` — and by
    Task 0's invariant the endpoint serves the STORED row, which meant the drops never reached the
    wire at all. This test used to assert that loss (`…_is_not_persisted_with_its_drops_yet`) rather
    than hide it; it now asserts its closure, on the same body.

    The evidence the drops were always recomputable FROM stays durable and joined to the row —
    `llm_call.raw_output` holds the partitioned body verbatim — because a served summary and the
    evidence for it are different obligations."""
    body = {"status": "classified",
            "candidates": [_candidate(CHURN, relationship="primary"), _blank_span_sibling()]}
    audited = recognize_with_audit(db, _RecordingClient(_script(body)),
                                   redacted_hypothesis=_HYPOTHESIS)
    assert audited.result.dropped_candidates          # in memory: present

    request_hash = "b4" * 32
    recognition_id = record_recognition_attempt(
        db, intent_id="intent_partition_drops", input_hash=request_hash, result=audited.result,
        actor="ds1", input_content_hash="c9" * 32, recognition_request_hash=request_hash,
        llm_call_ref=audited.llm_call_ref, quality=audited.quality)
    stored = load_recognition_attempt(db, recognition_id=recognition_id)

    assert stored is not None
    assert stored.result is not audited.result                        # rebuilt from the ROW
    assert stored.result.status is RecognitionStatus.CLASSIFIED
    assert [c.use_case_id for c in stored.result.candidates] == [CHURN]
    assert stored.result.dropped_candidates == (
        CandidateDrop(index=1, reason_code=MALFORMED_EVIDENCE_SPANS),)
    assert stored.quality is not None
    assert stored.quality.disposition is RecognitionDisposition.PARTIALLY_RECOVERED
    assert stored.quality.dropped_candidate_count == 1
    assert stored.quality.drop_reason_codes == (MALFORMED_EVIDENCE_SPANS,)
    assert stored.quality.repair_attempts == 2        # the budget was spent before anything was cut
    row = db.execute(
        "SELECT llm_call_ref FROM intent_recognition_attempt WHERE recognition_id = %s",
        (recognition_id,)).fetchone()
    assert row[0] == audited.llm_call_ref
    raw = db.execute("SELECT raw_output FROM llm_call WHERE llm_call_ref = %s",
                     (audited.llm_call_ref,)).fetchone()[0]
    assert len(raw["output"]["candidates"]) == 2      # the body the partition ran on, verbatim


def test_a_row_written_without_a_quality_reads_back_as_an_absence(db) -> None:
    """The legacy shape, and the honest-absence law on the read path. A caller that did not observe
    the quality writes three NULLs, and the reader says `None` — never a fabricated `clean`, which
    would claim the model answered first time when nobody recorded whether it did."""
    audited = recognize_with_audit(db, _RecordingClient(_script(_CLEAN_BODY)),
                                   redacted_hypothesis=_HYPOTHESIS)
    request_hash = "b5" * 32
    recognition_id = record_recognition_attempt(
        db, intent_id="intent_no_quality", input_hash=request_hash, result=audited.result,
        actor="ds1", input_content_hash="ca" * 32, recognition_request_hash=request_hash)
    stored = load_recognition_attempt(db, recognition_id=recognition_id)

    assert stored is not None
    assert stored.quality is None
    assert stored.result.dropped_candidates == ()
    assert db.execute(
        "SELECT recognition_disposition, repair_attempt_count, dropped_candidates "
        "FROM intent_recognition_attempt WHERE recognition_id = %s",
        (recognition_id,)).fetchone() == (None, None, None)


def test_a_quality_that_describes_a_different_result_is_refused(db) -> None:
    """The stored drops come from the RESULT and the served count is a projection of them, so a
    quality built from some other result would be persisted as a count that silently disagrees with
    the record it summarises. Refused at the writer, where it is still a bug rather than a lie."""
    audited = recognize_with_audit(db, _RecordingClient(_script(_CLEAN_BODY)),
                                   redacted_hypothesis=_HYPOTHESIS)
    mismatched = replace(audited.quality, dropped_candidate_count=3,
                         drop_reason_codes=(MALFORMED_EVIDENCE_SPANS,))
    request_hash = "b6" * 32
    with pytest.raises(ValueError, match="does not describe this result"):
        record_recognition_attempt(
            db, intent_id="intent_bad_quality", input_hash=request_hash, result=audited.result,
            actor="ds1", input_content_hash="cb" * 32, recognition_request_hash=request_hash,
            quality=mismatched)


def test_a_retry_is_not_a_repair(db) -> None:
    """The distinction `repair_attempts` exists to keep. A truncated turn is re-REQUESTED — the same
    question, asked again — while a repair turn tells the model its answer was faulted. Counting
    them together (the tempting `provider_calls - 1`) would tell a user their scope had been
    questioned when it had not, on a call where nothing was ever wrong with the answer."""
    truncated = FakeResponse(output={}, provider_status=PROVIDER_MAX_TOKENS)
    client = _RecordingClient(FakeLLM(script={RECOGNIZER_TASK: [
        truncated, FakeResponse(output=_CLEAN_BODY, provider_status=PROVIDER_OK)]}))
    audited = recognize_with_audit(db, client, redacted_hypothesis=_HYPOTHESIS)

    assert audited.result.status is RecognitionStatus.CLASSIFIED
    assert len(client.requests) == 2                      # two PHYSICAL provider calls…
    assert audited.provider_calls == 2
    assert audited.quality.repair_attempts == 0           # …and zero corrections
    assert audited.quality.disposition is RecognitionDisposition.CLEAN
    ledger = db.execute("SELECT repair_attempts FROM llm_call WHERE llm_call_ref = %s",
                        (audited.llm_call_ref,)).fetchone()[0]
    assert [a["class"] for a in ledger] == ["retry"]      # the ledger says which kind it was


def test_the_validator_version_moved_with_what_the_recognizer_accepts(monkeypatch) -> None:
    """Task 4 changes the ANSWER a body can produce, so the stored answer to the same words under
    the old rules is not the answer to this question. The request identity carries the validator
    version, so it re-keys — which is the whole reason Task 0 put it in the hash."""
    assert RECOGNITION_VALIDATOR_VERSION == "2"
    material = recognition_request_material(input_content_hash="c" * 64)
    assert material["semantic_validator_version"] == "2"

    base = recognition_request_hash(input_content_hash="c" * 64)
    monkeypatch.setattr(
        "featuregen.overlay.upload.taxonomy.recognizer.RECOGNITION_VALIDATOR_VERSION", "1")
    assert recognition_request_hash(input_content_hash="c" * 64) != base
