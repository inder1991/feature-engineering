"""Tests for the fail-closed free-text sanitizer (FTR adapter Task 2, round-5 resolution R5-2).

The invariant under test: no raw customer sample value ever survives into ``clean`` — AND no
legitimate business definition is wrongly blanked. The REAL-file run showed 100% of actual sample
values live in the canonical ``representative values such as ...`` clause, which
``strip_sample_values`` excises (safe facets preserved). The v2 value-shape guesser is GONE
(R5-2): it over-blanked 41 real "sample profile has no non-blank values" definitions plus a
payment definition with numbers, while still missing bare code-lists. What remains:

* ``strip_sample_values`` — canonical-clause excision (covers every real sample value);
* a fail-closed DATA-marker scan on the residual — ``representative values``, ``sample values``,
  ``observed values/entries``, ``example values`` surviving the strip means a sample clause the
  stripper could not consume → the whole definition is blanked (``suspected_unhandled``). Bare
  ``sample profile`` is NOT a marker (the 41 real "no non-blank values" rows must pass);
* ``redact_free_text`` for PII — a redactor that fails closed blanks the field too.

Accepted, documented tradeoff (R5-2): a bare non-canonical value list with no marker is not
auto-caught — the real file never does this, and distinguishing it from prose is intractable.

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

# ── Labeled corpus (REAL-file shapes) ─────────────────────────────────────────────────────────────

# The 41-row REAL case: "sample profile has no non-blank values" — SAFE prose, carries no data.
# The v2 guesser blanked these; R5-2 says they MUST be preserved verbatim.
_NO_NONBLANK_DESC = (
    "Reserved regulatory adjustment bucket for future use. The sample profile has no non-blank "
    "values, and this column is unpopulated."
)

# The canonical FTR clause with multi-word entity values — the ONE shape that carries real data.
_CANONICAL_ENTITY_DESC = (
    "The counterparty legal name. The sample profile is text, with representative values such as "
    "ARTKOM GLOBAL FZE; NOQODI, which supports interpretation."
)

# RECOGNIZED clause (FTR shape): token + uniform-length all-digit values → identifier facets.
_ACCOUNT_DESC = (
    "Customer Account Number links the financial transaction record to the customer account on "
    "which the posting occurred. The sample profile is NUMERIC, with representative values such as "
    "3708484836801; 3708446902413; 3708454004701, which supports interpretation of the field."
)
_ACCOUNT_VALUES = ("3708484836801", "3708446902413", "3708454004701")

# TWO canonical clauses in one definition (whole-branch re-review IMPORTANT): the single-pass v3
# strip excised only the FIRST clause and LEAKED the second clause's raw values into ``clean``
# under state="stripped". v4 must strip to a fixed point — BOTH clauses gone, prose kept.
_TWO_CLAUSE_DESC = (
    "Account identifier for the customer. The sample profile is NUMERIC, with representative "
    "values such as 3708484836801; 3708446902413, which supports interpretation as an identifier. "
    "A secondary panel lists values such as 25-345129408-1-151; 25-999."
)

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

# A REAL payment definition full of numbers — the v2 guesser wiped it; it carries NO sample data
# and MUST NOT be blanked (R5-2).
_PAYMENT_DESC = (
    "ISO 20022 pacs.008 settlement amounts reconciled across 103 and 202 message types."
)

# PLAIN definition: business prose only — no clause, no marker, no PII.
_PLAIN_DESC = (
    "Booking date is the business date on which the ledger entry was recorded in the core "
    "banking system."
)

# FAIL-CLOSED positives: a DATA-implying marker phrase survives the strip (the stripper cannot
# consume these — no "values such as" anchor), so the whole definition blanks.
_MARKER_RESIDUAL_DESC = "Balances observed. representative values remain embedded here"
_OBSERVED_ENTRIES_DESC = (
    "Counterparty legal name; observed entries include ARTKOM GLOBAL FZE and NORDIC HOLDINGS AS."
)
_SAMPLE_VALUES_DESC = "Ledger bucket; sample values are retained in the appendix."

# NEGATIVE controls: legitimate concept prose that must be PRESERVED verbatim — including the
# phrases the v1 whitelist / v2 guesser used to over-blank.
_SUCH_AS_PROSE_DESC = "Contract attributes such as tenor and rate drive the pricing model."
_REPRESENTATIVE_OFFICE_DESC = "Flag set when the branch is a representative office in the region."
_SAMPLE_POPULATION_DESC = "Count of accounts in the sample population size used for QA checks."
_GDP_DESC = "Macroeconomic input holding the GDP figure for the year."
_TAXONOMY_DESC = "Classified by party, product, account."
# Bare "sample profile" is NOT a data marker (R5-2): this prose mention must survive.
_SAMPLE_PROFILE_MENTION_DESC = 'Published in the "sample profile" appendix each month.'

# PII-only prose: the deterministic redactor scrubs the token, the rest of the prose survives.
_PII_DESC = "Reconciliation contact mailbox; escalations go to ops@bank.example when unresolved."

# RECOGNIZED clause AND PII in the surviving prose → both removals counted.
_CLAUSE_PLUS_PII_DESC = (
    "Queries go to ops@bank.example for this field. The sample profile is NUMERIC, with "
    "representative values such as 111; 222; 333, which supports interpretation of the field."
)


# ── version ───────────────────────────────────────────────────────────────────────────────────────


def test_sanitizer_version_bumped_for_multipass_strip():
    assert SANITIZER_VERSION == "ftr-sanitize-v4"


# ── sanitize_definition: the 41-row real case — MUST NOT blank ────────────────────────────────────


def test_no_nonblank_values_profile_preserved():
    """41 of the real file's 127 definitions say "sample profile has no non-blank values" — SAFE
    prose the v2 guesser wrongly blanked. R5-2: bare `sample profile` is not a data marker."""
    result = sanitize_definition(_NO_NONBLANK_DESC)
    assert result.state == "none"
    assert result.reason == ""
    assert result.clean == _NO_NONBLANK_DESC
    assert result.removed == 0


def test_payment_definition_with_numbers_not_blanked():
    """A real payment definition full of numerics carries no sample data — the v2 token gate wiped
    it; v3 must let it through untouched."""
    result = sanitize_definition(_PAYMENT_DESC)
    assert result.state in {"none", "stripped"}
    assert result.reason == ""
    assert result.clean == _PAYMENT_DESC


# ── sanitize_definition: canonical clauses stripped (facets preserved) ────────────────────────────


def test_canonical_entity_clause_stripped_with_facets():
    """The canonical FTR clause — the ONE real shape carrying data — is excised: no raw value
    reaches ``clean``, the business prose survives, and the safe facets are captured pre-strip."""
    result = sanitize_definition(_CANONICAL_ENTITY_DESC)
    assert result.state == "stripped"
    assert result.reason == ""
    assert "ARTKOM" not in result.clean
    assert "NOQODI" not in result.clean
    assert "counterparty legal name" in result.clean.lower()
    assert result.clean  # non-empty business prose — NOT blanked
    assert result.logical_representation == "text"
    assert result.semantic_type == "text"


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


def test_two_clause_definition_strips_both_clauses_no_leak():
    """Whole-branch re-review IMPORTANT: a SECOND `values such as` clause in a later sentence must
    not leak — stripping runs to a fixed point, so BOTH clauses go while the prose survives."""
    result = sanitize_definition(_TWO_CLAUSE_DESC)
    assert result.state == "stripped"
    assert result.reason == ""
    assert "3708484836801" not in result.clean
    assert "3708446902413" not in result.clean
    assert "25-345129408-1-151" not in result.clean
    assert "25-999" not in result.clean
    assert "values such as" not in result.clean.lower()
    assert "Account identifier for the customer." in result.clean  # business prose survives


def test_bare_values_such_as_anchor_fails_closed():
    """Belt AND braces: if a `values such as` anchor ever survives the multi-pass strip (here: no
    value text follows, so the stripper cannot consume it), the whole definition blanks."""
    result = sanitize_definition("Legacy appendix retains values such as")
    assert result.clean == ""
    assert result.state == "suspected_unhandled"
    assert result.reason == "unhandled_marker"


@pytest.mark.parametrize(
    ("desc", "raw_values", "logical", "semantic"),
    [
        (_AMOUNT_DESC, ("1250.00", "9.99"), "decimal", "amount"),
        (_TIME_DESC, ("15:07:08", "10:01:01", "11:00:56"), "time", "time"),
    ],
)
def test_decimal_and_time_samples_stripped(desc, raw_values, logical, semantic):
    result = sanitize_definition(desc)
    assert result.state == "stripped"
    assert result.logical_representation == logical
    assert result.semantic_type == semantic
    for value in raw_values:
        assert value not in result.clean


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
        _SAMPLE_PROFILE_MENTION_DESC,
    ],
)
def test_concept_prose_preserved_not_blanked(desc):
    """R5-2 negatives: introducer phrases, `a representative office`, `sample population size`,
    a bare `sample profile` mention, acronyms, and taxonomy lists all pass through verbatim."""
    result = sanitize_definition(desc)
    assert result.state == "none"
    assert result.reason == ""
    assert result.clean == desc


# ── sanitize_definition: fail-closed data-marker scan on the residual ─────────────────────────────


@pytest.mark.parametrize(
    "desc",
    [
        _MARKER_RESIDUAL_DESC,  # `representative values` with no "such as" anchor — strip can't consume
        _OBSERVED_ENTRIES_DESC,  # demonstrated leak: "observed entries include ..."
        _SAMPLE_VALUES_DESC,  # `sample values` marker
        "Codes recorded; example values appear in legacy exports.",  # `example values` marker
    ],
)
def test_surviving_data_marker_blanks_whole_definition(desc):
    """A DATA-implying marker phrase surviving the strip means a sample clause the stripper could
    not consume — fail closed: blank the WHOLE definition, never individual values."""
    assert sanitize_definition(desc).clean == ""  # nothing leaks
    result = sanitize_definition(desc)
    assert result.state == "suspected_unhandled"
    assert result.reason == "unhandled_marker"
    assert result.removed >= 1
    assert result.redaction_version is None  # nothing safe to redact — the field was blanked


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
    assert result.reason == "pii_redaction_failed"
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
