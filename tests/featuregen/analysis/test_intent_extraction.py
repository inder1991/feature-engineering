"""Question -> candidate plan, with a bounded vocabulary the model cannot escape.

The whole reason this is not text-to-SQL: a model handed column names writes confident wrong SQL on a
bank catalog, and the wrongness is invisible in the output. Here the model only CHOOSES among catalog
objects it was given, and two properties make that safe:

* a ref it was not offered is rejected — proven by `test_a_hallucinated_column_is_rejected`, which is
  the difference between a hallucination and a plausible plan that grounds against nothing;
* abstention is a real answer — `unresolved` leaves the field EMPTY instead of inventing a dimension
  or a window, because a plan filled with guesses cannot be told apart from a plan that was understood.

Hermetic: `FakeLLM`, no network, no key.
"""
from __future__ import annotations

import pytest

from featuregen.analysis.intent import (
    INTENT_SCHEMA,
    IntentCandidates,
    IntentUnavailable,
    extract_intent,
    validate_intent,
)
from featuregen.contracts import SchemaValidationError
from featuregen.intake.llm import (
    PROVIDER_REFUSAL,
    STATUS_OK,
    STATUS_REPAIRED,
    FakeLLM,
    FakeResponse,
)

_QUESTION = "which customers had fewer transactions this month than last, by segment and sector"

_COLUMNS = frozenset({
    "ftr::dpl_eib.tran_repos.cif_id",
    "ftr::dpl_eib.tran_repos.tran_month",
    "ftr::dpl_eib.tran_repos.tran_amt",
    "ftr::dpl_eib.customer_segment_history.segment",
    "ftr::dpl_eib.customer_segment_history.sector",
})
_TABLES = frozenset({"ftr::dpl_eib.tran_repos", "ftr::dpl_eib.customer_segment_history"})


def _candidates() -> IntentCandidates:
    return IntentCandidates(column_refs=_COLUMNS, table_refs=_TABLES,
                            labels={"ftr::dpl_eib.tran_repos.cif_id": "customer identifier"})


def _output(**over) -> dict:
    out = {
        "entity": "customer",
        "entity_ref": "ftr::dpl_eib.tran_repos.cif_id",
        "base_table_ref": "ftr::dpl_eib.tran_repos",
        "measure": {"op": "count", "logical_ref": ""},
        "windows": [
            {"label": "current", "anchor_ref": "ftr::dpl_eib.tran_repos.tran_month",
             "calendar_unit": "month", "calendar_length": 1, "calendar_offset": 0},
            {"label": "previous", "anchor_ref": "ftr::dpl_eib.tran_repos.tran_month",
             "calendar_unit": "month", "calendar_length": 1, "calendar_offset": 1},
        ],
        "dimensions": [{"logical_ref": "ftr::dpl_eib.customer_segment_history.segment"},
                       {"logical_ref": "ftr::dpl_eib.customer_segment_history.sector"}],
        "comparison": "decrease",
        "unresolved": [],
    }
    out.update(over)
    return out


def _llm(*responses: FakeResponse) -> FakeLLM:
    from featuregen.analysis.intent import TASK
    return FakeLLM(script={TASK: list(responses) or [FakeResponse(output=_output())]})


# ── the pilot question ───────────────────────────────────────────────────────────────────────────

def test_the_pilot_question_becomes_a_candidate_plan():
    got = extract_intent(_llm(), _QUESTION, _candidates())
    plan = got.plan
    assert plan.question == _QUESTION
    assert plan.entity == "customer"
    assert plan.comparison == "decrease"
    assert plan.measure.op == "count"
    assert [w.label for w in plan.windows] == ["current", "previous"]
    assert [d.logical_ref for d in plan.dimensions] == [
        "ftr::dpl_eib.customer_segment_history.segment",
        "ftr::dpl_eib.customer_segment_history.sector"]
    assert not got.needs_clarification


def test_the_windows_arrive_as_whole_calendar_periods():
    """Day spans cannot express a calendar month, so the model is asked for units — and the offsets
    are what make "this month vs last" two distinct partitions."""
    plan = extract_intent(_llm(), _QUESTION, _candidates()).plan
    current, previous = plan.windows
    assert (current.calendar_unit, current.calendar_offset) == ("month", 0)
    assert (previous.calendar_unit, previous.calendar_offset) == ("month", 1)


def test_the_extracted_plan_flows_through_grounding_and_execution(db):
    """The whole chain in one test: question -> plan -> partitions -> IR -> SQL -> the fixture's
    hand-counted answer. Without this the pieces could each pass and not compose."""
    from datetime import UTC, datetime

    from featuregen.analysis.execution import plan_to_execution_ir
    from featuregen.analysis.plan import GroundedPlan
    from featuregen.analysis.windows import PartitionGranularity, resolve_window_partitions
    from featuregen.data_agent.analysis import run_analysis
    from featuregen.data_agent.sql_postgres import PostgresDialect
    from tests.featuregen.analysis.test_plan_to_execution import _inputs
    from tests.featuregen.data_agent.pilot_fixture import EXPECTED, create_pilot_tables

    create_pilot_tables(db)
    plan = extract_intent(_llm(), _QUESTION, _candidates()).plan
    partitions = resolve_window_partitions(
        plan.windows, granularity=PartitionGranularity.MONTH,
        reference=datetime(2026, 6, 30, tzinfo=UTC))
    ir = plan_to_execution_ir(GroundedPlan(plan=plan, answerable=True),
                              _inputs(window_partitions=partitions))
    rows = run_analysis(db, ir, dialect=PostgresDialect())
    assert tuple(sorted(r.key for r in rows if r.decreased)) == EXPECTED["decreased_customers"]


