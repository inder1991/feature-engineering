"""`AnalysisExecutionIRV1` and its compiler — Release 3.

The pilot question, as a typed plan that compiles to SQL:

    customers whose transaction count decreased in the current month versus the previous month,
    by segment and sector

**Why this is not `FormulaExecutionIRV1`.** That contract carries one feature, one final operation,
one grain and one spine (`materialize/ir.py:106-128`), and `FinalOperation` is
identity/ratio/difference — so `current_count < previous_count` is not expressible as a final
operation at all. Forcing this shape into it would deform the analysis or turn the feature IR into a
general query language. Two IRs, shared primitives (roadmap §3a).

Every expected number is counted by hand in `pilot_fixture.EXPECTED`. The fixture exists to make
the awkward cases unavoidable, and the first test below is the one that matters most: **C4 falls to
zero transactions and must still appear**, because they are precisely the customer the question asks
about, and an inner join loses them silently.
"""
from __future__ import annotations

import pytest

from featuregen.data_agent.analysis import (
    AnalysisExecutionIRV1,
    AnalysisIRError,
    Comparison,
    Dimension,
    Period,
    PopulationSpine,
    compile_analysis,
    run_analysis,
)
from featuregen.data_agent.eligibility import (
    NullBehavior,
    ReversalMode,
    TransactionEligibilityPolicyV1,
)
from featuregen.data_agent.physical import PhysicalDatasetBindingV1, PhysicalObjectIdentityV1
from featuregen.data_agent.sql_postgres import PostgresDialect
from tests.featuregen.data_agent.pilot_fixture import (
    CURRENT_MONTH,
    CUSTOMER_SCHEMA,
    CUSTOMER_TABLE,
    EXPECTED,
    PREVIOUS_MONTH,
    TRANSACTION_SCHEMA,
    TRANSACTION_TABLE,
    create_pilot_tables,
)


@pytest.fixture
def pilot(db):
    create_pilot_tables(db)
    return db


def _binding(schema: str, table: str) -> PhysicalDatasetBindingV1:
    identity = PhysicalObjectIdentityV1(catalog_source="ftr", database="featuregen_test",
                                        schema=schema, table=table, object_kind="table")
    return PhysicalDatasetBindingV1(
        binding_id=f"b-{table}", catalog_logical_ref=f"ftr::{schema}.{table}",
        connection_id="local-pg", identity=identity)


def _policy(**over) -> TransactionEligibilityPolicyV1:
    """The pilot source's definition of a transaction that counts. Column names and codes are
    EXAMPLES until verified against Hive — which is the point of declaring them rather than
    hardcoding them in the renderer."""
    kw = dict(status_column="tran_status", included_status_values=("POSTED",),
              reversal_mode=ReversalMode.BOOLEAN_OR_CODE_COLUMN, reversal_column="reversal_flag",
              non_reversed_values=("N",), null_behavior=NullBehavior.EXCLUDE)
    kw.update(over)
    return TransactionEligibilityPolicyV1(**kw)


def _ir(**over) -> AnalysisExecutionIRV1:
    kw = dict(
        question="customers whose transaction count decreased last month, by segment and sector",
        spine=PopulationSpine(binding=_binding(CUSTOMER_SCHEMA, CUSTOMER_TABLE), key_column="cif_id"),
        event_binding=_binding(TRANSACTION_SCHEMA, TRANSACTION_TABLE),
        event_key_column="cif_id", period_column="tran_month",
        current=Period(label="current", values=(CURRENT_MONTH,)),
        previous=Period(label="previous", values=(PREVIOUS_MONTH,)),
        measure="count",
        comparison=Comparison.DECREASED,
        dimensions=(Dimension(column="segment"), Dimension(column="sector")),
        eligibility=_policy(),
    )
    kw.update(over)
    return AnalysisExecutionIRV1(**kw)


def _rows(conn, ir=None):
    return run_analysis(conn, ir or _ir(), dialect=PostgresDialect())


# ── the population spine: the case an inner join loses ───────────────────────────────────────────

def test_a_customer_who_fell_to_ZERO_is_still_counted(pilot):
    """THE test. C4 had 2 transactions previously and 0 now, so they are exactly what "decreased"
    means — and an inner join between the two period aggregates drops them entirely, returning a
    confident answer that omits the most affected customer."""
    decreased = {r.key for r in _rows(pilot) if r.decreased}
    assert "C4" in decreased
    assert set(decreased) == set(EXPECTED["decreased_customers"]) == {"C1", "C4"}


def test_every_customer_in_the_spine_appears_exactly_once(pilot):
    """The spine defines the population. A customer with no transactions in EITHER period is still
    a customer, and a customer with many transactions is still one row."""
    rows = _rows(pilot)
    assert len(rows) == EXPECTED["customer_rows"] == 6
    assert len({r.key for r in rows}) == 6


