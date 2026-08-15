"""Phase-1A Task 1 — recognition contracts + closed-taxonomy validator.

Exercises ``validate_recognition_output`` (the raw-dict ``validate_output`` callback
``drive_structured_call`` will invoke in Task 2) against the closed use-case taxonomy, plus the
``unscoped_result`` fail-open constructor. See
``docs/superpowers/plans/2026-07-09-phase1a-shadow-recognizer.md`` Task 1.
"""
from __future__ import annotations

import json
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
    RECOGNITION_AGGREGATE_CODES,
    RECOGNITION_CANDIDATE_LOCAL_CODES,
    RECOGNITION_FAILURE_CODES,
    STATUS_REQUIRES_A_CANDIDATE,
    TAXONOMY_VERSION,
    TOO_MANY_CANDIDATES,
    TOO_MANY_SECONDARY_CANDIDATES,
    UNKNOWN_MODELLING_CONTEXT,
    UNKNOWN_TARGET_ENTITY,
    UNKNOWN_USE_CASE_ID,
    CandidateDrop,
    RecognitionStatus,
    normalize_dimensions,
    partition_candidates,
    status_after_partition,
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


# ── Task 4 (2026-08-15): the STRICT partial partition ───────────────────────────────────────────
#
# One case per bullet of the rule the review FROZE. The two halves are not symmetrical and must not
# be made so: a candidate-local defect is one candidate being wrong, and dropping it loses nothing
# anyone said; an aggregate defect is the SET being wrong, and "fixing" it by dropping a candidate
# would be the platform quietly picking which of the model's two primaries it preferred.

def _kept_ids(kept) -> list[str]:
    return [c["use_case_id"] for c in kept]


# --- candidate-LOCAL defects MAY be dropped ------------------------------------------------------

@pytest.mark.parametrize(("code", "sibling"), [
    (UNKNOWN_USE_CASE_ID, _candidate("customer.not_a_real_leaf", relationship="secondary")),
    (INVALID_CONFIDENCE, _candidate(DEPOSIT, relationship="secondary", confidence="very-high")),
    (INVALID_RELATIONSHIP, _candidate(DEPOSIT, relationship="tertiary")),
    (MALFORMED_EVIDENCE_SPANS, _candidate(DEPOSIT, relationship="secondary",
                                          evidence_spans=("   ",))),
    (MALFORMED_CANDIDATE, "not an object at all"),
], ids=[UNKNOWN_USE_CASE_ID, INVALID_CONFIDENCE, INVALID_RELATIONSHIP, MALFORMED_EVIDENCE_SPANS,
        MALFORMED_CANDIDATE])
def test_a_candidate_local_defect_drops_only_that_candidate(code: str, sibling: Any) -> None:
    kept, dropped = partition_candidates(
        _classified([_candidate(CHURN, relationship="primary"), sibling]))

    assert _kept_ids(kept) == [CHURN]                     # the correct answer survives its sibling
    assert dropped == (CandidateDrop(index=1, reason_code=code),)
    assert code in RECOGNITION_CANDIDATE_LOCAL_CODES


def test_a_non_leaf_primary_is_a_candidate_local_drop() -> None:
    """`financial_crime` is IN the taxonomy but is a domain parent — a primary that would scope to
    zero recipes. Dropped on its own account; the valid secondary survives it."""
    kept, dropped = partition_candidates(_classified([
        _candidate("financial_crime", relationship="primary"),
        _candidate(DEPOSIT, relationship="secondary")]))

    assert _kept_ids(kept) == [DEPOSIT]
    assert dropped == (CandidateDrop(index=0, reason_code=PRIMARY_NOT_A_LEAF_OBJECTIVE),)


# --- AGGREGATE defects REFUSE the whole result ---------------------------------------------------

