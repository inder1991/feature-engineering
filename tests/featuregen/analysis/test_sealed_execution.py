"""A SEALED plan, executed — Release-B Task 9's "a preview that never executes does not satisfy
Release B", answered.

Against the FIXTURE engine: the pilot's own Postgres, the same connection the suite rolls back.
No cluster, no Hive, no live anything — the live run is a separate approval gate, and the shipped
engine provider refuses precisely so that gate cannot be walked through by accident.

What these prove:

  * the sealed plan is SELF-DESCRIBING enough to execute — its partitions, eligibility, comparison
    and row rule come out of the seal, not out of a clock;
  * the answer is the hand-counted one, including the customer who fell to zero;
  * a replay reads the SAME months tomorrow, because the windows are pinned rather than re-derived;
  * a stale plan REFUSES and runs nothing.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.featuregen.analysis.release_b_bank import (
    CUTOFF_REF,
    QUESTION,
    SRC,
    build_bank,
    pilot_plan,
    publish_temporal_policy_for,
)
from tests.featuregen.data_agent.pilot_fixture import (
    CURRENT_MONTH,
    EXPECTED,
    PREVIOUS_MONTH,
    TRANSACTION_TABLE,
)
from tests.featuregen.data_agent.test_analysis_ir import _policy as _eligibility

from featuregen.analysis.engine import (
    EXECUTION_ENGINE_NOT_APPROVED,
    AnalysisEngineUnavailable,
    AnalysisEngineV1,
    default_analysis_engine,
)
from featuregen.analysis.grounding import ground_analysis_plan
from featuregen.analysis.sealed_execution import (
    SealedRunV1,
    execution_inputs_for_plan,
    execution_inputs_from_sealed,
    rebuild_execution_ir,
    run_sealed_plan,
)
from featuregen.analysis.sealed_plan import (
    SEALED_PLAN_STALE_TEMPORAL_POLICY,
    SealedPlanError,
    build_execution_ir_v2,
)
from featuregen.analysis.sealed_plan_store import replay_sealed_plan, seal_analysis_plan
from featuregen.data_agent.binding_store import resolve_table
from featuregen.data_agent.eligibility_store import record_eligibility
from featuregen.data_agent.sql_postgres import PostgresDialect
from featuregen.overlay.upload.temporal_policy import TemporalSelectionKind

#: The instant the pilot question is asked AS OF. Pinned, because "last completed month" is a
#: question about now and the fixture's months are 2026-05 / 2026-06 — asking it in real time
#: would resolve to whatever this month happens to be and prove nothing about the answer.
REFERENCE = datetime(2026, 6, 30, 12, 0, tzinfo=UTC)


@pytest.fixture
def bank(db, monkeypatch):
    built = build_bank(db, monkeypatch)
    # WHICH ROWS COUNT — the one decision `plan_to_execution_ir` will not default. Stored rather
    # than passed in, because the route reads it from its store and this exercises that path.
    policy = _eligibility()
    record_eligibility(built, catalog_source=SRC, table=TRANSACTION_TABLE, policy=policy,
                       proposed_by="user:priya")
    return built


def _engine(bank) -> AnalysisEngineV1:
    """THE FIXTURE ENGINE: the pilot's own Postgres, which is also the catalog connection here."""
    return AnalysisEngineV1(conn=bank, dialect=PostgresDialect(), engine="postgres")


def _grounded(bank, **over):
    return ground_analysis_plan(bank, pilot_plan(**over), cutoff_value_ref=CUTOFF_REF,
                                recorded_by="task9", roles=())


def _seal(bank, *, reference=REFERENCE):
    grounded = _grounded(bank)
    inputs = execution_inputs_for_plan(bank, grounded, reference=reference)
    assert inputs is not None, "the assembler found nothing to run"
    from featuregen.analysis.execution import plan_to_execution_ir

    ir = build_execution_ir_v2(plan_to_execution_ir(grounded, inputs), grounded.selections,
                               question=QUESTION)
    return seal_analysis_plan(bank, ir, sealed_by="user:priya", question=QUESTION)


# ── the assembler ────────────────────────────────────────────────────────────────────────────────


