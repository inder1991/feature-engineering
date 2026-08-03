"""THE WORKED ACCEPTANCE for Release-B Task 8, end to end against fixtures.

    "customers whose transaction count decreased last completed month, by segment and sector"

The plan's own exit criterion, run rather than described:

  * the POPULATION remains the DECLARED customer master — explicit, never inferred, and the
    selection says so (`selection_basis=explicit_request`);
  * the TRANSACTIONS come from the event source a SERVING POLICY names, with the archive copy
    recorded as an eligible candidate that lost rather than as an absence;
  * SEGMENT and SECTOR use the SCD2 row VALID AT THE REPORT CUTOFF, resolved from the dataset's
    temporal policy and compiled by the existing half-open interval engine;
  * a customer with ZERO transactions in the current month is PRESENT in the result — C4, who is
    precisely the customer the question is about;
  * the selection and row-decision payloads are visible, with EVERY candidate's disposition.

No live anything: fixtures, the local Postgres dialect, and the same code path a real run takes.
"""
from __future__ import annotations

import pytest
from tests.featuregen.analysis.release_b_bank import build_bank
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
)
from tests.featuregen.data_agent.test_analysis_ir import _policy as _eligibility

from featuregen.analysis.execution import ExecutionInputs, plan_to_execution_ir
from featuregen.analysis.grounding import ground_analysis_plan
from featuregen.analysis.plan import AnalysisPlanV1, Dimension, Measure, Window
from featuregen.data_agent.analysis import run_analysis
from featuregen.data_agent.binding_store import (
    resolve_table,
)
from featuregen.data_agent.sql_postgres import PostgresDialect
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.source_selection import (
    CandidateDisposition,
    DatasetNeedRole,
    SelectionBasis,
)
from featuregen.overlay.upload.temporal_policy import (
    TemporalSelectionKind,
)
from featuregen.overlay.upload.temporal_policy_store import (
    current_temporal_policy,
)
from featuregen.overlay.upload.temporal_resolver import attribution_policy_for

_SRC = "ftr"
_ARCHIVE_TABLE = "tran_archive"

CUST = normalize_ref(_SRC, CUSTOMER_SCHEMA, CUSTOMER_TABLE)
CUST_KEY = normalize_ref(_SRC, CUSTOMER_SCHEMA, CUSTOMER_TABLE, "cif_id")
TRAN = normalize_ref(_SRC, TRANSACTION_SCHEMA, TRANSACTION_TABLE)
ARCHIVE = normalize_ref(_SRC, TRANSACTION_SCHEMA, _ARCHIVE_TABLE)
TRAN_KEY = normalize_ref(_SRC, TRANSACTION_SCHEMA, TRANSACTION_TABLE, "cif_id")
TRAN_MONTH = normalize_ref(_SRC, TRANSACTION_SCHEMA, TRANSACTION_TABLE, "tran_month")
SEG = normalize_ref(_SRC, CUSTOMER_SCHEMA, DIMENSION_TABLE)
SEG_COL = normalize_ref(_SRC, CUSTOMER_SCHEMA, DIMENSION_TABLE, "segment")
SEC_COL = normalize_ref(_SRC, CUSTOMER_SCHEMA, DIMENSION_TABLE, "sector")
SEG_FROM = normalize_ref(_SRC, CUSTOMER_SCHEMA, DIMENSION_TABLE, "effective_from")
SEG_TO = normalize_ref(_SRC, CUSTOMER_SCHEMA, DIMENSION_TABLE, "effective_to")
SEG_FLAG = normalize_ref(_SRC, CUSTOMER_SCHEMA, DIMENSION_TABLE, "is_current")

CUTOFF_REF = "report_cutoff_param"
QUESTION = ("customers whose transaction count decreased last completed month, "
            "by segment and sector")


