"""Tests for the fail-closed free-text sanitizer (FTR adapter Task 2, round-4 resolution #2).

The invariant under test: no raw customer sample value ever survives into ``clean``. A recognized
``representative values such as ...`` clause is STRIPPED (safe facets preserved); a sample-clause
INTRODUCER the stripper did not consume flips the field to ``suspected_unhandled`` and blanks it;
PII in the surviving prose is redacted, and a redactor that fails closed blanks the field too.

Marker precision (resolution #2): the ``suspected_unhandled`` detector keys on introducer PHRASES
(``e.g.``, ``such as``, ``for example``, ``examples include``, ``representative values``,
``sample values/profile``) — NEVER bare words. ``"sample population size"`` and
``"a representative office"`` are the mandated negative controls and must stay ``state == "none"``.

Descriptions are FTR-shaped but inline — no ``~/Downloads``.
"""
from __future__ import annotations

import pytest

import featuregen.intake.redaction as redaction_module
from featuregen.intake.redaction import RedactionResult
from featuregen.overlay.upload.sanitize import (
    SANITIZER_VERSION,
    DefinitionSanitize,
    redact_text,
    sanitize_definition,
)

# ── Labeled corpus ────────────────────────────────────────────────────────────────────────────────

# RECOGNIZED clause (FTR shape): token + uniform-length all-digit values → identifier facets.
_ACCOUNT_DESC = (
    "Customer Account Number links the financial transaction record to the customer account on "
    "which the posting occurred. The sample profile is NUMERIC, with representative values such as "
    "3708484836801; 3708446902413; 3708454004701, which supports interpretation of the field."
)
_ACCOUNT_VALUES = ("3708484836801", "3708446902413", "3708454004701")

# RECOGNIZED clause, decimal values → decimal/amount facets (no "sample profile is" token).
_AMOUNT_DESC = (
    "Posting amount is the monetary value of the ledger entry, with representative values such as "
    "1250.00; 9.99, which supports interpretation of the field."
)

# RECOGNIZED clause, time-of-day values → time/time facets.
_TIME_DESC = (
    "Transaction Time captures the timing dimension of the record. The sample profile is "
    "NUMERIC_SPECIAL, with representative values such as 15:07:08; 10:01:01; 11:00:56, which "
    "supports interpretation of the field."
)

# RECOGNIZED clause, alphanumeric codes → text/text facets.
_CODE_DESC = (
    "Transaction Identifier uniquely traces the financial transaction. The sample profile is "
    "ALPHA_NUMERIC, with representative values such as EI0300357; EI0046562; EI0061842, which "
    "supports interpretation of the field."
)

# PLAIN definition: business prose only — no clause, no introducer, no PII.
_PLAIN_DESC = (
    "Booking date is the business date on which the ledger entry was recorded in the core "
    "banking system."
)

# UNHANDLED introducers: a list of examples the stripper does not recognize.
_EXAMPLES_INCLUDE_DESC = "Total exposure across counterparties; examples include Acme and Beta."
_EG_DESC = "Counterparty short code, e.g. AB-01."

# NEGATIVE CONTROLS (resolution #2): bare words `sample` / `representative` without an introducer
# phrase must NOT trigger.
_SAMPLE_POPULATION_DESC = "Count of accounts in the sample population size used for QA checks."
_REPRESENTATIVE_OFFICE_DESC = "Flag set when the branch is a representative office in the region."

# PII-only prose: the deterministic redactor scrubs the token, the rest of the prose survives.
_PII_DESC = "Reconciliation contact mailbox; escalations go to ops@bank.example when unresolved."

# RECOGNIZED clause AND PII in the surviving prose → both removals counted.
_CLAUSE_PLUS_PII_DESC = (
    "Queries go to ops@bank.example for this field. The sample profile is NUMERIC, with "
    "representative values such as 111; 222; 333, which supports interpretation of the field."
)

# MULTIPLE clauses: the stripper excises only the FIRST recognized clause; the survivor's
# introducer must flip the whole field to suspected_unhandled (fail closed — no leak).
_MULTI_CLAUSE_DESC = (
    "The sample profile is NUMERIC, with representative values such as 111; 222, which supports "
    "interpretation of the field. Legacy systems show this differently, e.g. AB-1."
)


# ── sanitize_definition: recognized clauses ───────────────────────────────────────────────────────


def test_recognized_clause_stripped_with_facets():
    result = sanitize_definition(_ACCOUNT_DESC)
    assert result.state == "stripped"
    assert result.logical_representation == "numeric_string"
    assert result.semantic_type == "identifier"
    for value in _ACCOUNT_VALUES:
        assert value not in result.clean
    assert "Customer Account Number" in result.clean  # business prose survives
    assert result.removed == 1  # the stripped clause; no PII spans
    assert result.sanitizer_version == SANITIZER_VERSION
    assert result.redaction_version is not None


