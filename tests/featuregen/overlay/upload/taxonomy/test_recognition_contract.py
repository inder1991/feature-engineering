"""Phase-1A Task 1 — recognition contracts + closed-taxonomy validator.

Exercises ``validate_recognition_output`` (the raw-dict ``validate_output`` callback
``drive_structured_call`` will invoke in Task 2) against the closed use-case taxonomy, plus the
``unscoped_result`` fail-open constructor. See
``docs/superpowers/plans/2026-07-09-phase1a-shadow-recognizer.md`` Task 1.
"""
from __future__ import annotations

from typing import Any

import pytest

from featuregen.contracts import AttestedSchemaValidationError, SchemaValidationError
from featuregen.overlay.upload.enrich_llm import _SEMANTIC_REPAIR_CODE, _semantic_repair_code
from featuregen.overlay.upload.taxonomy.recognition import (
    CLASSIFIED_REQUIRES_ONE_PRIMARY,
    DUPLICATE_CANDIDATE,
    INVALID_CONFIDENCE,
    INVALID_RELATIONSHIP,
    INVALID_STATUS,
    MALFORMED_CANDIDATE,
    MALFORMED_CANDIDATE_LIST,
    MALFORMED_EVIDENCE_SPANS,
    MULTIPLE_PRIMARY_CANDIDATES,
    PRIMARY_NOT_A_LEAF_OBJECTIVE,
    RECOGNITION_FAILURE_CODES,
    STATUS_REQUIRES_A_CANDIDATE,
    TAXONOMY_VERSION,
    TOO_MANY_CANDIDATES,
    TOO_MANY_SECONDARY_CANDIDATES,
    UNKNOWN_MODELLING_CONTEXT,
    UNKNOWN_TARGET_ENTITY,
    UNKNOWN_USE_CASE_ID,
    RecognitionStatus,
    normalize_dimensions,
    unscoped_result,
    validate_recognition_output,
)

# A real selectable leaf (see use_cases.py) — the canonical well-formed primary.
CHURN = "customer.relationship_attrition.churn"
DEPOSIT = "customer.relationship_attrition.deposit_attrition"
PRIMACY = "customer.relationship_attrition.primacy_loss"


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