@pytest.mark.parametrize(("code", "body"), [
    (DUPLICATE_CANDIDATE, _classified([_candidate(CHURN, relationship="primary"),
                                       _candidate(CHURN, relationship="secondary")])),
    (TOO_MANY_CANDIDATES, _classified([
        _candidate(CHURN, relationship="primary"),
        _candidate(DEPOSIT, relationship="secondary"),
        _candidate(PRIMACY, relationship="secondary"),
        _candidate("customer.cross_sell", relationship="secondary")])),
    (MULTIPLE_PRIMARY_CANDIDATES, _classified([_candidate(CHURN, relationship="primary"),
                                               _candidate(DEPOSIT, relationship="primary")])),
    (TOO_MANY_SECONDARY_CANDIDATES, _classified([
        _candidate(CHURN, relationship="secondary"),
        _candidate(DEPOSIT, relationship="secondary"),
        _candidate(PRIMACY, relationship="secondary")])),
    (STATUS_REQUIRES_A_CANDIDATE, _classified([])),
    (INVALID_STATUS, {"status": "maybe", "candidates": []}),
    (MALFORMED_CANDIDATE_LIST, {"status": "unscoped", "candidates": "nope"}),
], ids=[DUPLICATE_CANDIDATE, TOO_MANY_CANDIDATES, MULTIPLE_PRIMARY_CANDIDATES,
        TOO_MANY_SECONDARY_CANDIDATES, STATUS_REQUIRES_A_CANDIDATE, INVALID_STATUS,
        MALFORMED_CANDIDATE_LIST])
def test_an_aggregate_defect_refuses_the_whole_result(code: str, body: dict[str, Any]) -> None:
    kept, dropped = partition_candidates(body)

    assert kept == ()
    # ONE drop, naming the RESULT — not the candidates. Blaming an aggregate defect on a particular
    # candidate would name one that may be perfectly well formed.
    assert dropped == (CandidateDrop(index=None, reason_code=code),)
    assert dropped[0].is_whole_result
    assert code in RECOGNITION_AGGREGATE_CODES


def test_the_live_incident_is_refused_by_the_aggregate_rule_not_rescued_by_partition() -> None:
    """THE case this whole rule exists to get right. The incident's body has one perfect candidate
    and one placeholder — and dropping the placeholder as a candidate-local `UNKNOWN_USE_CASE_ID`
    would leave exactly one primary and make the cap violation evaporate.

    It must not. The model marked TWO candidates primary; which one it meant is not ours to decide.
    Task 3's repair is what rescues this body; the partition refuses it."""
    kept, dropped = partition_candidates({
        "status": "classified",
        "candidates": [
            {"use_case_id": CHURN, "relationship": "primary", "confidence": "high",
             "evidence_spans": ["predict churn in the next 90 days"], "rationale": ""},
            {"use_case_id": "x", "relationship": "primary", "confidence": "high",
             "evidence_spans": ["x"], "rationale": "placeholder"}],
        "modelling_contexts": []})

    assert kept == ()
    assert dropped == (CandidateDrop(index=None, reason_code=MULTIPLE_PRIMARY_CANDIDATES),)
    assert UNKNOWN_USE_CASE_ID not in {d.reason_code for d in dropped}


def test_the_aggregate_rules_run_before_any_drop() -> None:
    """The ordering, stated as its own fact rather than inferred from the incident: a body whose
    cap violation involves a candidate that is ALSO locally invalid is still refused. Reverse the
    order and this returns one kept candidate."""
    kept, dropped = partition_candidates(_classified([
        _candidate(CHURN, relationship="primary"),
        _candidate(DEPOSIT, relationship="primary", confidence="very-high")]))
    assert (kept, dropped) == ((), (CandidateDrop(index=None,
                                                  reason_code=MULTIPLE_PRIMARY_CANDIDATES),))


def test_a_malformed_sibling_cannot_suppress_a_cap_check() -> None:
    """The aggregate counts skip junk rather than crashing on it — otherwise a non-object sibling
    would be a way to smuggle a duplicate or a second primary past the caps."""
    kept, dropped = partition_candidates(_classified([
        _candidate(CHURN, relationship="primary"), 42, _candidate(DEPOSIT, relationship="primary")]))
    assert (kept, dropped) == ((), (CandidateDrop(index=None,
                                                  reason_code=MULTIPLE_PRIMARY_CANDIDATES),))


# --- the post-partition status, and the promotion that must never happen -------------------------

def test_classified_after_partition_still_requires_exactly_one_primary() -> None:
    kept, _dropped = partition_candidates(_classified([
        _candidate(CHURN, relationship="primary"),
        _candidate(DEPOSIT, relationship="secondary", evidence_spans=("",))]))
    assert status_after_partition("classified", kept) == "classified"

    survivors_without_a_primary = [_candidate(DEPOSIT, relationship="secondary")]
    assert status_after_partition("classified", survivors_without_a_primary) == "ambiguous"
    # Other declared statuses are returned untouched — the partition stops losses, it does not
    # start inventing.
    assert status_after_partition("ambiguous", survivors_without_a_primary) == "ambiguous"
    assert status_after_partition("unscoped", survivors_without_a_primary) == "unscoped"


