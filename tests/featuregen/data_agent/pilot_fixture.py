"""Synthetic pilot tables — a customer table and a transaction table.

**What this is and is not.** It is a FIXTURE: it proves the machinery computes the right answers,
on numbers small enough to count by hand. It is not a stand-in for real data and cannot be — it
says nothing about whether the catalog's description of the bank's tables is accurate, which is the
class of defect that matters most and can only be found against the real thing.

It is shaped around the pilot question:

    customers whose transaction count decreased in the current month versus the previous month,
    by segment and sector

and it deliberately contains the cases that are easy to get wrong:

* **C4 drops to ZERO** eligible transactions in the current month. A naive inner join loses this
  customer entirely — and they are precisely the customer the question is about.
* **C5 appears only in the current month.** "Decreased" must not be true.
* **C6's only previous transaction is REVERSED**, so its eligible previous count is zero. Without
  reversal filtering it would read as a decrease that never happened.
* **C9 has transactions but no customer row** — an unmatched key.
* **NULL customer ids** exist, so a null rate is non-zero.
* **customers.cif_id is unique; transactions.cif_id is not.**

**The ineligible rows are TRAPS.** Every one of them is placed so that removing a filter changes an
answer a test asserts. Eligible counts are identical to the pre-eligibility fixture, so the analysis
expectations did not move — only the raw-row counts did.
"""
from __future__ import annotations

CUSTOMER_SCHEMA = "dpl_eib"
CUSTOMER_TABLE = "customer_master"
TRANSACTION_SCHEMA = "dpl_eib"
TRANSACTION_TABLE = "tran_repos"

PREVIOUS_MONTH = "2026-05"
CURRENT_MONTH = "2026-06"

POSTED = "POSTED"
NOT_REVERSED = "N"

#: cif_id, segment, sector
CUSTOMERS: tuple[tuple[str, str, str], ...] = (
    ("C1", "RETAIL", "TRADING"),
    ("C2", "RETAIL", "MANUFACTURING"),
    ("C3", "CORPORATE", "TRADING"),
    ("C4", "CORPORATE", "REAL_ESTATE"),
    ("C5", "RETAIL", "TRADING"),
    ("C6", "RETAIL", "MANUFACTURING"),
)

#: cif_id, tran_amt, tran_type, tran_month, tran_status, reversal_flag
#
# ELIGIBLE (POSTED / N) — the counts the question is actually about:
#   C1  prev 3  curr 1   DECREASED
#   C2  prev 2  curr 2   unchanged
#   C3  prev 1  curr 4   increased
#   C4  prev 2  curr 0   DECREASED   <- zero current; inner join would lose them
#   C5  prev 0  curr 2   increased   <- no previous period
#   C6  prev 0  curr 0   unchanged   <- its only previous row is reversed
_ELIGIBLE: tuple[tuple[str | None, str, str, str], ...] = (
    ("C1", "10.00", "REMITTANCE", PREVIOUS_MONTH),
    ("C1", "20.00", "REMITTANCE", PREVIOUS_MONTH),
    ("C1", "30.00", "CHARGES", PREVIOUS_MONTH),
    ("C2", "40.00", "CHARGES", PREVIOUS_MONTH),
    ("C2", "50.00", "REMITTANCE", PREVIOUS_MONTH),
    ("C3", "60.00", "CHARGES", PREVIOUS_MONTH),
    ("C4", "70.00", "REMITTANCE", PREVIOUS_MONTH),
    ("C4", "80.00", "REMITTANCE", PREVIOUS_MONTH),
    (None, "90.00", "CHARGES", PREVIOUS_MONTH),
    ("C1", "11.00", "REMITTANCE", CURRENT_MONTH),
    ("C2", "21.00", "CHARGES", CURRENT_MONTH),
    ("C2", "31.00", "CHARGES", CURRENT_MONTH),
    ("C3", "41.00", "REMITTANCE", CURRENT_MONTH),
    ("C3", "51.00", "REMITTANCE", CURRENT_MONTH),
    ("C3", "61.00", "CHARGES", CURRENT_MONTH),
    ("C3", "71.00", "CHARGES", CURRENT_MONTH),
    ("C5", "81.00", "REMITTANCE", CURRENT_MONTH),
    ("C5", "91.00", "CHARGES", CURRENT_MONTH),
    ("C9", "99.00", "CHARGES", CURRENT_MONTH),
    (None, "12.00", "REMITTANCE", CURRENT_MONTH),
)

