"""Release-B Task 8 — the two NEW row-selection engines (D12.6), against real rows.

Only ``valid_at_report_cutoff`` was reuse. This suite covers what was BUILT:

  * ENGINE A, ``current_record``: the ``current_value`` attribution basis, whose refusal is lifted
    because the temporal policy now DECLARES which row is today's. A cutoff on a "today" question
    is a contradiction and is refused rather than ignored;
  * ENGINE B, ``latest_snapshot_as_of``: the greatest snapshot at or before the cutoff, per entity,
    compiled through the same dialect-SQL path — and a TIE that refuses unless the policy's
    governed tie-breakers separate it deterministically.

Every SQL assertion runs against real rows in the test database, because the whole class of defect
these engines exist to prevent (a predicate that runs clean and answers a different question) is
invisible in a string comparison.
"""
from __future__ import annotations

import pytest
from tests.featuregen.data_agent.pilot_fixture import (
    CURRENT_MONTH,
    CUSTOMER_SCHEMA,
    CUSTOMER_TABLE,
    DIMENSION_TABLE,
    PILOT_JOIN_EVIDENCE,
    PREVIOUS_MONTH,
    REPORT_CUTOFF,
    SNAPSHOT_TABLE,
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
    compile_analysis,
    run_analysis,
)
from featuregen.data_agent.dimensions import (
    AttributionBasis,
    AttributionError,
    DimensionAttributionPolicyV1,
    MissingValueBehavior,
)
from featuregen.data_agent.snapshots import (
    LatestSnapshotPolicyV1,
    SnapshotScope,
    SnapshotSelectionError,
    assert_no_snapshot_tie,
)
from featuregen.data_agent.sql_postgres import PostgresDialect
from featuregen.overlay.upload.source_selection import TEMPORAL_SNAPSHOT_TIE

_Q = PostgresDialect().ident
_SNAPSHOT_REF = f'"{CUSTOMER_SCHEMA}"."{SNAPSHOT_TABLE}"'


@pytest.fixture
def pilot(db):
    create_pilot_tables(db)
    return db


def _snapshot(**over) -> LatestSnapshotPolicyV1:
    kw = dict(snapshot_column="snapshot_date", cutoff=REPORT_CUTOFF, key_column="cif_id",
              scope=SnapshotScope.PER_ENTITY)
    kw.update(over)
    return LatestSnapshotPolicyV1(**kw)


def _ir(**over) -> AnalysisExecutionIRV1:
    kw = dict(
        question="customers whose transaction count decreased, by segment and sector",
        spine=PopulationSpine(binding=binding(CUSTOMER_SCHEMA, CUSTOMER_TABLE),
                              key_column="cif_id"),
        event_binding=binding(TRANSACTION_SCHEMA, TRANSACTION_TABLE),
        event_key_column="cif_id", period_column="tran_month",
        current=Period(label="current", values=(CURRENT_MONTH,)),
        previous=Period(label="previous", values=(PREVIOUS_MONTH,)),
        measure="count", comparison=Comparison.DECREASED,
        dimensions=(Dimension(column="segment"), Dimension(column="sector")),
        eligibility=_policy(),
        dimension_binding=binding(CUSTOMER_SCHEMA, SNAPSHOT_TABLE),
        snapshot_selection=_snapshot(),
        join_evidence=PILOT_JOIN_EVIDENCE)
    kw.update(over)
    return AnalysisExecutionIRV1(**kw)


# ── ENGINE A: current_record ────────────────────────────────────────────────────────────────────


def test_the_current_flag_is_what_decides_which_row_is_today(pilot):
    """The DECLARATION, not a convention: `is_current` is what the policy names."""
    policy = DimensionAttributionPolicyV1(
        attribution_basis=AttributionBasis.CURRENT_VALUE,
        effective_from_column="effective_from", effective_to_column="effective_to",
        current_flag_column="is_current")
    rows = pilot.execute(
        f'SELECT cif_id, segment FROM "{CUSTOMER_SCHEMA}"."{DIMENSION_TABLE}" '
        f"WHERE {policy.validity_predicate(_Q)} ORDER BY cif_id").fetchall()
    # C7 is the one that matters: its only row is CLOSED on a scheduled 2027 date, so the source
    # flags it current and the open-ended convention below cannot see it at all.
    assert dict(rows) == {"C1": "SME", "C2": "SME", "C3": "CORPORATE", "C4": "CORPORATE",
                          "C5": "RETAIL", "C7": "CORPORATE", "C9": "CORPORATE"}