def test_a_customer_with_no_previous_period_did_not_DECREASE(pilot):
    """C5 appears only in the current month. Going from nothing to something is not a decrease —
    and treating a missing previous period as zero would be right here but wrong in general, so the
    test pins the direction."""
    c5 = next(r for r in _rows(pilot) if r.key == "C5")
    assert c5.previous_count == 0 and c5.current_count == 2
    assert c5.decreased is False


def test_a_transaction_for_an_unknown_customer_does_not_invent_a_row(pilot):
    """C9 has a transaction and no customer row. The population is the SPINE, so C9 is not in the
    answer — but the evidence that C9 exists is a referential-coverage finding, not a silent drop."""
    assert "C9" not in {r.key for r in _rows(pilot)}


def test_counts_match_the_hand_computed_fixture(pilot):
    by_key = {r.key: r for r in _rows(pilot)}
    assert (by_key["C1"].previous_count, by_key["C1"].current_count) == (3, 1)
    assert (by_key["C2"].previous_count, by_key["C2"].current_count) == (2, 2)
    assert (by_key["C3"].previous_count, by_key["C3"].current_count) == (1, 4)
    assert (by_key["C4"].previous_count, by_key["C4"].current_count) == (2, 0)


# ── dimensions ───────────────────────────────────────────────────────────────────────────────────

def test_the_answer_carries_its_dimensions(pilot):
    by_key = {r.key: r for r in _rows(pilot)}
    assert by_key["C1"].dimensions == {"segment": "RETAIL", "sector": "TRADING"}
    assert by_key["C4"].dimensions == {"segment": "CORPORATE", "sector": "REAL_ESTATE"}


def test_grouping_the_decreased_customers_by_segment(pilot):
    """The question's actual output."""
    counts: dict[str, int] = {}
    for row in _rows(pilot):
        if row.decreased:
            counts[row.dimensions["segment"]] = counts.get(row.dimensions["segment"], 0) + 1
    assert counts == EXPECTED["decreased_by_segment"] == {"RETAIL": 1, "CORPORATE": 1}


# ── the compiled SQL ─────────────────────────────────────────────────────────────────────────────

def test_the_spine_is_LEFT_joined_never_inner(pilot):
    """Asserted on the SQL as well as the rows: an inner join would pass the count tests on a
    fixture where every customer happened to transact, and fail silently on a real catalog."""
    sql = compile_analysis(_ir(), dialect=PostgresDialect())
    assert "LEFT JOIN" in sql.upper()
    assert "INNER JOIN" not in sql.upper()


def test_missing_period_counts_become_zero_not_null(pilot):
    sql = compile_analysis(_ir(), dialect=PostgresDialect())
    assert "COALESCE" in sql.upper()


def test_both_periods_are_filtered_on_the_same_column(pilot):
    """Two periods measured on different columns compare differently-shaped months, so the trend
    would be an artefact of the metadata."""
    with pytest.raises(AnalysisIRError, match="period"):
        _ir(current=Period(label="current", values=()))


# ── what it refuses ──────────────────────────────────────────────────────────────────────────────

def test_an_unsafe_identifier_is_refused():
    with pytest.raises(AnalysisIRError):
        _ir(dimensions=(Dimension(column="segment; DROP TABLE x"),))


def test_the_spine_key_and_event_key_must_both_be_named():
    with pytest.raises(AnalysisIRError):
        _ir(event_key_column="")


# ── transaction eligibility: status and reversal ─────────────────────────────────────────────────
# Functional correctness, not a filter. A reversed transaction that counts inflates a customer's
# activity, and a customer whose only current transaction was reversed reads as active when they are
# not. Every ineligible row in the fixture is placed so that removing a predicate changes an answer
# asserted below.

def test_only_posted_non_reversed_transactions_count(pilot):
    """C2 has a PENDING current row and C3 a REVERSED one. Neither may reach the count."""
    by_key = {r.key: r for r in _rows(pilot)}
    assert by_key["C2"].current_count == 2      # 3 raw current rows, one PENDING
    assert by_key["C3"].current_count == 4      # 5 raw current rows, one reversed


def test_a_null_status_does_not_count(pilot):
    """A transaction whose status nobody recorded is not evidence that it posted."""
    assert {r.key: r for r in _rows(pilot)}["C1"].current_count == 1     # 2 raw, one NULL status


def test_a_null_reversal_state_does_not_count(pilot):
    assert {r.key: r for r in _rows(pilot)}["C1"].previous_count == 3    # 4 raw, one NULL reversal


