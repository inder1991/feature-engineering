"""Refusals become learning evidence — the second unbuilt half of Release 3.

The release text is explicit about why this cannot wait for Release 5:

    Start recording learning evidence here, not in Release 5: unresolved semantic term, missing
    relationship, ambiguous period, missing dimension, and every failed or refused plan reason.
    **Otherwise the first working question's evidence is discarded.**

`learning.py` was built to receive it — immutable events, gap identity separate from demand, a
closed functional vocabulary — and then nothing in `src/` ever called `record_gap`. A recorder with
no producer records nothing, so the first pilot run would have thrown away exactly the evidence the
release was told to capture.

**The boundary this file defends** is the one `learning.py` exists to enforce: a refusal that is a
capability limit, a malformed request or a technical failure must NOT become an ontology candidate.
`test_a_capability_limit_is_not_an_ontology_gap` and its neighbours are the enforcement; the mapping
table is deliberately closed so a new refusal code is silently ignored rather than silently
promoted.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from featuregen.data_agent.analysis import (
    AnalysisExecutionIRV1,
    AnalysisIRError,
    Period,
    recording_refusals,
    run_analysis,
)
from featuregen.data_agent.dimensions import AttributionError
from featuregen.data_agent.learning import (
    GAP_CODES,
    REFUSAL_TO_GAP,
    open_gaps,
    record_refusal,
)
from featuregen.data_agent.sql_postgres import PostgresDialect
from tests.featuregen.data_agent.pilot_fixture import create_pilot_tables
from tests.featuregen.data_agent.test_analysis_ir import _attribution, _ir, _policy

_NOW = datetime(2026, 7, 29, tzinfo=UTC)
_SNAPSHOT = "snap-release3"
_SUBJECTS = ("ftr::dpl_eib.customer_master", "ftr::dpl_eib.tran_repos")


@pytest.fixture
def pilot(db):
    create_pilot_tables(db)
    return db


def _record(conn, code, **over):
    kw = dict(analysis_request_id="req-1", refusal_code=code, subject_refs=_SUBJECTS,
              dependency_snapshot_id=_SNAPSHOT, now=_NOW)
    kw.update(over)
    return record_refusal(conn, **kw)


# ── the boundary: what may NOT become ontology evidence ──────────────────────────────────────────

@pytest.mark.parametrize("code", [
    "ANALYSIS_UNSUPPORTED_MEASURE",          # a capability limit — the ontology is not missing
    "ATTRIBUTION_UNSUPPORTED_BASIS",         # ditto
    "ANALYSIS_EMPTY_PERIOD",                 # a malformed request
    "ELIGIBILITY_NO_STATUS_VALUES",          # a malformed policy
    "ATTRIBUTION_OVERLAPPING_RECORDS",       # a DATA defect — belongs to data quality
    "HIVE_CONNECTION_TIMEOUT",               # technical: the case learning.py was written to refuse
])
def test_a_refusal_that_is_not_an_ontology_gap_records_NOTHING(pilot, code):
    """`sum(a) == 0` is the assertion that matters: not "it did not raise" but "no event exists".
    A timeout that becomes "customer relationship missing" fills the ontology with candidates
    manufactured by an outage."""
    assert _record(pilot, code) is None
    assert open_gaps(pilot) == ()


def test_the_mapping_only_emits_codes_the_vocabulary_allows(pilot):
    """A typo in the mapping table would be caught at record time by `AnalysisLearningEventV1`, but
    only for a code that someone actually triggers. Checking the table itself catches it now."""
    for refusal, (gap_code, _) in REFUSAL_TO_GAP.items():
        assert gap_code in GAP_CODES, f"{refusal} maps to unknown gap code {gap_code}"


# ── the gaps that ARE real ───────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("refusal,gap", [
    ("JOIN_EVIDENCE_MISSING", "RELATIONSHIP_UNVERIFIED"),
    ("JOIN_EVIDENCE_MISMATCHED", "RELATIONSHIP_UNVERIFIED"),
    ("JOIN_KEY_NOT_UNIQUE", "RELATIONSHIP_UNVERIFIED"),
    ("JOIN_UNIQUENESS_UNKNOWN", "JOIN_CARDINALITY_UNKNOWN"),
    ("ANALYSIS_NO_ATTRIBUTION_POLICY", "DIMENSION_ATTRIBUTION_AS_OF_UNRESOLVED"),
    ("ATTRIBUTION_NO_CUTOFF", "DIMENSION_ATTRIBUTION_AS_OF_UNRESOLVED"),
    ("ELIGIBILITY_UNSUPPORTED_REVERSAL_MODE", "REVERSAL_AS_OF_UNRESOLVED"),
    ("ANALYSIS_NO_ELIGIBILITY_POLICY", "MEASURE_DEFINITION_UNRESOLVED"),
])
def test_a_functional_refusal_becomes_the_right_gap(pilot, refusal, gap):
    assert _record(pilot, refusal) is not None
    (recorded,) = open_gaps(pilot)
    assert recorded.code == gap


def test_the_parked_business_decisions_arrive_with_their_CHOICES(pilot):
    """The two decisions parked all session — "is a transaction reversed as of the cutoff or ever?"
    and "which instant classifies a customer?" — are exactly the gaps whose answer is a closed
    vocabulary. Recording them without the options would leave the decision-maker to rediscover
    what they were choosing between."""
    _record(pilot, "ELIGIBILITY_UNSUPPORTED_REVERSAL_MODE")
    (gap,) = open_gaps(pilot)
    assert gap.choices == ("reversed_by_cutoff", "reversed_at_any_time")


# ── demand: the same gap blocking two questions is ONE thing to decide ───────────────────────────

def test_two_questions_blocked_by_one_gap_produce_one_gap_with_demand_2(pilot):
    """This is the prioritisation signal the release wants and the reason identity excludes the
    request id. Two teams blocked on the same undecided relationship is a stronger case for
    deciding it than two unrelated gaps."""
    _record(pilot, "JOIN_EVIDENCE_MISSING", analysis_request_id="req-1")
    _record(pilot, "JOIN_EVIDENCE_MISSING", analysis_request_id="req-2")
    (gap,) = open_gaps(pilot)
    assert gap.blocked_requests == 2


def test_re_running_the_same_blocked_question_is_not_new_information(pilot):
    first = _record(pilot, "JOIN_EVIDENCE_MISSING")
    again = _record(pilot, "JOIN_EVIDENCE_MISSING")
    assert first == again
    assert len(open_gaps(pilot)) == 1


# ── the producer: a refused analysis records without the caller remembering to ───────────────────

def test_a_refused_ANALYSIS_records_its_gap(pilot):
    """The wiring that was missing. `run_analysis` refuses an unverified join; the recording seam
    turns that refusal into evidence without the caller doing anything but naming the request."""
    with pytest.raises(AnalysisIRError):
        with recording_refusals(pilot, analysis_request_id="req-pilot",
                                dependency_snapshot_id=_SNAPSHOT, subjects=_SUBJECTS, now=_NOW):
            run_analysis(pilot, _ir(join_evidence=None), dialect=PostgresDialect())
    (gap,) = open_gaps(pilot)
    assert gap.code == "RELATIONSHIP_UNVERIFIED"


def test_the_recorded_gap_names_the_COLUMNS_not_just_the_request(pilot):
    """A gap saying "something about these two tables" is not actionable. The refusal carries its
    own subjects and they take precedence over the caller's request-level ones."""
    with pytest.raises(AnalysisIRError):
        with recording_refusals(pilot, analysis_request_id="req-pilot",
                                dependency_snapshot_id=_SNAPSHOT, subjects=_SUBJECTS, now=_NOW):
            run_analysis(pilot, _ir(join_evidence=None), dialect=PostgresDialect())
    (gap,) = open_gaps(pilot)
    assert any("cif_id" in s for s in gap.subject_refs), gap.subject_refs


