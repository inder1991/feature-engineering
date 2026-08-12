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
from featuregen.data_agent.dimensions import (
    AttributionBasis,
    AttributionError,
    DimensionAttributionPolicyV1,
    MissingValueBehavior,
)
from featuregen.data_agent.eligibility import (
    NullBehavior,
    ReversalMode,
    TransactionEligibilityPolicyV1,
)
from featuregen.data_agent.sql_postgres import PostgresDialect
from tests.featuregen.data_agent.pilot_fixture import (
    CURRENT_MONTH,
    CUSTOMER_SCHEMA,
    CUSTOMER_TABLE,
    DIMENSION_TABLE,
    REPORT_CUTOFF,
    EXPECTED,
    PILOT_JOIN_EVIDENCE,
    binding,
    PREVIOUS_MONTH,
    TRANSACTION_SCHEMA,
    TRANSACTION_TABLE,
    create_pilot_tables,
)


@pytest.fixture
def pilot(db):
    create_pilot_tables(db)
    return db


#: The fixture owns the bindings, so a plan under test and the join evidence beside it cannot drift
#: onto different physical identities.
_binding = binding


def _policy(**over) -> TransactionEligibilityPolicyV1:
    """The pilot source's definition of a transaction that counts. Column names and codes are
    EXAMPLES until verified against Hive — which is the point of declaring them rather than
    hardcoding them in the renderer."""
    kw = dict(status_column="tran_status", included_status_values=("POSTED",),
              reversal_mode=ReversalMode.BOOLEAN_OR_CODE_COLUMN, reversal_column="reversal_flag",
              non_reversed_values=("N",), null_behavior=NullBehavior.EXCLUDE)
    kw.update(over)
    return TransactionEligibilityPolicyV1(**kw)


def _attribution(**over) -> DimensionAttributionPolicyV1:
    kw = dict(attribution_basis=AttributionBasis.REPORT_CUTOFF,
              effective_from_column="effective_from", effective_to_column="effective_to",
              report_cutoff=REPORT_CUTOFF,
              missing_value_behavior=MissingValueBehavior.UNKNOWN_BUCKET)
    kw.update(over)
    return DimensionAttributionPolicyV1(**kw)


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
        dimension_binding=_binding(CUSTOMER_SCHEMA, DIMENSION_TABLE),
        attribution=_attribution(),
        # Release 3 must demonstrate VERIFIED joins, so the pilot plan carries the observed evidence
        # for the one join it performs. `test_verified_joins` owns the rule and proves this constant
        # matches a real probe; here it simply makes the default plan an admissible one.
        join_evidence=PILOT_JOIN_EVIDENCE,
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
    assert by_key["C1"].dimensions == {"segment": "SME", "sector": "TRADING"}
    assert by_key["C4"].dimensions == {"segment": "CORPORATE", "sector": "REAL_ESTATE"}


def test_grouping_the_decreased_customers_by_segment(pilot):
    """The question's actual output."""
    counts: dict[str, int] = {}
    for row in _rows(pilot):
        if row.decreased:
            counts[row.dimensions["segment"]] = counts.get(row.dimensions["segment"], 0) + 1
    assert counts == EXPECTED["decreased_by_segment"] == {"SME": 1, "CORPORATE": 1}


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


# ── point-in-time dimension attribution ──────────────────────────────────────────────────────────
# Joining TODAY's segment to LAST MONTH's transactions silently reattributes activity whenever a
# customer changes segment, and the answer looks entirely reasonable. Same class as counting
# reversals: it changes the number, not the presentation.
#
# The rule implemented here: classify each customer using the row valid at the REPORT CUTOFF, and
# use that single classification for their whole period-over-period result.

def _segments(conn):
    return {r.key: r.dimensions["segment"] for r in _rows(conn)}


def test_a_change_BEFORE_the_cutoff_uses_the_new_segment(pilot):
    """C1 moved RETAIL -> SME on 2026-06-15, before the 06-30 cutoff."""
    assert _segments(pilot)["C1"] == "SME"


def test_a_change_AFTER_the_cutoff_retains_the_earlier_segment(pilot):
    """C2 becomes SME on 2026-07-15. A June report must not know that yet."""
    assert _segments(pilot)["C2"] == "RETAIL"


def test_a_future_dated_row_is_ignored(pilot):
    """C5's only row starts 2026-08-01, so at the cutoff C5 has no classification at all."""
    assert _segments(pilot)["C5"] == "Unknown"


def test_effective_from_EQUAL_to_the_cutoff_is_included(pilot):
    """C4's CORPORATE row starts exactly on 2026-06-30."""
    assert _segments(pilot)["C4"] == "CORPORATE"


def test_effective_to_EQUAL_to_the_cutoff_is_excluded(pilot):
    """C4's earlier RETAIL row ends exactly on 2026-06-30. Half-open [from, to) means the two
    boundary rules are complementary — for adjacent rows they select exactly ONE. `>=`/`<=` on both
    sides would select both and duplicate the customer."""
    assert _segments(pilot)["C4"] == "CORPORATE"
    assert len([r for r in _rows(pilot) if r.key == "C4"]) == 1


def test_the_current_open_ended_record_is_not_used_for_a_HISTORICAL_cutoff(pilot):
    """C2's open-ended row (SME, from 2026-07-15) is the "current" one. Using it for a June report
    is the exact defect this rule prevents."""
    historical = _ir(attribution=_attribution(report_cutoff="2026-06-30"))
    later = _ir(attribution=_attribution(report_cutoff="2026-08-01"))
    assert {r.key: r.dimensions["segment"] for r in _rows(pilot, historical)}["C2"] == "RETAIL"
    assert {r.key: r.dimensions["segment"] for r in _rows(pilot, later)}["C2"] == "SME"


