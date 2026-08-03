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

from featuregen.data_agent.physical import PhysicalDatasetBindingV1, PhysicalObjectIdentityV1
from featuregen.data_agent.relationship import RelationshipEvidenceV1

CATALOG_SOURCE = "ftr"
DATABASE = "featuregen_test"
CUSTOMER_SCHEMA = "dpl_eib"
CUSTOMER_TABLE = "customer_master"
#: SCD2 dimension history, SEPARATE from the spine. It cannot be the spine: several history rows per
#: customer would duplicate the population, and the population must be one row per customer.
DIMENSION_TABLE = "customer_segment_history"
#: The SNAPSHOT-shaped dimension source (Release-B Task 8, ENGINE B). The SAME classification, kept
#: the other way a bank keeps it: one row per customer per publication instant rather than an
#: interval. It exists beside the SCD2 table, not instead of it, because the two row rules give
#: DIFFERENT answers and the point of declaring a temporal policy is that nobody has to guess which
#: shape a table is.
SNAPSHOT_TABLE = "customer_segment_snapshot"

#: The instant the customer is classified AT. A business decision, not a rendering detail — see
#: `dimensions.DIMENSION_ATTRIBUTION_AS_OF_UNRESOLVED`.
REPORT_CUTOFF = "2026-06-30"
TRANSACTION_SCHEMA = "dpl_eib"
TRANSACTION_TABLE = "tran_repos"

PREVIOUS_MONTH = "2026-05"
CURRENT_MONTH = "2026-06"

POSTED = "POSTED"
NOT_REVERSED = "N"

#: The POPULATION — one row per customer, no dimensions.
CUSTOMERS: tuple[tuple[str], ...] = (
    ("C1",), ("C2",), ("C3",), ("C4",), ("C5",), ("C6",),
)

#: cif_id, segment, sector, effective_from, effective_to, is_current  (half-open: [from, to))
#
# Each row exists for a specific rule, at cutoff 2026-06-30:
#   C1  changed BEFORE the cutoff              -> SME        (not the old RETAIL)
#   C2  changes AFTER the cutoff               -> RETAIL     (not the future SME)
#   C3  one open-ended row                     -> CORPORATE
#   C4  effective_from == cutoff (INCLUDED) and the prior row's effective_to == cutoff (EXCLUDED),
#       so the two boundary rules must AGREE and select exactly one row -> CORPORATE
#   C5  only a FUTURE-dated row                -> no valid row -> Unknown
#   C6  no dimension row at all                -> Unknown
#
# `is_current` (Release-B Task 8, ENGINE A) is the SOURCE's own declaration of which row is today's
# — the thing whose absence is why `current_value` attribution was refused until now. C7 is the
# customer that makes the declaration MATTER: its only row is CLOSED on a scheduled future date, so
# the table has no open-ended row for it at all and the two conventions disagree. A loader that
# closes every row on a rollover date is a real shape, and an engine inferring "current = the row
# with no end date" answers a different question there.
DIMENSION_HISTORY: tuple[tuple[str, str, str, str, str | None, bool], ...] = (
    ("C1", "RETAIL", "TRADING", "2020-01-01", "2026-06-15", False),
    ("C1", "SME", "TRADING", "2026-06-15", None, True),
    ("C2", "RETAIL", "MANUFACTURING", "2020-01-01", "2026-07-15", False),
    ("C2", "SME", "MANUFACTURING", "2026-07-15", None, True),
    ("C3", "CORPORATE", "TRADING", "2020-01-01", None, True),
    ("C4", "RETAIL", "REAL_ESTATE", "2020-01-01", "2026-06-30", False),
    ("C4", "CORPORATE", "REAL_ESTATE", "2026-06-30", None, True),
    ("C5", "RETAIL", "TRADING", "2026-08-01", None, True),
    # C6: deliberately absent
    # C7: CLOSED on a scheduled date — flagged current, but no open-ended row exists for it
    ("C7", "CORPORATE", "SHIPPING", "2020-01-01", "2027-01-01", True),
    # an unknown customer's history must not invent a population member
    ("C9", "CORPORATE", "TRADING", "2020-01-01", None, True),
)

#: cif_id, segment, sector, snapshot_date, load_seq — the SNAPSHOT shape (ENGINE B).
#
# At cutoff 2026-06-30:
#   C1  two snapshots, the later one before the cutoff        -> SME
#   C2  a pre-cutoff snapshot and a POST-cutoff one           -> RETAIL (the later is unknowable)
#   C3  one snapshot                                          -> CORPORATE
#   C4  TWO rows stamped on the SAME snapshot date            -> a TIE: refused, unless the policy
#       declares a tie-breaker. `load_seq` separates them (2 wins); `sector` does NOT, which is what
#       makes "the tie_break_refs resolve it deterministically" a testable claim.
#   C5  only a POST-cutoff snapshot                           -> absent at the cutoff
#   C6  no snapshot at all                                    -> Unknown bucket, never dropped
SEGMENT_SNAPSHOTS: tuple[tuple[str, str, str, str, int], ...] = (
    ("C1", "RETAIL", "TRADING", "2026-05-31", 1),
    ("C1", "SME", "TRADING", "2026-06-30", 1),
    ("C2", "RETAIL", "MANUFACTURING", "2026-06-30", 1),
    ("C2", "SME", "MANUFACTURING", "2026-07-31", 1),
    ("C3", "CORPORATE", "TRADING", "2026-06-30", 1),
    ("C4", "RETAIL", "REAL_ESTATE", "2026-06-30", 1),
    ("C4", "CORPORATE", "REAL_ESTATE", "2026-06-30", 2),
    ("C5", "RETAIL", "TRADING", "2026-07-31", 1),
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
    # segments AT THE CUTOFF: C1 changed to SME before it; C4's new row starts exactly on it
    "decreased_by_segment": {"SME": 1, "CORPORATE": 1},
    #: what each customer classifies as at REPORT_CUTOFF
    "segment_at_cutoff": {"C1": "SME", "C2": "RETAIL", "C3": "CORPORATE", "C4": "CORPORATE",
                          "C5": "Unknown", "C6": "Unknown"},
}


