"""Direct SQL executor + hand-computed validation — Release 1 steps 3 and 4.

**Why this runs against PostgreSQL.** Step 4 asks for validation against a small local fixture with
hand-computed answers, and there is no Hive here. Running the same `ObservationPlanV1` through a
second dialect against a real database proves three things at once:

1. the plan → dialect → executor → typed-result path works end to end;
2. the arithmetic is right, checked against ten rows counted by hand below;
3. the dialect abstraction actually abstracts — §3c's claim that the ontology cannot tell which
   executor produced its evidence is only credible once a second one exists.

So when Hive access arrives, the *only* unproven piece is Hive's SQL text — not the pipeline.

The fixture is deliberately tiny and its expected values are written out longhand, so a wrong
result is obvious rather than plausible.
"""
from __future__ import annotations

import pytest

from featuregen.data_agent.executor import DirectSqlExecutor
from featuregen.data_agent.observation import ObservationPlanV1, PartitionSelector
from featuregen.data_agent.physical import PhysicalDatasetBindingV1, PhysicalObjectIdentityV1
from featuregen.data_agent.profile_policy import ProfilePolicyV1
from featuregen.data_agent.sql_postgres import PostgresDialect

# ── the fixture, counted by hand ────────────────────────────────────────────────────────────────
#
#  cif_id   tran_amt  tran_type   tran_date
#  ------   --------  ---------   ----------
#  C1        10.50    REMITTANCE  2026-06-01
#  C1        20.00    REMITTANCE  2026-06-01
#  C2        30.00    CHARGES     2026-06-01
#  C2        40.00    CHARGES     2026-06-02
#  C3        50.00    REMITTANCE  2026-06-02
#  NULL      60.00    CHARGES     2026-06-02
#  NULL      70.00    REMITTANCE  2026-06-02
#  C4        80.00    CHARGES     2026-06-03   <- outside the selected partitions
#  C5        90.00    CHARGES     2026-06-03   <- outside
#  C5       100.00    CHARGES     2026-06-03   <- outside
#
# Selecting 2026-06-01 and 2026-06-02 gives SEVEN rows:
#   row_count            = 7
#   cif_id non-null      = 5   (two NULLs)
#   cif_id distinct      = 3   (C1, C2, C3)
#   tran_amt non-null    = 7
#   tran_amt min / max   = 10.50 / 70.00
#   tran_type distinct   = 2   (REMITTANCE, CHARGES)

_ROWS = [
    ("C1", "10.50", "REMITTANCE", "2026-06-01"), ("C1", "20.00", "REMITTANCE", "2026-06-01"),
    ("C2", "30.00", "CHARGES", "2026-06-01"), ("C2", "40.00", "CHARGES", "2026-06-02"),
    ("C3", "50.00", "REMITTANCE", "2026-06-02"), (None, "60.00", "CHARGES", "2026-06-02"),
    (None, "70.00", "REMITTANCE", "2026-06-02"), ("C4", "80.00", "CHARGES", "2026-06-03"),
    ("C5", "90.00", "CHARGES", "2026-06-03"), ("C5", "100.00", "CHARGES", "2026-06-03"),
]


@pytest.fixture
def fixture_table(db):
    db.execute("CREATE SCHEMA IF NOT EXISTS dpl_eib")
    db.execute("DROP TABLE IF EXISTS dpl_eib.tran_repos")
    db.execute("CREATE TABLE dpl_eib.tran_repos ("
               "  cif_id text, tran_amt numeric, tran_type text, tran_date text)")
    for row in _ROWS:
        db.execute("INSERT INTO dpl_eib.tran_repos VALUES (%s, %s, %s, %s)", row)
    return db


def _plan(**over) -> ObservationPlanV1:
    identity = PhysicalObjectIdentityV1(catalog_source="ftr", database="featuregen_test",
                                        schema="dpl_eib", table="tran_repos", object_kind="table")
    binding = PhysicalDatasetBindingV1(
        binding_id="b-1", catalog_logical_ref="ftr::dpl_eib.tran_repos",
        connection_id="local-pg", identity=identity,
        partition_columns=("tran_date",), business_time_column="tran_date")
    kw = dict(binding=binding, columns=("cif_id", "tran_amt", "tran_type"),
              partitions=PartitionSelector("tran_date", ("2026-06-01", "2026-06-02")),
              policy=ProfilePolicyV1(exact_distinct=True), bounds_columns=("tran_amt",))
    kw.update(over)
    return ObservationPlanV1(**kw)