# ── the bounded vocabulary ───────────────────────────────────────────────────────────────────────

def test_a_hallucinated_column_is_rejected():
    """THE property. A ref the model was not offered must never pass: it would ground against nothing
    and read as a catalog gap rather than a model error."""
    with pytest.raises(SchemaValidationError, match="not one of the columns"):
        validate_intent(_output(entity_ref="ftr::dpl_eib.tran_repos.customer_name"), _candidates())


def test_a_hallucinated_TABLE_is_rejected():
    with pytest.raises(SchemaValidationError, match="not one of the tables"):
        validate_intent(_output(base_table_ref="ftr::dpl_eib.secret_table"), _candidates())


def test_a_hallucinated_DIMENSION_is_rejected():
    with pytest.raises(SchemaValidationError, match="dimension logical_ref"):
        validate_intent(
            _output(dimensions=[{"logical_ref": "ftr::dpl_eib.customer_segment_history.vip_tier"}]),
            _candidates())


def test_the_repair_loop_recovers_from_one_bad_ref():
    """Rejecting through the repair loop rather than refusing outright is what gives the model a
    named complaint and a second attempt — the bad ref is reported, not silently dropped."""
    bad = FakeResponse(output=_output(entity_ref="ftr::dpl_eib.tran_repos.nope"))
    got = extract_intent(_llm(bad, FakeResponse(output=_output())), _QUESTION, _candidates())
    assert got.status == STATUS_REPAIRED
    assert got.plan.entity_ref == "ftr::dpl_eib.tran_repos.cif_id"
    assert got.provider_calls == 2


def test_an_aggregate_with_no_column_is_rejected():
    """`count` needs no column; `sum` of nothing is not a measure."""
    with pytest.raises(SchemaValidationError, match="needs a column"):
        validate_intent(_output(measure={"op": "sum", "logical_ref": ""}), _candidates())


def test_count_star_needs_no_column():
    validate_intent(_output(measure={"op": "count", "logical_ref": ""}), _candidates())


def test_an_unlabelled_window_is_rejected():
    """Partition values are keyed by label all the way down; position would swap two periods."""
    with pytest.raises(SchemaValidationError, match="needs a label"):
        validate_intent(_output(windows=[
            {"label": "", "anchor_ref": "ftr::dpl_eib.tran_repos.tran_month",
             "calendar_unit": "month", "calendar_length": 1, "calendar_offset": 0}]), _candidates())


@pytest.mark.parametrize("bad", ["median", "top_n", ""])
def test_an_unknown_measure_op_is_rejected(bad):
    with pytest.raises(SchemaValidationError, match="measure op"):
        validate_intent(_output(measure={"op": bad, "logical_ref": ""}), _candidates())


def test_an_unactionable_abstention_code_is_rejected():
    """A free-text "not sure" cannot be routed to a clarification question, so the vocabulary is
    closed."""
    with pytest.raises(SchemaValidationError, match="not actionable"):
        validate_intent(_output(unresolved=["dunno"]), _candidates())


# ── abstention ───────────────────────────────────────────────────────────────────────────────────

def test_an_abstained_dimension_stays_EMPTY_rather_than_guessed():
    """A model forced to fill every field invents a split, and the answer then looks like it was
    asked for. The abstention is carried instead."""
    got = extract_intent(
        _llm(FakeResponse(output=_output(
            unresolved=["dimensions"],
            dimensions=[{"logical_ref": "ftr::dpl_eib.customer_segment_history.segment"}]))),
        _QUESTION, _candidates())
    assert got.plan.dimensions == ()
    assert got.needs_clarification
    assert got.unresolved == ("dimensions",)


def test_an_abstained_comparison_is_not_silently_a_decrease():
    got = extract_intent(
        _llm(FakeResponse(output=_output(unresolved=["comparison"], comparison="decrease"))),
        _QUESTION, _candidates())
    assert got.plan.comparison == ""


def test_an_abstained_window_leaves_no_windows():
    got = extract_intent(
        _llm(FakeResponse(output=_output(unresolved=["windows"]))), _QUESTION, _candidates())
    assert got.plan.windows == ()


# ── failure is not an empty plan ─────────────────────────────────────────────────────────────────

def test_a_provider_refusal_fails_into_clarification_not_a_blank_plan():
    """A caller handed an empty plan cannot tell a question the model could not read from a question
    whose answer is genuinely nothing."""
    with pytest.raises(IntentUnavailable):
        extract_intent(_llm(FakeResponse(output={}, provider_status=PROVIDER_REFUSAL)),
                       _QUESTION, _candidates())


# ── egress ───────────────────────────────────────────────────────────────────────────────────────

def test_only_metadata_leaves_the_building():
    """The offered refs and the question egress; no sample, profile or data value does. Asserted on
    the request the client actually received, not on intent."""
    captured: list = []

    class _Capture:
        def call(self, request):
            captured.append(request)
            return FakeLLM(script={request.task: FakeResponse(output=_output())}).call(request)

    extract_intent(_Capture(), _QUESTION, _candidates())
    (request,) = captured
    assert set(request.inputs) == {"question", "catalog_metadata", "instruction"}
    assert set(request.inputs["catalog_metadata"]) == {"column_refs", "table_refs", "labels"}
    assert request.output_schema is INTENT_SCHEMA


def test_every_wire_object_in_the_schema_is_CLOSED():
    """An open object let a model omit an unenforced field once before, and the run produced 100%
    ungrounded output that looked structurally fine."""
    def _walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False, node
                assert "required" in node, node
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(INTENT_SCHEMA)


def test_the_status_is_reported_so_a_repair_is_never_invisible():
    got = extract_intent(_llm(), _QUESTION, _candidates())
    assert got.status == STATUS_OK
