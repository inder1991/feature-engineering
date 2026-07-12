"""Tests for the deterministic sample-value parser (Task 5, review-fix #9).

The non-negotiable invariant under test: a parser-supported type must NEVER certify numeric
computation for an identifier-like value. A fixed-length all-digit account number must come back with
``computational_type is None`` — never ``"decimal"`` — so nothing downstream can sum or average it.

Descriptions are FTR-shaped (mirroring ``FTR_Column_Mapping.csv``) but inline — no ``~/Downloads``.
"""
import dataclasses

import pytest

from featuregen.overlay.upload.sample_parser import (
    ParsedProfile,
    parse_sample_profile,
    strip_sample_values,
)

# ── FTR-shaped descriptions (real phrasings; inline fixtures). ────────────────────────────────────

# A 13-digit account number: NUMERIC token, uniform-length all-digit values → an IDENTIFIER, never a
# summable measure. This is the review-#9 case.
_ACCOUNT_DESC = (
    "Customer Account Number is the account-level identifier used to link the financial transaction "
    "record to the customer account on which the posting occurred. The sample profile is NUMERIC, "
    "with representative values such as 3708484836801; 3708446902413; 3708454004701, which supports "
    "interpretation of the field as part of the FTR control and reporting record."
)

# A genuine monetary amount — decimals with a point. Deliberately carries NO "sample profile is"
# token, to prove classification is driven by the VALUES, not the token.
_AMOUNT_DESC = (
    "Posting amount is the monetary value of the ledger entry, with representative values such as "
    "1250.00; 9.99, which supports interpretation of the field."
)

# Amount whose values run to the end of the string (no trailing ", which ..." clause).
_AMOUNT_DESC_EOL = "Ledger amount. Values such as 1250.00; 9.99"

# Time of day: FTR labels this NUMERIC_SPECIAL — the SAME token it uses for dash-refs below, so the
# token alone cannot disambiguate; the value shape (HH:MM:SS) must.
_TIME_DESC = (
    "Transaction Time captures the timing dimension of a financial transaction record. The sample "
    "profile is NUMERIC_SPECIAL, with representative values such as 15:07:08; 10:01:01; 11:00:56, "
    "which supports interpretation of the field."
)

# Names: ALPHA_SPECIAL. Values contain internal spaces (a multi-word legal name) but no ';' inside.
_NAMES_DESC = (
    "Customer Name identifies the customer party associated with the record. The sample profile is "
    "ALPHA_SPECIAL, with representative values such as ARTKOM GLOBAL FZE; NOQODI; RASHED ALJABRI "
    "REAL ESTATE DEVELOPMENT LLC, which supports interpretation of the field."
)

# CIF: a shorter (8-digit) uniform all-digit identifier — a second identifier length.
_CIF_DESC = (
    "Customer Information File Identifier connects the transaction to the customer master profile. "
    "The sample profile is NUMERIC, with representative values such as 84848368; 84469024; 84540047, "
    "which supports interpretation of the field."
)

# A reference number: token is NUMERIC_SPECIAL (same as time) but the values carry dashes → NOT time,
# NOT computational. Proves the value shape overrides the ambiguous token.
_REFNUM_DESC = (
    "Financial Transaction Reference Number uniquely traces the transaction across systems. The "
    "sample profile is NUMERIC_SPECIAL, with representative values such as 25-345129408-1-151; "
    "25-345059940-1-151; 25-345073465-1-151, which supports interpretation of the field."
)

# Alphanumeric identifier (ALPHA_NUMERIC): letters + digits → text representation, non-computational.
_ALNUM_DESC = (
    "Transaction Identifier uniquely traces the financial transaction. The sample profile is "
    "ALPHA_NUMERIC, with representative values such as EI0300357; EI0046562; EI0061842, which "
    "supports interpretation of the field."
)

# A description with neither a profile phrase nor any representative values.
_NO_PROFILE_DESC = (
    "This column carries a business concept but its description records no profiling metadata and "
    "lists no representative values at all."
)

# A profile token present but with no representative-values list.
_TOKEN_ONLY_DESC = "The sample profile is NUMERIC. No representative sample was captured for this field."

# All-integer values of VARYING length — could be a count/code, not a fixed-length identifier. The
# parser must not silently call it an identifier, and must never certify computation.
_NONUNIFORM_INT_DESC = "Line counts, with representative values such as 1; 22; 333"


# ── Required tests (from the task brief). ─────────────────────────────────────────────────────────

def test_account_number_is_identifier_never_computational():
    p = parse_sample_profile(_ACCOUNT_DESC)
    assert p.logical_representation == "numeric_string"
    assert p.semantic_type == "identifier"
    # THE non-negotiable: a fixed-length all-digit account number is NOT a decimal measure.
    assert p.computational_type is None
    assert p.sample_values == ("3708484836801", "3708446902413", "3708454004701")
    assert p.diagnostic is None


def test_amount_with_decimal_point_is_decimal_amount():
    p = parse_sample_profile(_AMOUNT_DESC)
    assert p.logical_representation == "decimal"
    assert p.semantic_type == "amount"
    assert p.computational_type == "decimal"
    assert p.sample_values == ("1250.00", "9.99")
    assert p.diagnostic is None


def test_amount_values_at_end_of_string():
    p = parse_sample_profile(_AMOUNT_DESC_EOL)
    assert p.computational_type == "decimal"
    assert p.sample_values == ("1250.00", "9.99")


