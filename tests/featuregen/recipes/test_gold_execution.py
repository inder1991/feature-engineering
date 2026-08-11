"""BR-22 — row-semantics gold execution: the exemplar's meaning, tested against hand math.

The reference evaluator below is DELIBERATELY test-side and self-contained — it imports no
production formula code, so a production implementation can never pass by generating the
expected values with the computation under test (the acceptance's independence rule). The
expected values themselves are hand-computed literals in the gold file, with the arithmetic
written out for a reviewer to re-derive from the ledger alone.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

_HERE = Path(__file__).parent
LEDGER = json.loads((_HERE / "fixtures" / "synthetic_ledger.json").read_text())
GOLD = json.loads((_HERE / "gold" / "posted_debit_amount_expected.json").read_text())


def reference_posted_debit_amount(rows, rates, *, cutoff: str, trailing_days: int,
                                  base: str = "AED") -> dict[str, float]:
    """The EXEMPLAR's contract, as arithmetic: eligible posted DEBITS inside the trailing
    window, exact reversal pairs neutralized (both legs dropped), per-row currency converted
    through the booking-date rate, summed per account. Pure test code — no production import."""
    cutoff_d = date.fromisoformat(cutoff)
    start = cutoff_d - timedelta(days=trailing_days)
    reversed_ids = {row["reversal_of"] for row in rows if row["reversal_of"]}

    totals: dict[str, float] = {}
    for row in rows:
        ts = date.fromisoformat(row["event_ts"])
        if not (start < ts <= cutoff_d):
            continue
        if row["status"] != "posted" or row["direction"] != "D":
            continue
        if row["reversal_of"] or row["id"] in reversed_ids:
            continue                                        # both legs of the pair drop
        rate_table = rates[row["currency"]]
        rate = rate_table.get("always") or rate_table[row["event_ts"]]
        totals[row["account"]] = round(
            totals.get(row["account"], 0.0) + row["amount"] * rate, 2)
    return totals


def test_the_exemplar_matches_the_hand_computed_gold():
    got = reference_posted_debit_amount(
        LEDGER["rows"], LEDGER["booking_rates_to_base"],
        cutoff=GOLD["window"]["cutoff"], trailing_days=GOLD["window"]["trailing_days"])
    assert got == GOLD["expected"]


def test_the_mid_window_rate_change_is_load_bearing():
    """The single-rate shortcut is DETECTABLE: applying either window rate to all USD rows
    gives 73.00 or 74.00 for ACC2's USD portion — never the correct 73.50. A conversion
    implementation that ignores booking time fails this ledger."""
    rows = [r for r in LEDGER["rows"] if r["account"] == "ACC2"]
    for flat_rate in (3.65, 3.7):
        flat = {"AED": {"always": 1.0}, "USD": {"always": flat_rate},
                "EUR": {"always": 4.0}}
        got = reference_posted_debit_amount(rows, flat, cutoff="2026-06-30",
                                            trailing_days=30)
        assert got["ACC2"] != GOLD["expected"]["ACC2"]


def test_every_boundary_case_is_present_in_the_ledger():
    """The gold file names its boundary rows; the ledger must actually contain each — a
    trimmed fixture cannot silently weaken the corpus."""
    ids = {row["id"] for row in LEDGER["rows"]}
    named = GOLD["boundary_cases"]
    flat = set()
    for value in named.values():
        flat.update(value if isinstance(value, list) else [value])
    assert flat <= ids


def test_the_active_executable_recipes_all_have_linked_gold():
    """The acceptance: every ACTIVE executable (FORMULA_AUTHORABLE) recipe has linked gold —
    the two v1 anchors through the v1 gold corpus, the exemplar through this one."""
    from featuregen.overlay.upload.recipe_formula_gold import FORMULA_GOLD_CASES
    from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES

    authorable = {r.recipe_id for r in V2_RECIPES if r.readiness == "FORMULA_AUTHORABLE"}
    v1_gold = {case.recipe_id for case in FORMULA_GOLD_CASES}
    linked = v1_gold | {GOLD["recipe_id"]}
    assert authorable <= linked, sorted(authorable - linked)


def test_no_fixture_contains_production_data_markers():
    """The corpus is fabricated: accounts are the ACC* synthetics, and no fixture row
    carries anything shaped like a real identifier."""
    text = json.dumps(LEDGER)
    assert all(acct.startswith("ACC") for acct in {r["account"] for r in LEDGER["rows"]})
    assert "no real customer data" in LEDGER["note"]
    for marker in ("@", "cif", "passport", "iban"):
        assert marker not in text.lower()
