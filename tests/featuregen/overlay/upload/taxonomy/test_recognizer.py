"""Phase-1A Task 2 — the LLM-only, fail-open use-case recognizer.

FakeLLM-scripted (keyed on ``RECOGNIZER_TASK``) exercises of ``recognize``'s outcome mapping: a clean
CLASSIFIED body, an unknown-id body that exhausts the repair budget, a provider refusal, and an
UNSCOPED body — asserting the recognizer folds every failure to ``UNSCOPED``/``TECHNICAL_FAILURE`` and
never raises, never surfaces an invalid id. Plus ``build_recognition_prompt`` offers the selectable
ids and never the non-selectable ``financial_crime`` domain parent. See
``docs/superpowers/plans/2026-07-09-phase1a-shadow-recognizer.md`` Task 2.
"""
from __future__ import annotations

from typing import Any

from featuregen.contracts.contract_versions import contract_owner
from featuregen.intake.llm import (
    PROVIDER_OK,
    PROVIDER_REFUSAL,
    FakeLLM,
    FakeResponse,
)
from featuregen.overlay.upload.taxonomy.recognition import (
    TAXONOMY_VERSION,
    RecognitionStatus,
)
from featuregen.overlay.upload.taxonomy.recognizer import (
    RECOGNITION_REQUEST_CONTRACT,
    RECOGNITION_REQUEST_VERSION,
    RECOGNIZER_TASK,
    recognition_request_hash,
    recognition_request_material,
    recognize,
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
    # An unknown id is structurally valid (passes the JSON schema) but fails the closed-taxonomy
    # semantic post-pass (validate_recognition_output) -> fail-open. Never an invalid id, never raises.
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
    assert _leg("recognizer", "RECOGNITION_VALIDATOR_VERSION", "2") != base
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