#: cif_id, amt, type, month, status, reversal — each row breaks a specific test if its filter goes.
_INELIGIBLE: tuple[tuple[str, str, str, str, str | None, str | None], ...] = (
    # status filter: C2 current would become 3 and C2 would stop being "unchanged"
    ("C2", "22.00", "CHARGES", CURRENT_MONTH, "PENDING", NOT_REVERSED),
    # reversal filter: C3 current would become 5
    ("C3", "72.00", "CHARGES", CURRENT_MONTH, POSTED, "Y"),
    # null status: C1 current would become 2, so C1 would stop being a decrease
    ("C1", "13.00", "REMITTANCE", CURRENT_MONTH, None, NOT_REVERSED),
    # null reversal state: C1 previous would become 4
    ("C1", "14.00", "REMITTANCE", PREVIOUS_MONTH, POSTED, None),
    # C4's current is ONLY reversals: without the filter C4 current = 2 = previous, so the customer
    # the question is most about would silently stop being a decrease.
    ("C4", "82.00", "REMITTANCE", CURRENT_MONTH, POSTED, "Y"),
    ("C4", "83.00", "REMITTANCE", CURRENT_MONTH, POSTED, "Y"),
    # C6's ONLY previous row is reversed: without the filter C6 reads 1 -> 0, an invented decrease.
    ("C6", "84.00", "CHARGES", PREVIOUS_MONTH, POSTED, "Y"),
)

TRANSACTIONS: tuple[tuple[str | None, str, str, str, str | None, str | None], ...] = tuple(
    (cif, amt, kind, month, POSTED, NOT_REVERSED) for cif, amt, kind, month in _ELIGIBLE
) + _INELIGIBLE

#: Counted by hand. Referenced by tests so an implementation cannot quietly redefine "correct".
EXPECTED = {
    "customer_rows": 6,
    "customer_cif_distinct": 6,
    "transaction_rows": len(TRANSACTIONS),            # 20 eligible + 7 ineligible = 27
    "eligible_rows": len(_ELIGIBLE),                  # 20
    # raw transactions.cif_id: C1,C2,C3,C4,C5,C6,C9 = 7 distinct, 2 NULL
    "transaction_cif_distinct": 7,
    "transaction_cif_nulls": 2,
    "unmatched_ids": 1,                               # C9 has no customer row
    "matched_ids": 6,
    # raw rows per customer: C1 4+2=6, C3 5+1=6, C2 4+1=5, C4 2+2=4, C5 2, C6 1, C9 1
    "max_rows_per_customer": 6,
    # the pilot question, over ELIGIBLE rows only
    "decreased_customers": ("C1", "C4"),
    "decreased_by_segment": {"RETAIL": 1, "CORPORATE": 1},
}


def create_pilot_tables(conn) -> None:
    """Create and populate both tables. `tran_month` stands in for a Hive partition column."""
    conn.execute(f"CREATE SCHEMA IF NOT EXISTS {CUSTOMER_SCHEMA}")
    conn.execute(f"DROP TABLE IF EXISTS {TRANSACTION_SCHEMA}.{TRANSACTION_TABLE}")
    conn.execute(f"DROP TABLE IF EXISTS {CUSTOMER_SCHEMA}.{CUSTOMER_TABLE}")
    conn.execute(
        f"CREATE TABLE {CUSTOMER_SCHEMA}.{CUSTOMER_TABLE} ("
        "  cif_id text, segment text, sector text)")
    conn.execute(
        f"CREATE TABLE {TRANSACTION_SCHEMA}.{TRANSACTION_TABLE} ("
        "  cif_id text, tran_amt numeric, tran_type text, tran_month text,"
        "  tran_status text, reversal_flag text)")
    for row in CUSTOMERS:
        conn.execute(
            f"INSERT INTO {CUSTOMER_SCHEMA}.{CUSTOMER_TABLE} VALUES (%s, %s, %s)", row)
    for row in TRANSACTIONS:
        conn.execute(
            f"INSERT INTO {TRANSACTION_SCHEMA}.{TRANSACTION_TABLE} "
            "VALUES (%s, %s, %s, %s, %s, %s)", row)
