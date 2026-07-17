"""Tests for the fail-closed free-text sanitizer (FTR adapter Task 2, round-4 resolution #2).

The invariant under test: no raw customer sample value ever survives into ``clean``. A recognized
``representative values such as ...`` clause is STRIPPED (safe facets preserved); the POST-strip
residual is then judged by the VALUE-SHAPE RESIDUAL-SUSPICION GATE:

* ``unhandled_marker`` — a known sample-data marker phrase survived the strip (``representative
  values``, ``sample values/profile``, ``observed values/entries``, ``example values``) → the whole
  definition is blanked (``suspected_unhandled``).
* ``suspected_value_list`` — the residual carries >= 2 VALUE-SHAPED tokens (numeric run, time,
  short code, double-quoted literal, all-caps entity run) together with a list separator (``;`` or
  ``,`` ONLY — conjunctive ``and`` is NOT one) or a sample-context word (``values``/``entries``/
  ``codes``/``observed``/``include``) → blanked.

Everything else — concept prose, a single acronym, an ordinary lowercase taxonomy list, even a bare
introducer phrase like ``such as`` — is PRESERVED (the v1 introducer whitelist both leaked
non-whitelisted lists and over-blanked legitimate prose; the shape gate replaces it). PII in the
surviving prose is redacted, and a redactor that fails closed blanks the field too.

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

# RECOGNIZED clause with multi-word entity values: still stripped — no raw value survives.
_CANONICAL_ENTITY_DESC = (
    "Counterparty legal name, with representative values such as ACME FZE; NOQODI, which supports "
    "interpretation of the field."
)

# PLAIN definition: business prose only — no clause, no marker, no PII.
_PLAIN_DESC = (
    "Booking date is the business date on which the ledger entry was recorded in the core "
    "banking system."
)

# MULTIPLE clauses: the stripper excises only the FIRST recognized clause; the second clause's raw
# value list survives the strip and must trip the value-shape gate (fail closed — no leak).
_MULTI_CLAUSE_DESC = (
    "The sample profile is NUMERIC, with representative values such as 111; 222, which supports "
    "interpretation of the field. Legacy systems show these codes differently, e.g. AB-1; CD-2."
)

# POSITIVES (resolution #2): residuals the stripper does NOT handle but that plainly carry sample
# data — a surviving marker phrase, or a value-shaped list. All must blank the whole definition.
_VALUES_WERE_DESC = "Postal region of the account holder; the values were 84848; 90210."
_OBSERVED_ENTRIES_DESC = (
    "Counterparty legal name; observed entries include ARTKOM GLOBAL FZE and NORDIC HOLDINGS AS."
)
_BRANCH_CODES_DESC = "Branch codes: LON01, NYC02, SGP03."
_QUOTED_STATUSES_DESC = 'Statuses are "OPN", "CLS", "PND".'
_TIME_CUTOFFS_DESC = "Cutoffs 15:07:08; 23:59:59 apply."

# NEGATIVES (resolution #2): legitimate concept prose the v1 introducer whitelist over-blanked, plus
# bare-word controls. All must be PRESERVED verbatim.
_SUCH_AS_PROSE_DESC = "Contract attributes such as tenor and rate drive the pricing model."
_REPRESENTATIVE_OFFICE_DESC = "Flag set when the branch is a representative office in the region."
_SAMPLE_POPULATION_DESC = "Count of accounts in the sample population size used for QA checks."
_GDP_DESC = "Macroeconomic input holding the GDP figure for the year."
_TAXONOMY_DESC = "Classified by party, product, account."
_LEGAL_NAME_DESC = "Registered legal name of the counterparty."
_FOR_EXAMPLE_PROSE_DESC = "Product tiers, For Example: gold and platinum."
_SUCH_AS_CATEGORIES_DESC = "Categories Such As retail and corporate."
# Title-case proper nouns are shape-indistinguishable from concept words ("tenor and rate") — the
# gate deliberately lets them through (accepted residual risk of resolution #2).
_EXAMPLES_INCLUDE_DESC = "Total exposure across counterparties; examples include Acme and Beta."
# Numeric-range / year prose joined by conjunctive "and": >= 2 numeric tokens but NO ';'/',' and no
# sample-context word — "and" is NOT a list separator (resolution #2: "semicolons or commas"), so
# legitimate quantitative prose must be PRESERVED.
_NUMERIC_RANGE_DESC = "Threshold applies to exposures between 100 and 500 basis points."
_REPORTING_YEARS_DESC = "Revised for reporting periods 2019 and 2020 under Basel III."
_FISCAL_YEARS_DESC = "Ratios computed for 2021 and 2022 fiscal reporting."
# Possessive apostrophes are NOT quoted-value tokens: the quoted-literal pattern is DOUBLE-quote
# only, so spans between possessive `'` marks never count as values despite the commas.
_POSSESSIVE_LIST_DESC = (
    "the client's ledger, the bank's records, the firm's books, the fund's assets"
)

# THRESHOLD boundary: ONE value-shaped token is below the >= 2 gate — preserved by design (a single
# ambient token is indistinguishable from a prose reference).
_SINGLE_CODE_DESC = "Counterparty short code, e.g. AB-01."

# PII-only prose: the deterministic redactor scrubs the token, the rest of the prose survives.
_PII_DESC = "Reconciliation contact mailbox; escalations go to ops@bank.example when unresolved."

# RECOGNIZED clause AND PII in the surviving prose → both removals counted.
_CLAUSE_PLUS_PII_DESC = (
    "Queries go to ops@bank.example for this field. The sample profile is NUMERIC, with "
    "representative values such as 111; 222; 333, which supports interpretation of the field."
)


# ── version ───────────────────────────────────────────────────────────────────────────────────────


def test_sanitizer_version_bumped_for_value_shape_gate():
    assert SANITIZER_VERSION == "ftr-sanitize-v2"


# ── sanitize_definition: recognized clauses ───────────────────────────────────────────────────────


def test_recognized_clause_stripped_with_facets():
    result = sanitize_definition(_ACCOUNT_DESC)
    assert result.state == "stripped"
    assert result.reason == ""
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


def test_canonical_entity_values_stripped_no_leak():
    """The canonical FTR clause with multi-word entity values is still handled by the stripper —
    no raw value reaches ``clean`` and the surrounding prose is not blanked."""
    result = sanitize_definition(_CANONICAL_ENTITY_DESC)
    assert result.state == "stripped"
    assert result.reason == ""
    assert "ACME" not in result.clean
    assert "NOQODI" not in result.clean
    assert "Counterparty legal name" in result.clean


# ── sanitize_definition: plain prose + preserved concept prose (negative corpus) ─────────────────


def test_plain_definition_unchanged():
    result = sanitize_definition(_PLAIN_DESC)
    assert result.state == "none"
    assert result.reason == ""
    assert result.clean == _PLAIN_DESC
    assert result.removed == 0
    assert result.logical_representation == ""
    assert result.semantic_type == ""


@pytest.mark.parametrize(
    "desc",
    [
        _SUCH_AS_PROSE_DESC,
        _REPRESENTATIVE_OFFICE_DESC,
        _SAMPLE_POPULATION_DESC,
        _GDP_DESC,
        _TAXONOMY_DESC,
        _LEGAL_NAME_DESC,
        _FOR_EXAMPLE_PROSE_DESC,
        _SUCH_AS_CATEGORIES_DESC,
        _EXAMPLES_INCLUDE_DESC,
        _NUMERIC_RANGE_DESC,
        _REPORTING_YEARS_DESC,
        _FISCAL_YEARS_DESC,
        _POSSESSIVE_LIST_DESC,
    ],
)
def test_concept_prose_preserved_not_blanked(desc):
    """Resolution #2 negatives: introducer phrases, bare `sample`/`representative`, a single
    acronym, and lowercase taxonomy lists carry no value-shaped list — they must pass through."""
    result = sanitize_definition(desc)
    assert result.state in {"none", "stripped"}
    assert result.reason == ""
    assert result.clean == desc


def test_single_value_token_below_threshold_preserved():
    """The gate needs >= 2 value-shaped tokens: one ambient code is below the threshold and the
    definition is preserved (deliberate boundary of resolution #2)."""
    result = sanitize_definition(_SINGLE_CODE_DESC)
    assert result.state == "none"
    assert result.reason == ""
    assert result.clean == _SINGLE_CODE_DESC


# ── sanitize_definition: suspected_unhandled (value-shape residual gate, fail closed) ────────────


@pytest.mark.parametrize(
    ("desc", "reason"),
    [
        (_VALUES_WERE_DESC, "suspected_value_list"),  # demonstrated leak: numeric list
        (_OBSERVED_ENTRIES_DESC, "unhandled_marker"),  # demonstrated leak: "observed entries"
        (_BRANCH_CODES_DESC, "suspected_value_list"),  # demonstrated leak: code list
        (_QUOTED_STATUSES_DESC, "suspected_value_list"),  # quoted literals
        (_TIME_CUTOFFS_DESC, "suspected_value_list"),  # semicolon-separated time list
        ("Counterparty codes E.G. AB-01 and CD-02.", "suspected_value_list"),
        ("Values observed, e.g., 12:30 and 13:45.", "suspected_value_list"),
        ('Statuses (examples include "OPEN"; "CLOSED").', "suspected_value_list"),
        # A quoted marker phrase still fails closed — mention or not, the stripper never vouched.
        ('Published in the "sample profile" appendix each month.', "unhandled_marker"),
    ],
)
def test_suspicious_residual_blanks_whole_definition(desc, reason):
    result = sanitize_definition(desc)
    assert result.state == "suspected_unhandled"
    assert result.reason == reason
    assert result.clean == ""
    assert result.removed >= 1
    assert result.redaction_version is None  # nothing safe to redact — the field was blanked


def test_multiple_clauses_residual_value_list_fails_closed():
    """One recognized clause is excised, but a second (unrecognized) clause's value list survives
    the strip — the residual must blank the field rather than leak `AB-1; CD-2`."""
    result = sanitize_definition(_MULTI_CLAUSE_DESC)
    assert result.state == "suspected_unhandled"
    assert result.reason == "suspected_value_list"
    assert result.clean == ""
    assert "111" not in result.clean and "AB-1" not in result.clean
    assert result.removed == 2  # the stripped clause + the blanked field


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
    assert result.reason == ""


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
