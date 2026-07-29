"""The analysis compiler against BOTH dialects — the bug a Postgres-only test suite cannot see.

Release 3 says "compile to direct Hive SQL for this slice". Every test of `compile_analysis` ran
against `PostgresDialect`, and the compiler quoted its column names with a module-level helper that
hardcoded double quotes. Table names came from the dialect; column names did not.

**In HiveQL a double-quoted token is a STRING LITERAL, not an identifier.** So the pilot query
compiled for Hive did not fail — it silently meant something else:

    SELECT "cif_id" AS k ... GROUP BY "cif_id"    -- selects the constant 'cif_id', ONE group
    WHERE "tran_month" IN ('2026-05')             -- compares 'tran_month' to '2026-05', never true
    AND "cif_id" IS NOT NULL                      -- a literal is never null, always true

Each period CTE would have matched zero rows, every customer would have come back
`previous_count = 0, current_count = 0`, and the answer to "whose transactions decreased" would have
been a confident, well-formed, entirely wrong "nobody". No error, no empty result, nothing to
notice — which is the specific class of defect the release's "a real run against Hive, and a
validated tabular result" exists to catch.

These tests assert the property directly, so it stays caught without a cluster.
"""
from __future__ import annotations

import pytest

from featuregen.data_agent.analysis import compile_analysis
from featuregen.data_agent.sql_hive import HiveDialect
from featuregen.data_agent.sql_postgres import PostgresDialect
from tests.featuregen.data_agent.test_analysis_ir import _ir

_COLUMNS = ("cif_id", "tran_month", "tran_status", "reversal_flag", "segment", "sector",
            "effective_from", "effective_to")


def _hive() -> str:
    return compile_analysis(_ir(), dialect=HiveDialect())


# ── the trap ─────────────────────────────────────────────────────────────────────────────────────

def test_hive_sql_contains_no_double_quoted_token_at_all():
    """The blanket assertion, because naming the columns individually would miss the next one added.
    A double quote anywhere in HiveQL is a string literal, and every place this compiler emits one it
    means an identifier."""
    assert '"' not in _hive()


@pytest.mark.parametrize("column", _COLUMNS)
def test_every_column_is_quoted_as_a_HIVE_identifier(column):
    assert f"`{column}`" in _hive()


def test_the_period_filter_compares_a_COLUMN_to_the_partition_values():
    """The predicate that silently matched nothing. `'tran_month' IN ('2026-05')` is a comparison
    between two constants — always false — so both period CTEs returned no rows and every customer
    read as zero-to-zero."""
    assert "`tran_month` IN ('2026-05')" in _hive()


def test_the_group_by_is_a_COLUMN_not_a_constant():
    """`GROUP BY 'cif_id'` collapses the entire table into one group whose key is the word
    "cif_id" — the per-entity counts the whole analysis is built on would be a single meaningless
    number."""
    assert "GROUP BY `cif_id`" in _hive()


def test_the_eligibility_predicates_are_quoted_by_the_DIALECT_too():
    """`TransactionEligibilityPolicyV1.predicates` takes the quoting function as an argument, so it
    inherited the hardcoded one. Reversal and status filtering is a Release 3 demonstrable; against
    Hive it was comparing string literals."""
    hive = _hive()
    assert "`tran_status` IN ('POSTED')" in hive
    assert "`reversal_flag` IN ('N')" in hive


def test_the_dimension_validity_predicate_is_quoted_by_the_dialect():
    """Point-in-time attribution has the same seam and the same exposure — a validity window
    comparing the literal 'effective_from' to a date classifies every customer identically."""
    hive = _hive()
    assert "`effective_from`" in hive and "`effective_to`" in hive


# ── the other dialect must not move ──────────────────────────────────────────────────────────────

def test_postgres_output_is_unchanged():
    """Routing quoting through the dialect must be behaviour-neutral for the dialect that already
    worked — every hand-reconciled number in the suite depends on this SQL."""
    postgres = compile_analysis(_ir(), dialect=PostgresDialect())
    for column in _COLUMNS:
        assert f'"{column}"' in postgres
    assert "`" not in postgres


def test_the_two_dialects_differ_ONLY_in_quoting():
    """Same plan, same shape. If the two ever diverge structurally, the typed plan has stopped being
    the artifact of record and the executors have started disagreeing about the question."""
    hive = _hive().replace("`", "")
    postgres = compile_analysis(_ir(), dialect=PostgresDialect()).replace('"', "")
    # The table reference is the one legitimate difference: Hive names the database, Postgres
    # selects it via the connection.
    assert hive.replace("featuregen_test.", "") == postgres