def test_the_only_primary_being_invalid_does_not_promote_a_secondary() -> None:
    """The tempting wrong fix, forbidden. The surviving candidate keeps the relationship the MODEL
    gave it; the result loses its `classified` claim instead. `scope_from_recognition` reads a
    primary-less result as unscoped, so nothing narrows on a guess."""
    body = _classified([_candidate("customer.not_a_real_leaf", relationship="primary"),
                        _candidate(DEPOSIT, relationship="secondary")])
    kept, dropped = partition_candidates(body)

    assert _kept_ids(kept) == [DEPOSIT]
    assert [c["relationship"] for c in kept] == ["secondary"]        # NOT promoted
    assert dropped == (CandidateDrop(index=0, reason_code=UNKNOWN_USE_CASE_ID),)
    assert status_after_partition("classified", kept) == "ambiguous"


def test_nothing_valid_survives_is_unchanged() -> None:
    """Every candidate individually invalid: nothing is kept, every loss is named, and the caller's
    fail-open technical failure is what happens next (asserted end-to-end in test_recognizer.py)."""
    kept, dropped = partition_candidates(_classified([
        _candidate("customer.not_a_real_leaf", relationship="primary"),
        _candidate(DEPOSIT, relationship="secondary", confidence="very-high")]))

    assert kept == ()
    assert dropped == (CandidateDrop(index=0, reason_code=UNKNOWN_USE_CASE_ID),
                       CandidateDrop(index=1, reason_code=INVALID_CONFIDENCE))
    assert not any(d.is_whole_result for d in dropped)   # NOT a refusal — each candidate failed


# --- every drop carries a closed, value-free reason code ----------------------------------------

def test_every_drop_carries_a_closed_value_free_reason_code() -> None:
    """Sweep every body this module's tests know how to break, and hold each drop code to the same
    rule the repair complaints obey — because Task 5 puts these codes in an API payload and a UI
    string, where a leaked model value would have no further scrubbing between it and a screen."""
    bodies: list[Any] = [b for _c, b in _REJECTIONS]
    bodies += [_classified([_candidate(CHURN, relationship="primary"), s])
               for s in (_candidate("customer.not_a_real_leaf", relationship="secondary"),
                         _candidate(DEPOSIT, relationship="secondary", confidence="very-high"),
                         "not an object at all", 42)]
    seen: set[str] = set()
    for body in bodies:
        _kept, dropped = partition_candidates(body)
        for drop in dropped:
            assert drop.reason_code in RECOGNITION_FAILURE_CODES
            assert _SEMANTIC_REPAIR_CODE.fullmatch(drop.reason_code)
            assert _LEAKED not in drop.reason_code
            seen.add(drop.reason_code)
    # The sweep really did exercise both halves of the rule, rather than one of them twice.
    assert seen & RECOGNITION_AGGREGATE_CODES
    assert seen & RECOGNITION_CANDIDATE_LOCAL_CODES


def test_the_two_code_sets_partition_the_vocabulary() -> None:
    """No code may be BOTH droppable and refusing, and none may be neither by accident.
    `CLASSIFIED_REQUIRES_ONE_PRIMARY` is the one deliberate exclusion: it is not a drop reason at
    all — the status is downgraded instead, and nothing is discarded for it."""
    assert not (RECOGNITION_AGGREGATE_CODES & RECOGNITION_CANDIDATE_LOCAL_CODES)
    assert (RECOGNITION_FAILURE_CODES
            - RECOGNITION_AGGREGATE_CODES
            - RECOGNITION_CANDIDATE_LOCAL_CODES) == {CLASSIFIED_REQUIRES_ONE_PRIMARY}


def test_partition_never_raises_and_never_mutates_its_input() -> None:
    """Fail-open, and evidence-preserving: the body it is handed is the one the seam exposed and the
    audit row stored. A kept candidate is the model's own object, unmodified."""
    body = _classified([_candidate(CHURN, relationship="primary"),
                        _candidate(DEPOSIT, relationship="secondary", evidence_spans=("",))])
    before = json.dumps(body, sort_keys=True)
    kept, _dropped = partition_candidates(body)
    assert json.dumps(body, sort_keys=True) == before
    assert kept[0] is body["candidates"][0]

    for junk in (None, [], "string", {"status": None}, {"candidates": {}},
                 {"status": "classified", "candidates": [None]}):
        assert isinstance(partition_candidates(junk if isinstance(junk, dict) else {}), tuple)