def test_without_a_flag_the_open_ended_row_is_the_current_one(pilot):
    """The other declaration a source can make. Both shapes exist in one bank; neither is guessed —
    and on C7 they DISAGREE, which is why the flag is read first when the policy names one."""
    policy = DimensionAttributionPolicyV1(
        attribution_basis=AttributionBasis.CURRENT_VALUE,
        effective_from_column="effective_from", effective_to_column="effective_to")
    rows = pilot.execute(
        f'SELECT cif_id, segment FROM "{CUSTOMER_SCHEMA}"."{DIMENSION_TABLE}" '
        f"WHERE {policy.validity_predicate(_Q)} ORDER BY cif_id").fetchall()
    assert dict(rows) == {"C1": "SME", "C2": "SME", "C3": "CORPORATE", "C4": "CORPORATE",
                          "C5": "RETAIL", "C9": "CORPORATE"}
    assert "C7" not in dict(rows)


def test_current_value_and_report_cutoff_give_DIFFERENT_answers(pilot):
    """The reason the basis is declared rather than chosen by the renderer. C2 is RETAIL at the
    2026-06-30 cutoff and SME today; a report that silently used "today" would reclassify it."""
    at_cutoff = DimensionAttributionPolicyV1(
        attribution_basis=AttributionBasis.REPORT_CUTOFF,
        effective_from_column="effective_from", effective_to_column="effective_to",
        report_cutoff=REPORT_CUTOFF)
    today = DimensionAttributionPolicyV1(
        attribution_basis=AttributionBasis.CURRENT_VALUE,
        effective_from_column="effective_from", effective_to_column="effective_to",
        current_flag_column="is_current")

    def _segments(policy):
        return dict(pilot.execute(
            f'SELECT cif_id, segment FROM "{CUSTOMER_SCHEMA}"."{DIMENSION_TABLE}" '
            f"WHERE {policy.validity_predicate(_Q)} ORDER BY cif_id").fetchall())

    assert _segments(at_cutoff)["C2"] == "RETAIL"
    assert _segments(today)["C2"] == "SME"


def test_current_value_with_no_declaration_at_all_is_refused():
    with pytest.raises(AttributionError, match="ATTRIBUTION_NO_CURRENT_RULE"):
        DimensionAttributionPolicyV1(
            attribution_basis=AttributionBasis.CURRENT_VALUE,
            effective_from_column="effective_from", effective_to_column="")


def test_a_current_flag_on_a_REPORT_CUTOFF_policy_is_refused_not_ignored():
    """The mirror of the refusal below. Only `_current_predicate` reads the flag, so on a cutoff
    policy it changes no rows — while still entering `plan_hash`, which forks the identity of two
    plans that compile to the same statement. Existing policies are unaffected: no `report_cutoff`
    policy in the tree declares one."""
    with pytest.raises(AttributionError, match="ATTRIBUTION_CURRENT_FLAG_ON_CUTOFF"):
        DimensionAttributionPolicyV1(
            attribution_basis=AttributionBasis.REPORT_CUTOFF,
            effective_from_column="effective_from", effective_to_column="effective_to",
            report_cutoff=REPORT_CUTOFF, current_flag_column="is_current")


def test_a_cutoff_on_a_current_value_policy_is_refused_not_ignored():
    with pytest.raises(AttributionError, match="ATTRIBUTION_CUTOFF_ON_CURRENT_VALUE"):
        DimensionAttributionPolicyV1(
            attribution_basis=AttributionBasis.CURRENT_VALUE,
            effective_from_column="effective_from", effective_to_column="effective_to",
            current_flag_column="is_current", report_cutoff=REPORT_CUTOFF)


# ── ENGINE B: latest_snapshot_as_of ─────────────────────────────────────────────────────────────


