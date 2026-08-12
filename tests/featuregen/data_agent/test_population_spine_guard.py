"""The POPULATION SPINE is one row per member — asserted against the data, at execution.

Release-B Task 8 review, F6b. `compile_analysis` reads the spine RAW: `FROM customer_master s`,
no predicate, because every row of a population IS a member. That is the design and it is also the
hole — a spine that keeps history does not fail, it MULTIPLIES. Every count is scaled by however
many versions a customer happens to have, group totals inflate, and the answer stays plausible for
exactly the customers who changed the most. `pilot_fixture` has said "it cannot be the spine:
several history rows per customer would duplicate the population" since Release 3, with nothing
enforcing it.

Selection refuses a history-keeping population before it gets here
(`source_selector._population_history_refusal`), but that is a check on the DECLARED temporal
model. This is the check on the DATA — the same split `assert_no_dimension_overlap` and
`assert_no_snapshot_tie` already make, and for the same reason: a declaration that turns out not to
hold of the rows is exactly the case a declaration cannot catch.
"""
from __future__ import annotations

import pytest
from tests.featuregen.data_agent.pilot_fixture import (
    CURRENT_MONTH,
    CUSTOMER_SCHEMA,
    CUSTOMER_TABLE,
    DIMENSION_TABLE,
    EXPECTED,
    PILOT_JOIN_EVIDENCE,
    PREVIOUS_MONTH,
    REPORT_CUTOFF,
    TRANSACTION_SCHEMA,
    TRANSACTION_TABLE,
    binding,
    create_pilot_tables,
)
from tests.featuregen.data_agent.test_analysis_ir import _policy

from featuregen.data_agent.analysis import (
    AnalysisExecutionIRV1,
    AnalysisIRError,
    Comparison,
    Dimension,
    Period,
    PopulationSpine,
    assert_spine_is_unique,
    run_analysis,
)
from featuregen.data_agent.dimensions import (
    AttributionBasis,
    DimensionAttributionPolicyV1,
    MissingValueBehavior,
)
from featuregen.data_agent.sql_postgres import PostgresDialect


@pytest.fixture
def pilot(db):
    create_pilot_tables(db)
    return db


def _ir(**over) -> AnalysisExecutionIRV1:
    kw = dict(
        question="customers whose transaction count decreased, by segment",
        spine=PopulationSpine(binding=binding(CUSTOMER_SCHEMA, CUSTOMER_TABLE),
                              key_column="cif_id"),
        event_binding=binding(TRANSACTION_SCHEMA, TRANSACTION_TABLE),
        event_key_column="cif_id", period_column="tran_month",
        current=Period(label="current", values=(CURRENT_MONTH,)),
        previous=Period(label="previous", values=(PREVIOUS_MONTH,)),
        measure="count", comparison=Comparison.DECREASED,
        dimensions=(Dimension(column="segment"),),
        eligibility=_policy(),
        dimension_binding=binding(CUSTOMER_SCHEMA, DIMENSION_TABLE),
        attribution=DimensionAttributionPolicyV1(
            attribution_basis=AttributionBasis.REPORT_CUTOFF,
            effective_from_column="effective_from", effective_to_column="effective_to",
            report_cutoff=REPORT_CUTOFF,
            missing_value_behavior=MissingValueBehavior.UNKNOWN_BUCKET),
        join_evidence=PILOT_JOIN_EVIDENCE)
    kw.update(over)
    return AnalysisExecutionIRV1(**kw)


def _duplicate(conn, key: str = "C1") -> None:
    """One extra row for a customer who already exists — an SCD2 population, in miniature."""
    conn.execute(f"INSERT INTO {CUSTOMER_SCHEMA}.{CUSTOMER_TABLE} VALUES (%s)", (key,))


def test_the_worked_population_is_unaffected(pilot):
    """The pilot master is one row per customer, so the gate is silent — a guard that refused the
    shape the release ships would be a guard nobody could keep on."""
    assert_spine_is_unique(pilot, _ir(), dialect=PostgresDialect())
    rows = {r.key: r for r in run_analysis(pilot, _ir(), dialect=PostgresDialect())}
    assert sorted(rows) == ["C1", "C2", "C3", "C4", "C5", "C6"]


def test_a_DUPLICATED_population_row_is_refused_rather_than_multiplying_the_answer(pilot):
    _duplicate(pilot)
    with pytest.raises(AnalysisIRError) as exc:
        assert_spine_is_unique(pilot, _ir(), dialect=PostgresDialect())
    assert exc.value.code == "POPULATION_SPINE_NOT_UNIQUE"
    assert exc.value.subjects == (
        f"{binding(CUSTOMER_SCHEMA, CUSTOMER_TABLE).identity.table_id}.cif_id",)


def test_the_refusal_happens_BEFORE_the_answer_is_computed(pilot):
    """`run_analysis` is the gate. Without it C1's counts would DOUBLE and every segment total with
    them, and nothing in the result would say so."""
    _duplicate(pilot)
    with pytest.raises(AnalysisIRError) as exc:
        run_analysis(pilot, _ir(), dialect=PostgresDialect())
    assert exc.value.code == "POPULATION_SPINE_NOT_UNIQUE"


def test_the_multiplication_it_prevents_is_real(pilot):
    """The defect itself, made visible: with the gate stepped over, the duplicated customer appears
    twice and their transactions are counted twice — a plausible answer that is simply wrong."""
    _duplicate(pilot)
    from featuregen.data_agent.analysis import compile_analysis

    rows = pilot.execute(compile_analysis(_ir(), dialect=PostgresDialect())).fetchall()
    keys = [r[0] for r in rows]
    assert keys.count("C1") == 2
    assert sum(int(r[1]) for r in rows if r[0] == "C1") == 6      # C1's real previous count is 3


def test_a_population_row_with_NO_KEY_is_refused_too(pilot):
    """A member with no identity can never match an event, so it contributes a permanent zero to
    the answer while still counting as a member of the population."""
    pilot.execute(f"INSERT INTO {CUSTOMER_SCHEMA}.{CUSTOMER_TABLE} VALUES (NULL)")
    with pytest.raises(AnalysisIRError) as exc:
        assert_spine_is_unique(pilot, _ir(), dialect=PostgresDialect())
    assert exc.value.code == "POPULATION_SPINE_NOT_UNIQUE"
    assert "no identity" in str(exc.value)


def test_the_gate_reads_the_hand_counted_fixture(pilot):
    """The numbers this suite rests on come from `EXPECTED`, not from the query under test."""
    rows, distinct = pilot.execute(
        f"SELECT COUNT(*), COUNT(DISTINCT cif_id) FROM {CUSTOMER_SCHEMA}.{CUSTOMER_TABLE}"
    ).fetchone()
    assert rows == EXPECTED["customer_rows"]
    assert distinct == EXPECTED["customer_cif_distinct"]


def test_a_refused_spine_becomes_learning_evidence_only_if_someone_can_decide_it(pilot):
    """Deliberately NOT in `REFUSAL_TO_GAP`: a duplicated population row is a defect in the source
    or the wrong table, not a decision anyone is waiting to make — the same judgement
    ATTRIBUTION_OVERLAPPING_RECORDS gets."""
    from featuregen.data_agent.learning import REFUSAL_TO_GAP

    assert "POPULATION_SPINE_NOT_UNIQUE" not in REFUSAL_TO_GAP