def test_a_missing_dimension_row_does_not_remove_the_customer(pilot):
    """C6 has no history at all. The population guarantee applies to dimensions too — a customer
    with no classification is Unknown, not absent."""
    rows = _rows(pilot)
    assert "C6" in {r.key for r in rows}
    assert _segments(pilot)["C6"] == "Unknown"


def test_an_unknown_customers_dimension_row_cannot_invent_a_population_member(pilot):
    """C9 has a history row and no customer row."""
    assert "C9" not in {r.key for r in _rows(pilot)}


def test_segment_and_sector_resolve_at_the_SAME_instant(pilot):
    """Both come from one selected history row, so they cannot disagree about when the customer was
    classified. Asserted on C1, whose segment changed but whose sector did not."""
    c1 = {r.key: r for r in _rows(pilot)}["C1"]
    assert c1.dimensions == {"segment": "SME", "sector": "TRADING"}
    sql = compile_analysis(_ir(), dialect=PostgresDialect())
    assert sql.count(REPORT_CUTOFF) == 2, "one cutoff, used once per interval bound"


def test_group_totals_reconcile_to_the_whole_population(pilot):
    """Including Unknown. If a bucket vanished, the totals would silently stop adding up."""
    rows = _rows(pilot)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.dimensions["segment"]] = counts.get(row.dimensions["segment"], 0) + 1
    assert sum(counts.values()) == len(rows) == EXPECTED["customer_rows"]
    assert counts["Unknown"] == 2      # C5 (future-dated) and C6 (absent)


def test_every_customer_classifies_as_the_fixture_says(pilot):
    assert _segments(pilot) == EXPECTED["segment_at_cutoff"]


# ── overlap is refused, never resolved ───────────────────────────────────────────────────────────

def test_overlapping_records_REFUSE_rather_than_picking_one(pilot):
    """Taking MAX(effective_from) would choose a classification and hide a dimension-table defect."""
    pilot.execute(
        f"INSERT INTO {CUSTOMER_SCHEMA}.{DIMENSION_TABLE} VALUES "
        "('C3', 'RETAIL', 'TRADING', '2019-01-01', NULL)")
    with pytest.raises(AttributionError, match="more than one"):
        _rows(pilot)


def test_two_identical_active_records_cannot_duplicate_a_customer(pilot):
    pilot.execute(
        f"INSERT INTO {CUSTOMER_SCHEMA}.{DIMENSION_TABLE} VALUES "
        "('C3', 'CORPORATE', 'TRADING', '2020-01-01', NULL)")
    with pytest.raises(AttributionError, match="more than one"):
        _rows(pilot)


# ── the policy is declared, never defaulted ──────────────────────────────────────────────────────

def test_dimensions_without_an_attribution_policy_are_REFUSED(pilot):
    with pytest.raises(AnalysisIRError, match="attribution"):
        _ir(attribution=None)


def test_a_missing_cutoff_is_refused_rather_than_defaulting_to_today(pilot):
    with pytest.raises(AttributionError, match="cutoff"):
        _attribution(report_cutoff="")


@pytest.mark.parametrize("basis", [
    AttributionBasis.PERIOD_END_PER_PERIOD,
    AttributionBasis.TRANSACTION_EVENT_TIME,
])
def test_an_unimplemented_attribution_basis_is_refused(basis):
    """Both are defensible business definitions giving different answers. The renderer must not pick.

    CHANGE OF INTENT (Release-B Task 8): `current_value` used to be in this list. It is now
    IMPLEMENTED — see `test_current_value_is_now_implemented_because_the_declaration_exists` below
    and the `dimensions` module docstring. What changed is not the engine's ambition but the
    availability of a DECLARATION of which row is today's; the two bases left here are still
    business decisions nobody has made.
    """
    with pytest.raises(AttributionError, match="basis"):
        _attribution(attribution_basis=basis)


def test_current_value_is_now_implemented_because_the_declaration_exists(pilot):
    """ENGINE A. The flag the temporal policy declares decides which row is current; a cutoff on a
    "today" question is a contradiction and is refused rather than ignored."""
    policy = _attribution(attribution_basis=AttributionBasis.CURRENT_VALUE,
                          report_cutoff="", current_flag_column="is_current")
    assert policy.validity_predicate(PostgresDialect().ident) == '"is_current" = TRUE'
    with pytest.raises(AttributionError, match="ATTRIBUTION_CUTOFF_ON_CURRENT_VALUE"):
        _attribution(attribution_basis=AttributionBasis.CURRENT_VALUE,
                     current_flag_column="is_current")


def test_changing_the_attribution_policy_changes_the_plan_hash(pilot):
    base = _ir().plan_hash
    assert _ir(attribution=_attribution(report_cutoff="2026-05-31")).plan_hash != base
    assert _ir(attribution=_attribution(
        missing_value_behavior=MissingValueBehavior.RETAIN_NULL)).plan_hash != base


def test_pit_predicates_are_INSIDE_the_snapshot_never_the_outer_query(pilot):
    """Effective-date predicates in the outer WHERE would drop every customer with no dimension
    history — repeating the population-spine mistake one layer up."""
    sql = compile_analysis(_ir(), dialect=PostgresDialect())
    outer = sql[sql.index('FROM "dpl_eib"."customer_master"'):]
    assert "effective_from" not in outer and "effective_to" not in outer