def test_the_greatest_snapshot_at_or_before_the_cutoff_wins(pilot):
    policy = _snapshot(tie_break_columns=("load_seq",))
    sql = policy.ranked_selection(_Q, table_ref=_SNAPSHOT_REF, columns=("segment", "sector"))
    rows = pilot.execute(sql).fetchall()
    # Per the fixture: C1 has a 2026-05 and a 2026-06 snapshot; C2 has a 2026-06 and a POST-cutoff
    # 2026-07 one; C3 has one; C4's two rows tie on the snapshot date and are separated only by the
    # declared `load_seq`; C5 has ONLY a post-cutoff snapshot and must therefore be absent.
    assert {r[0]: r[1] for r in rows} == {"C1": "SME", "C2": "RETAIL", "C3": "CORPORATE",
                                          "C4": "CORPORATE"}


def test_a_snapshot_after_the_cutoff_is_never_read(pilot):
    """A row published after the cutoff is a fact nobody could have known — the same look-ahead
    leak the availability findings exist for, one layer down."""
    sql = _snapshot(tie_break_columns=("load_seq",)).ranked_selection(
        _Q, table_ref=_SNAPSHOT_REF, columns=("segment",))
    assert "C5" not in {r[0] for r in pilot.execute(sql).fetchall()}
    later = _snapshot(cutoff="2026-07-31", tie_break_columns=("load_seq",))
    sql = later.ranked_selection(_Q, table_ref=_SNAPSHOT_REF, columns=("segment",))
    picked = {r[0]: r[1] for r in pilot.execute(sql).fetchall()}
    assert picked["C2"] == "SME" and picked["C5"] == "RETAIL"


def test_a_snapshot_tie_refuses_rather_than_picking(pilot):
    """C4 has TWO rows stamped on the same snapshot date. Which one answers is read order."""
    with pytest.raises(SnapshotSelectionError) as exc:
        assert_no_snapshot_tie(pilot, _snapshot(), table_ref=_SNAPSHOT_REF,
                               dialect=PostgresDialect())
    assert exc.value.code == TEMPORAL_SNAPSHOT_TIE


def test_a_governed_tie_breaker_resolves_the_tie_deterministically(pilot):
    """The policy's `tie_break_refs`, doing the one job they exist for."""
    policy = _snapshot(tie_break_columns=("load_seq",))
    assert_no_snapshot_tie(pilot, policy, table_ref=_SNAPSHOT_REF, dialect=PostgresDialect())
    picked = {r[0]: r[1] for r in pilot.execute(
        policy.ranked_selection(_Q, table_ref=_SNAPSHOT_REF, columns=("segment",))).fetchall()}
    assert picked["C4"] == "CORPORATE"           # the higher load_seq of the two tied rows


def test_a_tie_breaker_that_does_not_separate_them_still_refuses(pilot):
    """"Unless the tie_break_refs resolve it DETERMINISTICALLY" is a testable claim, not a hope:
    `sector` is identical on C4's two tied rows, so declaring it changes nothing."""
    with pytest.raises(SnapshotSelectionError) as exc:
        assert_no_snapshot_tie(pilot, _snapshot(tie_break_columns=("sector",)),
                               table_ref=_SNAPSHOT_REF, dialect=PostgresDialect())
    assert exc.value.code == TEMPORAL_SNAPSHOT_TIE


# ── ENGINE B: the null half of a tie-breaker ────────────────────────────────────────────────────
#
# A tie-break column is nullable in every real bank table, and a bare `DESC` is not one ordering:
# PostgreSQL puts NULLS FIRST, Hive/Spark put them LAST. The same governed decision therefore
# selected a different row per engine, and the tie probe AGREED with the wrong one — a NULL sorted
# alone at rank 1, `RANK` saw no tie, and the row that answered the question was the one that
# declared nothing.


def _tied_pair(conn, key: str, *seq_values) -> None:
    """Two rows for one entity, stamped on the SAME snapshot date, differing only in `load_seq`."""
    for index, seq in enumerate(seq_values):
        conn.execute(
            f"INSERT INTO {CUSTOMER_SCHEMA}.{SNAPSHOT_TABLE} VALUES (%s, %s, %s, %s, %s)",
            (key, f"SEG{index}", f"SECTOR{index}", REPORT_CUTOFF, seq))