def binding(schema: str, table: str) -> PhysicalDatasetBindingV1:
    """The fixture's own physical bindings. Owned here rather than in a test module so the join
    evidence below and the analysis plans under test cannot end up describing different tables."""
    identity = PhysicalObjectIdentityV1(catalog_source=CATALOG_SOURCE, database=DATABASE,
                                        schema=schema, table=table, object_kind="table")
    return PhysicalDatasetBindingV1(
        binding_id=f"b-{table}", catalog_logical_ref=f"{CATALOG_SOURCE}::{schema}.{table}",
        connection_id="local-pg", identity=identity)


#: The pilot join, as OBSERVED evidence — transactions.cif_id referencing customers.cif_id.
#:
#: Written out here beside `EXPECTED` and derived from the same hand-counted numbers, so every test
#: can construct a verified analysis without running a probe first. `test_verified_joins` asserts
#: this equals what an exact probe actually returns, which is what stops it drifting into a
#: hand-written attestation that no longer describes the data.
PILOT_JOIN_EVIDENCE = RelationshipEvidenceV1(
    left_physical_id=binding(TRANSACTION_SCHEMA, TRANSACTION_TABLE).identity.table_id,
    left_binding_revision_id=(
        binding(TRANSACTION_SCHEMA, TRANSACTION_TABLE).binding_revision_id),
    left_binding_content_hash=binding(TRANSACTION_SCHEMA, TRANSACTION_TABLE).content_hash,
    left_column="cif_id",
    right_physical_id=binding(CUSTOMER_SCHEMA, CUSTOMER_TABLE).identity.table_id,
    right_binding_revision_id=binding(CUSTOMER_SCHEMA, CUSTOMER_TABLE).binding_revision_id,
    right_binding_content_hash=binding(CUSTOMER_SCHEMA, CUSTOMER_TABLE).content_hash,
    right_column="cif_id",
    left_rows=EXPECTED["transaction_rows"],
    left_distinct=EXPECTED["transaction_cif_distinct"],
    left_nulls=EXPECTED["transaction_cif_nulls"],
    right_rows=EXPECTED["customer_rows"],
    right_distinct=EXPECTED["customer_cif_distinct"],
    right_nulls=0,
    matched_distinct=EXPECTED["matched_ids"],
    unmatched_distinct=EXPECTED["unmatched_ids"],
    max_left_rows_per_tuple=EXPECTED["max_rows_per_customer"],
    max_right_matches_per_left_row=1,
    method="exact")


def create_pilot_tables(conn) -> None:
    """Create and populate both tables. `tran_month` stands in for a Hive partition column."""
    conn.execute(f"CREATE SCHEMA IF NOT EXISTS {CUSTOMER_SCHEMA}")
    conn.execute(f"DROP TABLE IF EXISTS {TRANSACTION_SCHEMA}.{TRANSACTION_TABLE}")
    conn.execute(f"DROP TABLE IF EXISTS {CUSTOMER_SCHEMA}.{CUSTOMER_TABLE}")
    conn.execute(f"DROP TABLE IF EXISTS {CUSTOMER_SCHEMA}.{DIMENSION_TABLE}")
    conn.execute(f"DROP TABLE IF EXISTS {CUSTOMER_SCHEMA}.{SNAPSHOT_TABLE}")
    conn.execute(
        f"CREATE TABLE {CUSTOMER_SCHEMA}.{CUSTOMER_TABLE} (cif_id text)")
    conn.execute(
        f"CREATE TABLE {CUSTOMER_SCHEMA}.{DIMENSION_TABLE} ("
        "  cif_id text, segment text, sector text,"
        "  effective_from text, effective_to text, is_current boolean)")
    conn.execute(
        f"CREATE TABLE {CUSTOMER_SCHEMA}.{SNAPSHOT_TABLE} ("
        "  cif_id text, segment text, sector text, snapshot_date text, load_seq int)")
    conn.execute(
        f"CREATE TABLE {TRANSACTION_SCHEMA}.{TRANSACTION_TABLE} ("
        "  cif_id text, tran_amt numeric, tran_type text, tran_month text,"
        "  tran_status text, reversal_flag text)")
    for row in CUSTOMERS:
        conn.execute(f"INSERT INTO {CUSTOMER_SCHEMA}.{CUSTOMER_TABLE} VALUES (%s)", row)
    for row in DIMENSION_HISTORY:
        conn.execute(
            f"INSERT INTO {CUSTOMER_SCHEMA}.{DIMENSION_TABLE} "
            "VALUES (%s, %s, %s, %s, %s, %s)", row)
    for row in SEGMENT_SNAPSHOTS:
        conn.execute(
            f"INSERT INTO {CUSTOMER_SCHEMA}.{SNAPSHOT_TABLE} "
            "VALUES (%s, %s, %s, %s, %s)", row)
    for row in TRANSACTIONS:
        conn.execute(
            f"INSERT INTO {TRANSACTION_SCHEMA}.{TRANSACTION_TABLE} "
            "VALUES (%s, %s, %s, %s, %s, %s)", row)
