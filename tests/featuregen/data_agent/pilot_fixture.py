"""Synthetic pilot tables — a customer table and a transaction table.

**What this is and is not.** It is a FIXTURE: it proves the machinery computes the right answers,
on numbers small enough to count by hand. It is not a stand-in for real data and cannot be — it
says nothing about whether the catalog's description of the bank's tables is accurate, which is the
class of defect that matters most and can only be found against the real thing.

It is shaped around the pilot question:

    customers whose transaction count decreased in the current month versus the previous month,
    by segment and sector

and it deliberately contains the cases that are easy to get wrong:

* **C4 drops to ZERO** in the current month. A naive inner join loses this customer entirely — and
  they are precisely the customer the question is about. This is the case the population spine
  exists for, and the one an earlier analysis prototype of mine would have silently dropped.
* **C5 appears only in the current month** (zero previous). "Decreased" must not be true here.
* **C9 has transactions but no customer row** — an unmatched key, so referential coverage is
  provably below 1.0.
* **NULL customer ids** exist, so a null rate is non-zero.
* **customers.cif_id is unique; transactions.cif_id is not** — the two sides of a join have
  genuinely different uniqueness, which is what makes an observed join multiplier meaningful.
* Two segments and three sectors, so a group-by has more than one bucket and small-cell suppression
  has something to bite on.

Hand-computed expectations live in :data:`EXPECTED` so a wrong answer is obvious rather than
plausible.
"""
from __future__ import annotations

CUSTOMER_SCHEMA = "dpl_eib"
CUSTOMER_TABLE = "customer_master"
TRANSACTION_SCHEMA = "dpl_eib"
TRANSACTION_TABLE = "tran_repos"

PREVIOUS_MONTH = "2026-05"
CURRENT_MONTH = "2026-06"

#: cif_id, segment, sector
CUSTOMERS: tuple[tuple[str, str, str], ...] = (
    ("C1", "RETAIL", "TRADING"),
    ("C2", "RETAIL", "MANUFACTURING"),
    ("C3", "CORPORATE", "TRADING"),
    ("C4", "CORPORATE", "REAL_ESTATE"),
    ("C5", "RETAIL", "TRADING"),
)

#: cif_id, tran_amt, tran_type, tran_month
#
#   customer   previous   current    verdict
#   --------   --------   -------    -----------------------------------------
#   C1            3          1       DECREASED
#   C2            2          2       unchanged
#   C3            1          4       increased
#   C4            2          0       DECREASED  <- vanishes under an inner join
#   C5            0          2       increased  <- no previous period at all
#   C9            0          1       unmatched: no customer row exists
#   NULL          1          1       null customer id
TRANSACTIONS: tuple[tuple[str | None, str, str, str], ...] = (
    # previous month
    ("C1", "10.00", "REMITTANCE", PREVIOUS_MONTH),
    ("C1", "20.00", "REMITTANCE", PREVIOUS_MONTH),
    ("C1", "30.00", "CHARGES", PREVIOUS_MONTH),
    ("C2", "40.00", "CHARGES", PREVIOUS_MONTH),
    ("C2", "50.00", "REMITTANCE", PREVIOUS_MONTH),
    ("C3", "60.00", "CHARGES", PREVIOUS_MONTH),
    ("C4", "70.00", "REMITTANCE", PREVIOUS_MONTH),
    ("C4", "80.00", "REMITTANCE", PREVIOUS_MONTH),
    (None, "90.00", "CHARGES", PREVIOUS_MONTH),
    # current month
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

#: Counted by hand from the tables above. Referenced by tests so an implementation cannot quietly
#: redefine what "correct" means.
EXPECTED = {
    "customer_rows": 5,
    "customer_cif_distinct": 5,          # unique: 5 distinct over 5 rows
    "transaction_rows": len(TRANSACTIONS),           # 20
    "current_rows": 11,
    "previous_rows": 9,
    # transactions.cif_id over BOTH months: C1,C2,C3,C4,C5,C9 = 6 distinct, 2 NULL
    "transaction_cif_distinct": 6,
    "transaction_cif_nulls": 2,
    # referential coverage: of the 6 distinct non-null ids, C9 has no customer row
    "unmatched_ids": 1,
    "matched_ids": 5,
    # the pilot question
    # per-customer transaction totals across BOTH months:
    #   C1 3+1=4   C2 2+2=4   C3 1+4=5   C4 2+0=2   C5 0+2=2   C9 0+1=1
    "max_rows_per_customer": 5,          # C3
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
        "  cif_id text, tran_amt numeric, tran_type text, tran_month text)")
    for row in CUSTOMERS:
        conn.execute(
            f"INSERT INTO {CUSTOMER_SCHEMA}.{CUSTOMER_TABLE} VALUES (%s, %s, %s)", row)
    for row in TRANSACTIONS:
        conn.execute(
            f"INSERT INTO {TRANSACTION_SCHEMA}.{TRANSACTION_TABLE} VALUES (%s, %s, %s, %s)", row)