def test_a_NULL_tie_break_value_cannot_WIN_a_tie(pilot):
    """The probe the review ran: on PostgreSQL the NULL beat the 5, because DESC defaults to NULLS
    FIRST there. A row carrying no `load_seq` has declared nothing about superseding anything."""
    _tied_pair(pilot, "C8", None, 5)
    policy = _snapshot(tie_break_columns=("load_seq",))
    picked = {r[0]: r[1] for r in pilot.execute(
        policy.ranked_selection(_Q, table_ref=_SNAPSHOT_REF, columns=("segment",))).fetchall()}
    assert picked["C8"] == "SEG1"                    # the row that declared load_seq = 5


def test_a_NULL_against_a_VALUE_is_separated_and_does_not_refuse(pilot):
    """The other half of the same rule: the ordering genuinely decides that pair, so the tie gate
    has nothing to refuse — and the winner is the declared row, not read order."""
    _tied_pair(pilot, "C8", None, 5)
    assert_no_snapshot_tie(pilot, _snapshot(tie_break_columns=("load_seq",)),
                           table_ref=_SNAPSHOT_REF, dialect=PostgresDialect())


def test_two_NULL_tie_break_values_are_UNRESOLVED_by_that_ref_and_refuse(pilot):
    """NULLS LAST separates a null from a value; it cannot separate two nulls. Nothing declared
    tells these two rows apart, so the answer would be read order — refused."""
    _tied_pair(pilot, "C8", None, None)
    with pytest.raises(SnapshotSelectionError) as exc:
        assert_no_snapshot_tie(pilot, _snapshot(tie_break_columns=("load_seq",)),
                               table_ref=_SNAPSHOT_REF, dialect=PostgresDialect())
    assert exc.value.code == TEMPORAL_SNAPSHOT_TIE


def test_an_unresolved_ref_falls_through_to_the_NEXT_declared_ref(pilot):
    """"Unresolved by that ref" means the ordering continues, not that it stops: both rows are NULL
    on `load_seq`, and the second declared tie-breaker separates them."""
    _tied_pair(pilot, "C8", None, None)
    policy = _snapshot(tie_break_columns=("load_seq", "sector"))
    assert_no_snapshot_tie(pilot, policy, table_ref=_SNAPSHOT_REF, dialect=PostgresDialect())
    picked = {r[0]: r[1] for r in pilot.execute(
        policy.ranked_selection(_Q, table_ref=_SNAPSHOT_REF, columns=("segment",))).fetchall()}
    assert picked["C8"] == "SEG1"                    # SECTOR1 > SECTOR0 descending


def test_both_rendered_statements_carry_EXPLICIT_null_placement_in_BOTH_dialects():
    """The defaults disagree, so neither statement may rely on one. Pinned on the SELECTION and the
    PROBE together: the two disagreeing is how a tie gate passes a query it cannot see."""
    from featuregen.data_agent.sql_hive import HiveDialect

    policy = _snapshot(tie_break_columns=("load_seq",))
    for dialect in (PostgresDialect(), HiveDialect()):
        quote = dialect.ident
        selection = policy.ranked_selection(quote, table_ref="t", columns=("segment",))
        probe = policy.tie_probe(quote, table_ref="t")
        for sql in (selection, probe):
            assert f"{quote('snapshot_date')} DESC NULLS LAST" in sql, (dialect.name, sql)
            assert f"{quote('load_seq')} DESC NULLS LAST" in sql, (dialect.name, sql)
        assert " DESC," not in selection and " DESC)" not in selection


def test_a_per_table_snapshot_scope_names_no_entity_key():
    with pytest.raises(SnapshotSelectionError, match="SNAPSHOT_SCOPE_CONTRADICTION"):
        _snapshot(scope=SnapshotScope.PER_TABLE)


def test_a_snapshot_selection_without_a_cutoff_is_refused():
    with pytest.raises(SnapshotSelectionError, match="SNAPSHOT_NO_CUTOFF"):
        _snapshot(cutoff="")


# ── ENGINE B inside the analysis IR ─────────────────────────────────────────────────────────────