def test_the_plan_time_assembler_builds_REAL_inputs_from_the_resolved_selections(bank):
    """`_execution_inputs_or_none` returned None unconditionally. It no longer can."""
    inputs = execution_inputs_for_plan(bank, _grounded(bank), reference=REFERENCE)
    assert inputs is not None
    assert inputs.spine_binding.identity.table == "customer_master"
    assert inputs.event_binding.identity.table == TRANSACTION_TABLE
    assert inputs.dimension_binding.identity.table == "customer_segment_history"
    assert inputs.window_partitions == {"current": (CURRENT_MONTH,),
                                        "previous": (PREVIOUS_MONTH,)}
    assert inputs.eligibility is not None
    assert inputs.attribution is not None
    # The cutoff VALUE is the instant the windows were measured back from — never a second clock
    # read, or the segment is attributed at a moment the periods do not correspond to.
    assert inputs.attribution.report_cutoff == "2026-06-30"


def test_the_assembler_returns_NOTHING_while_the_flag_is_off(bank, monkeypatch):
    monkeypatch.delenv("FEATUREGEN_SOURCE_TEMPORAL_SELECTION", raising=False)
    grounded = _grounded(bank)
    assert grounded.selections is None
    assert execution_inputs_for_plan(bank, grounded, reference=REFERENCE) is None


def test_a_plan_that_declares_no_partition_CALENDAR_assembles_nothing(bank):
    """Nothing records whether a partition column names months or days, and guessing from a value
    like `2026-05` fails silently on a table partitioned by something else."""
    from dataclasses import replace

    grounded = _grounded(bank)
    plan = replace(grounded.plan, windows=tuple(
        replace(w, calendar_unit="") for w in grounded.plan.windows))
    assert execution_inputs_for_plan(bank, replace(grounded, plan=plan),
                                     reference=REFERENCE) is None


# ── executing from the seal ──────────────────────────────────────────────────────────────────────


def test_the_sealed_plan_rebuilds_into_the_EXACT_computation_that_was_sealed(bank):
    record = _seal(bank)
    ir = rebuild_execution_ir(record, execution_inputs_from_sealed(bank, record))
    assert ir.identity_payload() == dict(record.computation)
    # The IR the executor runs is a V1: one executor, one compiler, no V2 awareness downstream.
    assert ir.comparison.value == "decreased"


def test_a_plan_sealed_by_a_build_THIS_one_cannot_reproduce_REFUSES(bank):
    """The version-skew case the check exists for.

    Tampering with a VALUE cannot trigger it — the rebuild reads every value faithfully, which is
    the property being asserted. What it catches is a seal carrying a fact this build's identity
    payload does not produce: a plan hashed by a NEWER build. Running it would report that plan's
    answer under a computation this build reconstructed differently, so it refuses instead.
    """
    from dataclasses import replace

    record = _seal(bank)
    from_the_future = replace(record, computation={**record.computation,
                                                   "currency_scope": "AED"})
    with pytest.raises(SealedPlanError, match="does not reproduce"):
        rebuild_execution_ir(from_the_future,
                             execution_inputs_from_sealed(bank, from_the_future))


def test_running_the_sealed_plan_gives_the_HAND_COUNTED_answer(bank):
    outcome = run_sealed_plan(bank, _seal(bank), engine=_engine(bank))
    assert isinstance(outcome, SealedRunV1)
    rows = {r.key: r for r in outcome.rows}
    assert set(rows) == {"C1", "C2", "C3", "C4", "C5", "C6"}
    assert tuple(sorted(k for k, r in rows.items() if r.decreased)) == \
        EXPECTED["decreased_customers"]


def test_the_customer_who_fell_to_ZERO_is_in_the_answer(bank):
    """C4 is the customer the question is most about, and an inner join loses them silently."""
    outcome = run_sealed_plan(bank, _seal(bank), engine=_engine(bank))
    rows = {r.key: r for r in outcome.rows}
    assert (rows["C4"].previous_count, rows["C4"].current_count) == (2, 0)


def test_the_segments_are_the_ones_valid_AT_THE_SEALED_CUTOFF(bank):
    outcome = run_sealed_plan(bank, _seal(bank), engine=_engine(bank))
    rows = {r.key: r for r in outcome.rows}
    assert {k: r.dimensions["segment"] for k, r in rows.items()} == EXPECTED["segment_at_cutoff"]


