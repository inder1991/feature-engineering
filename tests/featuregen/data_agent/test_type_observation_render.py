"""Schema observation — rendering and DB-API transport, proven without a cluster (Task 7).

The attested-type path (ingestion-richness Task 7) upgrades ``graph_node.data_type`` ONLY from a
real engine read. This file proves the engine half cluster-free, per the bridge plan's Task 7
precedent: every safety property is asserted on the SQL TEXT (rendering) or against a plain
DB-API 2.0 stand-in / local PostgreSQL (transport), so when cluster access arrives the only
unproven piece is HiveServer2 accepting text this suite has already pinned.

Two Hive traps this pins:

* double-quoted identifiers are STRING LITERALS in HiveQL — a schema statement using them would be
  accepted and evaluated to something else entirely, so the rendering must be backticked;
* a plain ``DESCRIBE`` on a partitioned table repeats the partition columns under a
  ``# Partition Information`` section — shaping must skip the marker rows and dedupe, or every
  partition column would be observed twice.
"""
from __future__ import annotations

import pytest

from featuregen.data_agent.executor import DirectSqlExecutor
from featuregen.data_agent.observation import ObservationPlanError, SchemaObservationPlanV1
from featuregen.data_agent.physical import PhysicalDatasetBindingV1, PhysicalObjectIdentityV1
from featuregen.data_agent.profile_policy import ProfilePolicyV1
from featuregen.data_agent.sql_hive import HiveDialect
from featuregen.data_agent.sql_postgres import PostgresDialect


def _identity(**over) -> PhysicalObjectIdentityV1:
    kw = dict(catalog_source="ftr", database="banking", schema="dpl_eib",
              table="tran_repos", object_kind="table")
    kw.update(over)
    return PhysicalObjectIdentityV1(**kw)


def _binding(**over) -> PhysicalDatasetBindingV1:
    kw = dict(binding_id="b-1", catalog_logical_ref="ftr::dpl_eib.tran_repos",
              connection_id="hive-pilot", identity=_identity(),
              partition_columns=("tran_date",), business_time_column="tran_date")
    kw.update(over)
    return PhysicalDatasetBindingV1(**kw)


def _plan(**over) -> SchemaObservationPlanV1:
    kw = dict(binding=_binding(), policy=ProfilePolicyV1())
    kw.update(over)
    return SchemaObservationPlanV1(**kw)


# ── rendering: Hive ──────────────────────────────────────────────────────────────────────────────

def test_hive_renders_a_backticked_two_part_DESCRIBE():
    sql = HiveDialect().render_schema_observation(_plan())
    assert sql == "DESCRIBE `dpl_eib`.`tran_repos`"


def test_hive_never_emits_a_double_quoted_identifier():
    """Double quotes are STRING LITERALS in HiveQL — the statement would be accepted and silently
    wrong, which is a worse failure mode than a syntax error."""
    sql = HiveDialect().render_schema_observation(_plan())
    assert '"' not in sql


def test_hive_schema_statement_names_two_parts_not_three():
    """A real HiveServer2 refuses `database.schema.table` (SemanticException [Error 10001]) — the
    same two-part discipline as `table_ref` for profiles."""
    sql = HiveDialect().render_schema_observation(_plan())
    assert "`banking`" not in sql, "the identity's database is an ADDRESS, never a SQL name part"


def test_hive_schema_read_needs_no_partition_selector():
    """A DESCRIBE is a metadata read — no scan, so an unpartitioned-style plan over a partitioned
    table must be constructible (unlike ObservationPlanV1, which rightly refuses)."""
    plan = _plan()   # partitioned binding, no selector anywhere on the plan
    assert "tran_date" not in HiveDialect().render_schema_observation(plan)


# ── rendering: Postgres ──────────────────────────────────────────────────────────────────────────

def test_postgres_reads_information_schema_with_literal_names():
    sql = PostgresDialect().render_schema_observation(_plan())
    assert "information_schema.columns" in sql
    assert "table_schema = 'dpl_eib'" in sql
    assert "table_name = 'tran_repos'" in sql
    assert "ORDER BY ordinal_position" in sql


def test_an_unsafe_schema_or_table_name_is_REFUSED_at_plan_construction():
    """The module rule: quoting is not the defence, refusal is. A name that is not a plain
    identifier never reaches either renderer — Hive's backticks and Postgres's literal escaping
    are belt-and-braces behind this, not the defence itself."""
    identity = _identity(schema="dpl'eib")
    binding = _binding(identity=identity, catalog_logical_ref="ftr::dpleib.tran_repos")
    with pytest.raises(ObservationPlanError, match="schema"):
        _plan(binding=binding)


# ── transport: plain DB-API 2.0, the driver seam ─────────────────────────────────────────────────