def test_the_snapshot_engine_compiles_through_the_existing_dialect_path(pilot):
    sql = compile_analysis(_ir(snapshot_selection=_snapshot(tie_break_columns=("load_seq",))),
                           dialect=PostgresDialect())
    assert "ROW_NUMBER() OVER" in sql
    assert "LEFT JOIN dim ON dim.k = s." in sql
    # The row rule stays INSIDE the CTE — an outer predicate would drop every customer with no
    # snapshot, repeating the population-spine mistake one layer up.
    outer = sql[sql.index('FROM "dpl_eib"."customer_master"'):]
    assert "snapshot_date" not in outer


def test_a_zero_transaction_customer_survives_the_snapshot_dimension(pilot):
    rows = run_analysis(pilot, _ir(snapshot_selection=_snapshot(tie_break_columns=("load_seq",))),
                        dialect=PostgresDialect())
    by_key = {r.key: r for r in rows}
    assert by_key["C4"].current_count == 0 and by_key["C4"].previous_count == 2
    assert by_key["C4"].decreased is True
    # C6 has no snapshot row at all and is bucketed, not dropped.
    assert by_key["C6"].dimensions["segment"] == "Unknown"


def test_run_analysis_refuses_an_unresolved_snapshot_tie(pilot):
    with pytest.raises(SnapshotSelectionError) as exc:
        run_analysis(pilot, _ir(), dialect=PostgresDialect())
    assert exc.value.code == TEMPORAL_SNAPSHOT_TIE


def test_two_row_rules_for_one_dimension_source_is_a_contradiction(pilot):
    with pytest.raises(AnalysisIRError, match="ANALYSIS_TWO_ROW_RULES"):
        _ir(attribution=DimensionAttributionPolicyV1(
            attribution_basis=AttributionBasis.REPORT_CUTOFF,
            effective_from_column="effective_from", effective_to_column="effective_to",
            report_cutoff=REPORT_CUTOFF,
            missing_value_behavior=MissingValueBehavior.UNKNOWN_BUCKET))


def test_a_per_table_snapshot_cannot_be_a_per_customer_dimension(pilot):
    with pytest.raises(AnalysisIRError, match="ANALYSIS_SNAPSHOT_SCOPE_UNJOINABLE"):
        _ir(snapshot_selection=LatestSnapshotPolicyV1(
            snapshot_column="snapshot_date", cutoff=REPORT_CUTOFF,
            scope=SnapshotScope.PER_TABLE))


def test_the_snapshot_selection_changes_the_plan_hash(pilot):
    base = _ir().plan_hash
    assert _ir(snapshot_selection=_snapshot(cutoff="2026-05-31")).plan_hash != base
    assert _ir(snapshot_selection=_snapshot(tie_break_columns=("load_seq",))).plan_hash != base


def test_the_snapshot_MISSING_VALUE_BEHAVIOUR_is_part_of_the_plan_hash(pilot):
    """The review's probe: `compile_analysis` BRANCHES on this field — one statement COALESCEs the
    unclassified customer into a named bucket, the other leaves the group NULL — and the two IRs
    shared one plan_hash. That is a cached answer computed under the other definition."""
    retain = _ir(snapshot_selection=_snapshot(
        missing_value_behavior=MissingValueBehavior.RETAIN_NULL))
    bucket = _ir(snapshot_selection=_snapshot(
        missing_value_behavior=MissingValueBehavior.UNKNOWN_BUCKET))
    assert retain.plan_hash != bucket.plan_hash
    # ... and the two statements really are different, which is why the identity has to be.
    dialect = PostgresDialect()
    assert compile_analysis(retain, dialect=dialect) != compile_analysis(bucket, dialect=dialect)


def test_a_plan_that_carries_NO_snapshot_selection_hashes_exactly_as_it_did():
    """The identity change is scoped to plans carrying a `snapshot_selection`, and NONE exist
    outside this release — the field shipped on this branch. The pre-Task-8 IR keeps its literal,
    which is what makes "append only, only when present" a checked claim rather than a comment."""
    from tests.featuregen.data_agent.test_analysis_ir import _ir as _pre_task8_ir

    assert _pre_task8_ir().plan_hash == "b876b7f5a812567aaab96394fd76d894"
    assert _pre_task8_ir().snapshot_selection is None
