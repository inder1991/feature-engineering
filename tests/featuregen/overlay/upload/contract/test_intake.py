"""Phase 1 — intent intake: mandatory hypothesis + text redaction."""
import pytest

from featuregen.overlay.upload.contract.intake import (
    Intent,
    IntentValidationError,
    submit_intent,
)


def test_blank_hypothesis_is_denied():
    # No hypothesis -> command-validation denial (resubmit), NOT a silent pass.
    with pytest.raises(IntentValidationError):
        submit_intent(hypothesis="", definition="avg balance over 90d", actor="ds1")
    with pytest.raises(IntentValidationError):
        submit_intent(hypothesis="   ", actor="ds1")


def test_hypothesis_only_mode():
    i = submit_intent(hypothesis="customers churn when their balance drops", actor="ds1")
    assert isinstance(i, Intent)
    assert i.intake_mode == "hypothesis"
    assert i.redacted_hypothesis                # populated
    assert i.redacted_definition == ""


def test_definition_mode_and_immutable_fields():
    i = submit_intent(hypothesis="churn from balance drop",
                      definition="90-day average ledger balance per customer", actor="ds1")
    assert i.intake_mode == "definition"
    assert i.redacted_definition


def test_pii_in_text_is_redacted_before_it_leaves_intake():
    i = submit_intent(hypothesis="churn signal", actor="ds1",
                      definition="avg balance for SSN 123-45-6789 and joe@bank.com over 90d")
    # the raw PII tokens must NOT survive into the redacted text that flows downstream
    assert "123-45-6789" not in i.redacted_definition
    assert "joe@bank.com" not in i.redacted_definition
    assert "REDACTED" in i.redacted_definition


def test_unredactable_pii_denies():
    # A classification the redactor cannot prove safe fails closed as a denial (resubmit).
    class _Unsafe:
        def redact(self, raw_intent, raw_input_classification):
            from featuregen.intake.redaction import RedactionResult
            return RedactionResult(None, "v", (), "fail_into_clarification")

    with pytest.raises(IntentValidationError):
        submit_intent(hypothesis="churn", definition="x", actor="ds1", redactor=_Unsafe())