@pytest.mark.parametrize(
    ("desc", "raw_values", "logical", "semantic"),
    [
        (_AMOUNT_DESC, ("1250.00", "9.99"), "decimal", "amount"),
        (_TIME_DESC, ("15:07:08", "10:01:01", "11:00:56"), "time", "time"),
        (_CODE_DESC, ("EI0300357", "EI0046562", "EI0061842"), "text", "text"),
    ],
)
def test_decimal_time_code_samples_stripped(desc, raw_values, logical, semantic):
    result = sanitize_definition(desc)
    assert result.state == "stripped"
    assert result.logical_representation == logical
    assert result.semantic_type == semantic
    for value in raw_values:
        assert value not in result.clean


# ── sanitize_definition: plain prose + negative controls ─────────────────────────────────────────


def test_plain_definition_unchanged():
    result = sanitize_definition(_PLAIN_DESC)
    assert result.state == "none"
    assert result.clean == _PLAIN_DESC
    assert result.removed == 0
    assert result.logical_representation == ""
    assert result.semantic_type == ""


@pytest.mark.parametrize("desc", [_SAMPLE_POPULATION_DESC, _REPRESENTATIVE_OFFICE_DESC])
def test_negative_controls_bare_words_do_not_trigger(desc):
    """Resolution #2: bare `sample` / `representative` without an introducer phrase stay clean."""
    result = sanitize_definition(desc)
    assert result.state == "none"
    assert result.clean == desc


# ── sanitize_definition: suspected_unhandled (fail closed) ────────────────────────────────────────


@pytest.mark.parametrize("desc", [_EXAMPLES_INCLUDE_DESC, _EG_DESC])
def test_unhandled_introducer_blanks_field(desc):
    result = sanitize_definition(desc)
    assert result.state == "suspected_unhandled"
    assert result.clean == ""
    assert result.removed == 1
    assert result.redaction_version is None  # nothing safe to redact — the field was blanked


@pytest.mark.parametrize(
    "desc",
    [
        "Counterparty codes E.G. AB-01 and CD-02.",  # upper-case e.g.
        "Product tiers, For Example: gold and platinum.",  # capitalised, punctuation variant
        "Values observed, e.g., 12:30 and 13:45.",  # comma-wrapped e.g.
        "Categories Such As retail and corporate.",  # capitalised such as
        "Statuses (examples include OPEN; CLOSED).",  # parenthesised list
        'Published in the "sample profile" appendix each month.',  # quoted introducer — still closed
    ],
)
def test_introducer_case_and_punctuation_variants_blank(desc):
    result = sanitize_definition(desc)
    assert result.state == "suspected_unhandled"
    assert result.clean == ""


def test_multiple_clauses_residual_introducer_fails_closed():
    """One recognized clause is excised, but a second (unrecognized) clause survives the strip —
    the residual introducer must blank the field rather than leak `AB-1`."""
    result = sanitize_definition(_MULTI_CLAUSE_DESC)
    assert result.state == "suspected_unhandled"
    assert result.clean == ""
    assert "111" not in result.clean and "AB-1" not in result.clean


# ── sanitize_definition: PII redaction ────────────────────────────────────────────────────────────


def test_pii_tokens_redacted_from_prose():
    result = sanitize_definition(_PII_DESC)
    assert result.state == "none"
    assert "ops@bank.example" not in result.clean
    assert "[REDACTED:EMAIL]" in result.clean
    assert "Reconciliation contact mailbox" in result.clean
    assert result.removed == 1


def test_clause_and_pii_both_counted():
    result = sanitize_definition(_CLAUSE_PLUS_PII_DESC)
    assert result.state == "stripped"
    assert "ops@bank.example" not in result.clean
    assert "111" not in result.clean
    assert result.removed == 2  # 1 stripped clause + 1 redacted span


class _FailClosedRedactor:
    def redact(self, raw_intent, raw_input_classification):
        return RedactionResult(None, "stub-redactor@1", (), "fail_into_clarification")


def test_redactor_fail_closed_blanks_field(monkeypatch):
    monkeypatch.setattr(redaction_module, "_INTENT_REDACTOR", _FailClosedRedactor())
    result = sanitize_definition(_PLAIN_DESC)
    assert result.clean == ""
    assert result.state == "none"
    assert result.removed == 1  # the blanked field counts
    assert result.redaction_version == "stub-redactor@1"


# ── sanitize_definition: empty input ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", ["", None])
def test_empty_or_none_definition(text):
    result = sanitize_definition(text)
    assert result == DefinitionSanitize("", "none", "", "", 0, SANITIZER_VERSION, None)


# ── redact_text (non-definition free-text fields) ─────────────────────────────────────────────────


def test_redact_text_clean_passthrough():
    clean, version = redact_text("Customer Segment")
    assert clean == "Customer Segment"
    assert version is not None


def test_redact_text_scrubs_pii():
    clean, version = redact_text("owner ops@bank.example")
    assert "ops@bank.example" not in clean
    assert "[REDACTED:EMAIL]" in clean
    assert version is not None


def test_redact_text_fail_closed(monkeypatch):
    monkeypatch.setattr(redaction_module, "_INTENT_REDACTOR", _FailClosedRedactor())
    clean, version = redact_text("any text at all")
    assert clean == ""
    assert version == "stub-redactor@1"


def test_redact_text_empty():
    assert redact_text("") == ("", None)