def test_a_refusal_raised_while_BUILDING_the_plan_is_recorded_too(pilot):
    """Half the refusals happen in `__post_init__`, before there is an IR to execute — a missing
    attribution policy is refused at construction. A seam that only wrapped execution would record
    none of them, which is why this is a context manager around both steps rather than a wrapper
    around `run_analysis`."""
    with pytest.raises(AnalysisIRError):
        with recording_refusals(pilot, analysis_request_id="req-build",
                                dependency_snapshot_id=_SNAPSHOT, subjects=_SUBJECTS, now=_NOW):
            _ir(attribution=None)
    (gap,) = open_gaps(pilot)
    assert gap.code == "DIMENSION_ATTRIBUTION_AS_OF_UNRESOLVED"
    assert gap.choices == ("report_cutoff", "period_end_per_period", "transaction_event_time",
                           "current_value")


def test_an_ATTRIBUTION_error_is_recorded_as_well_as_an_analysis_one(pilot):
    """The seam catches the three refusal types the analysis path can raise. `AttributionError` and
    `EligibilityError` are separate types precisely so a caller need not catch the wrong one — but
    the recorder must still see all of them, or a whole class of refusal is silently lost."""
    with pytest.raises(AttributionError):
        with recording_refusals(pilot, analysis_request_id="req-cutoff",
                                dependency_snapshot_id=_SNAPSHOT, subjects=_SUBJECTS, now=_NOW):
            _attribution(report_cutoff="")
    (gap,) = open_gaps(pilot)
    assert gap.code == "DIMENSION_ATTRIBUTION_AS_OF_UNRESOLVED"


def test_a_SUCCESSFUL_analysis_records_no_gap(pilot):
    """The seam must be silent on success. A gap recorded for a question that worked would inflate
    demand for a decision nobody is waiting on."""
    with recording_refusals(pilot, analysis_request_id="req-ok",
                            dependency_snapshot_id=_SNAPSHOT, subjects=_SUBJECTS, now=_NOW):
        rows = run_analysis(pilot, _ir(), dialect=PostgresDialect())
    assert rows
    assert open_gaps(pilot) == ()


def test_a_non_ontological_refusal_propagates_AND_records_nothing(pilot):
    """Both halves of one behaviour, because either alone is misleading.

    Recording is observation, not handling: if the seam absorbed the exception the caller would
    proceed as though the analysis had succeeded. And "it raised" must not be read as "it was
    recorded" — an empty period is a malformed request, so nothing is waiting on a human.
    """
    with pytest.raises(AnalysisIRError) as exc:
        with recording_refusals(pilot, analysis_request_id="req-1",
                                dependency_snapshot_id=_SNAPSHOT, subjects=_SUBJECTS, now=_NOW):
            AnalysisExecutionIRV1(
                **{**_ir_kwargs(), "current": Period(label="current", values=())})
    assert exc.value.code == "ANALYSIS_EMPTY_PERIOD"
    assert open_gaps(pilot) == ()


def _ir_kwargs() -> dict:
    """The pilot IR's parts, for tests that need to break exactly one of them."""
    ir = _ir()
    return dict(question=ir.question, spine=ir.spine, event_binding=ir.event_binding,
                event_key_column=ir.event_key_column, period_column=ir.period_column,
                current=ir.current, previous=ir.previous, eligibility=_policy(),
                dimensions=ir.dimensions, dimension_binding=ir.dimension_binding,
                attribution=_attribution(), join_evidence=ir.join_evidence)