def _classified(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    return {"status": "classified", "candidates": candidates, "ambiguity_note": None}


# ── the happy path ──────────────────────────────────────────────────────────────────────────────
def test_accepts_well_formed_classified_body():
    body = _classified([
        _candidate(CHURN, relationship="primary"),
        _candidate(DEPOSIT, relationship="secondary", confidence="medium"),
    ])
    # Returns None (raises on anything malformed).
    assert validate_recognition_output(body) is None


def test_accepts_unscoped_with_empty_candidates():
    # A unscoped/technical_failure status with NO candidates is valid.
    assert validate_recognition_output({"status": "unscoped", "candidates": []}) is None
    assert validate_recognition_output({"status": "technical_failure", "candidates": []}) is None


# ── the rejections ────────────────────────────────────────────────────────────────────────────────
def test_rejects_unknown_status():
    with pytest.raises(SchemaValidationError):
        validate_recognition_output({"status": "maybe", "candidates": []})
    with pytest.raises(SchemaValidationError):
        validate_recognition_output({"candidates": []})  # status missing


def test_rejects_unknown_use_case_id():
    with pytest.raises(SchemaValidationError):
        validate_recognition_output(_classified([_candidate("customer.not_a_real_leaf")]))


def test_rejects_non_selectable_primary():
    # financial_crime is the one non-selectable domain parent — never a valid primary objective.
    with pytest.raises(SchemaValidationError):
        validate_recognition_output(_classified([_candidate("financial_crime")]))


def test_rejects_two_primaries():
    with pytest.raises(SchemaValidationError):
        validate_recognition_output(_classified([
            _candidate(CHURN, relationship="primary"),
            _candidate(DEPOSIT, relationship="primary"),
        ]))


def test_rejects_three_secondaries():
    with pytest.raises(SchemaValidationError):
        validate_recognition_output(_classified([
            _candidate(CHURN, relationship="secondary"),
            _candidate(DEPOSIT, relationship="secondary"),
            _candidate(PRIMACY, relationship="secondary"),
        ]))


def test_rejects_more_than_three_candidates():
    with pytest.raises(SchemaValidationError):
        validate_recognition_output(_classified([
            _candidate(CHURN, relationship="primary"),
            _candidate(DEPOSIT, relationship="secondary"),
            _candidate(PRIMACY, relationship="secondary"),
            _candidate("customer.cross_sell", relationship="secondary"),
        ]))


def test_rejects_classified_with_no_candidates():
    with pytest.raises(SchemaValidationError):
        validate_recognition_output(_classified([]))
    with pytest.raises(SchemaValidationError):
        validate_recognition_output({"status": "ambiguous", "candidates": []})


def test_rejects_bad_confidence():
    with pytest.raises(SchemaValidationError):
        validate_recognition_output(_classified([_candidate(CHURN, confidence="very-high")]))


def test_rejects_bad_relationship():
    with pytest.raises(SchemaValidationError):
        validate_recognition_output(_classified([_candidate(CHURN, relationship="tertiary")]))


def test_rejects_empty_evidence_span():
    with pytest.raises(SchemaValidationError):
        validate_recognition_output(_classified([_candidate(CHURN, evidence_spans=("",))]))
    with pytest.raises(SchemaValidationError):
        validate_recognition_output(_classified([_candidate(CHURN, evidence_spans=(123,))]))  # type: ignore[arg-type]


# ── the fail-open constructor ─────────────────────────────────────────────────────────────────────
def test_unscoped_result_defaults_to_unscoped():
    result = unscoped_result("no target", model_id="m", prompt_version="1")
    assert result.status is RecognitionStatus.UNSCOPED
    assert result.candidates == ()
    assert result.ambiguity_note == "no target"
    assert result.taxonomy_version == TAXONOMY_VERSION
    assert result.recognizer_model_id == "m"
    assert result.prompt_version == "1"


def test_unscoped_result_technical_flag():
    result = unscoped_result("provider refused", model_id="m", prompt_version="1", technical=True)
    assert result.status is RecognitionStatus.TECHNICAL_FAILURE
    assert result.candidates == ()


def test_recognition_result_dimension_defaults():
    # A recognition carries no confirmed dimensions by default (they are optional, human-confirmed).
    result = unscoped_result("x", model_id="m", prompt_version="1")
    assert result.modelling_contexts == ()
    assert result.target_entity is None
    assert result.warnings == ()


# ── per-dimension normalization (non-fatal: never fails the whole recognition) ────────────────────
def test_normalize_dimensions_valid_values_preserved():
    contexts, entity, warnings = normalize_dimensions(
        {"modelling_contexts": ["ifrs9", "frtb"], "target_entity": "customer"})
    assert contexts == ("ifrs9", "frtb")
    assert entity == "customer"
    assert warnings == ()


def test_normalize_dimensions_drops_unknown_context_keeps_valid():
    contexts, entity, warnings = normalize_dimensions(
        {"modelling_contexts": ["ifrs9", "invented"], "target_entity": "customer"})
    assert contexts == ("ifrs9",)                      # unknown dropped, valid kept
    assert entity == "customer"                        # the OTHER dimension is untouched
    assert UNKNOWN_MODELLING_CONTEXT in warnings


def test_normalize_dimensions_clears_unknown_entity_keeps_context():
    contexts, entity, warnings = normalize_dimensions(
        {"modelling_contexts": ["ifrs9"], "target_entity": "not_an_entity"})
    assert contexts == ("ifrs9",)                      # the OTHER dimension is untouched
    assert entity is None                              # unknown entity cleared
    assert UNKNOWN_TARGET_ENTITY in warnings


def test_normalize_dimensions_absent_is_empty_no_warnings():
    # Backward-compat: a body with NO dimensions normalizes to empties with no warnings.
    contexts, entity, warnings = normalize_dimensions({"status": "classified"})
    assert contexts == ()
    assert entity is None
    assert warnings == ()


def test_normalize_dimensions_null_entity_is_not_a_warning():
    # An explicit null target_entity (the model declined to pick a grain) is clean, not a warning.
    contexts, entity, warnings = normalize_dimensions(
        {"modelling_contexts": [], "target_entity": None})
    assert contexts == ()
    assert entity is None
    assert warnings == ()


def test_validate_output_is_structural_for_core_only_ignores_dimensions():
    # An invalid OPTIONAL dimension NEVER fails the core structural validation (dimensions are
    # validated per-dimension, non-fatally, elsewhere) — but an invalid PRIMARY still fails hard.
    body = _classified([_candidate(CHURN, relationship="primary")])
    body["modelling_contexts"] = ["invented"]
    body["target_entity"] = "not_an_entity"
    assert validate_recognition_output(body) is None   # core still valid despite bad dimensions

    bad_primary = _classified([_candidate("customer.not_a_real_leaf")])
    bad_primary["modelling_contexts"] = ["ifrs9"]      # a valid dim does not rescue an invalid primary
    with pytest.raises(SchemaValidationError):
        validate_recognition_output(bad_primary)


# ── Task 3 (2026-08-15): every rejection names a CLOSED, VALUE-FREE code ─────────────────────────
#
# The codes are not decoration. Since the recognizer hands this validator to the audited seam as
# `validate_semantics`, each one is (a) re-prompted to the provider as the repair complaint, (b)
# persisted verbatim into `llm_call.repair_attempts`, and (c) folded into the human-visible
# `ambiguity_note` when repair never succeeds. A code that failed the seam's shape rule would be
# silently scrubbed to a dull constant and the model would be re-asked with no information at all.

#: One body per raise site, and the code it must attest. `_LEAKED` is the model-chosen text that
#: must not appear in the exception message — the message is what reaches the human-visible note.
_LEAKED = "zz-recognition-leak-sentinel-zz"

_REJECTIONS: list[tuple[str, dict[str, Any]]] = [
    (MALFORMED_CANDIDATE, _classified([_LEAKED])),                                    # type: ignore[list-item]
    (UNKNOWN_USE_CASE_ID, _classified([_candidate(_LEAKED)])),
    # In the registry, but a domain PARENT — a primary that would scope to zero recipes.
    (PRIMARY_NOT_A_LEAF_OBJECTIVE, _classified([_candidate("financial_crime")])),
    (INVALID_RELATIONSHIP, _classified([_candidate(CHURN, relationship=_LEAKED)])),
    (INVALID_CONFIDENCE, _classified([_candidate(CHURN, confidence=_LEAKED)])),
    (MALFORMED_EVIDENCE_SPANS, _classified([_candidate(CHURN, evidence_spans=("   ",))])),
    (INVALID_STATUS, {"status": _LEAKED, "candidates": []}),
    (MALFORMED_CANDIDATE_LIST, {"status": "unscoped", "candidates": _LEAKED}),
    (DUPLICATE_CANDIDATE, _classified([_candidate(CHURN, relationship="primary"),
                                       _candidate(CHURN, relationship="secondary")])),
    (MULTIPLE_PRIMARY_CANDIDATES, _classified([_candidate(CHURN, relationship="primary"),
                                               _candidate(DEPOSIT, relationship="primary")])),
    (TOO_MANY_SECONDARY_CANDIDATES, _classified([
        _candidate(CHURN, relationship="secondary"),
        _candidate(DEPOSIT, relationship="secondary"),
        _candidate(PRIMACY, relationship="secondary")])),
    (STATUS_REQUIRES_A_CANDIDATE, {"status": "ambiguous", "candidates": []}),
    (CLASSIFIED_REQUIRES_ONE_PRIMARY, _classified([
        _candidate(CHURN, relationship="secondary")])),
]


@pytest.mark.parametrize(("code", "body"), _REJECTIONS, ids=[c for c, _ in _REJECTIONS])
def test_each_rejection_attests_its_closed_code(code: str, body: dict[str, Any]) -> None:
    with pytest.raises(AttestedSchemaValidationError) as excinfo:
        validate_recognition_output(body)
    assert excinfo.value.llm_safe_reason == code
    assert code in RECOGNITION_FAILURE_CODES


@pytest.mark.parametrize(("code", "body"), _REJECTIONS, ids=[c for c, _ in _REJECTIONS])
def test_no_rejection_interpolates_the_offending_value(code: str, body: dict[str, Any]) -> None:
    """The message names the rule and, at most, a candidate POSITION. It never quotes what the model
    said — the value has not been through the §9.4 egress guard, and this text is persisted and
    displayed."""
    with pytest.raises(AttestedSchemaValidationError) as excinfo:
        validate_recognition_output(body)
    rendered = f"{excinfo.value} {excinfo.value.llm_safe_reason}"
    assert _LEAKED not in rendered
    assert "financial_crime" not in rendered      # a real id the model proposed is model text too
    assert CHURN not in rendered


def test_every_failure_code_survives_the_seam_shape_rule() -> None:
    """The whole vocabulary at once. `enrich_llm._SEMANTIC_REPAIR_CODE` scrubs anything that is not
    a closed token back to one value-free constant, so a code that failed this would cost a repair
    turn its only information — and the failure would be a log line, not a test."""
    for code in RECOGNITION_FAILURE_CODES:
        assert _SEMANTIC_REPAIR_CODE.fullmatch(code), code
        assert _semantic_repair_code(
            AttestedSchemaValidationError("m", llm_safe_reason=code)) == code


def test_the_declared_vocabulary_is_exactly_what_the_validator_can_raise() -> None:
    """Drift both ways: a code declared but unreachable is a promise nothing keeps, and a raise site
    that invents a code outside the set escapes Task 4's closed drop-reason contract."""
    raised = {code for code, _ in _REJECTIONS}
    unreached = RECOGNITION_FAILURE_CODES - raised
    # TOO_MANY_CANDIDATES is DOMINATED, not missed: with `relationship` closed to two values, the
    # per-relationship caps (1 primary + 2 secondary) already forbid a fourth candidate, and an
    # unparseable relationship is a candidate-LOCAL failure that raises before any aggregate runs.
    # The next test pins that domination; the constant stays because Task 4's partition, which
    # counts over the RAW list, does reach it.
    assert unreached == {TOO_MANY_CANDIDATES}


def test_the_total_cap_is_dominated_by_the_per_relationship_caps() -> None:
    with pytest.raises(AttestedSchemaValidationError) as excinfo:
        validate_recognition_output(_classified([
            _candidate(CHURN, relationship="primary"),
            _candidate(DEPOSIT, relationship="secondary"),
            _candidate(PRIMACY, relationship="secondary"),
            _candidate("customer.cross_sell", relationship="secondary")]))
    assert excinfo.value.llm_safe_reason == TOO_MANY_SECONDARY_CANDIDATES
