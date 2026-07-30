"""The pilot question against a REAL Hive engine — Release 3's remaining deliverable, minus the bank.

Skipped unless a HiveServer2 is reachable on `HIVE_TEST_HOST:HIVE_TEST_PORT` (default
localhost:10000). Bring one up with `deploy/hive-local/hive-up.sh`.

**What this proves and what it cannot.** It proves the ENGINE and the DIALECT: that the compiled
statement parses as HiveQL, that a partition is really a partition, and that a real HiveServer2
returns the same per-customer counts a human worked out by hand. It says nothing about whether the
catalog describes the bank's tables correctly — that needs the bank's tables, and no local container
substitutes for it.

It is worth the container anyway, because the defects in this layer do not raise. A quoting mistake
made the same plan mean something else and return a confident wrong answer; the suite was green
throughout, because every test of the compiler ran against PostgreSQL.

The connection is opened through `connection.open_connection` rather than by calling the driver
directly, so the governed path — allowlist, principal, credential resolution, driver table — is what
gets exercised. Reaching Hive through a hand-rolled `pyhive.connect` would have skipped past the
`password=` defect that made the governed path unusable for Kerberos.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from featuregen.data_agent.analysis import run_analysis
from featuregen.data_agent.connection import DataSourceConnectionV1, open_connection
from featuregen.data_agent.sql_hive import HiveDialect
from tests.featuregen.data_agent.hive_pilot_fixture import (
    HIVE_DATABASE,
    TRANSACTION_TABLE,
    script,
)
from tests.featuregen.data_agent.pilot_fixture import EXPECTED
from tests.featuregen.data_agent.test_analysis_ir import _ir

_HOST = os.environ.get("HIVE_TEST_HOST", "localhost")
_PORT = int(os.environ.get("HIVE_TEST_PORT", "10000"))
_CONTAINER = os.environ.get("HIVE_CONTAINER", "featuregen-hive")


#: These drive a real engine, so they need more than the suite's default per-test ceiling. The probe
#: below is what keeps that from becoming a way to hang CI.
pytestmark = pytest.mark.timeout(300)

#: A wedged HiveServer2 is the case that matters. The container reports `Up` while the server answers
#: nothing, so an unbounded probe turns "no engine available" — which must SKIP — into a suite
#: failure. Observed exactly that: a container up for 22 hours with beeline hanging past two minutes.
_PROBE_TIMEOUT_S = 20
_LOAD_TIMEOUT_S = 180


def _beeline(sql: str, *, timeout: int = _LOAD_TIMEOUT_S) -> str:
    """Run SQL inside the container. Setup deliberately does NOT go through the Python driver — a
    driver-side quirk that mangled identifiers would otherwise write and read the same wrong thing
    and hide itself."""
    done = subprocess.run(
        ["docker", "exec", "-i", _CONTAINER, "beeline", "-u", "jdbc:hive2://localhost:10000",
         "--silent=true", "-f", "/dev/stdin"],
        input=sql, text=True, capture_output=True, timeout=timeout)
    if done.returncode != 0:
        raise AssertionError(f"beeline failed:\n{done.stdout}\n{done.stderr}")
    return done.stdout


@pytest.fixture(scope="module")
def hive():
    """A governed connection to the local engine, with the pilot tables loaded."""
    spec = DataSourceConnectionV1(
        connection_id="hive-local", environment_id="dev", kind="hive",
        host=_HOST, port=_PORT, auth_mechanism="none",
        secret_ref="local://none", execution_principal="hive",
        allowed_schemas=frozenset({HIVE_DATABASE}), active=True)
    try:
        # Bounded probe FIRST. `subprocess.TimeoutExpired` is an ordinary exception and lands in the
        # skip below; a probe left unbounded would instead be killed by pytest-timeout, which no
        # `except` can turn into a skip.
        _beeline("SELECT 1;", timeout=_PROBE_TIMEOUT_S)
        conn = open_connection(spec, secret_resolver=lambda ref: "")
        conn.cursor().execute("SELECT 1")
    except Exception as exc:                                  # noqa: BLE001 - any failure = no engine
        pytest.skip(f"no usable HiveServer2 at {_HOST}:{_PORT} ({type(exc).__name__}: {exc}); "
                    "run deploy/hive-local/hive-up.sh")
    _beeline(script())
    return conn


# ── the namespace question static reading cannot settle ──────────────────────────────────────────

def test_the_compiled_table_reference_is_a_name_hive_accepts(hive):
    """`HiveDialect.table_ref` emits `database.schema.table`, and `physical.py` justifies the
    three-part address explicitly — "two same-named schemas can live in different Hive databases".
    But HiveQL's namespace is database-and-table: `CREATE SCHEMA` is an alias for `CREATE DATABASE`,
    with no level in between.

    Only the engine settles it, and the answer decides whether every analysis and profile statement
    this system emits is well-formed. Asserted through the compiler rather than by hand so it is the
    SHIPPING name that is tested.
    """
    class _Shim:
        def __init__(self, b): self.binding = b
    ref = HiveDialect().table_ref(_Shim(_ir().event_binding))
    cursor = hive.cursor()
    cursor.execute(f"SELECT COUNT(*) FROM {ref}")
    assert cursor.fetchone()[0] == EXPECTED["transaction_rows"]


# ── the partition is real, not a stand-in ────────────────────────────────────────────────────────

def test_tran_month_is_an_actual_PARTITION_on_this_engine(hive):
    """On Postgres `tran_month` is an ordinary column standing in for a partition. Release 1 says
    "partition pruning is part of the plan, not an optimisation", and that claim is only meaningful
    against a table that is genuinely partitioned."""
    parts = _beeline(f"SHOW PARTITIONS {HIVE_DATABASE}.{TRANSACTION_TABLE};")
    assert "tran_month=2026-05" in parts and "tran_month=2026-06" in parts


# ── THE result: the same answer as the hand-counted fixture ──────────────────────────────────────

def test_the_pilot_question_returns_the_HAND_RECONCILED_answer(hive):
    """The deliverable. Two engines, one typed plan, one set of numbers a human worked out — and the
    numbers must be identical, or the IR has stopped being the artifact of record."""
    rows = run_analysis(hive, _ir(), dialect=HiveDialect())
    by_key = {r.key: r for r in rows}
    assert len(rows) == EXPECTED["customer_rows"]
    assert (by_key["C1"].previous_count, by_key["C1"].current_count) == (3, 1)
    assert (by_key["C2"].previous_count, by_key["C2"].current_count) == (2, 2)
    assert (by_key["C3"].previous_count, by_key["C3"].current_count) == (1, 4)
    assert (by_key["C4"].previous_count, by_key["C4"].current_count) == (2, 0)
    assert tuple(sorted(r.key for r in rows if r.decreased)) == EXPECTED["decreased_customers"]


def test_the_customer_who_fell_to_zero_survives_on_hive_too(hive):
    """C4 is the whole reason the population spine exists, and the case a wrong join loses. It is
    also the case the quoting defect would have destroyed — with every count zero, C4 would have
    read as "unchanged" alongside everyone else."""
    rows = run_analysis(hive, _ir(), dialect=HiveDialect())
    c4 = next(r for r in rows if r.key == "C4")
    assert (c4.previous_count, c4.current_count) == (2, 0)
    assert c4.decreased


def test_the_counts_are_not_all_zero(hive):
    """The specific shape of the quoting bug, pinned on the engine that exhibits it. A green suite
    with every count zero is exactly what would have shipped."""
    rows = run_analysis(hive, _ir(), dialect=HiveDialect())
    assert any(r.previous_count or r.current_count for r in rows)


def test_the_dimensions_resolve_at_the_cutoff_on_hive(hive):
    """Point-in-time attribution goes through the same quoting seam as everything else, so it needs
    the same engine-level check: C1 changed segment BEFORE the cutoff, C4's new row starts exactly
    on it."""
    rows = run_analysis(hive, _ir(), dialect=HiveDialect())
    by_key = {r.key: r for r in rows}
    assert by_key["C1"].dimensions["segment"] == EXPECTED["segment_at_cutoff"]["C1"]
    assert by_key["C4"].dimensions["segment"] == EXPECTED["segment_at_cutoff"]["C4"]
