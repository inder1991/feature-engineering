"""The pilot fixture again, on a real Hive engine.

Same rows and the same hand-counted `EXPECTED` as `pilot_fixture`, so the two engines are answering
one question and any disagreement is a dialect or engine defect rather than a data difference. Only
the DDL and the loading differ, in three ways that are the point of running it here at all:

* **`tran_month` is a REAL partition column**, not an ordinary one. On Postgres it merely stands in
  for a partition; here the release's "partition pruning is part of the plan, not an optimisation"
  becomes something a query plan can be inspected for.
* **Hive types** — `string` and `decimal`, not `text` and `numeric`.
* **Hive namespacing** — a database and a table, with no third level. Whether the analysis compiler
  may emit `database.schema.table` is exactly the question this fixture exists to settle.

Loading goes through beeline rather than the Python driver on purpose: the driver's job in this
exercise is to run the ANALYSIS, and using it for setup too would let a driver-side quoting quirk
mask itself by writing and reading the same wrong thing.
"""
from __future__ import annotations

from tests.featuregen.data_agent.pilot_fixture import (
    CUSTOMERS,
    DIMENSION_HISTORY,
    TRANSACTIONS,
)

#: Hive has a database and a table, and no level in between. The Postgres fixture's SCHEMA is what
#: becomes the Hive database, because that is the name closest to the table.
HIVE_DATABASE = "dpl_eib"
CUSTOMER_TABLE = "customer_master"
DIMENSION_TABLE = "customer_segment_history"
TRANSACTION_TABLE = "tran_repos"


def _lit(value: str | None) -> str:
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def ddl() -> tuple[str, ...]:
    """Create the three tables. `tran_month` is declared as a partition, so it is deliberately
    ABSENT from the transaction table's column list — Hive carries a partition key outside the
    schema, which is why a partitioned table cannot be created by translating the Postgres DDL."""
    return (
        f"CREATE DATABASE IF NOT EXISTS {HIVE_DATABASE}",
        f"DROP TABLE IF EXISTS {HIVE_DATABASE}.{TRANSACTION_TABLE}",
        f"DROP TABLE IF EXISTS {HIVE_DATABASE}.{CUSTOMER_TABLE}",
        f"DROP TABLE IF EXISTS {HIVE_DATABASE}.{DIMENSION_TABLE}",
        f"CREATE TABLE {HIVE_DATABASE}.{CUSTOMER_TABLE} (cif_id string)",
        f"CREATE TABLE {HIVE_DATABASE}.{DIMENSION_TABLE} ("
        "  cif_id string, segment string, sector string,"
        "  effective_from string, effective_to string)",
        f"CREATE TABLE {HIVE_DATABASE}.{TRANSACTION_TABLE} ("
        "  cif_id string, tran_amt decimal(18,2), tran_type string,"
        "  tran_status string, reversal_flag string)"
        " PARTITIONED BY (tran_month string)",
    )


def inserts() -> tuple[str, ...]:
    """One multi-row INSERT per table, and one per partition for the transactions.

    STATIC partitioning — a separate `PARTITION (tran_month='…')` per month — rather than dynamic.
    Dynamic partitioning needs a session setting and takes the partition column from the end of the
    value list, which is two more things to get wrong in a fixture whose only job is to be
    obviously correct. Static also names each partition explicitly, so `SHOW PARTITIONS` asserting
    they exist is checking the loader did what it said.
    """
    customers = ", ".join(f"({_lit(c[0])})" for c in CUSTOMERS)
    history = ", ".join(
        "(" + ", ".join(_lit(v) for v in row) + ")" for row in DIMENSION_HISTORY)

    statements = [
        f"INSERT INTO {HIVE_DATABASE}.{CUSTOMER_TABLE} VALUES {customers}",
        f"INSERT INTO {HIVE_DATABASE}.{DIMENSION_TABLE} VALUES {history}",
    ]
    # pilot_fixture row order is (cif, amt, type, month, status, reversal); the month becomes the
    # partition and so leaves the value list entirely.
    for month in sorted({row[3] for row in TRANSACTIONS}):
        values = ", ".join(
            f"({_lit(cif)}, {amt}, {_lit(kind)}, {_lit(status)}, {_lit(rev)})"
            for cif, amt, kind, m, status, rev in TRANSACTIONS if m == month)
        statements.append(
            f"INSERT INTO {HIVE_DATABASE}.{TRANSACTION_TABLE} "
            f"PARTITION (tran_month='{month}') VALUES {values}")
    return tuple(statements)


def script() -> str:
    """The whole setup as one beeline script."""
    return ";\n".join(ddl() + inserts()) + ";\n"
