"""Joint Task 4 (d) — the deterministic ruleset covers every HIGH-IMPACT proposal group.

The plan's demand is precise and so is the review's caveat: "the critic runs for every high-impact
proposal (identifier, monetary, temporal, label/leakage), not only identifiers. Name and test the
deterministic signal for each contradiction; missing operational type is `unknown`, not evidence of
a conflict."

So each group gets: (1) the rule fires on the signal it names, and (2) the rule ABSTAINS when that
signal is absent — which is the normal case for a glossary catalog, where `operational_type` is
uniformly "unknown" and many columns declare no type at all. A ruleset that misfires on every FTR
table is worse than no ruleset, because it refutes correct assignments deterministically and no
model may overturn it.
"""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.attest.representation import (
    CRITIC_GROUPS,
    SHAPE_CONFLICT_CODES,
    shape_conflicts,
)
from featuregen.overlay.upload.concepts import CONCEPT_REGISTRY


def test_the_critic_groups_are_the_four_high_impact_ones() -> None:
    assert CRITIC_GROUPS == {"identifier", "monetary", "temporal", "label"}
    # Every member is a real registry group — a typo here would silently disable a whole group.
    groups = {c.group for c in CONCEPT_REGISTRY.values()}
    assert CRITIC_GROUPS <= groups


# ── identifier (unchanged behaviour, re-pinned under the new dispatch) ───────────────────────────


def test_identifier_rules_are_unchanged() -> None:
    assert shape_conflicts("counter_party_bic", "string", "SWIFT BIC of the counterparty",
                           "counterparty_id") == ("identifier_namespace_mismatch",)
    assert shape_conflicts("sol_desc", "string", "Branch description", "branch_id") == (
        "name_or_description_not_identifier",)
    assert "measure_not_identifier" in shape_conflicts(
        "actual_counter_party_amt", "double", "Transaction amount", "counterparty_id")


# ── monetary ─────────────────────────────────────────────────────────────────────────────────────


def test_monetary_refuses_an_identifier_namespace_shape() -> None:
    """A BIC is a value space, not a quantity: a column whose own wording claims the SWIFT BIC
    shape cannot be an amount under any reading."""
    assert shape_conflicts("counter_party_bic", "varchar(11)", "The SWIFT BIC of the bank",
                           "monetary_stock") == ("identifier_shape_not_monetary",)


def test_monetary_refuses_description_prose() -> None:
    assert shape_conflicts("sol_desc", "varchar", "Branch description text",
                           "monetary_flow") == ("text_not_monetary",)


def test_monetary_abstains_on_a_plain_amount_column() -> None:
    assert shape_conflicts("actual_counter_party_amt", "decimal", "Transaction amount posted",
                           "monetary_flow") == ()


def test_monetary_abstains_when_the_type_signal_is_absent() -> None:
    """The FTR shape: `operational_type` unknown, no declared type, a bare name. Every rule must
    stay silent — a varchar-declared amount is routine in extract layers, so a type-shaped
    refutation would fire across whole catalogs."""
    assert shape_conflicts("txn_amt", None, None, "monetary_flow") == ()
    assert shape_conflicts("txn_amt", "unknown", None, "monetary_flow") == ()
    assert shape_conflicts("txn_amt", "varchar", None, "monetary_flow") == ()


# ── temporal ─────────────────────────────────────────────────────────────────────────────────────


def test_temporal_refuses_an_identifier_namespace_shape() -> None:
    assert shape_conflicts("uetr_ref", "varchar(36)", "The UETR uuid of the payment",
                           "event_timestamp") == ("identifier_shape_not_temporal",)


def test_temporal_refuses_description_prose() -> None:
    assert shape_conflicts("sol_desc", "varchar", "Branch description text",
                           "as_of_date") == ("text_not_temporal",)


def test_temporal_refuses_a_measure_shape() -> None:
    """A `double`-declared column is not a date, and an `_amt`-named one is not a date either."""
    assert "measure_not_temporal" in shape_conflicts(
        "settlement_amt", "decimal", "Settled amount", "settlement_date")
    assert "measure_not_temporal" in shape_conflicts(
        "value_col", "double precision", None, "value_date")


def test_temporal_does_not_refuse_an_integer_encoded_date() -> None:
    """`20240131` in a bigint is one of the most common real date encodings in a bank extract.
    Refuting it would be a deterministic false positive no model could overturn."""
    assert shape_conflicts("pstd_date", "bigint", "Posting date as YYYYMMDD",
                           "booking_date") == ()


def test_temporal_abstains_when_the_signal_is_absent() -> None:
    assert shape_conflicts("pstd_date", None, None, "booking_date") == ()
    assert shape_conflicts("pstd_date", "unknown", None, "booking_date") == ()


# ── label / leakage ──────────────────────────────────────────────────────────────────────────────


def test_label_refuses_an_identifier_namespace_shape() -> None:
    assert shape_conflicts("cp_bic", "varchar(11)", "SWIFT BIC of the counterparty",
                           "outcome_label") == ("identifier_shape_not_label",)


def test_label_refuses_a_measure_shape() -> None:
    assert "measure_not_label" in shape_conflicts(
        "recovery_amt", "decimal", "Amount recovered", "outcome_label")


def test_label_does_not_refuse_a_short_status_word() -> None:
    """An outcome label legitimately arrives as a short categorical/status word, which
    `representation_role` reads as HUMAN_LABEL — so text shape is deliberately NOT a label
    conflict, unlike for identifiers."""
    assert shape_conflicts("churn_status_name", "varchar", "Whether the customer churned",
                           "outcome_label") == ()


def test_label_abstains_when_the_signal_is_absent() -> None:
    assert shape_conflicts("chrn_flg", None, None, "outcome_label") == ()
    assert shape_conflicts("chrn_flg", "unknown", None, "outcome_label") == ()


# ── scope + closure ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("concept", ["country_code", "boolean_flag", "product_type"])
def test_a_low_impact_group_is_never_refuted(concept: str) -> None:
    """The deterministic layer corroborates the four high-impact groups only. A categorical or a
    flag decides no join, no aggregation, no point-in-time semantics and no training target."""
    assert shape_conflicts("counter_party_bic", "double", "SWIFT BIC prose", concept) == ()


def test_an_unknown_concept_is_never_refuted() -> None:
    assert shape_conflicts("anything", "double", "prose", "not_a_registry_concept") == ()
    assert shape_conflicts("anything", "double", "prose", None) == ()


def test_every_emitted_code_is_in_the_closed_vocabulary() -> None:
    cases = [
        ("counter_party_bic", "varchar(11)", "The SWIFT BIC of the bank", "monetary_stock"),
        ("sol_desc", "varchar", "Branch description text", "monetary_flow"),
        ("uetr_ref", "varchar(36)", "The UETR uuid", "event_timestamp"),
        ("settlement_amt", "double", "Settled amount", "settlement_date"),
        ("cp_bic", "varchar(11)", "SWIFT BIC prose", "outcome_label"),
        ("recovery_amt", "double", "Amount recovered", "outcome_label"),
        ("sol_desc", "string", "Branch description", "branch_id"),
    ]
    emitted: set[str] = set()
    for case in cases:
        emitted |= set(shape_conflicts(*case))
    assert emitted            # the cases really do fire
    assert emitted <= SHAPE_CONFLICT_CODES
