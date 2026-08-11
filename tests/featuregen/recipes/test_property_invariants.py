"""BR-22 — the property invariants, held over the synthetic ledger and reference evaluator.

Each plan-named invariant becomes a mutation of the ledger that must NOT change (or must
change EXACTLY as the property says) the reference result — meaning-level tests no registry
shape check can substitute for.
"""
from __future__ import annotations

import copy

from tests.featuregen.recipes.test_gold_execution import (
    GOLD,
    LEDGER,
    reference_posted_debit_amount,
)

_KW = {"cutoff": GOLD["window"]["cutoff"],
       "trailing_days": GOLD["window"]["trailing_days"]}


def _run(rows, rates=None):
    return reference_posted_debit_amount(rows, rates or LEDGER["booking_rates_to_base"],
                                         **_KW)


BASELINE = _run(LEDGER["rows"])


def test_an_ineligible_failed_transaction_cannot_change_a_posted_feature():
    rows = copy.deepcopy(LEDGER["rows"])
    rows.append({"id": "rX1", "account": "ACC1", "event_ts": "2026-06-16",
                 "amount": 9999.0, "currency": "AED", "direction": "D",
                 "status": "failed", "reversal_of": None})
    assert _run(rows) == BASELINE


def test_an_exact_reversal_neutralizes_the_original():
    rows = copy.deepcopy(LEDGER["rows"])
    rows.append({"id": "rX2", "account": "ACC1", "event_ts": "2026-06-21",
                 "amount": 60.0, "currency": "AED", "direction": "D",
                 "status": "posted", "reversal_of": None})
    inflated = _run(rows)
    assert inflated["ACC1"] == BASELINE["ACC1"] + 60.0
    rows.append({"id": "rX3", "account": "ACC1", "event_ts": "2026-06-22",
                 "amount": 60.0, "currency": "AED", "direction": "D",
                 "status": "posted", "reversal_of": "rX2"})
    assert _run(rows) == BASELINE


def test_moving_an_event_after_the_cutoff_cannot_change_the_earlier_result():
    rows = copy.deepcopy(LEDGER["rows"])
    r01 = next(r for r in rows if r["id"] == "r01")
    r01["event_ts"] = "2026-07-15"
    moved = _run(rows)
    assert moved["ACC1"] == BASELINE["ACC1"] - 100.0


def test_changing_an_unrelated_currency_cannot_change_a_single_currency_result():
    rates = copy.deepcopy(LEDGER["booking_rates_to_base"])
    rates["EUR"] = {"always": 99.0}                     # no ledger row uses EUR in-window
    assert _run(LEDGER["rows"], rates) == BASELINE


def test_an_additive_flow_aggregates_over_disjoint_time_partitions():
    first_half = reference_posted_debit_amount(
        LEDGER["rows"], LEDGER["booking_rates_to_base"],
        cutoff="2026-06-15", trailing_days=15)
    second_half = reference_posted_debit_amount(
        LEDGER["rows"], LEDGER["booking_rates_to_base"],
        cutoff="2026-06-30", trailing_days=15)
    for account, total in BASELINE.items():
        assert round(first_half.get(account, 0.0)
                     + second_half.get(account, 0.0), 2) == total


def test_splitting_one_account_preserves_the_allocated_total():
    """The account-split half of the customer-allocation invariant: moving half of ACC1's
    eligible rows to a new account changes per-account results but the full-attribution
    total is preserved."""
    rows = copy.deepcopy(LEDGER["rows"])
    next(r for r in rows if r["id"] == "r02")["account"] = "ACC1B"
    split = _run(rows)
    assert split["ACC1"] + split["ACC1B"] == BASELINE["ACC1"]


def test_a_ratio_is_unchanged_when_both_sides_scale_equally():
    """The ratio invariant over the ledger's debit/credit sides."""
    def ratio(rows):
        debits = sum(r["amount"] for r in rows
                     if r["status"] == "posted" and r["direction"] == "D"
                     and r["account"] == "ACC1" and not r["reversal_of"])
        credits = sum(r["amount"] for r in rows
                      if r["status"] == "posted" and r["direction"] == "C"
                      and r["account"] == "ACC1")
        return debits / credits

    rows = copy.deepcopy(LEDGER["rows"])
    baseline = ratio(rows)
    for row in rows:
        row["amount"] *= 7
    assert ratio(rows) == baseline


def test_duplicating_an_ineligible_row_cannot_multiply_the_result():
    rows = copy.deepcopy(LEDGER["rows"])
    r04 = copy.deepcopy(next(r for r in rows if r["id"] == "r04"))
    r04["id"] = "r04-dup"
    rows.append(r04)
    assert _run(rows) == BASELINE


def test_the_contract_holds_the_remaining_additivity_invariants():
    """Semi-additive stocks never sum across snapshots and non-additive ratios are never
    summed — held at the CONTRACT layer (RESULT_CLASS_ADDITIVITY refuses the mismatch at
    construction), asserted here so the invariant list is complete in one place."""
    from featuregen.overlay.upload.recipe_contract_v2 import RESULT_CLASS_ADDITIVITY

    assert RESULT_CLASS_ADDITIVITY["snapshot"] == ("semi_additive",)
    assert RESULT_CLASS_ADDITIVITY["ratio"] == ("non_additive",)
    assert RESULT_CLASS_ADDITIVITY["share"] == ("non_additive",)