def test_a_REPLAY_reads_the_sealed_months_rather_than_re_deriving_them(bank):
    """The property that makes a replay a replay. Executing the same sealed plan under a reference
    instant two months later still reads 2026-05 and 2026-06, because the partitions are PINNED —
    a plan that re-derived its windows would read different months and still claim one identity."""
    record = _seal(bank)
    inputs = execution_inputs_from_sealed(bank, record)
    assert inputs.window_partitions == {"current": (CURRENT_MONTH,),
                                        "previous": (PREVIOUS_MONTH,)}
    later = run_sealed_plan(bank, replay_sealed_plan(bank, record.plan_hash),
                            engine=_engine(bank))
    assert {r.key for r in later.rows} == {"C1", "C2", "C3", "C4", "C5", "C6"}


def test_a_STALE_plan_refuses_and_runs_NOTHING(bank):
    record = _seal(bank)
    publish_temporal_policy_for(bank, expected_pointer_version=1,
                                historical_selection=TemporalSelectionKind.CURRENT_RECORD)

    outcome = run_sealed_plan(bank, record, engine=_engine(bank))
    assert not isinstance(outcome, SealedRunV1)
    assert outcome.code == SEALED_PLAN_STALE_TEMPORAL_POLICY


def test_a_sealed_plan_whose_source_MOVED_refuses_before_it_reads_a_row(bank):
    record = _seal(bank)
    bank.execute("UPDATE physical_dataset_binding SET database_name = %s WHERE catalog_source = %s",
                 ("featuregen_dr", SRC))
    outcome = run_sealed_plan(bank, record, engine=_engine(bank))
    assert not isinstance(outcome, SealedRunV1)
    assert "sealed against" in outcome.detail or "different physical object" in outcome.detail


# ── the flag law ─────────────────────────────────────────────────────────────────────────────────


def _sealed_rows(bank) -> int:
    from featuregen.analysis.sealed_plan import ANALYSIS_PLAN_CONTRACT

    return bank.execute("SELECT count(*) FROM structured_result WHERE result_type = %s",
                        (ANALYSIS_PLAN_CONTRACT,)).fetchone()[0]


def test_FLAG_OFF_previews_exactly_as_it_did_and_seals_nothing(bank, monkeypatch):
    """Off means OFF: no selections, no assembled inputs, no SQL, no seal, and the blocked reason
    is the one `analysis.assembly` has always given. Nothing is read to decide this — the flag is
    checked before any query."""
    from featuregen.api.routes.analysis import _previewed

    monkeypatch.delenv("FEATUREGEN_SOURCE_TEMPORAL_SELECTION", raising=False)
    view, sealed = _previewed(bank, _grounded(bank), reference=REFERENCE)
    assert sealed is None
    assert view.runnable is False
    assert view.sql == ""
    assert view.blocked_by is not None
    assert _sealed_rows(bank) == 0


def test_FLAG_ON_assembles_the_plan_and_seals_exactly_one(bank):
    """On means the V2 path: the preview shows the statement that would run, and the caller gets
    the identity to execute it by."""
    from featuregen.api.routes.analysis import _previewed

    view, sealed = _previewed(bank, _grounded(bank), reference=REFERENCE)
    assert sealed is not None
    assert view.runnable is True
    assert "coalesce" in view.sql.lower()          # the LEFT-joined spine, compiled for real
    assert _sealed_rows(bank) == 1
    # Re-previewing the same plan does not manufacture a second one.
    again_view, again_sealed = _previewed(bank, _grounded(bank), reference=REFERENCE)
    assert again_sealed == sealed
    assert again_view.plan_hash == view.plan_hash
    assert _sealed_rows(bank) == 1


# ── the engine seam ──────────────────────────────────────────────────────────────────────────────


def test_the_shipped_engine_provider_REFUSES_and_names_the_approval(bank):
    """Running against the bank's warehouse is a separate approval gate. A provider that opened the
    connection anyway would take that decision on someone's behalf the first time a route was hit."""
    _binding, connection = resolve_table(bank, catalog_source=SRC, table=TRANSACTION_TABLE)
    with pytest.raises(AnalysisEngineUnavailable) as exc:
        default_analysis_engine(connection)
    assert exc.value.code == EXECUTION_ENGINE_NOT_APPROVED
    assert exc.value.subject == connection.connection_id
