"""The driver seam — Release 1 step 5's plumbing.

Three defects this closes, all of which would have surfaced on the first real Hive run:

1. the executor called `conn.execute(...)`, a **psycopg3 convenience**. Standard DB-API 2.0 — which
   PyHive, impyla and JayDeBeApi all follow — has no such method, so a Hive connection would have
   failed with `AttributeError` before running a single query;
2. the statement timeout was `SET LOCAL statement_timeout`, **PostgreSQL syntax**, which Hive does
   not understand;
3. nothing opened a connection at all. The contract described one; the tests handed over an
   already-open psycopg connection.

The `_DbApiOnly` stand-in below is the load-bearing piece: it deliberately has **no `execute`
method on the connection**, so any regression to the psycopg shortcut fails here rather than on a
bank cluster.
"""
from __future__ import annotations

import pytest

from featuregen.data_agent.connection import ConnectionError_, DataSourceConnectionV1, open_connection
from featuregen.data_agent.executor import DirectSqlExecutor
from featuregen.data_agent.observation import ObservationPlanV1, PartitionSelector
from featuregen.data_agent.physical import PhysicalDatasetBindingV1, PhysicalObjectIdentityV1
from featuregen.data_agent.profile_policy import ProfilePolicyV1
from featuregen.data_agent.sql_hive import HiveDialect
from featuregen.data_agent.sql_postgres import PostgresDialect


class _Cursor:
    def __init__(self, owner): self._owner = owner
    def execute(self, sql, *args): self._owner.statements.append(sql)
    def fetchone(self): return self._owner.row
    def close(self): self._owner.closed_cursors += 1


class _DbApiOnly:
    """A connection exposing ONLY DB-API 2.0 — `cursor()`, and nothing else.

    Deliberately has no `execute`. This is what PyHive and impyla give you, and asserting against it
    is what stops the psycopg shortcut creeping back in.
    """

    def __init__(self, row=None):
        self.statements: list[str] = []
        self.row = row or (7, 5, 3)
        self.closed_cursors = 0

    def cursor(self): return _Cursor(self)


def _plan(**over) -> ObservationPlanV1:
    identity = PhysicalObjectIdentityV1(catalog_source="ftr", database="banking", schema="dpl_eib",
                                        table="tran_repos", object_kind="table")
    binding = PhysicalDatasetBindingV1(
        binding_id="b-1", catalog_logical_ref="ftr::dpl_eib.tran_repos", connection_id="hive-pilot",
        identity=identity, partition_columns=("tran_date",), business_time_column="tran_date")
    kw = dict(binding=binding, columns=("cif_id",),
              partitions=PartitionSelector("tran_date", ("2026-06-01",)),
              policy=ProfilePolicyV1())
    kw.update(over)
    return ObservationPlanV1(**kw)


# ── 1. the executor must not require a psycopg connection ────────────────────────────────────────

def test_the_executor_works_with_a_plain_DBAPI_connection():
    """THE regression guard. This connection has no `execute` method — exactly like a Hive driver."""
    conn = _DbApiOnly()
    result = DirectSqlExecutor(conn, HiveDialect()).observe(_plan())
    assert result.complete is True and result.row_count == 7


def test_the_cursor_is_closed_even_though_the_connection_is_not():
    """The executor borrows a connection it does not own — closing it would break a caller mid
    transaction. It owns the cursor, so it closes that."""
    conn = _DbApiOnly()
    DirectSqlExecutor(conn, HiveDialect()).observe(_plan())
    assert conn.closed_cursors >= 1


# ── 2. the timeout belongs to the engine ─────────────────────────────────────────────────────────

def test_postgres_emits_a_postgres_timeout():
    stmts = PostgresDialect().timeout_statements(_plan(policy=ProfilePolicyV1(
        statement_timeout_ms=4500)))
    assert stmts == ("SET LOCAL statement_timeout = 4500",)


def test_hive_emits_a_hive_timeout_in_SECONDS():
    """Hive's setting is in seconds, not milliseconds. Sending the millisecond number would set a
    timeout roughly a thousand times longer than intended — a bound that silently isn't one."""
    stmts = HiveDialect().timeout_statements(_plan(policy=ProfilePolicyV1(
        statement_timeout_ms=30_000)))
    assert stmts == ("SET hive.query.timeout.seconds=30",)


def test_a_sub_second_timeout_never_rounds_down_to_zero():
    """Zero means "no limit" in Hive. Rounding 400ms to 0s would remove the bound entirely."""
    stmts = HiveDialect().timeout_statements(_plan(policy=ProfilePolicyV1(
        statement_timeout_ms=400)))
    assert stmts == ("SET hive.query.timeout.seconds=1",)


def test_the_executor_runs_whatever_the_dialect_asks_for():
    conn = _DbApiOnly()
    DirectSqlExecutor(conn, HiveDialect()).observe(_plan())
    assert any("hive.query.timeout" in s for s in conn.statements)
    assert not any("statement_timeout" in s for s in conn.statements), "postgres syntax on hive"


# ── 3. opening a connection ──────────────────────────────────────────────────────────────────────

def _conn_spec(**over) -> DataSourceConnectionV1:
    kw = dict(connection_id="hive-pilot", environment_id="uat", kind="hive",
              host="hiveserver2.internal", port=10000, auth_mechanism="kerberos",
              secret_ref="vault://featuregen/hive-pilot", execution_principal="svc_ro",
              allowed_schemas=frozenset({"dpl_eib"}), active=True)
    kw.update(over)
    return DataSourceConnectionV1(**kw)


def test_opening_an_inactive_connection_is_refused_before_any_driver_loads():
    with pytest.raises(ConnectionError_, match="not active"):
        open_connection(_conn_spec(active=False), secret_resolver=lambda ref: "secret")


def test_the_secret_is_resolved_through_the_caller_never_stored():
    """The spec holds a REFERENCE; the credential is fetched at the point of use and never returned
    on the connection object or in an error."""
    seen = []
    def resolver(ref):
        seen.append(ref)
        return "kerberos-ticket"
    with pytest.raises(ConnectionError_) as exc:      # no driver installed in this environment
        open_connection(_conn_spec(), secret_resolver=resolver)
    assert seen == ["vault://featuregen/hive-pilot"]
    assert "kerberos-ticket" not in str(exc.value)


def test_a_missing_driver_is_a_typed_refusal_naming_what_to_install():
    """An ImportError deep in a driver is a bad first experience. Say which package is missing."""
    with pytest.raises(ConnectionError_, match="driver"):
        open_connection(_conn_spec(), secret_resolver=lambda ref: "x")


def test_an_injected_factory_is_used_instead_of_a_real_driver():
    """The seam: a caller may supply the connect callable, so tests and a future engine adapter need
    no dependency on this module's driver table."""
    made = _DbApiOnly()
    conn = open_connection(_conn_spec(), secret_resolver=lambda ref: "x",
                           connect=lambda **kw: made)
    assert conn is made