def test_time_of_day_is_time():
    p = parse_sample_profile(_TIME_DESC)
    assert p.logical_representation == "time"
    assert p.semantic_type == "time"
    assert p.computational_type is None
    assert p.sample_values == ("15:07:08", "10:01:01", "11:00:56")


def test_names_are_text():
    p = parse_sample_profile(_NAMES_DESC)
    assert p.logical_representation == "text"
    assert p.semantic_type == "text"
    assert p.computational_type is None
    assert p.sample_values == (
        "ARTKOM GLOBAL FZE", "NOQODI", "RASHED ALJABRI REAL ESTATE DEVELOPMENT LLC")


def test_no_profile_phrase_yields_diagnostic_and_all_none():
    p = parse_sample_profile(_NO_PROFILE_DESC)
    assert p.logical_representation is None
    assert p.semantic_type is None
    assert p.computational_type is None
    assert p.sample_values == ()
    assert p.diagnostic is not None and p.diagnostic != ""


# ── Robustness / additional coverage. ─────────────────────────────────────────────────────────────

def test_cif_shorter_uniform_all_digit_is_also_identifier():
    p = parse_sample_profile(_CIF_DESC)
    assert p.logical_representation == "numeric_string"
    assert p.semantic_type == "identifier"
    assert p.computational_type is None


def test_numeric_special_dash_ref_is_text_not_time_not_computational():
    # Same NUMERIC_SPECIAL token as the time field; the dash-bearing values must win → text.
    p = parse_sample_profile(_REFNUM_DESC)
    assert p.logical_representation == "text"
    assert p.semantic_type == "text"
    assert p.computational_type is None
    assert p.sample_values[0] == "25-345129408-1-151"


def test_alphanumeric_identifier_is_text():
    p = parse_sample_profile(_ALNUM_DESC)
    assert p.logical_representation == "text"
    assert p.computational_type is None
    assert p.sample_values == ("EI0300357", "EI0046562", "EI0061842")


def test_token_present_but_no_values_is_diagnostic_not_a_false_type():
    p = parse_sample_profile(_TOKEN_ONLY_DESC)
    # A coarse representation may be inferred from the token, but semantics/computation must NOT be
    # asserted without values, and a diagnostic must explain the gap.
    assert p.semantic_type is None
    assert p.computational_type is None
    assert p.sample_values == ()
    assert p.diagnostic is not None


def test_nonuniform_integers_are_not_certified_and_flagged():
    p = parse_sample_profile(_NONUNIFORM_INT_DESC)
    assert p.logical_representation == "numeric_string"
    assert p.semantic_type is None          # varying length → not confidently an identifier
    assert p.computational_type is None     # and never computational without a decimal point
    assert p.sample_values == ("1", "22", "333")
    assert p.diagnostic is not None


def test_empty_description_is_diagnostic():
    p = parse_sample_profile("")
    assert p.logical_representation is None
    assert p.semantic_type is None
    assert p.computational_type is None
    assert p.diagnostic is not None


def test_sample_values_is_a_tuple_of_str():
    p = parse_sample_profile(_ACCOUNT_DESC)
    assert isinstance(p.sample_values, tuple)
    assert all(isinstance(v, str) for v in p.sample_values)


def test_parsed_profile_is_frozen():
    p = parse_sample_profile(_ACCOUNT_DESC)
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.semantic_type = "mutated"  # type: ignore[misc]


def test_parsed_profile_is_constructible():
    p = ParsedProfile(
        logical_representation="text", semantic_type="text", computational_type=None,
        sample_values=("a", "b"), diagnostic=None)
    assert p.sample_values == ("a", "b")


# ── Whole-branch review CRITICAL: strip_sample_values excises the embedded sample-value clause so no
# raw customer DATA VALUE can egress to the LLM as `business_definition`, while the business meaning
# stays intact. Mirrors the anchors parse_sample_profile reads. ──

# A realistic FTR glossary definition: business meaning FOLLOWED by the embedded sample-profile clause
# that carries raw customer values (an account number, a time, a decimal amount, a short code).
_LEAKY_DEF = (
    "Financial transaction record identifying the customer's posting. The sample profile is NUMERIC, "
    "with representative values such as 3708484836801; 15:07:08; 1250.00; 84848368, which supports "
    "interpretation of the field. It is used for reconciliation."
)


def test_strip_sample_values_excises_values_and_keeps_meaning():
    out = strip_sample_values(_LEAKY_DEF)
    # every raw sample value is gone (account number, time, decimal, short code)...
    for value in ("3708484836801", "15:07:08", "1250.00", "84848368"):
        assert value not in out
    # ...and the sample-profile scaffolding is gone too (no dangling "representative values" / "which")
    assert "representative values" not in out
    assert "sample profile" not in out.lower()
    # ...but the surrounding business meaning survives
    assert "financial transaction record" in out.lower()
    assert "customer" in out.lower()
    assert "reconciliation" in out.lower()          # prose AFTER the excised clause is preserved


def test_strip_sample_values_handles_values_running_to_end_of_string():
    # No trailing ", which ..." clause — the values list runs to the end of the string.
    out = strip_sample_values("Ledger amount. The sample profile is NUMERIC, with representative "
                              "values such as 1250.00; 9.99; 42.50")
    assert "1250.00" not in out and "9.99" not in out and "42.50" not in out
    assert "ledger amount" in out.lower()


def test_strip_sample_values_leaves_a_plain_business_definition_unchanged():
    plain = "The legal name of the customer as registered."
    assert strip_sample_values(plain) == plain


def test_strip_sample_values_tolerates_empty():
    assert strip_sample_values("") == ""