def _run(conn, plan=None):
    return DirectSqlExecutor(conn, PostgresDialect()).observe(plan or _plan())


# ── the arithmetic ───────────────────────────────────────────────────────────────────────────────

def test_the_hand_computed_numbers_come_back_exactly(fixture_table):
    result = _run(fixture_table)
    assert result.row_count == 7, "the three 2026-06-03 rows must be pruned away"
    by_column = {c.column: c for c in result.columns}
    assert by_column["cif_id"].non_null_count == 5
    assert by_column["cif_id"].null_count == 2
    assert by_column["cif_id"].distinct_count == 3
    assert by_column["tran_amt"].non_null_count == 7
    assert by_column["tran_type"].distinct_count == 2


def test_null_rate_is_derived_not_guessed(fixture_table):
    cif = {c.column: c for c in _run(fixture_table).columns}["cif_id"]
    assert cif.null_rate == pytest.approx(2 / 7)


def test_value_bounds_appear_only_for_the_opted_in_column(fixture_table):
    by_column = {c.column: c for c in _run(fixture_table).columns}
    assert by_column["tran_amt"].minimum == "10.50" and by_column["tran_amt"].maximum == "70.00"
    assert by_column["cif_id"].minimum is None, "an identifier's bounds are real customer ids"


# ── partition pruning is real, not decorative ────────────────────────────────────────────────────

def test_pruning_actually_excludes_rows(fixture_table):
    """The whole table is 10 rows; the plan selects two partitions covering 7. If pruning were
    cosmetic this would read 10 and every downstream statistic would be quietly wrong."""
    everything = _plan(partitions=PartitionSelector(
        "tran_date", ("2026-06-01", "2026-06-02", "2026-06-03")))
    assert _run(fixture_table).row_count == 7
    assert _run(fixture_table, everything).row_count == 10


# ── the typed result ─────────────────────────────────────────────────────────────────────────────

def test_the_result_carries_its_input_snapshot_and_method(fixture_table):
    """§3c: the ontology must not be able to tell WHICH executor produced its evidence — so the
    result has to state what was read and how, not just the numbers."""
    result = _run(fixture_table)
    assert result.physical_id.endswith("dpl_eib::tran_repos")
    assert result.partitions_read == ("2026-06-01", "2026-06-02")
    assert result.method == "exact"
    assert result.complete is True and result.failures == ()


def test_the_result_reports_what_the_ENGINE_did_not_what_was_asked(fixture_table):
    """PostgreSQL has no approximate distinct, so an APPROXIMATE request runs a census here. The
    result must report the census.

    An earlier version of this test asserted `approximate` and so enshrined a lie: the number was
    exact, and a downstream consumer would have refused a uniqueness proof it could legitimately
    have made. Overstating imprecision is still misstating the evidence."""
    result = _run(fixture_table, _plan(policy=ProfilePolicyV1(exact_distinct=False),
                                       bounds_columns=("tran_amt",)))
    assert result.method == "exact"


def test_the_result_carries_no_raw_rows(fixture_table):
    """The egress boundary, asserted on the object that crosses it. Only the opted-in bounds may
    contain a value, and only for the columns that asked."""
    result = _run(fixture_table)
    text = repr(result)
    for identifier in ("C1", "C2", "C3", "REMITTANCE", "CHARGES"):
        assert identifier not in text, f"{identifier!r} leaked into the observation result"


# ── failure is reported, not raised ──────────────────────────────────────────────────────────────

def test_a_missing_column_is_a_typed_failure_not_a_crash(fixture_table):
    """A partial observation must report exact coverage and never masquerade as complete — a run
    that raises loses the columns that did succeed."""
    result = _run(fixture_table, _plan(columns=("cif_id", "no_such_column"),
                                       bounds_columns=()))
    assert result.complete is False
    assert result.failures and "no_such_column" in result.failures[0]
    assert result.columns == (), "a failed statement yields no half-parsed statistics"


def test_the_statement_timeout_is_applied(fixture_table):
    """One observation must not pin a connection. Read back off the same transaction, since
    SET LOCAL is transaction-scoped."""
    _run(fixture_table, _plan(policy=ProfilePolicyV1(statement_timeout_ms=1234,
                                                     exact_distinct=True)))
    assert fixture_table.execute("SHOW statement_timeout").fetchone()[0] == "1234ms"
