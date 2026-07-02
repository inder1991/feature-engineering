from dataclasses import dataclass as _dc

import pytest

from featuregen.intake.redaction import (
    INPUT_KEY_CATALOG,
    INPUT_KEY_CLASSIFICATION,
    INPUT_KEY_INTENT,
    INPUT_KEY_REDACTION,
    INPUT_KEY_REDACTION_VERSION,
    DefaultIntentRedactor,
    EgressViolation,
    RedactionResult,
    assert_llm_safe,
    build_llm_inputs,
)


def test_clean_intent_passes_through_stamped():
    r = DefaultIntentRedactor().redact(
        "90-day rolling count of declined card authorizations per customer", "clean"
    )
    assert r.disposition == "ok"
    assert r.text == "90-day rolling count of declined card authorizations per customer"
    assert r.redacted_spans == ()
    assert r.redaction_version == "default-redactor@1"


def test_contains_pii_scrubs_located_spans():
    r = DefaultIntentRedactor().redact(
        "count logins for jane.doe@bank.example and SSN 123-45-6789", "contains_pii"
    )
    assert r.disposition == "ok"
    assert r.text is not None
    # the located PII is gone; placeholders are digit/at-free so a residual scan is clean
    assert "jane.doe@bank.example" not in r.text
    assert "123-45-6789" not in r.text
    assert "[REDACTED:EMAIL]" in r.text and "[REDACTED:SSN]" in r.text
    # spans record TYPE + position only (never the scrubbed value)
    kinds = {s["type"] for s in r.redacted_spans}
    assert kinds == {"EMAIL", "SSN"}
    assert all("start" in s and "end" in s and "value" not in s for s in r.redacted_spans)


def test_unscanned_fails_closed_no_text():
    r = DefaultIntentRedactor().redact("anything at all", "unscanned")
    assert r.disposition == "fail_into_clarification"
    assert r.text is None


def test_contains_pii_but_unlocatable_fails_closed():
    # SP-0 says contains_pii, but the default redactor finds no locatable span it can scrub:
    # it cannot prove the text is safe, so it fails closed rather than emit an unsafe payload.
    r = DefaultIntentRedactor().redact("the applicant's maiden name is on file", "contains_pii")
    assert r.disposition == "fail_into_clarification"
    assert r.text is None


def test_build_llm_inputs_assembles_reserved_keys():
    red = RedactionResult(
        text="count declined auths per customer",
        redaction_version="default-redactor@1",
        redacted_spans=(),
        disposition="ok",
    )
    inputs = build_llm_inputs(
        red,
        catalog_metadata={"objects": ["card_authorizations"], "columns": {"auth_result": "text"}},
        raw_input_classification="clean",
    )
    assert inputs[INPUT_KEY_INTENT] == "count declined auths per customer"
    assert inputs[INPUT_KEY_CATALOG]["objects"] == ["card_authorizations"]
    assert inputs[INPUT_KEY_CLASSIFICATION] == "clean"
    assert inputs[INPUT_KEY_REDACTION_VERSION] == "default-redactor@1"
    assert inputs[INPUT_KEY_REDACTION] == {"redacted_spans": []}


def test_build_llm_inputs_refuses_failed_redaction():
    red = RedactionResult(text=None, redaction_version="default-redactor@1",
                          redacted_spans=(), disposition="fail_into_clarification")
    with pytest.raises(EgressViolation):
        build_llm_inputs(red, catalog_metadata={}, raw_input_classification="unscanned")


def test_intent_redactor_seam_registers_and_fails_closed_when_unset():
    # R10 module-global DI seam: current_ fails closed until register_ is called; then round-trips.
    from featuregen.intake import redaction as _rmod
    from featuregen.intake.redaction import current_intent_redactor, register_intent_redactor

    _rmod._INTENT_REDACTOR = None  # ensure unset for a deterministic fail-closed assertion
    with pytest.raises(RuntimeError):
        current_intent_redactor()
    register_intent_redactor(DefaultIntentRedactor())
    assert isinstance(current_intent_redactor(), DefaultIntentRedactor)


@_dc(frozen=True)
class _Req:  # a duck-typed stand-in for LLMRequest (Task 3.3) — the guard reads .inputs only
    inputs: dict


def _safe_inputs():
    return {
        INPUT_KEY_INTENT: "count declined auths per customer",
        INPUT_KEY_CATALOG: {"objects": ["card_authorizations"], "columns": {"auth_result": "text"}},
        INPUT_KEY_CLASSIFICATION: "clean",
        INPUT_KEY_REDACTION_VERSION: "default-redactor@1",
        INPUT_KEY_REDACTION: {"redacted_spans": []},
    }


def test_egress_allows_clean_metadata_only_payload():
    assert_llm_safe(_Req(_safe_inputs()))  # no raise


def test_egress_refuses_unscanned():
    i = _safe_inputs()
    i[INPUT_KEY_CLASSIFICATION] = "unscanned"
    with pytest.raises(EgressViolation):
        assert_llm_safe(_Req(i))


def test_egress_refuses_missing_classification():
    i = _safe_inputs()
    del i[INPUT_KEY_CLASSIFICATION]
    with pytest.raises(EgressViolation):
        assert_llm_safe(_Req(i))


def test_egress_refuses_data_value_keys():
    i = _safe_inputs()
    i["column_values"] = ["D", "A", "R"]  # profiled value set — SP-1/SP-3 territory, never to the LLM
    with pytest.raises(EgressViolation):
        assert_llm_safe(_Req(i))


def test_egress_refuses_contains_pii_without_redaction_version():
    i = _safe_inputs()
    i[INPUT_KEY_CLASSIFICATION] = "contains_pii"
    del i[INPUT_KEY_REDACTION_VERSION]
    with pytest.raises(EgressViolation):
        assert_llm_safe(_Req(i))


def test_egress_refuses_unredacted_pii_in_content():
    i = _safe_inputs()
    i[INPUT_KEY_INTENT] = "count logins for jane.doe@bank.example"  # slipped past redaction
    with pytest.raises(EgressViolation):
        assert_llm_safe(_Req(i))