class _Cursor:
    def __init__(self, owner):
        self._owner = owner

    def execute(self, sql, *args):
        self._owner.statements.append(sql)

    def fetchall(self):
        return self._owner.rows

    def close(self):
        self._owner.closed_cursors += 1


class _DbApiOnly:
    """A connection exposing ONLY DB-API 2.0 — `cursor()`, and nothing else. What PyHive and
    impyla give you; asserting against it stops the psycopg `conn.execute` shortcut creeping in."""

    def __init__(self, rows=None):
        self.statements: list[str] = []
        self.rows = rows if rows is not None else [("cif_id", "string", "")]
        self.closed_cursors = 0

    def cursor(self):
        return _Cursor(self)


def test_observe_schema_works_over_a_plain_DBAPI_connection():
    conn = _DbApiOnly()
    result = DirectSqlExecutor(conn, HiveDialect()).observe_schema(_plan())
    assert result.complete is True
    assert [(c.column, c.engine_type) for c in result.columns] == [("cif_id", "string")]
    assert result.physical_id == "ftr::banking::dpl_eib::tran_repos"


def test_observe_schema_closes_the_cursor_but_not_the_connection():
    conn = _DbApiOnly()
    DirectSqlExecutor(conn, HiveDialect()).observe_schema(_plan())
    assert conn.closed_cursors >= 1


def test_observe_schema_applies_the_engine_timeout_through_the_same_seam():
    conn = _DbApiOnly()
    DirectSqlExecutor(conn, HiveDialect()).observe_schema(
        _plan(policy=ProfilePolicyV1(statement_timeout_ms=30_000)))
    assert any("hive.query.timeout.seconds=30" in s for s in conn.statements)
    assert not any("statement_timeout" in s for s in conn.statements), "postgres syntax on hive"


def test_hive_DESCRIBE_partition_section_is_skipped_and_deduped():
    """Plain DESCRIBE on a partitioned table repeats the partition column under a marker section.
    One physical column must yield ONE observation, and no marker row may masquerade as one."""
    conn = _DbApiOnly(rows=[
        ("cif_id", "string", ""),
        ("tran_amt", "decimal(18,2)", ""),
        ("tran_date", "string", ""),
        ("", None, None),
        ("# Partition Information", None, None),
        ("# col_name", "data_type", "comment"),
        ("tran_date", "string", ""),
    ])
    result = DirectSqlExecutor(conn, HiveDialect()).observe_schema(_plan())
    assert [(c.column, c.engine_type) for c in result.columns] == [
        ("cif_id", "string"), ("tran_amt", "decimal(18,2)"), ("tran_date", "string")]


def test_a_failed_schema_read_is_typed_coverage_not_a_crash():
    class _Boom(_DbApiOnly):
        def cursor(self):
            raise RuntimeError("Table not found tran_repos")

    result = DirectSqlExecutor(_Boom(), HiveDialect()).observe_schema(_plan())
    assert result.complete is False
    assert result.columns == ()
    assert result.failures and "Table not found" in result.failures[0]


# ── transport: the real engine path, against local PostgreSQL ────────────────────────────────────

@pytest.fixture
def pg_fixture_table(db):
    db.execute("CREATE SCHEMA IF NOT EXISTS dpl_eib")
    db.execute("DROP TABLE IF EXISTS dpl_eib.tran_repos")
    db.execute("CREATE TABLE dpl_eib.tran_repos ("
               "  cif_id text, tran_amt numeric(18,2), tran_seq bigint)")
    return db


def _pg_plan() -> SchemaObservationPlanV1:
    identity = PhysicalObjectIdentityV1(catalog_source="ftr", database="featuregen_test",
                                        schema="dpl_eib", table="tran_repos", object_kind="table")
    binding = PhysicalDatasetBindingV1(
        binding_id="b-1", catalog_logical_ref="ftr::dpl_eib.tran_repos",
        connection_id="local-pg", identity=identity)
    return SchemaObservationPlanV1(binding=binding, policy=ProfilePolicyV1())


def test_postgres_reports_the_engine_types_it_actually_holds(pg_fixture_table):
    result = DirectSqlExecutor(pg_fixture_table, PostgresDialect()).observe_schema(_pg_plan())
    assert result.complete is True
    types = {c.column: c.engine_type for c in result.columns}
    assert types["cif_id"] == "text"
    assert types["tran_amt"] == "numeric"
    assert types["tran_seq"] == "bigint"


def test_postgres_schema_read_carries_no_row_data(pg_fixture_table):
    """The egress boundary: a schema observation is names and types, never values."""
    pg_fixture_table.execute(
        "INSERT INTO dpl_eib.tran_repos VALUES ('C-SECRET-1', 10.5, 1)")
    result = DirectSqlExecutor(pg_fixture_table, PostgresDialect()).observe_schema(_pg_plan())
    assert "C-SECRET-1" not in repr(result)