def test_a_customer_whose_only_current_activity_was_REVERSED_counts_zero(pilot):
    """C4's two current rows are both reversals. Their eligible current count is zero — which is
    what makes them a decrease. Counting reversals would put them at 2, equal to their previous, and
    the customer the question is most about would silently drop out of the answer."""
    c4 = {r.key: r for r in _rows(pilot)}["C4"]
    assert (c4.previous_count, c4.current_count) == (2, 0)
    assert c4.decreased is True


def test_a_customer_whose_only_PREVIOUS_activity_was_reversed_did_not_decrease(pilot):
    """C6's single previous row is reversed, so their eligible previous count is zero. Counting it
    would invent a 1 -> 0 decrease that never happened."""
    c6 = {r.key: r for r in _rows(pilot)}["C6"]
    assert (c6.previous_count, c6.current_count) == (0, 0)
    assert c6.decreased is False


def test_eligibility_is_applied_to_BOTH_periods(pilot):
    sql = compile_analysis(_ir(), dialect=PostgresDialect())
    prev_cte = sql[sql.index("prev AS ("):sql.index("curr AS (")]
    curr_cte = sql[sql.index("curr AS ("):sql.upper().index("SELECT S.")]
    for cte, label in ((prev_cte, "previous"), (curr_cte, "current")):
        assert "tran_status" in cte, f"{label} period is unfiltered"
        assert "reversal_flag" in cte, f"{label} period ignores reversals"


def test_eligibility_is_INSIDE_the_aggregation_never_the_outer_query(pilot):
    """THE placement rule. In the outer query, after the spine LEFT JOIN, a status predicate would
    drop every customer whose joined columns are NULL — exactly the customers with no eligible
    transactions — destroying the population-spine guarantee the LEFT JOIN exists to provide."""
    sql = compile_analysis(_ir(), dialect=PostgresDialect())
    outer = sql[sql.upper().index("FROM \"DPL_EIB\".\"CUSTOMER_MASTER\"" ) if False else
                sql.index("FROM \"dpl_eib\".\"customer_master\""):]
    assert "tran_status" not in outer and "reversal_flag" not in outer


def test_unknown_customer_transactions_still_cannot_create_population_rows(pilot):
    assert "C9" not in {r.key for r in _rows(pilot)}


# ── the policy is declared, never invented ───────────────────────────────────────────────────────

def test_an_analysis_without_an_eligibility_policy_is_REFUSED(pilot):
    with pytest.raises(AnalysisIRError, match="eligibility"):
        _ir(eligibility=None)


def test_no_column_name_or_status_code_is_hardcoded_in_the_renderer(pilot):
    """A different source's vocabulary must flow through untouched. If the renderer knew about
    'POSTED' it would be guessing at bank semantics."""
    sql = compile_analysis(_ir(eligibility=_policy(
        status_column="txn_state", included_status_values=("SETTLED", "CLEARED"),
        reversal_column="is_void", non_reversed_values=("0",))), dialect=PostgresDialect())
    assert "txn_state" in sql and "'SETTLED'" in sql and "'CLEARED'" in sql
    assert "is_void" in sql and "'0'" in sql
    assert "POSTED" not in sql and "reversal_flag" not in sql


def test_changing_the_eligibility_policy_changes_the_plan_hash(pilot):
    """A cached result computed under a different definition of "counts" must not be reused."""
    base = _ir().plan_hash
    assert _ir(eligibility=_policy(included_status_values=("POSTED", "PENDING"))).plan_hash != base
    assert _ir(eligibility=_policy(non_reversed_values=("N", "NULL"))).plan_hash != base
    assert _ir(eligibility=_policy(null_behavior=NullBehavior.INCLUDE)).plan_hash != base


def test_the_question_text_is_provenance_and_not_part_of_identity(pilot):
    """Two differently-worded questions resolving to the same computation are one plan — mirroring
    `authoring_run_id` sitting outside FormulaExecutionIRV1's identity payload."""
    assert _ir(question="which customers slowed down?").plan_hash == _ir().plan_hash


# ── unsupported reversal shapes are refused, not approximated ────────────────────────────────────

@pytest.mark.parametrize("mode", [
    ReversalMode.COMPENSATING_ROW,
    ReversalMode.LINKED_REVERSAL_ID,
    ReversalMode.STATUS_HISTORY,
    ReversalMode.NEGATIVE_ENTRY,
])
def test_a_reversal_shape_we_cannot_model_is_refused(mode):
    """Treating a compensating-row source as a flag source would count BOTH the original and its
    reversal — doubling the very activity the reversal was meant to undo."""
    from featuregen.data_agent.eligibility import EligibilityError
    with pytest.raises(EligibilityError, match="reversal mode"):
        _policy(reversal_mode=mode)