@pytest.fixture
def bank(db, monkeypatch):
    """The pilot's real tables PLUS the catalog, policies and physical routing that address them.

    The setup now lives in `release_b_bank` because Task 9's seal/revalidation/explain/execute
    suites need the SAME bank, and three hand-rolled copies of a fixture this load-bearing is how
    two suites end up asserting against two different banks and both passing. Behaviour is
    unchanged: `database_name` is still the test database, so the derived binding's `table_id`
    equals the one `PILOT_JOIN_EVIDENCE` was observed against — otherwise the verified-join gate
    would refuse the very plan this acceptance is about, which is the gate working and would hide
    the acceptance.
    """
    return build_bank(db, monkeypatch)


def _plan() -> AnalysisPlanV1:
    return AnalysisPlanV1(
        question=QUESTION, entity="customer", entity_ref=TRAN_KEY, base_table_ref=TRAN,
        measure=Measure(op="count"),
        windows=(Window(anchor_ref=TRAN_MONTH, length_days=30, offset_days=0, label="current"),
                 Window(anchor_ref=TRAN_MONTH, length_days=30, offset_days=30, label="previous")),
        dimensions=(Dimension(logical_ref=SEG_COL), Dimension(logical_ref=SEC_COL)),
        comparison="decrease",
        # THE DECLARATION. `spine.py`'s doctrine: governed facts may VALIDATE this, never make it.
        population_table_ref=CUST, population_key_ref=CUST_KEY)


def _grounded(bank):
    return ground_analysis_plan(bank, _plan(), cutoff_value_ref=CUTOFF_REF,
                                recorded_by="acceptance")


def _by_role(selections):
    return {s.need.need_role: s for s in selections.source_selections}


# ── the three decisions ─────────────────────────────────────────────────────────────────────────


def test_the_population_is_the_DECLARED_customer_master(bank):
    selections = _grounded(bank).selections
    population = _by_role(selections)[DatasetNeedRole.POPULATION]
    assert population.selected_dataset_ref == CUST
    assert population.selection_basis is SelectionBasis.EXPLICIT_REQUEST
    assert population.serving_policy_revision_id is None


def test_the_transactions_come_from_the_event_source_the_policy_names(bank):
    selections = _grounded(bank).selections
    event = _by_role(selections)[DatasetNeedRole.EVENT_SOURCE]
    assert event.selected_dataset_ref == TRAN
    assert event.selection_basis is SelectionBasis.SERVING_POLICY
    assert event.serving_policy_revision_id is not None


def test_segment_and_sector_use_the_SCD2_row_valid_at_the_report_cutoff(bank):
    selections = _grounded(bank).selections
    assert [r.dataset_logical_ref for r in selections.row_selections] == [SEG]
    row = selections.row_selections[0]
    assert row.selection_kind is TemporalSelectionKind.VALID_AT_REPORT_CUTOFF
    # HALF-OPEN [from, to), as REFS — the cutoff VALUE is a runtime parameter, never in the record.
    assert [(p["column_ref"], p["operator"]) for p in row.predicate_payloads] == [
        (SEG_FROM, "<="), (SEG_TO, ">")]
    assert row.cutoff_value_ref == CUTOFF_REF


def test_every_candidate_considered_carries_its_disposition(bank):
    """Functional rule 9, on the decision a person is most likely to question: the archive copy is
    recorded as an eligible candidate that LOST, not as something nobody looked at."""
    event = _by_role(_grounded(bank).selections)[DatasetNeedRole.EVENT_SOURCE]
    dispositions = {c.dataset_ref: (c.disposition, c.reason_codes)
                    for c in event.considered_candidates}
    assert dispositions[TRAN][0] is CandidateDisposition.SELECTED
    assert "policy_preferred" in dispositions[TRAN][1]
    assert dispositions[ARCHIVE][0] is CandidateDisposition.ELIGIBLE
    assert "policy_eligible" in dispositions[ARCHIVE][1]
    # Only the WINNER names a persisted binding revision; a loser naming a `pbr_` id would point at
    # a row that was never written.
    assert event.selected_binding_revision_id.startswith("pbr_")
    losing = [c for c in event.considered_candidates if c.dataset_ref == ARCHIVE][0]
    assert losing.binding_revision_id is None


def test_the_decision_payloads_are_visible_on_the_plan(bank):
    """"Separately explainable" (§5.7) is only true if a preview can SHOW both halves."""
    selections = _grounded(bank).selections
    assert len(selections.source_selections) == 3
    assert len(selections.row_selections) == 1
    assert selections.resolved and selections.refusals == ()
    assert all(s.content_hash for s in selections.source_selections)
    assert all(r.content_hash for r in selections.row_selections)


# ── the answer itself ───────────────────────────────────────────────────────────────────────────


def _execute(bank):
    """Run the pilot question through the SELECTED sources and the RESOLVED row rule."""
    grounded = _grounded(bank)
    selections = grounded.selections
    by_role = _by_role(selections)

    def _binding(ref: str):
        table = ref.rsplit(".", 1)[-1]
        resolved = resolve_table(bank, catalog_source=_SRC, table=table)
        assert resolved is not None, ref
        return resolved[0]

    spine = _binding(by_role[DatasetNeedRole.POPULATION].selected_dataset_ref)
    events = _binding(by_role[DatasetNeedRole.EVENT_SOURCE].selected_dataset_ref)
    dimension = _binding(by_role[DatasetNeedRole.DIMENSION_SOURCE].selected_dataset_ref)
    # The BINDING the decision pinned is the binding the run addresses — not a look-alike.
    assert spine.binding_revision_id == by_role[
        DatasetNeedRole.POPULATION].selected_binding_revision_id

    policy, _pointer = current_temporal_policy(bank, SEG)
    attribution = attribution_policy_for(
        policy, kind=selections.row_selections[0].selection_kind, cutoff_value=REPORT_CUTOFF)

    ir = plan_to_execution_ir(grounded, ExecutionInputs(
        spine_binding=spine, spine_key_column="cif_id",
        event_binding=events, event_key_column="cif_id", period_column="tran_month",
        window_partitions={"current": (CURRENT_MONTH,), "previous": (PREVIOUS_MONTH,)},
        eligibility=_eligibility(), dimension_binding=dimension, attribution=attribution,
        join_evidence=PILOT_JOIN_EVIDENCE))
    return run_analysis(bank, ir, dialect=PostgresDialect())


def test_a_customer_who_fell_to_ZERO_transactions_is_in_the_answer(bank):
    """C4 is the customer the question is most about, and an inner join loses them silently."""
    rows = {r.key: r for r in _execute(bank)}
    assert set(rows) == {"C1", "C2", "C3", "C4", "C5", "C6"}
    assert rows["C4"].current_count == 0
    assert rows["C4"].previous_count == 2
    assert rows["C4"].decreased is True


def test_the_answer_matches_the_hand_counted_expectation(bank):
    rows = {r.key: r for r in _execute(bank)}
    decreased = tuple(sorted(k for k, r in rows.items() if r.decreased))
    assert decreased == EXPECTED["decreased_customers"]


def test_the_segments_are_the_ones_valid_AT_THE_CUTOFF_not_today(bank):
    """The whole point of the row decision. C2 changes segment AFTER the cutoff and must still read
    RETAIL; C1 changed BEFORE it and must read SME."""
    rows = {r.key: r for r in _execute(bank)}
    assert {k: r.dimensions["segment"] for k, r in rows.items()} == EXPECTED["segment_at_cutoff"]
    by_segment: dict[str, int] = {}
    for row in rows.values():
        if row.decreased:
            by_segment[row.dimensions["segment"]] = by_segment.get(
                row.dimensions["segment"], 0) + 1
    assert by_segment == EXPECTED["decreased_by_segment"]


def test_with_the_flag_OFF_the_same_plan_grounds_exactly_as_it_did(bank, monkeypatch):
    monkeypatch.delenv("FEATUREGEN_SOURCE_TEMPORAL_SELECTION", raising=False)
    grounded = ground_analysis_plan(bank, _plan())
    assert grounded.selections is None
    assert grounded.answerable is True
